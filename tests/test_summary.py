import asyncio
import logging

import httpx

from app.summary import Summarizer, SummaryService
from app.storage import ConversationStore
from tests.support import ChunkStream, _settings, api, chunk, events, sqlite_factory, streamed

SETTINGS = {"context_summary_enabled": True}
TEXT = "用户报订单 1001，保温杯漏水，要求退货退款，留下电话，问题尚未解决。"


class FakeSummarizer:
    def __init__(self, text=TEXT):
        self.text = text
        self.calls = []

    async def summarize(self, messages, previous):
        self.calls.append((messages, previous))
        return self.text


class Broken:
    async def summarize(self, messages, previous):
        raise TimeoutError("upstream")


def seed(store):
    conversation_id, first_id = store.start_turn(user_id="u", message="订单 1001 要退款",
                                                with_message_id=True)
    store.add_message(conversation_id, "assistant", content="好的，我记下了。")
    store.start_turn(user_id="u", message="退货退款", conversation_id=conversation_id)
    last_id = store.add_message(conversation_id, "assistant", content="已为你登记。")
    return conversation_id, first_id, last_id


def run(service, conversation_id, *, from_msg_id, upto_msg_id):
    async def main():
        scheduled = service.schedule(conversation_id, from_msg_id=from_msg_id, upto_msg_id=upto_msg_id)
        await service.wait()
        return scheduled
    return asyncio.run(main())


def test_summary_appends_segment_and_advances_anchor():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, first_id, last_id = seed(store)
        summarizer = FakeSummarizer()
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=summarizer)
        assert run(service, conversation_id, from_msg_id=0, upto_msg_id=last_id) is True
        assert store.anchors(conversation_id)[0] == last_id
        rows = store.summaries(conversation_id)
        assert [row["seq"] for row in rows] == [1]
        assert rows[0]["content"] == TEXT
        assert (rows[0]["from_msg_id"], rows[0]["upto_msg_id"]) == (0, last_id)
        # 摘要只吃 (from, upto] 区间的最终消息，旧摘要作为背景传给摘要器。
        assert [[row["content"] for row in summarizer.calls[0][0]]] == [
            ["订单 1001 要退款", "好的，我记下了。", "退货退款", "已为你登记。"]]
        assert summarizer.calls[0][1] == []
        assert first_id < last_id


def test_segments_accumulate_without_rewriting_previous_ones():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _, last_id = seed(store)
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=FakeSummarizer())
        run(service, conversation_id, from_msg_id=0, upto_msg_id=last_id)
        store.start_turn(user_id="u", message="那什么时候到账", conversation_id=conversation_id)
        newest = store.add_message(conversation_id, "assistant", content="审核通过后 1-3 个工作日。")
        summarizer = FakeSummarizer("用户追问退款到账时间，已告知时效。")
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=summarizer)
        run(service, conversation_id, from_msg_id=last_id, upto_msg_id=newest)
        rows = store.summaries(conversation_id)
        assert [row["seq"] for row in rows] == [1, 2]
        assert rows[0]["content"] == TEXT
        assert rows[1]["content"] == "用户追问退款到账时间，已告知时效。"
        assert store.anchors(conversation_id)[0] == newest
        # 第二段只总结新增区间，旧摘要只作背景输入。
        assert [row["content"] for row in summarizer.calls[0][0]] == ["那什么时候到账", "审核通过后 1-3 个工作日。"]
        assert summarizer.calls[0][1] == [TEXT]


def test_failed_summary_does_not_advance_anchor():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _, last_id = seed(store)
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=Broken())
        assert run(service, conversation_id, from_msg_id=0, upto_msg_id=last_id) is True
        assert store.anchors(conversation_id) == (0, 0)
        assert store.summaries(conversation_id) == []
        assert service.pending == 0


def test_summarizer_returning_none_keeps_anchor():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _, last_id = seed(store)
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=FakeSummarizer(None))
        run(service, conversation_id, from_msg_id=0, upto_msg_id=last_id)
        assert store.anchors(conversation_id) == (0, 0)
        assert store.summaries(conversation_id) == []


def test_stale_summary_is_discarded():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _, last_id = seed(store)
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=FakeSummarizer())
        run(service, conversation_id, from_msg_id=0, upto_msg_id=last_id)
        # 落后于当前锚点的批次直接丢弃，不覆盖已有摘要。
        stale = SummaryService(None, store, _settings(SETTINGS), summarizer=FakeSummarizer("过期摘要"))
        run(stale, conversation_id, from_msg_id=0, upto_msg_id=last_id)
        rows = store.summaries(conversation_id)
        assert [row["seq"] for row in rows] == [1]
        assert rows[0]["content"] == TEXT
        assert store.anchors(conversation_id)[0] == last_id


