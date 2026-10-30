import asyncio

from app.knowledge.expansion import QueryExpander
from app.schemas import QueryExpansion


class Structured:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value


class FakeModel:
    def __init__(self, value=None, error=None):
        self.structured = Structured(value, error)

    def with_structured_output(self, schema, **kwargs):
        return self.structured


def test_expansion_keeps_order_and_dedupes():
    model = FakeModel(QueryExpansion(queries=["退款政策", "退货运费谁承担", "退款政策"]))
    expander = QueryExpander(model, max_queries=3, timeout=5)
    assert asyncio.run(expander.expand("这个能退吗")) == ["退款政策", "退货运费谁承担", "这个能退吗"]


def test_expansion_falls_back_to_original_on_error():
    expander = QueryExpander(FakeModel(error=ValueError("bad")), max_queries=3, timeout=5)
    assert asyncio.run(expander.expand("这个能退吗")) == ["这个能退吗"]


def test_expansion_disabled_skips_model():
    model = FakeModel(QueryExpansion(queries=["a", "b"]))
    expander = QueryExpander(model, enabled=False, timeout=5)
    assert asyncio.run(expander.expand("这个能退吗")) == ["这个能退吗"]
    assert model.structured.calls == 0


def test_expansion_caches_repeated_questions():
    model = FakeModel(QueryExpansion(queries=["退款政策", "退货运费"]))
    expander = QueryExpander(model, timeout=5)
    asyncio.run(expander.expand("这个能退吗"))
    asyncio.run(expander.expand("这个能退吗"))
    assert model.structured.calls == 1


def test_expansion_caps_at_max_queries():
    model = FakeModel(QueryExpansion(queries=["a", "b", "c"]))
    expander = QueryExpander(model, max_queries=2, timeout=5)
    assert asyncio.run(expander.expand("q")) == ["a", "b"]


def test_expansion_failure_is_not_cached():
    model = FakeModel(error=ValueError("bad"))
    expander = QueryExpander(model, timeout=5)
    assert asyncio.run(expander.expand("这个能退吗")) == ["这个能退吗"]
    assert asyncio.run(expander.expand("这个能退吗")) == ["这个能退吗"]
    assert model.structured.calls == 2
