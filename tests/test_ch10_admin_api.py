import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import LowConfidenceQuestion
from app.topics.taxonomy import LABELS
from app.topics.worker import store_classifications
from tests.support import api_with_db, streamed


def noop(request):
    return streamed()


def seed(factory, text):
    with factory() as session:
        row = LowConfidenceQuestion(raw_question=text, source="self_check")
        session.add(row)
        session.commit()
        return row.id


def test_distribution_counts_percentages_and_order():
    with api_with_db(noop) as (client, _, factory):
        first, second, third = seed(factory, "怎么查物流"), seed(factory, "运费怎么算"), seed(factory, "在吗")
        store_classifications(factory, {first: ["物流", "运费"], second: ["其他"]})
        body = client.get("/api/admin/topics/distribution").json()
        assert body["total_classified"] == 2 and body["pending"] == 1
        assert body["multi_label"] == 1
        assert [row["label"] for row in body["classes"]] == list(LABELS)
        by_label = {row["label"]: row for row in body["classes"]}
        assert by_label["物流"]["count"] == 1 and by_label["物流"]["pct"] == 0.5
        assert by_label["其他"]["count"] == 1
        assert body["last_classified_at"] is not None


def test_distribution_empty_database_is_all_zero():
    with api_with_db(noop) as (client, _, _factory):
        body = client.get("/api/admin/topics/distribution").json()
        assert body["total_classified"] == 0 and body["pending"] == 0
        assert body["multi_label"] == 0 and body["last_classified_at"] is None
        assert len(body["classes"]) == 17
        assert all(row["count"] == 0 and row["pct"] == 0.0 for row in body["classes"])


def test_distribution_db_failure_is_503(monkeypatch):
    import app.topics.stats as topic_stats

    def boom(factory):
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(topic_stats, "distribution", boom)
    with api_with_db(noop) as (client, _, _factory):
        response = client.get("/api/admin/topics/distribution")
        assert response.status_code == 503
        assert response.json()["code"] == "database_unavailable"
