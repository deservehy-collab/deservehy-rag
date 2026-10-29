from app.knowledge.rewrite import QueryRewriter


class FakeModel:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return type("Reply", (), {"content": self.content})()


def test_rewrite_parses_json_and_caches():
    model = FakeModel('```json\n{"canonical_query": "普通地区邮费标准"}\n```')
    rewriter = QueryRewriter(model)
    assert rewriter.rewrite("邮费咋收") == "普通地区邮费标准"
    assert rewriter.rewrite("邮费咋收") == "普通地区邮费标准"
    assert model.calls == 1


def test_rewrite_falls_back_to_original_on_bad_output():
    rewriter = QueryRewriter(FakeModel("不是 JSON"))
    assert rewriter.rewrite("退钱要几天") == "退钱要几天"


def test_rewrite_can_be_disabled():
    model = FakeModel('{"canonical_query": "x"}')
    assert QueryRewriter(model, enabled=False).rewrite("原话") == "原话"
    assert model.calls == 0
