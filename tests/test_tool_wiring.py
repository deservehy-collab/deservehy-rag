import asyncio
import logging
import sys
import types

import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine, select

from app.config import Settings
from app.context import ContextBudget, tool_schema_tokens
from app.database import build_session_factory
from app.models import Base, ToolAuditLog
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools.registry import spec_from_tool
from app.tools.wiring import build_engine, build_registry, extra_specs
from tests.test_tool_policy import make_tool


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'wiring.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    yield ConversationStore(factory)
    engine.dispose()


def rows(store):
    with store.session_factory() as session:
        return list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))


def test_demo_module_is_collected_by_name_only():
    specs = extra_specs(settings(tool_extra_modules="examples.tools.demo_coupon"))
    assert {item.name for item in specs} == {"query_coupon", "issue_coupon"}
    for item in specs:
        assert item.source == "builtin" and item.description and item.json_schema["properties"]
    assert {item.name: item.permission for item in specs} == {"query_coupon": "read",
                                                              "issue_coupon": "write"}
    assert {item.name: item.label for item in specs} == {"query_coupon": "查询优惠券",
                                                          "issue_coupon": "补发优惠券"}


def test_registry_merges_builtin_and_dynamic_without_touching_core_code(store):
    registry = build_registry(store, 1, None, settings=settings(
        tool_extra_modules="examples.tools.demo_coupon"))
    assert set(registry.names()) >= {"query_order", "query_product",
                                     "query_faq", "create_ticket", "query_coupon", "issue_coupon"}
    assert registry.get("query_coupon").label == "查询优惠券"


def test_same_name_keeps_the_first_registered_and_warns(store, caplog):
    """合并顺序是契约的一部分：内置先到先得，MCP 同名工具被丢且留下 WARNING。"""
    duplicate = spec_from_tool(make_tool("query_order"), source="mcp", mcp_server="logistics",
                               label="MCP 查询订单")
    with caplog.at_level(logging.WARNING, logger="app.tools.wiring"):
        registry = build_registry(store, 1, None, settings=settings(), mcp_specs=[duplicate])
    kept = registry.get("query_order")
    assert (kept.source, kept.mcp_server) == ("builtin", None)
    assert "query_order" in caplog.text


def test_dynamic_read_tool_runs_through_the_engine_and_lands_audit(store):
    registry = build_registry(store, 1, None, settings=settings(
        tool_extra_modules="examples.tools.demo_coupon"))
    engine = build_engine(registry, settings=settings(), session_factory=store.session_factory)
    outcome = asyncio.run(engine.run(name="query_coupon", args={"order_id": "1001"}, conversation_id=1))
    assert outcome.ok is True and "满 100 减 10" in outcome.content
    assert "优惠券" in outcome.summary
    assert rows(store)[0].tool_name == "query_coupon" and rows(store)[0].status == "成功"


def test_dynamic_write_tool_is_denied_until_the_local_allowlist_lists_it(store):
    registry = build_registry(store, 1, None, settings=settings(
        tool_extra_modules="examples.tools.demo_coupon"))
    denied = asyncio.run(build_engine(registry, settings=settings(),
                                      session_factory=store.session_factory).run(
        name="issue_coupon", args={"order_id": "1001"}, confirmed=True, conversation_id=1))
    assert denied.ok is False and denied.code == "permission_denied"
    assert rows(store)[0].status == "权限拒绝"

    allowed_settings = settings(tool_extra_modules="examples.tools.demo_coupon",
                                tool_write_allowlist="create_ticket,issue_coupon")
    allowed = asyncio.run(build_engine(build_registry(store, 1, None, settings=allowed_settings),
                                       settings=allowed_settings,
                                       session_factory=store.session_factory).run(
        name="issue_coupon", args={"order_id": "1001"}, confirmed=True, conversation_id=1))
    assert allowed.ok is True


def test_unreadable_module_is_skipped_not_fatal(store):
    registry = build_registry(store, 1, None, settings=settings(tool_extra_modules="no.such.module"))
    assert "query_order" in registry.names()


def test_malformed_extra_module_is_skipped_not_fatal(monkeypatch):
    """模块能被 import 但结构不对（EXTRA_TOOLS 不是列表、元素不是工具）也不能拖垮整轮。"""
    monkeypatch.setitem(sys.modules, "broken_extra_shapes",
                        types.SimpleNamespace(EXTRA_TOOLS={"not": "a list"}))
    assert extra_specs(settings(tool_extra_modules="broken_extra_shapes")) == []

    monkeypatch.setitem(sys.modules, "broken_extra_tools",
                        types.SimpleNamespace(EXTRA_TOOLS=[object()], EXTRA_PERMISSION=["nope"]))
    assert extra_specs(settings(tool_extra_modules="broken_extra_tools")) == []

    monkeypatch.setitem(sys.modules, "broken_extra_scalar", types.SimpleNamespace(EXTRA_TOOLS=5))
    assert extra_specs(settings(tool_extra_modules="broken_extra_scalar")) == []


def test_exclude_drops_a_tool_for_a_route(store):
    registry = build_registry(store, 1, None, settings=settings(), exclude={"query_faq"})
    assert "query_faq" not in registry.names() and "query_order" in registry.names()


def test_spec_from_tool_accepts_pydantic_and_dict_schemas():
    builtin = spec_from_tool(make_tool("query_order"), source="builtin", label="查询订单")
    assert builtin.permission == "read" and builtin.json_schema["properties"]["order_id"]

    external = StructuredTool(name="query_warranty", description="查在保。",
                              coroutine=lambda **kwargs: None,
                              args_schema={"type": "object", "properties": {"order_id": {"type": "string"}}})
    converted = spec_from_tool(external, source="mcp", mcp_server="aftersale")
    assert converted.json_schema == external.args_schema and converted.label == "query_warranty"
    assert (converted.source, converted.mcp_server, converted.permission) == ("mcp", "aftersale", "read")


def test_spec_from_tool_gives_a_tool_without_arguments_an_empty_object_schema():
    class Ping:
        name = "ping"
        description = "心跳。"
        args_schema = None

    converted = spec_from_tool(Ping(), source="builtin")
    assert converted.json_schema == {"type": "object", "properties": {}}
    assert converted.label == "ping"


def test_schema_reserve_widens_the_system_cost_and_shrinks_history():
    assert tool_schema_tokens(400) - tool_schema_tokens() == 400
    assert tool_schema_tokens(-100) == tool_schema_tokens()     # 负数按 0 算，不能把成本估少

    tight = ContextBudget.from_settings(settings(context_window_tokens=20000,
                                                  tool_schema_reserve_tokens=0))
    wide = ContextBudget.from_settings(settings(context_window_tokens=20000,
                                                 tool_schema_reserve_tokens=1200))
    assert wide.system - tight.system == 1200
    assert tight.available - wide.available == 1200
    assert tight.history - wide.history == 1200                 # 窗口够小，reserve 真的挤掉历史


def test_main_keeps_both_engine_builders_under_distinct_names():
    """真实启动（不注入 session_factory）才会走到 wiring 版：两个同名 build_engine 曾在 main.py 里互相盖掉，启动即 TypeError。"""
    import app.main
    from app.database import build_engine as database_builder
    from app.tools.wiring import build_engine as tool_builder

    assert app.main.build_engine is database_builder
    assert app.main.build_tool_engine is tool_builder
