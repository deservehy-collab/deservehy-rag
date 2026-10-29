import pytest

from app.knowledge.rerank import BgeReranker, RerankerUnavailable


class FakeCrossEncoder:
    def predict(self, pairs, batch_size=8, show_progress_bar=False):
        return [0.9 if "G500" in passage else 0.1 for _, passage in pairs]


def test_scores_passages_in_order(monkeypatch):
    reranker = BgeReranker("not-used", device="cpu")
    monkeypatch.setattr(reranker, "_load", lambda: FakeCrossEncoder())
    scores = reranker.score("G500 杯盖", ["杯盖 G500 适配", "普通地区邮费"])
    assert scores[0] == pytest.approx(0.9)
    assert scores[1] == pytest.approx(0.1)


def test_missing_local_model_is_explicit(tmp_path):
    reranker = BgeReranker(str(tmp_path / "missing"), device="cpu")
    with pytest.raises(RerankerUnavailable):
        reranker.score("x", ["y"])


def test_empty_passages_do_not_load_model():
    reranker = BgeReranker("missing", device="cpu")
    assert reranker.score("x", []) == []
