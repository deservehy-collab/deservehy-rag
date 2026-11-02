"""T2 追踪表面的单测：不连网、不建真客户端，全部用假对象。

覆盖面按 T2 Step 8b + 评审修复轮 1 裁定后的表面来：`build_client`（三层降级）、`turn_trace`
（关闭时直通 None / 开启时给 TurnTrace / 建不出来也不许抛 / 退出时收掉根 span）、
`set_intent_on_trace`（**只**写 contextvar 里的本轮根 span，含 `_unwrap_span` 解包那条分支；
拿不到根 span ⇒ False，ambient 一个字都不写）。
"""

import asyncio
from types import SimpleNamespace

from langgraph.types import Command
from opentelemetry import trace as otel_trace
from opentelemetry.trace import NonRecordingSpan, SpanContext

from app.config import Settings
from app.observability import tracing
from app.observability.tracing import (
    build_client, flush_tracing, set_intent_on_trace, trace_config, turn_trace,
)


def settings(**overrides):
    base = dict(openai_api_key="k", openai_base_url="http://u/v1", openai_model="m",
                langfuse_enabled=False, langfuse_host="http://127.0.0.1:3000",
                langfuse_public_key="", langfuse_secret_key="")
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakeSpan(NonRecordingSpan):
    """记 set_attribute 调用的假 span（**真·OTel Span 子类**）。

    必须走继承：`opentelemetry.trace.get_current_span()` 里有 `isinstance(span, Span)` 校验
    （api 1.45.2，`trace/propagation/__init__.py:26-32`），鸭子类型的假 span 会被换成
    INVALID_SPAN —— 评审修复轮 1 之前 `set_intent_on_trace` 会读 ambient，那样「ambient 该不该被写」
    根本测不出来。ambient 兜底删掉后继承照样留着：fake 跟真 OTEL span 同形状，
    `otel_trace.use_span(ambient)` 激活的也正是它。
    """

    def __init__(self, name="root"):
        super().__init__(SpanContext(trace_id=0x0123456789ABCDEF0123456789ABCDEF,
                                     span_id=0x0123456789ABCDEF, is_remote=False))
        self.name = name
        self.attributes: dict = {}
        self.closed = False

    def is_recording(self):
        return True

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def end(self, end_time=None):
        self.closed = True


class RootSpanContext:
    """假 client 的 `start_as_current_observation` 返回值：**在 `__exit__` 里真的把根 span 收掉**。

    评审第 2 条：原来返回 `nullcontext(root)`，而 `nullcontext.__exit__` 什么都不做 ⇒ 把
    `tracing.py` 的 `stack.close()` 删掉测试照样全绿，线上却是「根 span 永不结束 ⇒ 永不导出」。
    这里 `end()` 一下（真 SDK 的退出路径就是结束 span），`closed` 才可断言。
    """

    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, exc_type, exc, tb):
        self.span.end()
        return False


