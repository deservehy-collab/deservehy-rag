import asyncio
import json
import threading
import time

import pytest
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.database import build_session_factory
from app.models import Base, ToolAuditLog
from app.tools.audit import ToolAuditWriter
from app.tools.engine import ToolEngine
from app.tools.registry import ToolSpec


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=64, description="订单号")


def spec_of(handle, *, name, permission="read", source="builtin", server=None, label="测试"):
    return ToolSpec(name=name, description=handle.description,
                    json_schema=handle.args_schema.model_json_schema() if isinstance(
                        handle.args_schema, type) else handle.args_schema,
                    source=source, mcp_server=server, permission=permission, label=label, tool=handle)


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k",
            "tool_timeout_seconds": 0.05, "tool_max_attempts": 2}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def audit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'engine.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield ToolAuditWriter(factory), factory
    engine.dispose()


def rows(factory):
    with factory() as session:
        return list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))


def ok_tool(name="query_order"):
    @tool(name, args_schema=Args, description="按订单号查询。")
    def handle(order_id: str) -> dict:
        return {"ok": True, "data": {"order_id": order_id, "status": "SHIPPED"}}
    return handle


def run(engine, **kwargs):
    return asyncio.run(engine.run(**kwargs))


def test_schema_only_violation_is_caught_before_pydantic(tmp_path):
    """JSON Schema 比 pydantic 更严时必须先拦在引擎这层，而不是靠 LangChain 的参数校验兜。"""
    engine_db = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    Base.metadata.create_all(engine_db)
    factory = build_session_factory(engine_db)
    calls = []

    @tool("query_order", args_schema=Args, description="按订单号查询。")
    def spy(order_id: str) -> dict:
        calls.append(order_id)
        return {"ok": True}

    strict = Args.model_json_schema()
    strict["properties"]["order_id"]["maxLength"] = 4          # 只写进 JSON Schema，pydantic 仍允许 64
    spec = ToolSpec(name="query_order", description="按订单号查询。", json_schema=strict,
                    source="builtin", mcp_server=None, permission="read", label="查询订单", tool=spy)
    engine = ToolEngine(_registry([spec]), settings=settings(), audit=ToolAuditWriter(factory))
    outcome = run(engine, name="query_order", args={"order_id": "12345"})
    assert outcome.code == "invalid_arguments" and calls == []
    assert "长度不在范围内" in outcome.content
    engine_db.dispose()


