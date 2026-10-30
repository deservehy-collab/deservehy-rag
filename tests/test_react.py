import asyncio
import json

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from app.agent.react import run_react
from app.tools import ToolOutcome


def chunk(text="", finish="stop", tool_calls=None):
    return AIMessageChunk(content=text, tool_calls=tool_calls or [],
                          response_metadata={"finish_reason": finish})


def call(name, args, call_id="call_1"):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class BoundModel:
    def __init__(self, model):
        self.model = model

    async def astream(self, messages):
        self.model.bound_calls += 1
        async for item in self.model._next():
            yield item


class ScriptedModel:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0
        self.bound_calls = 0
        self.tool_names = []

    def bind_tools(self, tools):
        self.tool_names = [tool.name for tool in tools]
        return BoundModel(self)

    async def _next(self):
        self.calls += 1
        item = self.rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        for part in item:
            await asyncio.sleep(0)
            yield part

    async def astream(self, messages):
        async for part in self._next():
            yield part


class FakeExecutor:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def execute(self, name, args):
        self.calls.append((name, args))
        if name in self.results:
            return self.results[name]
        return ToolOutcome(name, True, "ok", json.dumps({"ok": True, "data": {}}), "ok", 1)


def tool(name):
    class FakeTool:
        def __init__(self, value):
            self.name = value
    return FakeTool(name)


TOOLS = {name: tool(name) for name in ("query_order", "query_logistics", "query_faq", "create_ticket")}
MESSAGES = [SystemMessage(content="system"), HumanMessage(content="订单 1001 到哪了")]


async def run(model, **overrides):
    events = []
    options = dict(model=model, tools=TOOLS, executor=FakeExecutor(), messages=MESSAGES,
                   on_token=lambda text: events.append(("token", text)),
                   on_status=lambda payload: events.append(("status", payload["phase"])),
                   max_steps=4, token_budget=6000, timeout=5)
    options.update(overrides)
    result = await run_react(**options)
    return result, events


def test_single_round_streams_answer():
    model = ScriptedModel([[chunk("包裹在上海", "stop")]])
    result, events = asyncio.run(run(model))
    assert result.reply == "包裹在上海"
    assert result.steps == 1 and result.tool_calls == 0 and result.stopped_by is None
    assert events == [("token", "包裹在上海")]
    assert model.calls == 1 and model.bound_calls == 1


def test_two_tool_rounds_then_answer():
    model = ScriptedModel([
        [chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [chunk("", "tool_calls", [call("query_logistics", {"order_id": "1001"}, "call_2")])],
        [chunk("订单已发货，包裹在上海。", "stop")],
    ])
    executor = FakeExecutor()
    result, _ = asyncio.run(run(model, executor=executor))
    assert [name for name, _ in executor.calls] == ["query_order", "query_logistics"]
    assert result.steps == 3 and result.tool_calls == 2
    assert result.reply == "订单已发货，包裹在上海。"


def test_max_steps_forces_unbound_convergence_call():
    model = ScriptedModel([
        [chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [chunk("", "tool_calls", [call("query_logistics", {"order_id": "1001"}, "call_2")])],
        [chunk("", "tool_calls", [call("query_order", {"order_id": "1001"}, "call_3")])],
        [chunk("", "tool_calls", [call("query_logistics", {"order_id": "1001"}, "call_4")])],
        [chunk("依据已有结果，包裹在运输中。", "stop")],
    ])
    result, _ = asyncio.run(run(model, max_steps=4))
    assert model.bound_calls == 4
    assert model.calls == 5
    assert result.steps == 5 and result.tool_calls == 4


def test_token_budget_stops_before_extra_round():
    model = ScriptedModel([
        [chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [chunk("不该被调用", "stop")],
    ])
    result, events = asyncio.run(run(model, token_budget=1))
    assert result.stopped_by == "token_budget"
    assert "处理步骤太多" in result.reply
    assert model.calls == 1
    assert events[-1][0] == "token"


def test_token_budget_reply_is_exactly_what_was_streamed():
    """预算出口的 reply 必须等于已流给客户端的全部文本（工具轮之前的前言 + BUDGET_TEXT）。"""
    model = ScriptedModel([
        [chunk("先帮您查一下订单", "tool_calls"),
         chunk("", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [chunk("不该被调用", "stop")],
    ])
    result, events = asyncio.run(run(model, token_budget=1))
    streamed = "".join(text for kind, text in events if kind == "token")
    assert result.stopped_by == "token_budget"
    assert result.reply == streamed
    assert "先帮您查一下订单" in result.reply and "处理步骤太多" in result.reply
    assert model.calls == 1


def test_tool_failure_is_fed_back_and_loop_continues():
    model = ScriptedModel([
        [chunk("", "tool_calls", [call("query_order", {"order_id": "9999"})])],
        [chunk("没有查到订单 9999。", "stop")],
    ])
    failure = ToolOutcome("query_order", False, "not_found", '{"ok": false}', "没有找到订单 9999。", 1)
    result, _ = asyncio.run(run(model, executor=FakeExecutor({"query_order": failure})))
    assert result.reply == "没有查到订单 9999。"
    assert result.steps == 2


def test_tool_outcome_can_force_refusal():
    model = ScriptedModel([[chunk("", "tool_calls", [call("query_faq", {"keyword": "退款"})])]])

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
        [chunk("我来帮您查询", "tool_calls"),
         chunk("", "tool_calls", [call("query_faq", {"keyword": "退款"})])],
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
    model = ScriptedModel([[chunk("半截", None)]])
    with pytest.raises(ValueError):
        asyncio.run(run(model))


def test_empty_answer_uses_fallback_plain():
    model = ScriptedModel([[chunk("", "stop")]])
    result, events = asyncio.run(run(model))
    assert result.reply.startswith("抱歉，刚才没能生成有效回答")
    assert events[-1] == ("token", result.reply)


def test_too_many_tool_calls_raises():
    model = ScriptedModel([[chunk("", "tool_calls", [call(f"tool_{i}", {}, f"c{i}") for i in range(6)])]])
    with pytest.raises(ValueError):
        asyncio.run(run(model))