class FakeClient:
    """只实现 turn_trace 用到的两个方法（`create_trace_id` / `start_as_current_observation`）。"""

    def __init__(self, root=None, error=None):
        self.root = root if root is not None else FakeSpan()
        self.error = error
        self.seeds: list = []
        self.observation_kwargs: list = []

    def create_trace_id(self, *, seed=None):
        if self.error is not None:
            raise self.error
        self.seeds.append(seed)
        return "0123456789abcdef0123456789abcdef"

    def start_as_current_observation(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.observation_kwargs.append(kwargs)
        return RootSpanContext(self.root)


class FakeHandler:
    def __init__(self, *, public_key=None, trace_context=None):
        self.public_key = public_key
        self.trace_context = trace_context


def test_disabled_returns_none_handler():
    assert build_client(settings()) is None


def test_missing_keys_returns_none_handler():
    assert build_client(settings(langfuse_enabled=True, langfuse_public_key="lf-public-x")) is None


def test_trace_config_shape_without_handler():
    config = trace_config(7, None)
    assert config["callbacks"] == []
    assert config["metadata"]["langfuse_session_id"] == "conv-7"
    assert config["metadata"]["langfuse_user_id"] == "conv-7"
    assert config["metadata"]["langfuse_trace_name"] == "chat-turn"
    assert "langfuse_tags" not in config["metadata"]


def test_trace_config_merges_tags():
    config = trace_config(7, object())
    config2 = trace_config(7, object(), tags=["intent:售后"])
    assert config2["metadata"]["langfuse_tags"] == ["intent:售后"]
    assert len(config["callbacks"]) == 1


def test_trace_name_follows_the_name_argument(monkeypatch):
    """评审第 6 条裁定：`langfuse_trace_name` 跟着 `turn_trace(name=…)` 走，不再硬编码。

    名字对不上的话，报表按 trace 名找根 observation 那一行就找不到（T16 的 `name=chat-turn`
    过滤同理）。默认值仍是 chat-turn ⇒ 现有调用与断言不用改。
    """
    monkeypatch.setattr("langfuse.langchain.CallbackHandler", FakeHandler)
    assert trace_config(7, None)["metadata"]["langfuse_trace_name"] == "chat-turn"
    assert trace_config(7, None, name="refund-turn")["metadata"]["langfuse_trace_name"] == "refund-turn"

    client = FakeClient()
    with turn_trace(client, 7, name="refund-turn") as turn:
        assert turn.config["metadata"]["langfuse_trace_name"] == "refund-turn"
        assert client.observation_kwargs[0]["name"] == "refund-turn"


def test_set_intent_on_trace_is_silent_without_span():
    # 单测常态：没有 OTEL 上下文 → False，不抛。
    assert set_intent_on_trace("售后", 0.9) is False


def test_flush_tracing_never_raises():
    flush_tracing()


def test_turn_trace_passthrough_when_client_is_none():
    # 观测关闭：直通 None，零副作用（不建 span、不注册 contextvar、不抛）。
    seen = []
    with turn_trace(None, 7) as turn:
        seen.append(turn)
        assert tracing._turn_root.get() is None
    assert seen == [None]


def test_turn_trace_builds_root_span_handler_and_config(monkeypatch):
    monkeypatch.setattr("langfuse.langchain.CallbackHandler", FakeHandler)
    root = FakeSpan()
    client = FakeClient(root=root)

    with turn_trace(client, 7) as turn:
        assert turn is not None
        assert turn.trace_id
        assert turn.config["metadata"]["langfuse_session_id"] == "conv-7"
        assert len(turn.config["callbacks"]) == 1
        assert isinstance(turn.config["callbacks"][0], FakeHandler)
        # handler 必须绑到本轮 trace_id，否则回调写进另一条 trace。
        assert turn.config["callbacks"][0].trace_context == {"trace_id": turn.trace_id}
        # 根 span 由 langfuse 客户端自己建：探针实验 1 证明只有它的属性会被事件管线认。
        assert client.observation_kwargs[0] == {
            "name": "chat-turn", "as_type": "span",
            "trace_context": {"trace_id": turn.trace_id},
        }
        # 图内节点顺着 contextvar 找根 span，不靠 ambient。
        assert tracing._turn_root.get() is root
    assert tracing._turn_root.get() is None
    # 评审第 2 条：with 块结束后根 span 必须被收掉（没结束 ⇒ 永不导出 ⇒ intent 载体静默消失）。
    assert root.closed is True


def test_turn_trace_never_raises_when_client_breaks(monkeypatch):
    monkeypatch.setattr("langfuse.langchain.CallbackHandler", FakeHandler)

    with turn_trace(FakeClient(error=RuntimeError("boom")), 7) as turn:
        assert turn is None
        assert tracing._turn_root.get() is None
    assert tracing._turn_root.get() is None


def test_turn_trace_yields_none_when_handler_cannot_be_built(monkeypatch):
    """CallbackHandler 建不出来（参数不对/没装 langfuse）→ 这一轮 None，不抛。"""

    class Exploding:
        def __init__(self, *, public_key=None, trace_context=None):
            raise TypeError("unexpected keyword")

    monkeypatch.setattr("langfuse.langchain.CallbackHandler", Exploding)
    root = FakeSpan()
    with turn_trace(FakeClient(root=root), 7) as turn:
        assert turn is None
    assert tracing._turn_root.get() is None
    # 评审第 2 条的异常分支：handler 建不出来时根 span 也已经进过栈，同样必须被收掉，
    # 不许泄漏一个永不结束的 span。
    assert root.closed is True


def test_set_intent_prefers_context_root_span_over_ambient():
    root, ambient = FakeSpan("root"), FakeSpan("ambient")
    token = tracing._turn_root.set(root)
    try:
        with otel_trace.use_span(ambient, end_on_exit=False):
            assert set_intent_on_trace("退款退货", 0.91) is True
    finally:
        tracing._turn_root.reset(token)

    assert root.attributes["langfuse.trace.tags"] == ["intent:退款退货"]
    assert root.attributes["langfuse.trace.metadata.intent"] == "退款退货"
    assert root.attributes["langfuse.trace.metadata.intent_confidence"] == 0.91
    assert ambient.attributes == {}


def test_set_intent_unwraps_langfuse_span_wrapper_before_writing():
    """评审第 1 条：真 SDK 的 observation 包装对象把 OTEL span 藏在 `_otel_span` 上
    （`langfuse/_client/span.py:316`，包装类自己没有 `set_attribute`/`is_recording`）⇒
    `_unwrap_span` 这条分支是线上唯一会走到的路径，之前一条测试都没覆盖过。
    """
    inner = FakeSpan()
    wrapper = type("W", (), {"_otel_span": inner})()   # 真 LangfuseSpan 的形状：只有 _otel_span
    token = tracing._turn_root.set(wrapper)
    try:
        assert set_intent_on_trace("售后", 0.8) is True
    finally:
        tracing._turn_root.reset(token)

    assert inner.attributes["langfuse.trace.tags"] == ["intent:售后"]
    assert inner.attributes["langfuse.trace.metadata.intent"] == "售后"
    assert inner.attributes["langfuse.trace.metadata.intent_confidence"] == 0.8


def test_set_intent_ignores_ambient_span_when_there_is_no_context_root():
    """评审第 4 条裁定：ambient 兜底已删——只有 ambient 时**不写、返回 False**。

    那一轮探针假阳性就是「写在 ASGI 的 POST /api/chat 上、返回 True、实际永不导出」；
    更要紧的是分类节点之外调用它可能标脏别人的 trace。这是有意的行为变化。
    """
    ambient = FakeSpan("ambient")
    with otel_trace.use_span(ambient, end_on_exit=False):
        assert set_intent_on_trace("查订单", 0.4) is False
    assert ambient.attributes == {}


# --------------------------------------------------------------------------------------
# T4 接线：`_drive` 是唯一开 trace 的地方（handler 要 trace_id，只能进 turn_trace 之后建），
# intent 在**整形之后**落本轮根 observation。假对象一律复用上面这套，不另造。
# --------------------------------------------------------------------------------------


class FakeGraph:
    """只实现 astream：记下收到的 config，按给定帧原样吐出（双模式流的形状）。"""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.configs = []

    async def astream(self, graph_input, config, **kwargs):
        self.configs.append(config)
        for frame in self.frames:
            yield frame


FRAMES = [("custom", ("token", {"content": "好"}))]


def workflow_with(client, frames=FRAMES):
    from app.workflow.graph import Workflow, WorkflowDeps

    workflow = object.__new__(Workflow)
    workflow.deps = WorkflowDeps(model=None, settings=None, store=None, langfuse=client)
    workflow.graph = FakeGraph(frames)
    return workflow


def drive(workflow, conversation_id=7, **kwargs):
    async def collect():
        return [item async for item in workflow._drive(
            conversation_id, {"question": "我要退款"}, workflow._config(conversation_id), **kwargs)]

    return asyncio.run(collect())


def test_drive_opens_root_span_and_merges_turn_config(monkeypatch):
    monkeypatch.setattr("langfuse.langchain.CallbackHandler", FakeHandler)
    root = FakeSpan()
    workflow = workflow_with(FakeClient(root=root))

    assert drive(workflow) == [("token", {"content": "好"})]

    config = workflow.graph.configs[0]
    assert config["configurable"] == {"thread_id": "7"}
    assert isinstance(config["callbacks"][0], FakeHandler)
    # handler 绑的必须是**本轮** trace id（= 本轮根 span 的 trace id），否则回调写进另一条 trace。
    turn_trace_id = f"{root.get_span_context().trace_id:032x}"
    assert config["callbacks"][0].trace_context == {"trace_id": turn_trace_id}
    assert config["metadata"]["langfuse_session_id"] == "conv-7"
    assert root.closed is True        # 这一轮的根 span 必须被收掉，否则永不导出


def test_drive_without_client_leaves_config_untouched():
    workflow = workflow_with(None)

    # 观测关闭时连 intent 都不碰 config：turn_intent 传了也只能「一个字段都不加」。
    assert drive(workflow, turn_intent=("售后", 0.83)) == [("token", {"content": "好"})]
    assert workflow.graph.configs[0] == {"configurable": {"thread_id": "7"}}


def test_resume_drive_writes_checkpointed_intent(monkeypatch):
    """resume 不重跑 intent 节点：调用方把 checkpoint 里的意图递进来，必须落在**本轮**根行上。

    上一轮评审点名的缺口：这一轮的生成往往是整段对话最大的一笔 token，根行没有 intent
    就归不了账（T16 按 traceId join）。这里按 resume 的真实形参形状调 `_drive`
    （`Command(resume=...)` + 仅关键字 `turn_intent`），顺便钉住前三个位置参数的顺序。
    """
    monkeypatch.setattr("langfuse.langchain.CallbackHandler", FakeHandler)
    root = FakeSpan()
    workflow = workflow_with(FakeClient(root=root))

    async def collect():
        return [item async for item in workflow._drive(
            7, Command(resume="确认"), workflow._config(7), turn_intent=("售后", 0.83))]

    assert asyncio.run(collect()) == [("token", {"content": "好"})]
    assert root.attributes["langfuse.trace.metadata.intent"] == "售后"
    assert root.attributes["langfuse.trace.metadata.intent_confidence"] == 0.83
    assert root.attributes["langfuse.trace.tags"] == ["intent:售后"]
    assert root.closed is True        # 写在本轮根 span 上，且这一轮结束后它被收掉


def _resume_with(snapshot_values):
    """跑一次 `resume`，返回它喂给 `_drive` 的 (位置参数, 关键字参数)，不真跑图。"""
    workflow = workflow_with(FakeClient())
    seen = {}

    async def get_state(conversation_id):
        return SimpleNamespace(next=("confirm_ticket",), tasks=[], values=dict(snapshot_values))

    async def fake_drive(conversation_id, graph_input, config, **kwargs):
        seen["args"] = (conversation_id, graph_input, config)
        seen["kwargs"] = kwargs
        yield "done", {"ok": True}

    workflow.get_state = get_state
    workflow._drive = fake_drive

    async def collect():
        return [item async for item in workflow.resume(7, "确认")]

    assert asyncio.run(collect()) == [("done", {"ok": True})]
    return seen["args"], seen["kwargs"]


def test_resume_forwards_checkpointed_intent():
    """checkpoint 里有 intent ⇒ 原样转发给 `_drive`，**且**前三个位置参数不许错位/丢会话 id。"""
    args, kwargs = _resume_with({"intent": "售后", "intent_confidence": 0.83})

    assert kwargs["turn_intent"] == ("售后", 0.83)
    assert args == (7, Command(resume="确认"), {"configurable": {"thread_id": "7"}})

    # 旧 checkpoint 没有 intent ⇒ 传 None（本轮根行留空 = T16 的「未归因」），不许编默认值。
    _, kwargs = _resume_with({})
    assert kwargs["turn_intent"] is None


def intent_node(calls, monkeypatch, *, intent="售后", confidence=0.83, threshold=0.6):
    from app.workflow import input_nodes

    monkeypatch.setattr(input_nodes, "set_intent_on_trace",
                        lambda i, c: calls.append((i, c)) or True)

    class Classifier:
        async def classify(self, question, previous_question=None):
            from app.schemas import IntentResult

            return IntentResult(intent=intent, confidence=confidence)

    deps = type("D", (), {
        "classifier": Classifier(),
        "settings": type("S", (), {"workflow_intent_min_confidence": threshold})(),
    })()
    return input_nodes.make_intent_node(deps)


def test_intent_node_writes_effective_intent(monkeypatch):
    calls = []
    result = asyncio.run(intent_node(calls, monkeypatch)(
        {"question": "我要退款", "conversation_id": 1, "history": []}))
    assert result["intent"] == "售后"
    assert calls == [("售后", 0.83)]


def test_intent_node_writes_clamped_intent(monkeypatch):
    """低置信度被整成「其他」之后，trace 上必须是**整形后**的意图，不然报表与路由对不上。"""
    calls = []
    result = asyncio.run(intent_node(calls, monkeypatch, confidence=0.2)(
        {"question": "我要退款", "conversation_id": 1, "history": []}))
    assert result["intent"] == "其他"
    assert calls == [("其他", 0.2)]


def test_chitchat_path_still_writes_trace(monkeypatch):
    calls = []
    result = asyncio.run(intent_node(calls, monkeypatch)(
        {"question": "你好", "conversation_id": 1, "history": []}))
    assert result["intent"] == "闲聊"
    assert calls == [("闲聊", 1.0)]
