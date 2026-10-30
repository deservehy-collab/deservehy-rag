import asyncio
from types import SimpleNamespace

from app.knowledge.retrieval import Evidence, RetrievalResult
from app.storage import ConversationStore
from app.tools import list_orders
from app.workflow.policy_nodes import (
    extract_order_id,
    make_order_node,
    make_policy_retrieve_node,
    policy_selector,
)
from tests.support import _settings, sqlite_factory


class FakeExpander:
    def __init__(self, queries):
        self.queries = queries
        self.calls = []

    async def expand(self, question):
        self.calls.append(question)
        return list(self.queries)


class FakeKnowledge:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def retrieve_expanded(self, queries):
        self.seen.append(list(queries))
        return self.result


class SlowKnowledge:
    def retrieve_expanded(self, queries):
        import time
        time.sleep(0.3)
        return None


def settings(**overrides):
    return _settings({"tool_timeout_seconds": 1, "knowledge_tool_timeout_seconds": 1, **overrides})


def context(question, resolved=None, intent="退款退货"):
    return {"conversation_id": 1, "question": question, "resolved_question": resolved or question,
            "intent": intent, "pending": None}


def test_extract_order_id_prefers_prefixed_number():
    assert extract_order_id("订单 1001 能退吗") == "1001"
    assert extract_order_id("单号：1002 的货坏了") == "1002"
    assert extract_order_id("1001 能退吗") == "1001"
    assert extract_order_id("1001能退吗") == "1001"
    assert extract_order_id("2026 年 10 月买的能退吗") is None
    assert extract_order_id("这个能退吗") is None
    assert extract_order_id("13800138000 是我的电话") is None


def test_extract_order_id_ignores_amounts_models_dates_and_units():
    # 「缺订单号不让模型猜」：金额、型号、数量、日期都不能被当成订单号。
    assert extract_order_id("P1001 的保温杯能退吗") is None
    assert extract_order_id("G500 杯盖能装在 B750 上吗") is None
    assert extract_order_id("1999 元买的能退吗") is None
    assert extract_order_id("1000 个能退吗") is None
    assert extract_order_id("订单 2026 年 10 月买的能退吗") is None
    assert extract_order_id("SF1234567890 这个快递能退吗") is None
    assert extract_order_id("2026-10-30 下单的能退吗") is None


def test_list_orders_returns_demo_orders():
    orders = list_orders()
    assert {order["order_id"] for order in orders} == {"1001", "1002"}


def test_policy_order_found_path_sets_order():
    with sqlite_factory() as factory:
        deps = SimpleNamespace(store=ConversationStore(factory), knowledge=None, settings=settings())
        result = asyncio.run(make_order_node(deps)(context("订单 1001 能退吗")))
        assert result["order"]["order_id"] == "1001"
        assert policy_selector({**context("订单 1001 能退吗"), **result}) == "retrieve"


def test_policy_order_missing_slot_asks_with_pending():
    with sqlite_factory() as factory:
        deps = SimpleNamespace(store=ConversationStore(factory), knowledge=None, settings=settings())
        result = asyncio.run(make_order_node(deps)(context("这个能退吗")))
        assert result["order"] is None
        assert result["pending"] == {"question": "这个能退吗", "intent": "退款退货"}
        assert result["actions"] == [] and result["offer"] is None
        assert "点选" in result["reply"]
        assert policy_selector({**context("这个能退吗"), **result}) == "log"


def test_policy_order_unknown_number_falls_back_to_picker():
    with sqlite_factory() as factory:
        deps = SimpleNamespace(store=ConversationStore(factory), knowledge=None, settings=settings())
        result = asyncio.run(make_order_node(deps)(context("订单 9999 能退吗")))
        assert result["order"] is None
        assert "9999" in result["reply"]
        assert result["pending"]["question"] == "订单 9999 能退吗"


def test_policy_retrieve_uses_expanded_queries_and_evidence():
    evidence = Evidence(id=1, question="退款政策", answer="支持七天无理由", category="售后政策",
                        section_path="售后 › 退款", content_type="policy", n=1)
    knowledge = FakeKnowledge(RetrievalResult(strategy="hybrid_rerank", canonical_query="这个能退吗",
                                              queries=["这个能退吗", "退款政策"],
                                              items=[evidence], evidence=[evidence]))
    deps = SimpleNamespace(knowledge=knowledge, settings=settings(),
                           expander=FakeExpander(["这个能退吗", "退款政策"]))
    result = asyncio.run(make_policy_retrieve_node(deps)(context("这个能退吗")))
    assert knowledge.seen == [["这个能退吗", "退款政策"]]
    assert result["queries"] == ["这个能退吗", "退款政策"]
    assert result["retrieval"]["low_confidence"] is False
    assert [item["n"] for item in result["evidence"]] == [1]


def test_policy_retrieve_timeout_becomes_low_confidence():
    deps = SimpleNamespace(knowledge=SlowKnowledge(),
                           settings=settings(knowledge_tool_timeout_seconds=0.02),
                           expander=FakeExpander(["这个能退吗"]))
    result = asyncio.run(make_policy_retrieve_node(deps)(context("这个能退吗")))
    assert result["retrieval"]["low_confidence"] is True
    assert result["retrieval"]["reason"] == "retrieval_timeout"
    assert result["evidence"] == []
