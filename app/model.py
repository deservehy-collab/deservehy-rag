import httpx
from langchain_openai import ChatOpenAI

from app.config import Settings


def build_model(settings: Settings, *, async_client: httpx.AsyncClient | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.openai_model,
        base_url=str(settings.openai_base_url),
        api_key=settings.openai_api_key,
        max_tokens=settings.max_output_tokens,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
        use_responses_api=False,
        stream_usage=False,
        temperature=None,
        extra_body=settings.openai_extra_body or None,
        http_async_client=async_client,
    )
