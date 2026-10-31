import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import create_engine, select

from app.agent.react import run_react
from app.config import Settings
from app.database import build_session_factory
from app.models import Base, Message, ToolAuditLog
from app.prompts import chat_system
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools.wiring import build_engine, build_registry, fetch_mcp_specs
from app.workflow.policy_nodes import make_order_node
from tests.support import ScriptedModel, call, msg_chunk


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k",
            "tool_timeout_seconds": 2, "tool_max_attempts": 2}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def ctx(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'callsites.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id="anon-1", message="帮我建个工单")
    yield store, conversation_id, factory
    engine.dispose()


def audit_rows(store):
    with store.session_factory() as session:
        return list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))


def tool_rows(store):
    with store.session_factory() as session:
        return list(session.scalars(
            select(Message).where(Message.role == "tool").order_by(Message.id)))


def run_round(ctx, rounds, *, question="订单 1001 到哪了"):
    store, conversation_id, factory = ctx
    cfg = settings()
    registry = build_registry(store, conversation_id, None, settings=cfg, exclude={"query_faq"})
    model = ScriptedModel(rounds)
    statuses, tokens = [], []
    result = asyncio.run(run_react(
        model=model, registry=registry,
        engine=build_engine(registry, settings=cfg, session_factory=factory),
        messages=[SystemMessage(content=chat_system("测试店")), HumanMessage(content=question)],
        on_token=tokens.append, on_status=statuses.append,
        store=store, conversation_id=conversation_id, timeout=5))
    return result, statuses, tokens, store


def test_write_call_stops_the_loop_and_hands_back_pending_write(ctx):
    args = {"description": "杯子碎了，想申请售后", "ticket_type": "售后"}
    result, statuses, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [call("create_ticket", args, "call_1")])],
              [msg_chunk("好的")]])
    assert result.pending_write["tool"] == "create_ticket"
    assert result.pending_write["args"] == args
    assert result.pending_write["tool_call_id"] == "call_1"
    assert len(result.pending_write["pending_id"]) >= 8
    assert result.stopped_by == "await_confirm" and result.tool_calls == 1

    last = statuses[-1]
    assert (last["phase"], last["tool"], last["ok"]) == ("end", "create_ticket", False)
    assert last["source"] == "builtin" and last["mcp_server"] is None and "确认" in last["summary"]

    with store.session_factory() as session:
        rows = list(session.scalars(select(Message).where(Message.role == "tool")))
    assert json.loads(rows[-1].content)["error"] == "confirm_required"
    assert rows[-1].tool_call_id == "call_1"
    assert audit_rows(store) == []


def test_confirm_pause_marks_the_remaining_calls_as_not_executed(ctx):
    args = {"description": "杯子碎了，想申请售后", "ticket_type": "售后"}
    result, statuses, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [
            call("query_order", {"order_id": "1001"}, "call_1"),
            call("create_ticket", args, "call_2"),
            call("query_logistics", {"order_id": "1001"}, "call_3"),
        ])]])
    assert result.pending_write["tool"] == "create_ticket"
    assert result.pending_write["tool_call_id"] == "call_2"
    assert result.tool_calls == 2
    assert [item["tool"] for item in statuses if item["phase"] == "start"] == \
        ["query_order", "create_ticket"]

    rows = tool_rows(store)
    assert [row.tool_call_id for row in rows] == ["call_1", "call_2", "call_3"]
    assert json.loads(rows[1].content)["error"] == "confirm_required"
    assert json.loads(rows[2].content)["error"] == "not_executed"
    assert [row.tool_name for row in audit_rows(store)] == ["query_order"]


