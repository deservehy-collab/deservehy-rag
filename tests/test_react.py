import asyncio

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from app.agent.react import run_react
from app.config import Settings
from app.tools.registry import ToolRegistry, spec_from_tool
from app.tools.wiring import build_engine
from tests.support import ScriptedModel, call, msg_chunk

SETTINGS = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1",
                    openai_model="m", openai_api_key="k")


class FakeArgs(BaseModel):
    model_config = ConfigDict(extra="allow")


class FakeTools:
    NAMES = ("query_order", "query_logistics", "query_faq", "create_ticket")

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def _make(self, name):
        def impl(**kwargs):
            self.calls.append((name, kwargs))
            return self.results.get(name, {"ok": True, "data": {}})

        return StructuredTool(name=name, description=f"测试用工具 {name}",
                              args_schema=FakeArgs, func=impl)

    def registry(self):
        return ToolRegistry([spec_from_tool(self._make(name), source="builtin", permission="read")
                             for name in self.NAMES])


MESSAGES = [SystemMessage(content="system"), HumanMessage(content="订单 1001 到哪了")]


async def run(model, fakes=None, **overrides):
    events = []
    fakes = fakes or FakeTools()
    registry = fakes.registry()
    options = dict(model=model, registry=registry,
                   engine=build_engine(registry, settings=SETTINGS, session_factory=None),
                   messages=MESSAGES,
                   on_token=lambda text: events.append(("token", text)),
                   on_status=lambda payload: events.append(("status", payload["phase"])),
                   max_steps=4, token_budget=6000, timeout=5)
    options.update(overrides)
    result = await run_react(**options)
    return result, events


def test_single_round_streams_answer():
    model = ScriptedModel([[msg_chunk("包裹在上海", "stop")]])
    result, events = asyncio.run(run(model))
    assert result.reply == "包裹在上海"
    assert result.steps == 1 and result.tool_calls == 0 and result.stopped_by is None
    assert events == [("token", "包裹在上海")]
    assert model.calls == 1 and model.bound_calls == 1


def test_two_tool_rounds_then_answer():
    model = ScriptedModel([
        [msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [msg_chunk("", "tool_calls", [call("query_logistics", {"order_id": "1001"}, "call_2")])],
        [msg_chunk("订单已发货，包裹在上海。", "stop")],
    ])
    fakes = FakeTools()
    result, _ = asyncio.run(run(model, fakes=fakes))
    assert [name for name, _ in fakes.calls] == ["query_order", "query_logistics"]
    assert result.steps == 3 and result.tool_calls == 2
    assert result.reply == "订单已发货，包裹在上海。"


def test_max_steps_forces_unbound_convergence_call():
    model = ScriptedModel([
        [msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [msg_chunk("", "tool_calls", [call("query_logistics", {"order_id": "1001"}, "call_2")])],
        [msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"}, "call_3")])],
        [msg_chunk("", "tool_calls", [call("query_logistics", {"order_id": "1001"}, "call_4")])],
        [msg_chunk("依据已有结果，包裹在运输中。", "stop")],
    ])
    result, _ = asyncio.run(run(model, max_steps=4))
    assert model.bound_calls == 4
    assert model.calls == 5
    assert result.steps == 5 and result.tool_calls == 4


def test_token_budget_stops_before_extra_round():
    model = ScriptedModel([
        [msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [msg_chunk("不该被调用", "stop")],
    ])
    result, events = asyncio.run(run(model, token_budget=1))
    assert result.stopped_by == "token_budget"
    assert "处理步骤太多" in result.reply
    assert model.calls == 1
    assert events[-1][0] == "token"


def test_token_budget_reply_is_exactly_what_was_streamed():
    """预算出口的 reply 必须等于已流给客户端的全部文本（工具轮之前的前言 + BUDGET_TEXT）。"""
    model = ScriptedModel([
        [msg_chunk("先帮您查一下订单", "tool_calls"),
         msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [msg_chunk("不该被调用", "stop")],
    ])
    result, events = asyncio.run(run(model, token_budget=1))
    streamed = "".join(text for kind, text in events if kind == "token")
    assert result.stopped_by == "token_budget"
    assert result.reply == streamed
    assert "先帮您查一下订单" in result.reply and "处理步骤太多" in result.reply
    assert model.calls == 1


def test_tool_failure_is_fed_back_and_loop_continues():
    model = ScriptedModel([
        [msg_chunk("", "tool_calls", [call("query_order", {"order_id": "9999"})])],
        [msg_chunk("没有查到订单 9999。", "stop")],
    ])
    failure = {"ok": False, "error": "not_found", "message": "没有找到订单 9999。"}
    result, _ = asyncio.run(run(model, fakes=FakeTools({"query_order": failure})))
    assert result.reply == "没有查到订单 9999。"
    assert result.steps == 2


def test_tool_outcome_can_force_refusal():
    model = ScriptedModel([[msg_chunk("", "tool_calls", [call("query_faq", {"keyword": "退款"})])]])

    async def hook(name, outcome):
        return "固定拒答"

    result, events = asyncio.run(run(model, on_tool_outcome=hook))
    assert result.reply == "固定拒答"
    assert result.stopped_by == "tool_refusal"
    assert model.calls == 1
    assert events[-1] == ("token", "固定拒答")


def test_tool_refusal_reply_is_exactly_what_was_streamed():
    """拒答出口的 reply 必须等于已流给客户端的全部文本（前言 + 强制拒答），不能只有 forced。"""
    model = ScriptedModel([
        [msg_chunk("我来帮您查询", "tool_calls"),
         msg_chunk("", "tool_calls", [call("query_faq", {"keyword": "退款"})])],
    ])

    async def hook(name, outcome):
        return "固定拒答"

    result, events = asyncio.run(run(model, on_tool_outcome=hook))
    streamed = "".join(text for kind, text in events if kind == "token")
    assert result.stopped_by == "tool_refusal"
    assert result.reply == streamed
    assert "我来帮您查询" in result.reply and "固定拒答" in result.reply
    assert events[-1] == ("token", "固定拒答")
    assert model.calls == 1


def test_invalid_finish_reason_raises():
    model = ScriptedModel([[msg_chunk("半截", None)]])
    with pytest.raises(ValueError):
        asyncio.run(run(model))


def test_empty_answer_uses_fallback_plain():
    model = ScriptedModel([[msg_chunk("", "stop")]])
    result, events = asyncio.run(run(model))
    assert result.reply.startswith("抱歉，刚才没能生成有效回答")
    assert events[-1] == ("token", result.reply)


def test_too_many_tool_calls_raises():
    model = ScriptedModel([[msg_chunk("", "tool_calls", [call(f"tool_{i}", {}, f"c{i}") for i in range(6)])]])
    with pytest.raises(ValueError):
        asyncio.run(run(model))
