import asyncio
import json
from collections.abc import AsyncIterator

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_openai import ChatOpenAI
from openai import APITimeoutError, OpenAIError, RateLimitError
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.context import build_messages
from app.prompts import chat_system, extraction_system
from app.schemas import AfterSales, ChatRequest, ExtractRequest
from app.storage import ConversationStore
from app.tools import TOOL_LABELS, ToolExecutor, make_tools

MAX_TOOL_CALLS = 5
ANONYMOUS_USER = "anonymous"
DB_FAILURE_MESSAGE = "会话存储暂时不可用，请稍后重试。"
LEAK_MARKERS = ("DSML", "<｜", "<|", "｜DSML")
HOLD_SUFFIXES = ("DSM", "DS", "D", "<｜", "<", "｜")
FALLBACK_TOOL = "抱歉，刚才的查询结果没能整理成回答，请再问一次，或转人工客服。"
FALLBACK_PLAIN = "抱歉，刚才没能生成有效回答，请再问一次。"


class ReplyGuard:
    """拦截上游漏进正文的 DeepSeek DSML 工具标记，流式转发前先扣住疑似标记的尾巴。"""

    def __init__(self) -> None:
        self.pending = ""
        self.clean = ""
        self.blocked = False

    def feed(self, text: str) -> str:
        if self.blocked:
            return ""
        self.pending += text
        cut = self._cut()
        if cut != -1:
            self.blocked = True
            safe, self.pending = self.pending[:cut], ""
        else:
            hold = 0
            for suffix in HOLD_SUFFIXES:
                if self.pending.endswith(suffix):
                    hold = len(suffix)
                    break
            if len(self.pending) <= hold:
                return ""
            if hold:
                safe, self.pending = self.pending[:-hold], self.pending[-hold:]
            else:
                safe, self.pending = self.pending, ""
        self.clean += safe
        return safe

    def flush(self) -> str:
        if not self.blocked:
            self.clean += self.pending
            self.pending = ""
        return self.clean

    def _cut(self) -> int:
        cut = -1
        for marker in LEAK_MARKERS:
            index = self.pending.find(marker)
            if index != -1 and (cut == -1 or index < cut):
                cut = index
        return cut



class ModelFailure(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message}


def model_failure(exc: Exception) -> ModelFailure:
    if isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
        return ModelFailure("upstream_timeout", "模型响应超时，请稍后重试。", 504)
    if isinstance(exc, SQLAlchemyError):
        return ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503)
    if isinstance(exc, RateLimitError):
        return ModelFailure("upstream_rate_limited", "模型服务额度不足或请求过于频繁，请检查上游额度后重试。")
    if isinstance(exc, ValueError):
        return ModelFailure("invalid_model_output", "模型没有返回符合要求的结果，请重试或检查模型的 JSON 输出能力。")
    return ModelFailure("upstream_error", "模型服务暂时不可用，请检查上游配置后重试。")


class CustomerService:
    def __init__(self, settings: Settings, model: ChatOpenAI, store: ConversationStore | None = None):
        self.settings = settings
        self.model = model
        self.store = store
        self.structured = model.with_structured_output(AfterSales, method="json_mode", include_raw=True)

    def prepare_chat(self, request: ChatRequest) -> list[BaseMessage]:
        return build_messages(chat_system(self.settings.store_name), request, self.settings.input_budget)

    async def respond(self, request: ChatRequest, messages: list[BaseMessage]) -> AsyncIterator[tuple[str, dict]]:
        if self.store is None:
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

        tools = make_tools(self.store, conversation_id)
        executor = ToolExecutor(
            tools, timeout=self.settings.tool_timeout_seconds, attempts=self.settings.tool_max_attempts
        )

        try:
            reply, finish_reason = None, None
            guard = ReplyGuard()
            stream = None
            try:
                # 第一遍绑定工具：普通问答只走这一遍，token 直接流向页面；命中工具时这一遍通常没有正文。
                stream = self.model.bind_tools(list(tools.values())).astream(messages)
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    async for chunk in stream:
                        reply = chunk if reply is None else reply + chunk
                        if chunk.text:
                            safe = guard.feed(chunk.text)
                            if safe:
                                yield "token", {"content": safe}
                        finish_reason = chunk.response_metadata.get("finish_reason") or finish_reason
            finally:
                if stream is not None:
                    await stream.aclose()
            if not isinstance(reply, AIMessageChunk):
                raise ValueError("Empty model reply")

            reply_text = guard.flush()
            tool_calls = reply.tool_calls
            if not tool_calls:
                if finish_reason not in {"stop", "length"} or (not reply_text and not guard.blocked):
                    raise ValueError("Missing text or valid finish reason")
                if not reply_text:
                    reply_text = FALLBACK_PLAIN
                    yield "token", {"content": reply_text}
                await asyncio.to_thread(self.store.add_message, conversation_id, "assistant", content=reply_text)
                yield "done", {"finish_reason": finish_reason}
                return
            if len(tool_calls) > MAX_TOOL_CALLS:
                raise ValueError("Too many tool calls")

            stored_calls = [
                {"name": call["name"], "args": call.get("args") or {}, "id": call.get("id") or f"call_{index + 1}", "type": "tool_call"}
                for index, call in enumerate(tool_calls)
            ]
            await asyncio.to_thread(self.store.add_message, conversation_id, "assistant", tool_calls=stored_calls)

            tool_messages: list[ToolMessage] = []
            for call in stored_calls:
                label = TOOL_LABELS.get(call["name"], call["name"])
                yield "status", {"phase": "start", "tool": call["name"], "label": label}
                outcome = await executor.execute(call["name"], call["args"])
                await asyncio.to_thread(
                    self.store.add_message, conversation_id, "tool", content=outcome.content, tool_call_id=call["id"]
                )
                yield "status", {
                    "phase": "end",
                    "tool": call["name"],
                    "label": label,
                    "ok": outcome.ok,
                    "summary": outcome.summary,
                    "duration_ms": outcome.duration_ms,
                }
                tool_messages.append(ToolMessage(content=outcome.content, tool_call_id=call["id"], name=call["name"]))

            # 第二遍不绑定工具，只负责依据工具结果收敛回答，因此单轮最多一次工具调用。
            final, finish_reason = None, None
            final_guard = ReplyGuard()
            stream = None
            try:
                stream = self.model.astream(
                    [*messages, AIMessage(content=reply_text, tool_calls=stored_calls), *tool_messages]
                )
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    async for chunk in stream:
                        final = chunk if final is None else final + chunk
                        if chunk.text:
                            safe = final_guard.feed(chunk.text)
                            if safe:
                                yield "token", {"content": safe}
                        finish_reason = chunk.response_metadata.get("finish_reason") or finish_reason
            finally:
                if stream is not None:
                    await stream.aclose()
            if not isinstance(final, AIMessageChunk) or finish_reason not in {"stop", "length"}:
                raise ValueError("Missing text or valid finish reason")
            final_text = final_guard.flush()
            if not final_text:
                if not final_guard.blocked:
                    raise ValueError("Missing text")
                final_text = FALLBACK_TOOL
                yield "token", {"content": final_text}
            await asyncio.to_thread(self.store.add_message, conversation_id, "assistant", content=final_text)
            yield "done", {"finish_reason": finish_reason}
        except (OpenAIError, httpx.HTTPError, SQLAlchemyError, ValueError, TimeoutError, IndexError, TypeError, AttributeError) as exc:
            yield "error", model_failure(exc).payload()

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
