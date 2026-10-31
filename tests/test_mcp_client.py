import asyncio
import time

import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine

from app.config import Settings
from app.database import build_session_factory
from app.models import Base
from app.storage import ConversationStore
from app.tools.mcp import McpDiscovery, server_endpoints
from app.tools.registry import spec_from_tool


def fake_tool(name, description):
    def impl(**kwargs):
        return {"ok": True, "data": {}}

    return StructuredTool(name=name, description=description, func=impl,
                          args_schema={"type": "object", "properties": {"order_id": {"type": "string"}}})


class FakeAdaptersClient:
    """只替身 adapters 的 get_tools，验我们这侧的合并与权限口径。"""

    def __init__(self, connections, **kwargs):
        self.connections = connections
        self.kwargs = kwargs
        self.handle_tool_errors = kwargs.get("handle_tool_errors")
        self.seen_servers = []

    async def get_tools(self, *, server_name=None):
        self.seen_servers.append(server_name)
        if server_name == "broken":
            raise OSError("连不上")
        if server_name == "logistics":
            return [fake_tool("query_logistics", "按订单号查询物流轨迹。")]
        return [fake_tool("query_warranty", "查询是否在保。"),
                fake_tool("create_shipment", "发货（外部写操作）")]


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def store_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield factory
    engine.dispose()


def test_endpoints_come_from_settings_and_keep_local_permission(monkeypatch):
    table = server_endpoints(settings(mcp_servers="logistics, aftersale"))
    assert set(table) == {"logistics", "aftersale"}
    assert table["logistics"][0].endswith("/mcp")
    assert table["aftersale"][1] == "read"
    assert server_endpoints(settings(mcp_servers="")) == {}
    assert server_endpoints(settings(mcp_servers="unknown")) == {}


def test_discovery_marks_source_server_and_permission(monkeypatch):
    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", FakeAdaptersClient)
    specs = asyncio.run(McpDiscovery(settings(mcp_servers="logistics,aftersale",
                                              mcp_aftersale_permission="write")).specs())
    by_name = {item.name: item for item in specs}
    assert by_name["query_logistics"].source == "mcp"
    assert by_name["query_logistics"].mcp_server == "logistics"
    assert by_name["query_logistics"].label == "查询物流"          # TOOL_LABELS 里留着中文名
    assert by_name["query_warranty"].label == "query_warranty"    # 没登记的用工具名
    assert by_name["query_warranty"].permission == "write"         # 我们这侧标的，不看 Server 声明
    assert by_name["create_shipment"].permission == "write"


def test_handle_tool_errors_is_forced_off(monkeypatch):
    captured = {}

    def build(connections, **kwargs):
        captured["kwargs"] = kwargs
        return FakeAdaptersClient(connections, **kwargs)

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", build)
    asyncio.run(McpDiscovery(settings(mcp_servers="logistics")).specs())
    assert captured["kwargs"]["handle_tool_errors"] is False
    assert captured["kwargs"]["tool_name_prefix"] is False


def test_dead_server_does_not_break_the_others_and_is_refetched_next_time(monkeypatch):
    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", FakeAdaptersClient)
    discovery = McpDiscovery(settings(mcp_servers="logistics,broken", tool_discovery_cache_seconds=0))
    names = [item.name for item in asyncio.run(discovery.specs())]
    assert names == ["query_logistics"]
    assert asyncio.run(discovery.specs())                          # 不抛，下一轮再试


def test_a_stalled_server_is_bounded_by_the_tool_timeout(monkeypatch):
    """Server 卡住（不是拒绝连接）时，握手也必须在一个工具超时内放弃，别把整轮对话拖死。"""

    class Hanging(FakeAdaptersClient):
        async def get_tools(self, *, server_name=None):
            await asyncio.sleep(5)
            return []

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", Hanging)
    discovery = McpDiscovery(settings(mcp_servers="logistics", tool_timeout_seconds=0.2))
    started = time.monotonic()
    assert asyncio.run(discovery.specs()) == []
    assert time.monotonic() - started < 2


