import asyncio
import json
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient


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


@contextmanager
def api(handler, **settings_overrides):
    from app.config import Settings
    from app.main import create_app
    from app.model import build_model

    seen = []
    def capture(request):
        seen.append((request.url.path, json.loads(request.content)))
        return handler(request)

    settings = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1", openai_model="test-model",
                        openai_api_key="test-secret-not-real", **settings_overrides)
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    model = build_model(settings, async_client=async_client)
    try:
        with TestClient(create_app(settings, model)) as client:
            yield client, seen
    finally:
        asyncio.run(model.root_async_client.close())
        model.root_client.close()


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        result.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return result