def test_expired_plan_is_clamped_to_the_current_anchor():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, first_id = store.start_turn(user_id="u", message="订单 1001 要退款",
                                                      with_message_id=True)
        second = store.add_message(conversation_id, "assistant", content="好的，我记下了。")
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=FakeSummarizer())
        run(service, conversation_id, from_msg_id=0, upto_msg_id=second)
        store.start_turn(user_id="u", message="那什么时候到账", conversation_id=conversation_id)
        newest = store.add_message(conversation_id, "assistant", content="审核通过后 1-3 个工作日。")
        summarizer = FakeSummarizer("用户追问到账时间，已告知时效。")
        late = SummaryService(None, store, _settings(SETTINGS), summarizer=summarizer)
        # 本轮 plan 的起点还停在 0：按当前锚点夹一次，只压锚点之后新增的两条。
        run(late, conversation_id, from_msg_id=0, upto_msg_id=newest)
        rows = store.summaries(conversation_id)
        assert [row["from_msg_id"] for row in rows] == [0, second]
        assert [row["content"] for row in summarizer.calls[0][0]] == ["那什么时候到账", "审核通过后 1-3 个工作日。"]
        assert first_id < second
        assert store.anchors(conversation_id)[0] == newest


def test_fully_covered_range_skips_without_calling_the_model(caplog):
    caplog.set_level(logging.INFO)
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _, last_id = seed(store)
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=FakeSummarizer())
        run(service, conversation_id, from_msg_id=0, upto_msg_id=last_id)
        summarizer = FakeSummarizer("不该被调用")
        stale = SummaryService(None, store, _settings(SETTINGS), summarizer=summarizer)
        assert run(stale, conversation_id, from_msg_id=0, upto_msg_id=last_id) is True
        # 锚点已追平 upto：区间为空，直接跳过，不再回头重压整通历史，也不白烧一次模型调用。
        assert summarizer.calls == []
        assert [row["seq"] for row in store.summaries(conversation_id)] == [1]
        assert any("summary skip" in record.getMessage() and "reason=empty_range" in record.getMessage()
                   for record in caplog.records)


def test_backlog_over_one_batch_is_compressed_from_the_oldest_end():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _ = store.start_turn(user_id="u", message="订单 1001 的保温杯漏水，我要退货退款",
                                               with_message_id=True)
        for index in range(30):
            store.add_message(conversation_id, "assistant", content=f"第 {index + 1} 条答复")
        newest = store.add_message(conversation_id, "user", content="最后这一问")
        summarizer = FakeSummarizer()
        service = SummaryService(None, store, _settings(SETTINGS), summarizer=summarizer)
        run(service, conversation_id, from_msg_id=0, upto_msg_id=newest)
        # 一批只吃 20 条：必须从最老的行开始往后分批，越过没看过的行就是永久丢失。
        assert [len(rows) for rows, _ in summarizer.calls] == [20, 12]
        assert summarizer.calls[0][0][0]["content"].startswith("订单 1001")
        rows = store.summaries(conversation_id)
        assert [row["seq"] for row in rows] == [1, 2]
        assert (rows[0]["from_msg_id"], rows[0]["upto_msg_id"]) == (0, summarizer.calls[0][0][-1]["id"])
        assert rows[1]["from_msg_id"] == rows[0]["upto_msg_id"]
        assert rows[1]["upto_msg_id"] == newest
        assert store.anchors(conversation_id)[0] == newest
        # 前一段作背景传给后一段，但每段只总结自己那段新增对话。
        assert summarizer.calls[1][1] == [TEXT]


def test_disabled_or_busy_conversation_is_not_scheduled():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id, _, last_id = seed(store)
        summarizer = FakeSummarizer()
        disabled = SummaryService(None, store, _settings({"context_summary_enabled": False}),
                                  summarizer=summarizer)
        assert run(disabled, conversation_id, from_msg_id=0, upto_msg_id=last_id) is False
        assert summarizer.calls == []

        service = SummaryService(None, store, _settings(SETTINGS), summarizer=summarizer)

        async def main():
            first = service.schedule(conversation_id, from_msg_id=0, upto_msg_id=last_id)
            second = service.schedule(conversation_id, from_msg_id=0, upto_msg_id=last_id)
            pending = service.pending
            await service.wait()
            return first, second, pending
        first, second, pending = asyncio.run(main())
        assert (first, second, pending) == (True, False, 1)
        assert len(summarizer.calls) == 1