def test_confirm_exit_returns_exactly_the_streamed_preface(ctx):
    args = {"description": "杯子碎了，想申请售后", "ticket_type": "售后"}
    # 尾字 "D" 会被 ReplyGuard 当成疑似 DSML 标记扣住，确认出口必须 finish() 才能把它交出来。
    result, _, tokens, store = run_round(
        ctx, [[msg_chunk("好的，我帮你建工单D"),
               msg_chunk("", "tool_calls", [call("create_ticket", args, "call_1")])]])
    assert "".join(tokens) == "好的，我帮你建工单D"
    assert result.reply == "".join(tokens)
    rows = tool_rows(store)
    assert len(rows) == 1 and json.loads(rows[0].content)["error"] == "confirm_required"
    assert audit_rows(store) == []


def test_mcp_specs_awaits_and_falls_back_to_empty():
    class Good:
        async def specs(self):
            return ["query_logistics"]

    class Broken:
        async def specs(self):
            raise RuntimeError("Server 没起")

    assert asyncio.run(fetch_mcp_specs(Good())) == ("query_logistics",)
    assert asyncio.run(fetch_mcp_specs(Broken())) == ()
    assert asyncio.run(fetch_mcp_specs(None)) == ()


def test_read_call_goes_through_the_engine_and_writes_audit(ctx):
    result, statuses, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"}, "call_1")])],
              [msg_chunk("已发货")]])
    assert result.pending_write is None and result.tool_calls == 1 and result.reply == "已发货"
    assert statuses[0] == {"phase": "start", "tool": "query_order", "label": "查询订单",
                           "source": "builtin", "mcp_server": None}
    row = audit_rows(store)[0]
    assert (row.tool_name, row.status, row.tool_source) == ("query_order", "成功", "builtin")
    assert row.retry_count == 0


def test_unknown_tool_from_the_model_is_reported_not_raised(ctx):
    result, _, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [call("query_weather", {"city": "上海"}, "call_1")])],
              [msg_chunk("我查不到天气")]])
    assert result.reply == "我查不到天气" and result.tool_calls == 1
    assert audit_rows(store) == []
    with store.session_factory() as session:
        row = list(session.scalars(select(Message).where(Message.role == "tool")))[-1]
    assert json.loads(row.content)["error"] == "unknown_tool"


def test_policy_order_node_uses_the_engine(ctx, monkeypatch):
    store, conversation_id, factory = ctx
    frames = []
    monkeypatch.setattr("app.workflow.policy_nodes.emit",
                        lambda event, payload: frames.append((event, payload)))

    class Deps:
        pass

    deps = Deps()
    deps.store, deps.settings, deps.knowledge = store, settings(), None
    state = asyncio.run(make_order_node(deps)({"conversation_id": conversation_id,
                                               "question": "订单 1001 到哪了", "intent": "物流"}))
    assert state["order"]["order_id"] == "1001"
    row = audit_rows(store)[0]
    assert (row.tool_name, row.status) == ("query_order", "成功")

    starts = [payload for event, payload in frames if event == "status" and payload["phase"] == "start"]
    ends = [payload for event, payload in frames if event == "status" and payload["phase"] == "end"]
    assert starts == [{"phase": "start", "tool": "query_order", "label": "查询订单",
                       "source": "builtin", "mcp_server": None}]
    assert (ends[0]["source"], ends[0]["mcp_server"], ends[0]["ok"]) == ("builtin", None, True)


def test_policy_order_node_reports_a_failed_lookup_in_the_end_frame(ctx, monkeypatch):
    store, conversation_id, _ = ctx
    frames = []
    monkeypatch.setattr("app.workflow.policy_nodes.emit",
                        lambda event, payload: frames.append((event, payload)))

    class Deps:
        pass

    deps = Deps()
    deps.store, deps.settings, deps.knowledge = store, settings(), None
    state = asyncio.run(make_order_node(deps)({"conversation_id": conversation_id,
                                               "question": "订单 9999 到哪了", "intent": "物流"}))
    assert state["order"] is None
    ends = [payload for event, payload in frames if event == "status" and payload["phase"] == "end"]
    assert (ends[0]["ok"], ends[0]["source"], ends[0]["mcp_server"]) == (False, "builtin", None)
