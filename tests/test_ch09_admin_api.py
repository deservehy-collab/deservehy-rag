import httpx
import pytest

from app.models import LowConfidenceQuestion, ReviewQueue
from tests.support import api_with_db, streamed


class FakeIngestor:
    def __init__(self):
        self.calls = []

    def ingest_pairs(self, pairs, *, source_ref=None):
        self.calls.append((pairs, source_ref))
        return 1

    def vectorize_pending(self):
        return 1


def noop(request):
    return streamed()


@pytest.fixture
def admin():
    with api_with_db(noop) as (client, _seen, factory):
        ingestor = FakeIngestor()
        client.app.state.ingestor = ingestor          # 绕开 Milvus（T12 Step 9 的惰性构造）
        yield client, factory, ingestor


def _seed(factory, **kwargs):
    with factory() as session:
        row = ReviewQueue(**{"normalized_question": "运费怎么算",
                             "ai_suggested_answer": "以商品页为准", **kwargs})
        session.add(row)
        session.commit()
        return row.id


def test_list_returns_briefs_and_total(admin):
    client, factory, _ = admin
    _seed(factory, occurrence_count=5)
    _seed(factory, normalized_question="退货流程", review_status="驳回")

    body = client.get("/api/admin/review-queue").json()

    assert body["total"] == 2 and body["limit"] == 20 and body["offset"] == 0
    # 排序口径是 occurrence_count DESC, id DESC：5 次那条排前面
    assert [item["occurrence_count"] for item in body["items"]] == [5, 1]
    assert {"id", "normalized_question", "occurrence_count", "review_status",
            "ai_suggested_answer", "approved_answer", "created_at", "updated_at"} <= set(body["items"][0])


def test_list_filters_by_status_and_paginates(admin):
    client, factory, _ = admin
    _seed(factory)
    _seed(factory, normalized_question="退货流程", review_status="驳回")

    assert client.get("/api/admin/review-queue", params={"status": "驳回"}).json()["total"] == 1
    paged = client.get("/api/admin/review-queue", params={"limit": 1, "offset": 1}).json()
    assert paged["total"] == 2 and len(paged["items"]) == 1
    assert client.get("/api/admin/review-queue", params={"status": "随便"}).status_code == 422


def test_detail_includes_merged_originals(admin):
    client, factory, _ = admin
    review_id = _seed(factory)
    with factory() as session:
        session.add(LowConfidenceQuestion(
            conversation_id=3, raw_question="邮费多少啊", source="user_feedback",
            reason="feedback:message_id=11", matched_review_id=review_id,
            retrieved_chunks=[{"question": "运费", "answer": "满 99 包邮", "score": 0.42}]))
        session.commit()

    body = client.get(f"/api/admin/review-queue/{review_id}").json()

    assert body["id"] == review_id
    assert body["sources"][0]["raw_question"] == "邮费多少啊"
    assert body["sources"][0]["retrieved_chunks"][0]["score"] == 0.42


def test_detail_missing_row_is_404(admin):
    client, _, _ = admin
    response = client.get("/api/admin/review-queue/999")
    assert response.status_code == 404 and response.json()["code"] == "review_not_found"


def test_approve_writes_kb(admin):
    client, factory, ingestor = admin
    review_id = _seed(factory)

    body = client.post(f"/api/admin/review-queue/{review_id}/approve",
                       json={"answer": "满 99 包邮"}).json()

    assert body == {"ok": True, "review_id": review_id, "status": "通过",
                    "ingested": 1, "vectorized": 1}
    assert ingestor.calls == [([{"question": "运费怎么算", "answer": "满 99 包邮"}],
                               f"review_queue:{review_id}")]
    assert client.get(f"/api/admin/review-queue/{review_id}").json()["review_status"] == "通过"


def test_approve_without_body_uses_suggested_answer(admin):
    client, factory, ingestor = admin
    review_id = _seed(factory)

    assert client.post(f"/api/admin/review-queue/{review_id}/approve").status_code == 200
    assert ingestor.calls[0][0] == [{"question": "运费怎么算", "answer": "以商品页为准"}]


def test_approve_twice_is_409_and_does_not_touch_kb(admin):
    client, factory, ingestor = admin
    review_id = _seed(factory)
    client.post(f"/api/admin/review-queue/{review_id}/approve")
    ingestor.calls.clear()

    response = client.post(f"/api/admin/review-queue/{review_id}/approve")

    assert response.status_code == 409 and response.json()["code"] == "already_approved"
    assert ingestor.calls == []


def test_approve_empty_answer_is_422(admin):
    client, factory, _ = admin
    review_id = _seed(factory, ai_suggested_answer=None)

    response = client.post(f"/api/admin/review-queue/{review_id}/approve", json={})

    assert response.status_code == 422 and response.json()["code"] == "empty_answer"


def test_reject_and_conflicts(admin):
    client, factory, _ = admin
    review_id = _seed(factory)

    assert client.post(f"/api/admin/review-queue/{review_id}/reject",
                       json={"reason": "重复"}).json() == {
        "ok": True, "review_id": review_id, "status": "驳回"}
    assert client.post("/api/admin/review-queue/999/reject").status_code == 409

    approved = _seed(factory, normalized_question="退货流程", review_status="通过")
    response = client.post(f"/api/admin/review-queue/{approved}/reject")
    assert response.status_code == 409 and response.json()["code"] == "review_conflict"
