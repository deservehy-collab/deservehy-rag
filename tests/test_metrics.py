import pytest

from app.knowledge.metrics import recall_at, reciprocal_rank, summarize


def test_recall_and_mrr():
    ranked = ["A › 1", "B › 2", "C › 3"]
    assert recall_at(ranked, {"A › 1", "C › 3"}, 5) == 1.0
    assert recall_at(ranked, {"A › 1", "D › 4"}, 1) == 0.5
    assert reciprocal_rank(ranked, {"B › 2"}) == 0.5
    assert reciprocal_rank(ranked, {"Z"}, 10) == 0.0


def test_summarize_counts_refusals():
    rows = [
        {"answerable": True, "recall@5": 1.0, "recall@10": 1.0, "mrr@10": 1.0, "refused": False},
        {"answerable": False, "recall@5": 0.0, "recall@10": 0.0, "mrr@10": 0.0, "refused": True},
    ]
    summary = summarize(rows)
    assert summary["count"] == 2
    assert summary["recall@5"] == pytest.approx(1.0)
    assert summary["refusal_rate"] == pytest.approx(1.0)
