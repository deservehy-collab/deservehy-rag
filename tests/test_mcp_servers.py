import asyncio
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_servers import mockgen

REPO = Path(__file__).resolve().parents[1]
LOGISTICS = {"url": "http://127.0.0.1:8931/mcp", "transport": "streamable_http"}
AFTERSALE = {"url": "http://127.0.0.1:8932/mcp", "transport": "streamable_http"}
SERVER_ENV_PREFIXES = ("LOGISTICS_", "AFTERSALE_", "MCP_HOST")

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_MCP_INTEGRATION", "1") == "1",
                                reason="需要真进程")


def clean_env(overrides: dict | None = None) -> dict:
    """外部 shell 里的 LOGISTICS_PORT/EXTRA_TOOLS 不能渗进「默认」场景，否则默认用例会绑错端口。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(SERVER_ENV_PREFIXES)}
    env.update(overrides or {})
    return env


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_until_open(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server 进程提前退出，返回码 {proc.returncode}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"端口 {port} 在 {timeout}s 内没起来")


class Server:
    def __init__(self, module: str, port: int, env: dict | None = None):
        # 端口上可能已经有个旧版本进程：连上去会跑出假绿，所以先拒跑。
        if port_in_use(port):
            raise RuntimeError(f"端口 {port} 已被占用，先关掉已有进程再跑集成测试")
        self.port = port
        self.proc = subprocess.Popen([sys.executable, "-m", module],
                                     cwd=REPO, env=clean_env(env))
        try:
            wait_until_open(self.port, self.proc)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        # 进程退出后监听套接字还有一小段收尾时间，等它真正释放端口再让下一个用例起。
        deadline = time.monotonic() + 5
        while port_in_use(self.port) and time.monotonic() < deadline:
            time.sleep(0.1)


async def call(url: str, name: str, args: dict):
    client = MultiServerMCPClient({"probe": {"url": url, "transport": "streamable_http"}},
                                  handle_tool_errors=False)
    tools = await client.get_tools(server_name="probe")
    tool = next((item for item in tools if item.name == name), None)
    assert tool is not None, [item.name for item in tools]
    message = await tool.ainvoke({"type": "tool_call", "name": name, "args": args, "id": "call_1"})
    blocks = message.content if isinstance(message.content, list) else [{"type": "text", "text": message.content}]
    text = "".join(item["text"] for item in blocks if item.get("type") == "text")
    return tools, json.loads(text)


@pytest.fixture()
def logistics():
    server = Server("mcp_servers.logistics_server", 8931)
    yield server
    server.stop()


@pytest.fixture()
def aftersale():
    server = Server("mcp_servers.aftersale_server", 8932)
    yield server
    server.stop()


def test_logistics_server_exposes_query_logistics_with_our_contract(logistics):
    tools, payload = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))
    assert {item.name for item in tools} == {"query_logistics"}
    assert tools[0].metadata["readOnlyHint"] is True
    assert payload["ok"] is True
    assert set(payload["data"]) >= {"order_id", "carrier", "tracking_no", "status", "nodes"}
    assert payload["data"]["status"] in {"PICKED_UP", "IN_TRANSIT", "DELIVERED"}


def test_mock_payloads_are_internally_consistent():
    for order_id in ("1001", "1002", "2002", "9999"):
        data = mockgen.logistics_payload(order_id)
        assert data["tracking_no"].startswith(mockgen.CARRIER_PREFIX[data["carrier"]])
        stations = {name for name in mockgen.STATIONS
                    if any(name in node["text"] for node in data["nodes"])}
        assert len(stations) <= 1
        assert data["status"] == mockgen.LOGISTICS_STATUS[len(data["nodes"]) - 1]
        assert (data["status"] == "DELIVERED") == data["nodes"][-1]["text"].startswith("已签收")

        warranty = mockgen.warranty_payload(order_id)
        assert (warranty["status"] == "IN_WARRANTY") == (warranty["days_left"] >= 0)
        assert warranty["expires_on"] == (
            mockgen.WARRANTY_BASE_DATE + dt.timedelta(days=warranty["days_left"])).isoformat()


def test_same_order_id_always_gives_the_same_track(logistics):
    first = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))[1]
    second = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))[1]
    other = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "2002"}))[1]
    assert first == second                                   # 确定性：验收与测试能对答案
    assert first["data"] != other["data"]                     # 不同订单给不同运单/轨迹


def test_aftersale_server_lists_two_warranty_tools(aftersale):
    tools, payload = asyncio.run(call(AFTERSALE["url"], "query_warranty", {"order_id": "1001"}))
    assert {item.name for item in tools} == {"query_warranty", "query_refund_progress"}
    assert payload["data"]["status"] in {"IN_WARRANTY", "OUT_OF_WARRANTY"}
    progress = asyncio.run(call(AFTERSALE["url"], "query_refund_progress", {"order_id": "1001"}))[1]
    assert progress["data"]["status"] in {"PROCESSING", "APPROVED", "REJECTED", "REFUNDED"}


def test_fail_times_knob_raises_on_the_first_calls():
    server = Server("mcp_servers.logistics_server", 8931, {"LOGISTICS_FAIL_TIMES": "1"})
    try:
        with pytest.raises(Exception, match="物流系统忙"):
            asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))
        ok = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))[1]
        assert ok["ok"] is True
    finally:
        server.stop()


def test_extra_tools_knob_adds_a_tool_without_touching_client_code():
    before = Server("mcp_servers.logistics_server", 8931)
    try:
        names = {item.name for item in asyncio.run(
            MultiServerMCPClient({"probe": LOGISTICS}).get_tools(server_name="probe"))}
        assert "query_station_info" not in names
    finally:
        before.stop()
    after = Server("mcp_servers.logistics_server", 8931, {"LOGISTICS_EXTRA_TOOLS": "query_station_info"})
    try:
        tools, payload = asyncio.run(call(LOGISTICS["url"], "query_station_info", {"station": "上海"}))
        assert "query_station_info" in {item.name for item in tools}
        assert payload["ok"] is True
    finally:
        after.stop()