def test_unknown_tool_is_reported_and_not_audited_as_failure_of_a_real_tool(audit):
    writer, factory = audit
    engine = ToolEngine(_registry([]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_weather", args={})
    assert outcome.ok is False and outcome.code == "unknown_tool"
    assert rows(factory) == []           # 表里没这个工具，落审计只会脏数据（决定见 §18-C3 的口径）


def _registry(specs):
    from app.tools.registry import ToolRegistry
    return ToolRegistry(specs)


def test_validation_blocks_before_execution_and_lands_as_checked(audit):
    writer, factory = audit
    calls = []

    @tool("query_order", args_schema=Args, description="按订单号查询。")
    def spy(order_id: str) -> dict:
        calls.append(order_id)
        return {"ok": True}

    engine = ToolEngine(_registry([spec_of(spy, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": ""}, conversation_id=3)
    assert outcome.ok is False and outcome.code == "invalid_arguments"
    assert "order_id" in outcome.content and calls == []
    assert rows(factory)[0].status == "校验拦下"


def test_write_without_confirmation_returns_confirm_required_and_writes_nothing(audit):
    writer, factory = audit

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def handle(order_id: str) -> dict:
        raise AssertionError("未确认不许执行")

    engine = ToolEngine(_registry([spec_of(handle, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="create_ticket", args={"order_id": "1001"})
    assert outcome.ok is False and outcome.code == "confirm_required"
    assert outcome.audit_status == "confirm_required"
    assert rows(factory) == []


def test_write_outside_allowlist_is_denied_and_audited(audit):
    writer, factory = audit

    @tool("delete_order", args_schema=Args, description="删单。")
    def handle(order_id: str) -> dict:
        raise AssertionError("白名单外不许执行")

    engine = ToolEngine(_registry([spec_of(handle, name="delete_order", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="delete_order", args={"order_id": "1001"}, confirmed=True)
    assert outcome.ok is False and outcome.code == "permission_denied"
    assert rows(factory)[0].status == "权限拒绝"


def test_record_denial_leaves_a_permission_row_without_executing(audit):
    """用户在卡片上点「取消」：调用根本没发生，但第 5 条要求这次也留痕（T13 的取消分支靠它）。"""
    writer, factory = audit

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def never(order_id: str) -> dict:
        raise AssertionError("取消不该执行工具")

    engine = ToolEngine(_registry([spec_of(never, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    engine.record_denial(name="create_ticket", args={"order_id": "1001"},
                         reason="用户在页面取消了这次操作。", tool_call_id="call_9", conversation_id=3)
    row = rows(factory)[0]
    assert (row.status, row.tool_call_id, row.retry_count) == ("权限拒绝", "call_9", 0)
    assert row.arguments == {"order_id": "1001"}


def test_record_denial_of_an_unregistered_tool_writes_nothing(audit):
    writer, factory = audit
    engine = ToolEngine(_registry([]), settings=settings(), audit=writer)
    engine.record_denial(name="ghost_tool", args={}, reason="取消")
    assert rows(factory) == []


def test_read_tool_retries_transient_failure_then_succeeds(audit):
    writer, factory = audit
    seen = []

    @tool("query_order", args_schema=Args, description="查询。")
    def flaky(order_id: str) -> dict:
        seen.append(order_id)
        if len(seen) == 1:
            raise OperationalError("select 1", {}, Exception("忙"))
        return {"ok": True, "data": {"order_id": order_id}}

    engine = ToolEngine(_registry([spec_of(flaky, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"})
    assert outcome.ok is True and seen == ["1001", "1001"] and outcome.retry_count == 1
    assert rows(factory)[0].retry_count == 1 and rows(factory)[0].status == "成功"


def test_business_empty_result_is_not_retried(audit):
    writer, factory = audit
    seen = []

    @tool("query_order", args_schema=Args, description="查询。")
    def empty(order_id: str) -> dict:
        seen.append(order_id)
        return {"ok": False, "error": "not_found", "message": f"没有找到订单 {order_id}。"}

    engine = ToolEngine(_registry([spec_of(empty, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "9999"})
    assert outcome.ok is False and outcome.code == "not_found" and seen == ["9999"]
    row = rows(factory)[0]
    assert row.status == "失败"
    assert row.error_message == "没有找到订单 9999。"        # 业务失败也要留下原因，否则审计看不出为什么失败


def test_write_timeout_is_not_retried_and_retry_count_is_zero(audit):
    """验收⑥后半段：写操作超时绝不自动重试，重复执行比失败更糟。"""
    writer, factory = audit
    seen = []

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def slow(order_id: str) -> dict:
        seen.append(order_id)
        time.sleep(0.2)
        return {"ok": True}

    engine = ToolEngine(_registry([spec_of(slow, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="create_ticket", args={"order_id": "1001"}, confirmed=True)
    assert outcome.ok is False and outcome.code == "tool_timeout" and len(seen) == 1
    # 同步工具跑在 executor 线程里，超时只是不再等：任务可能已经落库，必须如实报「结果未知」。
    assert "可能已经执行" in outcome.content and "重复提交" in outcome.content
    assert "可能已经执行" in outcome.summary
    row = rows(factory)[0]
    assert row.status == "超时" and row.retry_count == 0


def test_read_timeout_retries_and_reports_attempts(audit):
    writer, factory = audit
    seen = []

    @tool("query_order", args_schema=Args, description="查询。")
    def slow(order_id: str) -> dict:
        seen.append(order_id)
        time.sleep(0.2)
        return {"ok": True}

    engine = ToolEngine(_registry([spec_of(slow, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"})
    assert outcome.code == "tool_timeout" and len(seen) == 2
    row = rows(factory)[0]
    assert row.status == "超时" and row.retry_count == 1 and row.duration_ms > 0


def test_pydantic_cross_field_rules_are_triaged_as_invalid_arguments(audit):
    writer, factory = audit

    class ProductArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        product_id: str | None = Field(default=None, description="商品 ID")
        keyword: str | None = Field(default=None, description="关键词")

        from pydantic import model_validator

        @model_validator(mode="after")
        def require_one(self):
            if not (self.product_id or self.keyword):
                raise ValueError("product_id 和 keyword 至少填写一个。")
            return self

    @tool("query_product", args_schema=ProductArgs, description="查商品。")
    def handle(product_id=None, keyword=None) -> dict:
        raise AssertionError("JSON Schema 放行、pydantic 拦住，不该执行到这里")

    spec = ToolSpec(name="query_product", description="查商品。",
                    json_schema=ProductArgs.model_json_schema(), source="builtin", mcp_server=None,
                    permission="read", label="查询商品", tool=handle)
    engine = ToolEngine(_registry([spec]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_product", args={})
    assert outcome.ok is False and outcome.code == "invalid_arguments"
    assert "至少填写一个" in outcome.content
    assert "Value error" not in outcome.content and "。。" not in outcome.content
    assert rows(factory)[0].status == "校验拦下"


def test_mcp_structured_result_is_normalised_and_audited_with_the_server_name(audit):
    writer, factory = audit
    from langchain_core.tools import StructuredTool

    async def call(order_id: str):
        return ('{"ok": true, "data": {"carrier": "\\u987a\\u4e30"}}',
                {"structured_content": {"ok": True, "data": {"carrier": "顺丰速运", "status": "IN_TRANSIT",
                                                             "nodes": [{"time": "t", "text": "已揽收"}]}}})

    mcp_tool = StructuredTool(name="query_logistics", description="查物流。",
                              coroutine=call, args_schema={"type": "object",
                                                            "properties": {"order_id": {"type": "string"}},
                                                            "required": ["order_id"]},
                              response_format="content_and_artifact")
    spec = ToolSpec(name="query_logistics", description="查物流。", json_schema=mcp_tool.args_schema,
                    source="mcp", mcp_server="logistics", permission="read", label="查询物流", tool=mcp_tool)
    engine = ToolEngine(_registry([spec]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_logistics", args={"order_id": "1001"}, conversation_id=9)
    assert outcome.ok is True and outcome.source == "mcp" and outcome.mcp_server == "logistics"
    assert "顺丰" in outcome.content and "\\u" not in outcome.content          # 中文不转义
    assert outcome.summary.startswith("1 条物流记录")
    row = rows(factory)[0]
    assert (row.tool_source, row.mcp_server, row.status) == ("mcp", "logistics", "成功")
    assert row.arguments == {"order_id": "1001"} and row.conversation_id == 9


def test_engine_invokes_with_the_tool_call_shape(audit):
    """实测：`ainvoke(args_dict)` 会把 MCP 的 artifact 丢掉，只有 tool_call 形状才回 ToolMessage。
    引擎对内置和 MCP 统一走这一条路，用一个只记录入参的假工具把形状钉住。"""
    from langchain_core.messages import ToolMessage

    writer, factory = audit
    received = []

    class Stub:
        name = "query_order"
        description = "查询。"
        args_schema = {"type": "object", "properties": {"order_id": {"type": "string"}}}

        async def ainvoke(self, payload):
            received.append(payload)
            return ToolMessage(content='{"ok": true, "data": {"order_id": "1001"}}',
                               tool_call_id=payload["id"], name=payload["name"])

    engine = ToolEngine(_registry([ToolSpec(name="query_order", description="查询。",
                                            json_schema=Stub.args_schema, source="builtin", mcp_server=None,
                                            permission="read", label="查询订单", tool=Stub())]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"}, tool_call_id="call-42",
                  conversation_id=5)
    assert received == [{"type": "tool_call", "name": "query_order", "args": {"order_id": "1001"},
                         "id": "call-42"}]
    assert outcome.ok is True and json.loads(outcome.content)["data"]["order_id"] == "1001"
    assert outcome.tool_call_id == "call-42"
    assert rows(factory)[0].tool_call_id == "call-42"        # 审计里的 id 与模型给出的对齐


def test_engine_mints_a_call_id_when_the_caller_has_none(audit):
    writer, factory = audit
    engine = ToolEngine(_registry([spec_of(ok_tool(), name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"}, conversation_id=5)
    assert outcome.ok is True
    assert rows(factory)[0].tool_call_id


def test_audit_failure_does_not_change_the_outcome(audit):
    """§5：审计写失败必须被引擎咽下去，工具结果照常返回（第 5 条：审计不许反噬执行）。"""
    class ExplodingAudit:
        calls = 0

        def record(self, item):
            ExplodingAudit.calls += 1
            raise RuntimeError("审计库挂了")

    engine = ToolEngine(_registry([spec_of(ok_tool(), name="query_order")]), settings=settings(),
                        audit=ExplodingAudit())
    outcome = run(engine, name="query_order", args={"order_id": "1001"})
    assert outcome.ok is True and ExplodingAudit.calls == 1


def test_malformed_schema_is_reported_without_crashing(audit):
    """外部 MCP Server 可能送来非法 schema：不能让它把整个调用炸掉，也不能当它校验通过。"""
    writer, factory = audit
    calls = []

    class Stub:
        name = "query_warranty"
        description = "查在保。"
        args_schema = {"type": "nonsense", "properties": {"order_id": {"type": "string"}}}

        async def ainvoke(self, payload):
            calls.append(payload)
            return {"ok": True}

    spec = ToolSpec(name="query_warranty", description="查在保。", json_schema=Stub.args_schema,
                    source="mcp", mcp_server="aftersale", permission="read", label="查在保", tool=Stub())
    outcome = run(ToolEngine(_registry([spec]), settings=settings(), audit=writer),
                  name="query_warranty", args={"order_id": "1001"})
    assert outcome.ok is False and outcome.code == "invalid_arguments" and calls == []
    assert "参数定义有问题" in outcome.content
    assert rows(factory)[0].status == "校验拦下"


def test_cancelled_call_is_audited_as_failure_then_reraised(audit):
    """前端断开或图被中断时，CancelledError 也要留一条「失败」，然后原样抛给上层。"""
    writer, factory = audit

    class Stub:
        name = "query_order"
        description = "查询。"
        args_schema = {"type": "object", "properties": {"order_id": {"type": "string"}},
                       "required": ["order_id"]}

        async def ainvoke(self, payload):
            raise asyncio.CancelledError()

    engine = ToolEngine(_registry([ToolSpec(name="query_order", description="查询。",
                                            json_schema=Stub.args_schema, source="builtin", mcp_server=None,
                                            permission="read", label="查询订单", tool=Stub())]),
                        settings=settings(), audit=writer)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await engine.run(name="query_order", args={"order_id": "1001"}, conversation_id=2)

    asyncio.run(scenario())
    row = rows(factory)[0]
    assert row.status == "失败" and row.tool_call_id.startswith("engine-")
    assert "取消" in row.error_message


def test_hot_path_audit_runs_off_the_event_loop(audit):
    """审计是一次同步 INSERT+commit：热路径必须在线程里，否则所有会话的 SSE 都停在这一往返上（评审 I3）。"""
    writer, factory = audit
    original = writer.record
    seen = []

    def spy(item):
        seen.append(threading.current_thread())
        return original(item)

    writer.record = spy

    @tool("query_order", args_schema=Args, description="查询。")
    def handle(order_id: str) -> dict:
        return {"ok": True, "data": {"order_id": order_id}}

    @tool("boom_tool", args_schema=Args, description="查询。")
    def boom(order_id: str) -> dict:
        raise OperationalError("select 1", {}, Exception("忙"))

    engine = ToolEngine(_registry([spec_of(handle, name="query_order"), spec_of(boom, name="boom_tool")]),
                        settings=settings(), audit=writer)
    assert run(engine, name="query_order", args={"order_id": "1001"}).ok is True
    assert run(engine, name="boom_tool", args={"order_id": "1001"}).code == "tool_error"
    assert [thread is not threading.main_thread() for thread in seen] == [True, True]
    assert [row.status for row in rows(factory)] == ["成功", "失败"]


def test_a_result_that_cannot_be_formatted_is_reported_without_re_executing(audit, monkeypatch):
    """整理结果失败不能重跑工具：读工具白跑一遍，写工具会真建出第二张单（评审 Minor 3）。"""
    writer, factory = audit
    seen = []

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def handle(order_id: str) -> dict:
        seen.append(order_id)
        return {"ok": True, "data": {"ticket_no": "T20261031001"}}

    def broken(*args, **kwargs):
        raise ValueError("格式化炸了")

    monkeypatch.setattr("app.tools.engine.format_content", broken)
    engine = ToolEngine(_registry([spec_of(handle, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="create_ticket", args={"order_id": "1001"}, confirmed=True)
    assert seen == ["1001"]                       # 工具只跑了一次：写操作重跑就是重复副作用
    assert outcome.ok is False and outcome.code == "tool_error"
    assert "可能已经执行" in outcome.content        # 工具确实执行过了，只能报「结果未知」
    assert rows(factory)[0].status == "失败"


def test_pydantic_error_on_the_second_attempt_carries_the_attempt_count(audit):
    writer, factory = audit
    seen = []

    class Stub:
        name = "query_order"
        description = "查询。"
        args_schema = {"type": "object", "properties": {"order_id": {"type": "string"}},
                       "required": ["order_id"]}

        async def ainvoke(self, payload):
            seen.append(payload)
            if len(seen) == 1:
                raise OperationalError("select 1", {}, Exception("忙"))
            Args.model_validate({})          # 第二次尝试才爆 pydantic：重试过几次要如实记

    spec = ToolSpec(name="query_order", description="查询。", json_schema=Stub.args_schema,
                    source="builtin", mcp_server=None, permission="read", label="查询订单", tool=Stub())
    outcome = run(ToolEngine(_registry([spec]), settings=settings(), audit=writer),
                  name="query_order", args={"order_id": "1001"})
    assert outcome.code == "invalid_arguments" and len(seen) == 2
    row = rows(factory)[0]
    assert (row.status, row.retry_count) == ("校验拦下", 1)


def test_record_denial_of_a_read_tool_writes_nothing(audit):
    """只有写操作才会弹卡片；给只读工具补「取消」记录没有意义（也不该有这种调用）。"""
    writer, factory = audit
    engine = ToolEngine(_registry([spec_of(ok_tool(), name="query_order")]),
                        settings=settings(), audit=writer)
    engine.record_denial(name="query_order", args={"order_id": "1001"}, reason="取消")
    assert rows(factory) == []
