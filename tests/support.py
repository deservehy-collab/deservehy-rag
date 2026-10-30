import asyncio
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.database import build_session_factory
from app.models import Base
from app.seed import seed_faq


def completion(content, finish_reason="stop"):
    return httpx.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })


def chunk(content=None, finish_reason=None):
    delta = {} if content is None else {"role": "assistant", "content": content}
    payload = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def tool_call_chunk(name=None, arguments=None, call_id=None, finish_reason=None):
    delta = {"role": "assistant"} if call_id else {}
    entry = {"index": 0, "function": {}}
    if call_id:
        entry["id"] = call_id
        entry["type"] = "function"
    if name:
        entry["function"]["name"] = name
    if arguments is not None:
        entry["function"]["arguments"] = arguments
    if entry["function"] or call_id:
        delta["tool_calls"] = [entry]
    payload = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, parts, error=None):
        self.parts = parts
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(0)
            yield part
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def streamed(parts=("hello", " world"), finish_reason="stop"):
    stream = ChunkStream([*(chunk(text) for text in parts), chunk(finish_reason=finish_reason), b"data: [DONE]\n\n"])
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


def streamed_tool_call(name="query_logistics", arguments=None, call_id="call_1"):
    payload = json.dumps(arguments or {"order_id": "1001"}, ensure_ascii=False)
    parts = [
        tool_call_chunk(name=name, arguments=payload, call_id=call_id),
        tool_call_chunk(finish_reason="tool_calls"),
        b"data: [DONE]\n\n",
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(parts))


def _settings(settings_overrides):
    from app.config import Settings

    # 默认关掉改写、自评与后台摘要，避免旧用例额外调用模型或落日志文件。
    # 检查点走内存，避免 pytest 往 .cache 落 sqlite。
    defaults = {
        "openai_base_url": "http://upstream.invalid/v1",
        "openai_model": "test-model",
        "openai_api_key": "test-secret-not-real",
        "knowledge_query_rewrite": False,
        "knowledge_self_check": False,
        "workflow_understanding_enabled": False,
        "context_summary_enabled": False,
        "log_file": ":memory:",
        "workflow_checkpoint_path": ":memory:",
    }
    defaults.update(settings_overrides)
    return Settings(_env_file=None, **defaults)


class FixedClassifier:
    def __init__(self, intent: str = "售后"):
        self.intent = intent

    async def classify(self, question, previous_question=None):
        from app.schemas import IntentResult

        return IntentResult(intent=self.intent, confidence=1.0)


@contextmanager
def sqlite_factory():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'api.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        try:
            factory = build_session_factory(engine)
            seed_faq(factory)
            yield factory
        finally:
            engine.dispose()


@contextmanager
def api_with_db(handler, *, classifier=None, **settings_overrides):
    from app.main import create_app
    from app.model import build_model

    seen = []

    def capture(request):
        seen.append((request.url.path, json.loads(request.content)))
        return handler(request)

    settings = _settings(settings_overrides)
    with sqlite_factory() as factory:
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        model = build_model(settings, async_client=async_client)
        try:
            with TestClient(create_app(settings, model, session_factory=factory,
                                       classifier=classifier or FixedClassifier("订单"))) as client:
                yield client, seen, factory
        finally:
            asyncio.run(model.root_async_client.close())
            model.root_client.close()


@contextmanager
def api(handler, *, classifier=None, **settings_overrides):
    with api_with_db(handler, classifier=classifier, **settings_overrides) as (client, seen, _):
        yield client, seen


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        result.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return result
