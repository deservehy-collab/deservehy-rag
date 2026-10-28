import asyncio
import json
import threading
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from app.model import build_model
from tests.support import chunk


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, *, fail=False):
        self.release = threading.Event()
        self.closed = threading.Event()
        self.fail = fail

    async def __aiter__(self):
        yield chunk("first")
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        if self.fail:
            raise httpx.ReadError("deliberate interruption")
        yield chunk("second")
        yield chunk(finish_reason="stop")
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed.set()


@contextmanager
def running_server(stream):
    settings = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1", openai_model="test-model", openai_api_key="test", request_timeout_seconds=5)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)))
    model = build_model(settings, async_client=client)
    config = uvicorn.Config(create_app(settings, model), host="127.0.0.1", port=0, log_level="error", timeout_graceful_shutdown=2)
    sock = config.bind_socket()
    server = uvicorn.Server(config)
    started = threading.Event()
    original_startup = server.startup

    async def startup(sockets=None):
        await original_startup(sockets=sockets)
        started.set()
    server.startup = startup
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        assert started.wait(5), "Local test server did not start"
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        stream.release.set()
        server.should_exit = True
        thread.join(5)
        sock.close()
        asyncio.run(model.root_async_client.close())
        model.root_client.close()
        assert not thread.is_alive(), "Local test server did not stop"


def test_first_chunk_arrives_before_upstream_finishes():
    stream = GatedStream()
    with running_server(stream) as base_url, httpx.Client(timeout=2) as client:
        with client.stream("POST", base_url + "/api/chat", json={"message": "hello"}) as response:
            lines = response.iter_lines()
            assert next(lines) == "event: token"
            assert json.loads(next(lines)[6:]) == {"content": "first"}
            assert not stream.release.is_set()
            stream.release.set()
            rest = "\n".join(lines)
            assert "second" in rest and "event: done" in rest
    assert stream.closed.is_set()


def test_client_disconnect_closes_waiting_upstream():
    stream = GatedStream()
    with running_server(stream) as base_url, httpx.Client(timeout=2) as client:
        with client.stream("POST", base_url + "/api/chat", json={"message": "hello"}) as response:
            lines = response.iter_lines()
            assert next(lines) == "event: token"
            assert "first" in next(lines)
        assert stream.closed.wait(2), "Upstream stayed open after downstream disconnect"
        assert not stream.release.is_set()


def test_real_connection_keeps_error_after_partial_output():
    stream = GatedStream(fail=True)
    with running_server(stream) as base_url, httpx.Client(timeout=2) as client:
        with client.stream("POST", base_url + "/api/chat", json={"message": "hello"}) as response:
            lines = response.iter_lines()
            assert next(lines) == "event: token"
            assert "first" in next(lines)
            stream.release.set()
            rest = "\n".join(lines)
            assert "event: error" in rest and "event: done" not in rest
    assert stream.closed.is_set()
