import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

CUSTOM_UPDATES = ["custom", "updates"]


class SpikeState(TypedDict, total=False):
    log: Annotated[list, operator.add]
    pending: dict | None
    decision: str
    reruns: int


def _build():
    """agent 发一帧后挂起；confirm 节点会重跑，所以副作用必须留在 agent 里。"""

    def agent(state):
        get_stream_writer()(("token", {"content": "agent"}))
        return {"pending": {"tool": "create_ticket"}, "log": ["agent"]}

    def confirm_ticket(state):
        # 这帧是「含 interrupt 的节点 resume 时从头重跑」的唯一探针：真重跑 ⇒ resume 轮会再看见一次
        # entered；若哪天 langgraph 改成原地恢复，resume 轮就只剩 status 一帧，本测试立刻变红。
        # （没有这一行，「重跑」与「原地恢复」两种实现的输出完全相同，断言锁不住任何事实。）
        get_stream_writer()(("entered", {"tool": state["pending"]["tool"]}))
        decision = interrupt(state["pending"])
        get_stream_writer()(("status", {"phase": "resumed", "decision": decision}))
        return {"decision": decision, "log": [f"decision:{decision}"], "reruns": 1}

    builder = StateGraph(SpikeState)
    builder.add_node("agent", agent)
    builder.add_node("confirm_ticket", confirm_ticket)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", "confirm_ticket")
    builder.add_edge("confirm_ticket", END)
    return builder.compile(checkpointer=InMemorySaver())


async def _drain(graph, payload, config):
    return [item async for item in graph.astream(payload, config, stream_mode=CUSTOM_UPDATES,
                                                 durability="sync")]


def test_interrupt_surfaces_as_updates_chunk():
    async def run():
        config = {"configurable": {"thread_id": "t-one"}}
        chunks = await _drain(_build(), {"log": [], "pending": None}, config)
        # 首轮四帧（本机 langgraph 1.2.18 实测）：agent 的 custom(token) → agent 的 updates
        # → confirm 的 custom(entered) → 带 __interrupt__ 的 updates。Task 1 首轮执行时 confirm
        # 还没有 entered 那帧、只有三帧，于是评审后裁决加回探针——它同时把「重跑」变成可断言的事实。
        assert [c[0] for c in chunks] == ["custom", "updates", "custom", "updates"]
        assert chunks[0][1] == ("token", {"content": "agent"})
        assert chunks[1][1] == {"agent": {"pending": {"tool": "create_ticket"}, "log": ["agent"]}}
        assert chunks[2][1] == ("entered", {"tool": "create_ticket"})
        assert "__interrupt__" in chunks[3][1]
        interrupted = chunks[-1][1]["__interrupt__"]
        assert interrupted[0].value == {"tool": "create_ticket"}
        # 纯 custom 模式看不见中断信号——这就是服务层必须用双模式的原因。
        # stream_mode 传单个字符串时 langgraph 吐的是裸 payload（不是 (mode, payload)），
        # 所以这里比整帧内容，别写成 i[0]——那取到的是标签名，恰好等于 "token" 才假绿。
        single = [i async for i in _build().astream({"log": [], "pending": None},
                                                    {"configurable": {"thread_id": "t-two"}},
                                                    stream_mode="custom", durability="sync")]
        assert single == [("token", {"content": "agent"}), ("entered", {"tool": "create_ticket"})]
    asyncio.run(run())


def test_resumed_node_reruns_from_the_top_and_custom_writer_still_works():
    async def run():
        graph = _build()
        config = {"configurable": {"thread_id": "t-three"}}
        await _drain(graph, {"log": [], "pending": None}, config)
        resumed = await _drain(graph, Command(resume="confirm"), config)
        # entered 再次出现＝节点真的从头重跑过。T13 的副作用必须留在 agent，理由就在这行。
        assert [c[1] for c in resumed if c[0] == "custom"] == [
            ("entered", {"tool": "create_ticket"}),
            ("status", {"phase": "resumed", "decision": "confirm"}),
        ]
        values = (await graph.aget_state(config)).values
        assert values["log"] == ["agent", "decision:confirm"]
    asyncio.run(run())


def test_double_resume_is_a_silent_no_op_and_pending_looks_like_a_new_run():
    async def run():
        graph = _build()
        config = {"configurable": {"thread_id": "t-four"}}
        await _drain(graph, {"log": [], "pending": None}, config)
        await _drain(graph, Command(resume="confirm"), config)
        assert await _drain(graph, Command(resume="confirm"), config) == []
        # 用户无视卡片直接问下一句：不报错，但会再挂一个新 interrupt，旧卡片必须作废。
        again = await _drain(graph, {"log": [], "pending": None}, config)
        assert again[-1][1]["__interrupt__"][0].value == {"tool": "create_ticket"}
    asyncio.run(run())


def test_mcp_style_tool_only_supports_ainvoke():
    """引擎只用一条 ainvoke 路径：同步工具照样能 ainvoke（落 executor 线程），
    而 adapters 造的 MCP 工具没有 func，同步 invoke 会炸。"""
    import pytest
    from langchain_core.tools import StructuredTool

    @tool("sync_probe")
    def sync_probe(order_id: str) -> dict:
        """测试用。"""
        return {"ok": True, "order_id": order_id}

    async def coroutine_probe(order_id: str) -> dict:
        """测试用：只有 coroutine，形状与 langchain-mcp-adapters 造的 MCP 工具一致。"""
        return {"ok": True, "order_id": order_id}

    funcless = StructuredTool.from_function(coroutine=coroutine_probe, name="mcp_style_probe",
                                            description="测试用。")

    async def run():
        assert (await sync_probe.ainvoke({"order_id": "1001"}))["ok"] is True
        assert (await funcless.ainvoke({"order_id": "1001"}))["ok"] is True
        assert funcless.func is None
        with pytest.raises(NotImplementedError):
            funcless.invoke({"order_id": "1001"})
    asyncio.run(run())
