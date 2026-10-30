import asyncio
import typing
from types import SimpleNamespace

import pytest

from app.schemas import IntentResult
from app.workflow.classifier import IntentClassifier
from app.workflow.input_nodes import make_intent_node
from app.workflow.routing import INTENTS, ROUTES, fast_chitchat, route_for
from tests.support import _settings


def test_routes_cover_eight_intents():
    assert ROUTES == {
        "商品咨询": "knowledge", "退款退货": "policy",
        "物流": "agent", "订单": "agent", "售后": "policy", "其他": "agent",
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


def test_classifier_returns_intent_with_confidence():
    model = FakeModel(IntentResult(intent="物流", confidence=0.93))
    classifier = IntentClassifier(model, timeout=5)
    result = asyncio.run(classifier.classify("包裹到哪了", "我买了保温杯"))
    assert result.intent == "物流" and result.confidence == 0.93
    assert model.seen[0][1]["method"] == "json_mode"


def test_classifier_falls_back_to_other_on_error():
    classifier = IntentClassifier(FakeModel(error=ValueError("bad json")), timeout=5)
    result = asyncio.run(classifier.classify("随便问问"))
    assert result.intent == "其他" and result.confidence == 0.0


class StubClassifier:
    def __init__(self, intent, confidence):
        self.result = IntentResult(intent=intent, confidence=confidence)

    async def classify(self, question, previous_question=None):
        return self.result


def intent_state(question, resolved=None):
    return {"question": question, "resolved_question": resolved or question}


def test_low_confidence_falls_back_to_other():
    deps = SimpleNamespace(settings=_settings({}), classifier=StubClassifier("退款退货", 0.4))
    result = asyncio.run(make_intent_node(deps)(intent_state("这个能退吗")))
    assert result["intent"] == "其他" and result["intent_confidence"] == 0.4


def test_fast_chitchat_skips_classifier():
    deps = SimpleNamespace(settings=_settings({}), classifier=StubClassifier("退款退货", 0.9))
    result = asyncio.run(make_intent_node(deps)(intent_state("你好")))
    assert result["intent"] == "闲聊" and result["intent_confidence"] == 1.0


def test_intent_node_prefers_resolved_question():
    seen = {}

    class Capture:
        async def classify(self, question, previous_question=None):
            seen["question"] = question
            return IntentResult(intent="退款退货", confidence=0.9)

    deps = SimpleNamespace(settings=_settings({}), classifier=Capture())
    result = asyncio.run(make_intent_node(deps)(intent_state("它能退吗", "订单 1001 的保温杯能退吗")))
    assert seen["question"] == "订单 1001 的保温杯能退吗"
    assert result["intent"] == "退款退货"
