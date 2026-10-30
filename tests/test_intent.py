import asyncio
import typing

import pytest

from app.workflow.classifier import IntentClassifier
from app.workflow.routing import INTENTS, ROUTES, fast_chitchat, route_for
from app.schemas import IntentResult


def test_routes_cover_seven_intents():
    assert ROUTES == {
        "商品咨询": "knowledge", "退款退货": "knowledge",
        "物流": "agent", "订单": "agent", "售后": "agent",
        "投诉": "complaint", "闲聊": "chitchat",
    }
    assert route_for("不存在的意图") == "agent"


def test_intent_sets_do_not_drift_apart():
    assert set(INTENTS) == set(ROUTES) == set(
        typing.get_args(IntentResult.model_fields["intent"].annotation))


@pytest.mark.parametrize("text", ["你好", "谢谢！", "再见", "在吗？", "你是谁", "早上好呀", "哈喽~", "谢谢你呀"])
def test_fast_chitchat_matches_whole_greetings(text):
    assert fast_chitchat(text) is True


@pytest.mark.parametrize("text", ["你好，我要退货", "订单 1001 到哪了", "谢谢你的帮助，不过我要投诉", "我要投诉", ""])
def test_fast_chitchat_rejects_business_text(text):
    assert fast_chitchat(text) is False


class Structured:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    async def ainvoke(self, messages):
        if self.error:
            raise self.error
        return self.value


class FakeModel:
    def __init__(self, value=None, error=None):
        self.structured = Structured(value, error)
        self.seen = []

    def with_structured_output(self, schema, **kwargs):
        self.seen.append((schema, kwargs))
        return self.structured


def test_classifier_returns_intent():
    model = FakeModel(IntentResult(intent="物流"))
    classifier = IntentClassifier(model, timeout=5)
    assert asyncio.run(classifier.classify("包裹到哪了", "我买了保温杯")) == "物流"
    assert model.seen[0][1]["method"] == "json_mode"


def test_classifier_falls_back_on_error():
    classifier = IntentClassifier(FakeModel(error=ValueError("bad json")), timeout=5)
    assert asyncio.run(classifier.classify("随便问问")) == "售后"
