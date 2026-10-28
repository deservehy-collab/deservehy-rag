import asyncio
import json
from collections.abc import AsyncIterator

import httpx
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from openai import APITimeoutError, OpenAIError, RateLimitError

from app.config import Settings
from app.context import build_messages
from app.prompts import chat_system, extraction_system
from app.schemas import AfterSales, ChatRequest, ExtractRequest


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
    if isinstance(exc, RateLimitError):
        return ModelFailure("upstream_rate_limited", "模型服务额度不足或请求过于频繁，请检查上游额度后重试。")
    if isinstance(exc, ValueError):
        return ModelFailure("invalid_model_output", "模型没有返回符合要求的结果，请重试或检查模型的 JSON 输出能力。")
    return ModelFailure("upstream_error", "模型服务暂时不可用，请检查上游配置后重试。")


class CustomerService:
    def __init__(self, settings: Settings, model: ChatOpenAI):
        self.settings = settings
        self.model = model
        self.structured = model.with_structured_output(AfterSales, method="json_mode", include_raw=True)

    def prepare_chat(self, request: ChatRequest) -> list[BaseMessage]:
        return build_messages(chat_system(self.settings.store_name), request, self.settings.input_budget)

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