def test_service_triggers_summary_after_reply_when_layer2_over_budget(caplog):
    caplog.set_level(logging.INFO)
    settings = {"context_window_tokens": 4000, "max_output_tokens": 200, "max_user_input_tokens": 200,
                "knowledge_top_k": 1, "workflow_agent_max_steps": 1, "tool_result_max_tokens": 100,
                "history_keep_rounds": 1, "history_round_tokens": 200}
    with api(lambda request: streamed(("好的。" + "补" * 60,)), **settings) as (client, _):
        conversation_id = None
        for question in ["第一问", "第二问", "第三问", "第四问"]:
            payload = {"message": question}
            if conversation_id:
                payload["conversation_id"] = conversation_id
            conversation_id = events(client.post("/api/chat", json=payload))[0][1]["conversation_id"]

    logged = [record.getMessage() for record in caplog.records]
    assert any(item.startswith("summary trigger") for item in logged)
    # 测试夹具默认关掉后台摘要：只应看到 skip，不应对上游发起摘要调用。
    assert any("summary skip" in item and "reason=disabled" in item for item in logged)
    assert not any(item.startswith("summary start") for item in logged)


def test_error_round_does_not_schedule_a_summary(caplog):
    """出错那一轮不压摘要：历史边界不可信，宁可下一轮再补（`app/service.py` 里那句 `not failed`）。

    差分写法：同一套设置连跑两遍，第二遍只把最后一轮换成了半途炸掉的流。
    摘要关闭时锚点不前进，层 2 只会越堆越厚，所以「第 4 轮超预算」在两遍里都成立——
    少掉的那一条 trigger 只能来自失败轮。变异（去掉 `not failed`）两遍计数相等，本条即红。
    """
    settings = {"context_window_tokens": 4000, "max_output_tokens": 200, "max_user_input_tokens": 200,
                "knowledge_top_k": 1, "workflow_agent_max_steps": 1, "tool_result_max_tokens": 100,
                "history_keep_rounds": 1, "history_round_tokens": 200}
    caplog.set_level(logging.INFO)

    def run(fail_last: bool):
        caplog.clear()
        counter = {"n": 0}

        def handler(request):
            counter["n"] += 1
            if fail_last and counter["n"] == 4:
                stream = ChunkStream([chunk("好的。")], httpx.ReadError("boom"))
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
            return streamed(("好的。" + "补" * 60,))

        with api(handler, **settings) as (client, _):
            conversation_id = None
            frames = []
            for question in ["第一问", "第二问", "第三问", "第四问"]:
                payload = {"message": question}
                if conversation_id:
                    payload["conversation_id"] = conversation_id
                response = events(client.post("/api/chat", json=payload))
                conversation_id = response[0][1]["conversation_id"]
                frames = response
        triggers = sum(1 for record in caplog.records
                       if record.getMessage().startswith("summary trigger"))
        return triggers, [name for name, _ in frames]

    ok_triggers, ok_frames = run(fail_last=False)
    assert ok_triggers >= 1 and "error" not in ok_frames
    bad_triggers, bad_frames = run(fail_last=True)
    assert "error" in bad_frames
    assert bad_triggers == ok_triggers - 1


class Structured:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        if self.error:
            raise self.error
        return self.payload


class Segment:
    def __init__(self, summary):
        self.summary = summary


class StructuredModel:
    def __init__(self, payload=None, error=None):
        self.structured = Structured(payload, error)

    def with_structured_output(self, schema, method=None):
        self.schema = schema
        self.method = method
        return self.structured


def test_summarizer_renders_background_and_new_dialogue_with_clipping():
    model = StructuredModel(Segment(TEXT))
    summarizer = Summarizer(model, timeout=5)
    messages = [{"role": "user", "content": "长" * 900},
                {"role": "assistant", "content": "答" * 400}]
    result = asyncio.run(summarizer.summarize(messages, ["旧摘要一", "旧摘要二", "旧摘要三"]))
    assert result == TEXT
    assert model.method == "json_mode"
    system, human = model.structured.messages
    assert "只提炼事实与诉求" in system.content
    # 旧摘要只保留最近两段作背景，且明确只总结新增对话。
    assert "旧摘要一" not in human.content
    assert "旧摘要二" in human.content and "旧摘要三" in human.content
    assert "【新增对话】" in human.content
    assert "长" * 500 in human.content and "长" * 501 not in human.content
    assert "答" * 200 in human.content and "答" * 201 not in human.content


def test_summarizer_returns_none_on_upstream_failure():
    model = StructuredModel(error=TimeoutError("upstream"))
    summarizer = Summarizer(model, timeout=5)
    assert asyncio.run(summarizer.summarize([{"role": "user", "content": "订单 1001"}], [])) is None
