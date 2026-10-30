import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from openai import OpenAIError
from sqlalchemy.exc import SQLAlchemyError

from app.agent.guards import DB_FAILURE_MESSAGE, ModelFailure, model_failure
from app.config import Settings
from app.context import (
    ContextBuilder,
    ContextBudgetError,
    build_messages,
    estimate_text_tokens,
    history_window,
    summary_log_lines,
    tool_schema_tokens,
)
from app.prompts import chat_system, extraction_system
from app.schemas import AfterSales, ChatRequest, ExtractRequest
from app.storage import ConversationStore
from app.summary import SummaryService
from app.workflow.classifier import IntentClassifier
from app.workflow.coref import QuestionResolver
from app.workflow.graph import Workflow, WorkflowDeps
from app.knowledge.expansion import QueryExpander

logger = logging.getLogger(__name__)

ANONYMOUS_USER = "anonymous"


class CustomerService:
    def __init__(
        self,
        settings: Settings,
        model: ChatOpenAI,
        store: ConversationStore | None = None,
        knowledge=None,
        *,
        pool=None,
        selfcheck=None,
        classifier=None,
        checkpointer=None,
    ):
        self.settings = settings
        self.model = model
        self.store = store
        self.workflow = None
        self.context = ContextBuilder(store, settings) if store is not None else None
        self.summary = (SummaryService(model, store, settings) if store is not None else None)
        if store is not None:
            resolver = (
                QuestionResolver(model, timeout=settings.workflow_understanding_timeout_seconds)
                if settings.workflow_understanding_enabled else None
            )
            self.workflow = Workflow(
                deps=WorkflowDeps(
                    model=model, settings=settings, store=store, knowledge=knowledge,
                    pool=pool, selfcheck=selfcheck,
                    classifier=classifier or IntentClassifier(
                        model, timeout=settings.workflow_intent_timeout_seconds),
                    resolver=resolver,
                    expander=QueryExpander(
                        model, enabled=settings.workflow_query_expansion,
                        max_queries=settings.workflow_query_expansion_max,
                        timeout=settings.workflow_understanding_timeout_seconds),
                ),
                checkpointer=checkpointer,
            )
        self.structured = model.with_structured_output(AfterSales, method="json_mode", include_raw=True)

    def prepare_chat(self, request: ChatRequest) -> list[BaseMessage]:
        return build_messages(chat_system(self.settings.store_name), request, self.settings.input_budget)

    def preflight(self, request: ChatRequest) -> None:
        question_tokens = estimate_text_tokens(request.message)
        if question_tokens > self.settings.max_user_input_tokens:
            raise ContextBudgetError("当前问题过长，请缩短后重试。")
        system = estimate_text_tokens(chat_system(self.settings.store_name)) + tool_schema_tokens()
        if system + question_tokens > self.settings.input_budget:
            raise ContextBudgetError("当前问题超过上下文预算，请缩短后重试。")

    async def respond(self, request: ChatRequest) -> AsyncIterator[tuple[str, dict]]:
        if self.store is None or self.workflow is None:
            yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
            return
        try:
            conversation_id, current_id = await asyncio.to_thread(
                self.store.start_turn,
                user_id=request.client_id or ANONYMOUS_USER,
                message=request.message,
                conversation_id=request.conversation_id,
                with_message_id=True,
            )
        except SQLAlchemyError:
            yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
            return
        yield "meta", {"conversation_id": conversation_id}

        plan = None
        try:
            rows = await asyncio.to_thread(self.store.context_messages, conversation_id)
            turn = sum(1 for row in rows if row["role"] == "user")
            plan = await asyncio.to_thread(self.context.build, conversation_id, rows, current_id, turn)
            logger.info("history_ctx conv=%s turn=%s summary_upto=%s layer1_from=%s layer2=%s "
                        "layer2_raw_tokens=%s summary=%s window=%s",
                        conversation_id, turn, plan.summary_upto, plan.layer1_from,
                        len(plan.layer2), plan.layer2_raw_tokens,
                        summary_log_lines(plan.summary_segments),
                        history_window(rows))
            messages = [HumanMessage(content=row["content"], id=str(row["id"])) if row["role"] == "user"
                        else AIMessage(content=row["content"], id=str(row["id"])) for row in rows]
        except SQLAlchemyError:
            yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
            return
        except sqlite3.Error as exc:
            yield "error", model_failure(exc).payload()
            return

        # 节点跑在 LangGraph 自己的任务上。Starlette 的 StreamingResponse 用 anyio 任务组监听
        # 断连，取消信号会在每个 await 点反复重投，图内 finally 的 aclose() 因此排不完、上游
        # 连接和节点任务一起被孤儿化。把消费放进独立任务，断开时只对它做一次干净 Task.cancel()。
        # 同一会话的建工单接口用同一把锁（Workflow.lock），避免工单回写与新一轮 coref 互相覆盖 offer。
        lock = self.workflow.lock(conversation_id)
        held = False
        feeder: asyncio.Task | None = None
        try:
            await lock.acquire()
            held = True
            stream = self.workflow.respond(conversation_id, request.message, messages,
                                           context_plan=plan.as_dict() if plan else None,
                                           current_message_id=current_id)
            queue: asyncio.Queue = asyncio.Queue()

            async def pump() -> None:
                try:
                    async for event, payload in stream:
                        queue.put_nowait(("event", (event, payload)))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    queue.put_nowait(("failure", exc))
                queue.put_nowait(("closed", None))

            feeder = asyncio.create_task(pump())
            while True:
                kind, data = await queue.get()
                if kind == "event":
                    yield data[0], data[1]
                    continue
                if kind == "failure":
                    raise data
                # 摘要只在本轮正常收尾后调度：异常轮的历史边界不可信，宁可下一轮再压。
                if plan is not None and plan.layer2_over and self.summary is not None:
                    logger.info("summary trigger conv=%s layer2_raw_tokens=%s layer2_budget=%s "
                                "from=%s upto=%s", conversation_id, plan.layer2_raw_tokens,
                                plan.layer2_budget, plan.summary_upto, plan.layer1_from)
                    self.summary.schedule(conversation_id, from_msg_id=plan.summary_upto,
                                          upto_msg_id=plan.layer1_from)
                return
        except (OpenAIError, httpx.HTTPError, SQLAlchemyError, sqlite3.Error, ValueError, TimeoutError,
                IndexError, TypeError, AttributeError) as exc:
            failure = model_failure(exc)
            logger.warning("回复生成失败：%s", failure.code, exc_info=True)
            yield "error", failure.payload()
        finally:
            if feeder is not None:
                feeder.cancel()
            if held:
                lock.release()

    async def stream_chat(self, messages: list[BaseMessage]) -> AsyncIterator[tuple[str, dict]]:
        stream = self.model.astream(messages)
        has_text = False
        finish_reason = None
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                async for chunk in stream:
                    if chunk.text:
                        has_text = True
                        yield "token", {"content": chunk.text}
                    finish_reason = chunk.response_metadata.get("finish_reason") or finish_reason
            if not has_text or finish_reason not in {"stop", "length"}:
                raise ValueError("Missing text or valid finish reason")
            yield "done", {"finish_reason": finish_reason}
        except (OpenAIError, httpx.HTTPError, ValueError, TimeoutError, IndexError, TypeError, AttributeError) as exc:
            yield "error", model_failure(exc).payload()
        finally:
            await stream.aclose()

    async def extract(self, request: ExtractRequest) -> AfterSales:
        messages = build_messages(
            extraction_system(), ChatRequest(message=request.description), self.settings.input_budget
        )
        try:
            async with asyncio.timeout(self.settings.request_timeout_seconds):
                result = await self.structured.ainvoke(messages)
            if result["parsing_error"] is not None or result["raw"].response_metadata.get("finish_reason") != "stop":
                raise ValueError("Invalid or incomplete JSON result")
            if not isinstance(result["parsed"], AfterSales):
                raise ValueError("Missing structured result")
            # LangChain may repair incomplete JSON; validate the untouched response.
            return AfterSales.model_validate(json.loads(result["raw"].content))
        except (OpenAIError, httpx.HTTPError, ValueError, TimeoutError, IndexError, TypeError, AttributeError) as exc:
            raise model_failure(exc) from None
