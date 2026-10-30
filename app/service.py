import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator

import httpx
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from openai import OpenAIError
from sqlalchemy.exc import SQLAlchemyError

from app.agent.guards import DB_FAILURE_MESSAGE, ModelFailure, model_failure
from app.config import Settings
from app.context import build_messages
from app.prompts import chat_system, extraction_system
from app.schemas import AfterSales, ChatRequest, ExtractRequest
from app.storage import ConversationStore
from app.workflow.classifier import IntentClassifier
from app.workflow.graph import Workflow, WorkflowDeps

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
        if store is not None:
            self.workflow = Workflow(
                deps=WorkflowDeps(
                    model=model, settings=settings, store=store, knowledge=knowledge,
                    pool=pool, selfcheck=selfcheck,
                    classifier=classifier or IntentClassifier(
                        model, timeout=settings.workflow_intent_timeout_seconds),
                ),
                checkpointer=checkpointer,
            )
        self.structured = model.with_structured_output(AfterSales, method="json_mode", include_raw=True)

    def prepare_chat(self, request: ChatRequest) -> list[BaseMessage]:
        return build_messages(chat_system(self.settings.store_name), request, self.settings.input_budget)

    async def respond(self, request: ChatRequest, messages: list[BaseMessage]) -> AsyncIterator[tuple[str, dict]]:
        if self.store is None or self.workflow is None:
            yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
            return
        try:
            conversation_id = await asyncio.to_thread(
                self.store.start_turn,
                user_id=request.client_id or ANONYMOUS_USER,
                message=request.message,
                conversation_id=request.conversation_id,
            )
        except SQLAlchemyError:
            yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
            return
        yield "meta", {"conversation_id": conversation_id}

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
            stream = self.workflow.respond(conversation_id, request.message, messages)
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
