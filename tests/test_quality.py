from app.knowledge.quality import REFUSAL_TEXT, CitationGuard, number_evidence, order_evidence, strip_invalid_citations


def test_most_relevant_sits_at_head_and_tail():
    assert order_evidence(list("abcdefghij")) == list("acegijhfdb")


def test_numbers_follow_the_reordered_positions():
    numbered = number_evidence([{"id": i} for i in range(1, 6)])
    assert [item["n"] for item in numbered] == [1, 2, 3, 4, 5]
    assert [item["id"] for item in numbered] == [1, 3, 5, 4, 2]


def test_citation_guard_drops_unknown_numbers_and_keeps_valid():
    guard = CitationGuard({1, 2})
    text = guard.feed("退货运费由店铺承担[1]，") + guard.feed("七天无理由[9]。") + guard.flush()
    assert text == "退货运费由店铺承担[1]，七天无理由。"
    assert guard.dropped == 1


def test_citation_guard_handles_split_chunks():
    guard = CitationGuard({3})
    assert guard.feed("依据[") == "依据"
    assert guard.feed("3]") == "[3]"
    assert guard.feed("[1") == ""
    assert guard.flush() == ""


def test_strip_invalid_citations_for_stored_text():
    assert strip_invalid_citations("A[1] B[7]", {1}) == "A[1] B"


def test_refusal_text_is_fixed():
    assert REFUSAL_TEXT.startswith("这个问题我在店铺知识库里没有找到可靠依据")
