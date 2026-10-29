from app.knowledge.selfcheck import EvidenceSelfCheck


class FakeModel:
    def __init__(self, content):
        self.content = content

    def invoke(self, messages):
        return type("Reply", (), {"content": self.content})()


ITEMS = [{"n": 1, "question": "退款多久到账", "answer": "1-2 个工作日发起退款", "section_path": "退款到账时间"}]


def test_selfcheck_marks_insufficient_evidence():
    check = EvidenceSelfCheck(FakeModel('{"useful": false, "reason": "证据只讲发起时间，没有到账时间"}'))
    assert check.check("退款几天到账", "退款到账时间", ITEMS) == (False, "证据只讲发起时间，没有到账时间")


def test_selfcheck_passes_on_bad_json():
    assert EvidenceSelfCheck(FakeModel("not json")).check("q", "q", ITEMS) == (True, "")


def test_selfcheck_disabled_never_calls_model():
    class Boom:
        def invoke(self, messages):
            raise AssertionError("should not call")

    assert EvidenceSelfCheck(Boom(), enabled=False).check("q", "q", ITEMS) == (True, "")
