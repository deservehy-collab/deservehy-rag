import json

from sqlalchemy import select

from app.knowledge.retrieval import Evidence, RetrievalResult
from app.models import LowConfidenceQuestion, Message
from tests.support import api_with_db, events, streamed, streamed_tool_call


class FakeKnowledge:
    def __init__(self, low=False):
        self.low = low

    def retrieve(self, query, category=None):
        if self.low:
            return RetrievalResult("hybrid_rerank", query, low_confidence=True, reason="top1=0.1<0.30")
        item = Evidence(7, "G500 杯盖适配", "G500 适配 P1001 和 B500，不适用 B750。",
                        "型号与兼容性 › 保温杯系列", "型号与兼容性 › 保温杯系列 › 杯盖与配件适配",
                        "policy", {"rrf": 0.03, "rerank": 0.92}, 1)
        return RetrievalResult("hybrid_rerank", "G500 是否适配 B750", [item], [item])


class FakeSelfCheck:
    def __init__(self, useful=True, reason=""):
        self.useful, self.reason = useful, reason

    def check(self, question, canonical, items):
        return self.useful, self.reason


def test_faq_answer_emits_citations_and_drops_out_of_range_markers():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return streamed_tool_call("query_faq", {"keyword": "G500 能装在 B750 上吗"})
        return streamed(("G500 不适用 B750[1]。", "普通地区满 99 元包邮[9]。"))

    with api_with_db(handler) as (client, seen, factory):
        client.app.state.service.knowledge = FakeKnowledge()
        client.app.state.service.selfcheck = FakeSelfCheck(True)
        response = client.post("/api/chat", json={"message": "G500 能装在 B750 上吗", "client_id": "anon-ch04-1"})
        frames = events(response)
        names = [name for name, _ in frames]
        assert names[:2] == ["meta", "status"]
        citations = [payload for name, payload in frames if name == "citations"]
        assert citations and citations[0]["items"][0]["n"] == 1
        assert citations[0]["items"][0]["chunk_id"] == 7
        assert citations[0]["items"][0]["section_path"].endswith("杯盖与配件适配")
        tokens = "".join(payload["content"] for name, payload in frames if name == "token")
        assert "[1]" in tokens and "[9]" not in tokens
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
            assert rows[-1].content == "G500 不适用 B750[1]。普通地区满 99 元包邮。"


def test_self_check_rejection_writes_pool_and_skips_second_generation():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return streamed_tool_call("query_faq", {"keyword": "退款几天到账"})
        raise AssertionError("自评拒绝时不应发起第二轮生成")

    with api_with_db(handler) as (client, seen, factory):
        client.app.state.service.knowledge = FakeKnowledge()
        client.app.state.service.selfcheck = FakeSelfCheck(False, "证据没有到账时间")
        frames = events(client.post("/api/chat", json={"message": "退款几天到账", "client_id": "anon-ch04-2"}))
        tokens = "".join(payload["content"] for name, payload in frames if name == "token")
        assert tokens.startswith("这个问题我在店铺知识库里没有找到可靠依据")
        assert [name for name, _ in frames][-1] == "done"
        assert len(calls) == 1
        with factory() as session:
            row = session.scalar(select(LowConfidenceQuestion))
            assert row.source == "self_check" and row.reason == "证据没有到账时间"


def test_retrieval_gate_rejection_writes_pool():
    def handler(request):
        return streamed_tool_call("query_faq", {"keyword": "支持货到付款吗"})

    with api_with_db(handler) as (client, seen, factory):
        client.app.state.service.knowledge = FakeKnowledge(low=True)
        frames = events(client.post("/api/chat", json={"message": "你们支持货到付款吗", "client_id": "anon-ch04-3"}))
        tokens = "".join(payload["content"] for name, payload in frames if name == "token")
        assert tokens.startswith("这个问题我在店铺知识库里没有找到可靠依据")
        with factory() as session:
            row = session.scalar(select(LowConfidenceQuestion))
            assert row.source == "retrieval_low_conf" and "top1=0.1" in row.reason
