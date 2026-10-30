import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.schemas import ResolvedQuestion
from app.workflow.coref import QuestionResolver, make_coref_node, merge_order_selection


class Structured:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error
        self.prompts = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[-1].content)
        if self.error:
            raise self.error
        return self.value


class FakeModel:
    def __init__(self, value=None, error=None):
        self.structured = Structured(value, error)

    def with_structured_output(self, schema, **kwargs):
        return self.structured


def settings():
    return SimpleNamespace(workflow_understanding_timeout_seconds=5)


def test_merge_order_selection_joins_pending_question():
    pending = {"question": "这个能退吗", "intent": "退款退货"}
    assert merge_order_selection(pending, "订单 1001") == "订单 1001 的这笔订单，这个能退吗"
    assert merge_order_selection(pending, "订单 1001。") == "订单 1001 的这笔订单，这个能退吗"


def test_merge_order_selection_ignores_normal_questions():
    pending = {"question": "这个能退吗"}
    assert merge_order_selection(pending, "订单 1001 能退吗") is None
    assert merge_order_selection(None, "订单 1001") is None


def test_resolver_returns_model_question():
    model = FakeModel(ResolvedQuestion(question="订单 1001 的保温杯能退吗"))
    resolver = QuestionResolver(model, timeout=5)
    resolved = asyncio.run(resolver.resolve(
        "它能退吗", previous_question="保温杯到货了", history=[("user", "保温杯到货了")],
        pending_question=None))
    assert resolved == "订单 1001 的保温杯能退吗"
    assert '"previous_question": "保温杯到货了"' in model.structured.prompts[0]


def test_resolver_falls_back_to_raw_text_on_error():
    resolver = QuestionResolver(FakeModel(error=ValueError("bad")), timeout=5)
    assert asyncio.run(resolver.resolve("它能退吗")) == "它能退吗"


def state(question="它能退吗", resolved=None):
    return {
        "conversation_id": 1, "question": question, "resolved_question": resolved or question,
        "last_question": "保温杯到货了", "messages": [SystemMessage(content="系统"),
                                                   HumanMessage(content="保温杯到货了"),
                                                   AIMessage(content="到货后可以试用")],
        "pending": {"question": "这个能退吗", "intent": "退款退货"},
        "actions": ["human"], "offer": {"turn": 1},
    }


def test_coref_node_merges_order_selection_without_model():
    class Exploding:
        def with_structured_output(self, schema, **kwargs):
            raise AssertionError("确定性合并不应调用模型")

    deps = SimpleNamespace(resolver=Exploding(), settings=settings())
    result = asyncio.run(make_coref_node(deps)(state(question="订单 1001")))
    assert result["resolved_question"] == "订单 1001 的这笔订单，这个能退吗"
    assert result["pending"] is None
    assert result["actions"] == [] and result["offer"] is None
    assert result["previous_question"] == "保温杯到货了"
    assert result["last_question"] == "订单 1001"


def test_coref_node_keeps_complete_question():
    model = FakeModel(ResolvedQuestion(question="订单 1001 的保温杯能退吗"))
    deps = SimpleNamespace(resolver=QuestionResolver(model, timeout=5), settings=settings())
    result = asyncio.run(make_coref_node(deps)(state(question="订单 1001 的保温杯能退吗")))
    assert result["resolved_question"] == "订单 1001 的保温杯能退吗"


def test_resolver_puts_summary_before_recent_window():
    model = FakeModel(ResolvedQuestion(question="订单 1001 的退货后来怎么说"))
    resolver = QuestionResolver(model, timeout=5)
    asyncio.run(resolver.resolve("最开始那个订单后来怎么说", history=[("user", "人工几点在线")],
                                 summary="用户反馈订单 1001 保温杯漏水，要求退货退款。"))
    prompt = model.structured.prompts[0]
    assert "【会话摘要】用户反馈订单 1001 保温杯漏水" in prompt
    assert prompt.index("【会话摘要】") < prompt.index("人工几点在线")


def test_resolver_without_summary_keeps_plain_history():
    model = FakeModel(ResolvedQuestion(question="它能退吗"))
    asyncio.run(QuestionResolver(model, timeout=5).resolve("它能退吗", history=[("user", "到货了")]))
    assert "【会话摘要】" not in model.structured.prompts[0]


def test_coref_node_passes_plan_summary_to_resolver():
    model = FakeModel(ResolvedQuestion(question="订单 1001 的退货后来怎么说"))
    deps = SimpleNamespace(resolver=QuestionResolver(model, timeout=5), settings=settings())
    plan_state = state(question="最开始那个订单后来怎么说")
    plan_state["context_plan"] = {"summary": "用户反馈订单 1001 保温杯漏水，要求退货退款。"}
    asyncio.run(make_coref_node(deps)(plan_state))
    assert "【会话摘要】用户反馈订单 1001" in model.structured.prompts[0]
