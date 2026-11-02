"""ch09 T3：`stream_usage` 打开后，「只带 usage 的 chunk」必须被流式消费点当空气。

为什么要有这个文件：验收⑤「按意图统计 token 花费」的前提是 Langfuse 里每条 generation
有真实 token 数。`app/model.py` 原本 `stream_usage=False` ⇒ 流式回复永远拿不到 usage ⇒
T16 只能读到一串 0。改这一行的风险在两个消费点：

- `app/agent/react.py:86`（`if item.text:`，走 /api/chat 的工作流 agent 节点）
- `app/service.py:230`（`if chunk.text:`，`stream_chat`，`scripts/evaluate.py:39` 在用）

usage-only chunk 的 `content` 是空的、只有 `usage_metadata`，一旦漏进 `if` 里面，客户端就会
收到空 token 帧。这里把四件事钉住：模型确实开了流式 usage、两个消费点都不发空帧、端到端 SSE
的事件序列不变、flag 的 response 半边（真 client 聚合出的 usage 不是 None）。形状全部按本机实测：
`AIMessageChunk(content="", usage_metadata={...}, response_metadata={})`，
SSE 侧是 `{"choices": [], "usage": {...}}` 那一帧、排在带 `finish_reason` 的帧之后。
"""

import asyncio
import json

import httpx
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from app.agent.guards import AgentStream
from app.agent.react import run_react
from app.config import Settings
from app.model import build_model
from app.service import CustomerService
from app.tools.registry import ToolRegistry, spec_from_tool
from app.tools.wiring import build_engine
from tests.support import ChunkStream, _settings, api, chunk, events, msg_chunk, streamed

USAGE = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}

# 评审修复轮 1（第 5 条）：改用仓库夹具 _settings() 叠本文件的 override，
# 拿满它的 9 个护栏默认值（log_file/checkpoint 走内存、mcp_servers="" 等），不再裸 new Settings。
# api_key 保持显然假的值，测试里永远不许出现真实 key。
UPSTREAM_SETTINGS = _settings({"openai_base_url": "http://upstream.invalid/v1",
                               "openai_model": "test-model",
                               "openai_api_key": "test-secret-not-real"})
MESSAGES = [HumanMessage(content="你好")]


def settings(**overrides):
    base = dict(openai_api_key="k", openai_base_url="http://u/v1", openai_model="m")
    base.update(overrides)
    return Settings(_env_file=None, **base)


def usage_chunk():
    # 真实形态：content 为空，只有 usage_metadata / response_metadata。
    # 探针实测：providers 把 usage 单发一帧，choices 已经空掉，所以 response_metadata 是 {}；
    # finish_reason 在它前面那一帧上（见 usage_sse_frames）。
    return AIMessageChunk(content="", usage_metadata=dict(USAGE), response_metadata={})