def test_two_stalled_servers_wait_concurrently(monkeypatch):
    """两台都卡住时等待时间应是「一次超时」，不是「台数×超时」。"""

    class Hanging(FakeAdaptersClient):
        async def get_tools(self, *, server_name=None):
            await asyncio.sleep(5)
            return []

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", Hanging)
    discovery = McpDiscovery(settings(mcp_servers="logistics,aftersale", tool_timeout_seconds=0.5))
    started = time.monotonic()
    assert asyncio.run(discovery.specs()) == []
    assert time.monotonic() - started < 0.8


def test_incomplete_external_tool_is_skipped_not_fatal(monkeypatch):
    """第三方 Server 的工具可能没有描述：跳过它，别让整张表在 ToolRegistry 里被拒收。"""
    from app.tools.registry import ToolRegistry

    class WithBadTool(FakeAdaptersClient):
        async def get_tools(self, *, server_name=None):
            return [fake_tool("query_warranty", "查询是否在保。"),
                    StructuredTool(name="no_doc", description="", func=lambda **kwargs: None,
                                   args_schema={"type": "object", "properties": {}})]

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", WithBadTool)
    specs = asyncio.run(McpDiscovery(settings(mcp_servers="aftersale")).specs())
    assert [item.name for item in specs] == ["query_warranty"]
    assert ToolRegistry(specs).names() == ["query_warranty"]


def test_a_failed_refetch_keeps_the_old_list_for_this_turn_only(monkeypatch):
    calls = []
    state = {"up": True}

    class Flaky(FakeAdaptersClient):
        async def get_tools(self, *, server_name=None):
            calls.append(server_name)
            if not state["up"]:
                raise OSError("挂了")
            return await super().get_tools(server_name=server_name)

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", Flaky)
    discovery = McpDiscovery(settings(mcp_servers="logistics"))
    assert [item.name for item in asyncio.run(discovery.specs())] == ["query_logistics"]
    state["up"] = False
    assert [item.name for item in asyncio.run(discovery.specs(force=True))] == ["query_logistics"]
    assert asyncio.run(discovery.specs()) == []                    # 下一轮重新握手，仍失败就空
    assert calls == ["logistics", "logistics", "logistics"]


def test_cache_honours_ttl_and_force_skips_it(monkeypatch):
    calls = []

    class Counting(FakeAdaptersClient):
        async def get_tools(self, *, server_name=None):
            calls.append(server_name)
            return await super().get_tools(server_name=server_name)

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", Counting)
    discovery = McpDiscovery(settings(mcp_servers="logistics", tool_discovery_cache_seconds=60))
    asyncio.run(discovery.specs())
    asyncio.run(discovery.specs())
    assert calls == ["logistics"]                                  # 第二次命中缓存
    asyncio.run(discovery.specs(force=True))
    assert calls == ["logistics", "logistics"]
    discovery.forget()
    asyncio.run(discovery.specs())
    assert calls == ["logistics", "logistics", "logistics"]


def test_registry_merges_mcp_tools_and_builtin_no_longer_clashes(store_factory):
    """验收②的形状：物流只剩一个工具，来源是 MCP；内置的四个还在。"""
    from app.tools.builtin import build_specs
    from app.tools.wiring import build_registry

    store = ConversationStore(store_factory)
    conversation_id = store.start_turn(user_id="anon-1", message="物流到哪了")
    mcp_spec = spec_from_tool(fake_tool("query_logistics", "按订单号查询物流轨迹。"),
                              source="mcp", mcp_server="logistics", label="查询物流")
    registry = build_registry(store, conversation_id, None, settings=settings(), mcp_specs=[mcp_spec])
    assert registry.names().count("query_logistics") == 1
    assert registry.get("query_logistics").source == "mcp"
    assert {"query_order", "query_product", "query_faq", "create_ticket"} <= set(registry.names())
    assert "query_logistics" not in {item.name for item in build_specs(store, conversation_id, None)}