def usage_sse_frames(usage=None):
    """真实流式的最后一帧：`choices: []` + `usage`。`tests.support.streamed()` 造不出这一帧，
    所以在本文件里补（不动共用夹具的默认行为，一堆旧用例靠它）。"""
    frame = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
             "model": "test-model", "choices": [],
             "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
    return ("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode("utf-8")


def streamed_with_usage(parts=("hi",), finish_reason="stop"):
    """`streamed()` 的「带 usage 帧」版：文本帧 → finish 帧 → usage 帧 → [DONE]。"""
    stream = ChunkStream([*(chunk(text) for text in parts), chunk(finish_reason=finish_reason),
                          usage_sse_frames(), b"data: [DONE]\n\n"])
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


class FakeArgs(BaseModel):
    model_config = ConfigDict(extra="allow")


def registry() -> ToolRegistry:
    """最小替身注册表：`run_react` 一定要真的 ToolRegistry + ToolEngine（照 tests/test_react.py 的形状）。"""
    def impl(**kwargs):
        return {"ok": True, "data": {}}

    tool = StructuredTool(name="query_order", description="测试用工具 query_order",
                          args_schema=FakeArgs, func=impl)
    return ToolRegistry([spec_from_tool(tool, source="builtin", permission="read")])


def test_build_model_enables_stream_usage():
    assert build_model(settings()).stream_usage is True


def test_react_tolerates_usage_only_chunk():
    """usage-only chunk 不得多发 token 帧，也不得污染累积文本。

    评审修复轮 1（第 2 条）：改名 from `test_react_loop_ignores_usage_only_chunk`——这条用例
    真正钉住的是 `react.py:91` 的 `.get("finish_reason") or step_finish` 容忍性：就算删掉
    `react.py:86` 的 `if item.text:`，`AgentStream.feed("")` 返回 `""`、`if safe:` 再吞一层，
    本用例照样绿。所以补 `assert all(seen)`，把「客户端不会收到空帧」写成显式不变量——
    将来 `AgentStream.feed` 语义变了（比如原样返回入参），这里能红，而不是静默放行。
    """
    from tests.support import ScriptedModel

    rounds = [[msg_chunk("你好", finish=""), msg_chunk("", "stop"), usage_chunk()]]
    model = ScriptedModel(rounds)
    tools = registry()
    seen: list[str] = []

    async def collect():
        return await run_react(model=model, registry=tools,
                               engine=build_engine(tools, settings=UPSTREAM_SETTINGS,
                                                   session_factory=None),
                               messages=list(MESSAGES),
                               on_token=seen.append,
                               on_status=lambda payload: seen.append(payload["phase"]),
                               timeout=5, guard=AgentStream())

    result = asyncio.run(collect())
    assert result.reply == "你好"
    assert seen == ["你好"]
    assert all(seen)
    assert result.finish_reason == "stop"
    assert result.tool_calls == 0 and result.steps == 1


def test_stream_chat_ignores_usage_only_chunk():
    """`CustomerService.stream_chat`（scripts/evaluate.py 在用）也不许把 usage 帧发成 token。

    评审修复轮 1（第 4 条）：整个用例收进**一个** `asyncio.run`，`client.aclose()` 进
    `finally`——原来 `asyncio.run(collect())` + `asyncio.run(client.aclose())` 开了两个事件
    循环，MockTransport 下侥幸能过，换真实 transport 就是莫名其妙的失败或漏 socket。
    """
    transport = httpx.MockTransport(lambda request: streamed_with_usage(parts=("你好",)))

    async def scenario():
        client = httpx.AsyncClient(transport=transport)
        try:
            model = build_model(UPSTREAM_SETTINGS, async_client=client)
            service = CustomerService(UPSTREAM_SETTINGS, model)
            frames = []
            async for event, payload in service.stream_chat(list(MESSAGES)):
                frames.append((event, payload))
            return frames
        finally:
            await client.aclose()

    frames = asyncio.run(scenario())
    assert [name for name, _ in frames] == ["token", "done"]
    assert [payload["content"] for name, payload in frames if name == "token"] == ["你好"]
    # `CustomerService.stream_chat`（scripts/evaluate.py 在用）不落 assistant 行，done 只有
    # finish_reason——这里钉住「恰好这一个键」，多一个少一个都红（T9 的 message_id 只在工作流
    # 的 log 节点发，别把两条 done 的形状混成一谈）。
    assert set(frames[-1][1]) == {"finish_reason"}
    assert frames[-1][1]["finish_reason"] == "stop"


def gateway_transport():
    """按真实网关行为造替身：只有请求体带 `stream_options={"include_usage": true}` 才吐 usage 帧。

    为什么必须带这个「门」：评审修复轮 1 实测（原文贴进报告），langchain-openai 1.7.3 的
    `_convert_chunk_to_generation_chunk`（`.venv/.../langchain_openai/chat_models/base.py:1626`）
    **无条件**解析 chunk 里的 `usage` 字段——`stream_usage=False` 只不发 `stream_options`，
    不影响解析。所以拿「无条件吐 usage 帧」的 `streamed_with_usage()` 断聚合结果，flag 改回
    False 断言照样是 18、永远不会红；加了门才是真的因果锁（网关行为照 DeepSeek 实测）。
    """
    def handler(request):
        if json.loads(request.content).get("stream_options", {}).get("include_usage"):
            return streamed_with_usage(parts=("你好",))
        return streamed(parts=("你好",))

    return httpx.MockTransport(handler)


def test_stream_usage_flag_makes_aggregated_usage_metadata_real():
    """T3 前提的「response 半边」：真 `ChatOpenAI`（build_model）流式聚合出的 usage_metadata
    必须是真实数字。评审者独立复现过本仓库 fixture 下的 chunk 形状：
    `('你好', {'model_provider': 'openai'}, None)` → `('', {'finish_reason': 'stop', ...}, None)`
    → `('', {}, {'input_tokens': 11, 'output_tokens': 7, 'total_tokens': 18, ...})`
    ——usage 帧的 `response_metadata` 是 `{}`、`finish_reason` 在前一帧，聚合恰好 18。

    这条断言的意义：`stream_usage` 被改回 False、`output_version` 变化、或有人用
    `model_kwargs["stream_options"]` 覆盖掉 ⇒ 新断言变红，而不是等 Langfuse 面板空白
    （验收⑤「按意图统计 token 花费」建在这条之上）。变异验真见 task-3-report.md §6。
    """
    async def scenario():
        client = httpx.AsyncClient(transport=gateway_transport())
        try:
            model = build_model(UPSTREAM_SETTINGS, async_client=client)
            agg = None
            async for piece in model.astream(MESSAGES):
                agg = piece if agg is None else agg + piece
            return agg
        finally:
            await client.aclose()

    agg = asyncio.run(scenario())
    assert agg.usage_metadata["total_tokens"] == 18
    assert agg.usage_metadata["input_tokens"] == USAGE["input_tokens"]
    assert agg.usage_metadata["output_tokens"] == USAGE["output_tokens"]


def test_api_chat_event_shape_survives_usage_only_chunk():
    """端到端：走 /api/chat（工作流 agent 节点 ⇒ react.py 那个消费点）时事件序列一个字没变。

    问题故意不写「你好」——`app/workflow/routing.py:17` 的 fast_chitchat 会直接走固定话术节点，
    一个模型 token 都不发，那样这条用例根本没碰到要锁的代码。
    """
    with api(lambda request: streamed_with_usage(parts=("hi",))) as (client, seen):
        response = client.post("/api/chat", json={"message": "订单 1001 到哪了",
                                                 "client_id": "anon-ch09-t3"})
        frames = events(response)

    assert [name for name, _ in frames] == ["meta", "token", "done"]
    assert [payload["content"] for name, payload in frames if name == "token"] == ["hi"]
    # 工作流的 done 帧形状是 T10 认领轮次的契约：finish_reason + message_id，不许有第三个键。
    assert set(frames[-1][1]) == {"finish_reason", "message_id"}
    assert frames[-1][1]["finish_reason"] == "stop"
    # 上游真被打过一次：没有这一句，问题被 fast_chitchat 截走时这条用例也会「绿着测空气」。
    assert len(seen) == 1 and seen[0][0] == "/v1/chat/completions"
