import threading

import pytest

from app.flywheel.review import approve, list_queue, queue_detail, reject
from app.models import LowConfidenceQuestion, ReviewQueue
from tests.support import sqlite_factory


@pytest.fixture
def factory():
    with sqlite_factory() as session_factory:
        yield session_factory


class FakeIngestor:
    def __init__(self, ingested=1, vectorized=1):
        self.ingested = ingested
        self.vectorized = vectorized
        self.calls: list[tuple] = []

    def ingest_pairs(self, pairs, *, source_ref=None):
        self.calls.append((pairs, source_ref))
        return self.ingested

    def vectorize_pending(self):
        return self.vectorized


def _row(factory, **kwargs):
    with factory() as session:
        row = ReviewQueue(**{"normalized_question": "运费怎么算", **kwargs})
        session.add(row)
        session.commit()
        return row.id


def test_approve_writes_kb_and_marks_passed(factory):
    review_id = _row(factory, ai_suggested_answer="以商品页为准")
    ingestor = FakeIngestor()

    result = approve(factory, ingestor, review_id)

    assert (result.ok, result.status, result.ingested, result.vectorized) == (True, "通过", 1, 1)
    assert ingestor.calls == [([{"question": "运费怎么算", "answer": "以商品页为准"}],
                               f"review_queue:{review_id}")]


def test_approve_prefers_explicit_answer(factory):
    review_id = _row(factory, ai_suggested_answer="以商品页为准")

    result = approve(factory, FakeIngestor(), review_id, answer="满 99 包邮")

    assert result.ok and result.status == "通过"
    with factory() as session:
        assert session.get(ReviewQueue, review_id).approved_answer == "满 99 包邮"


def test_approve_twice_does_not_touch_kb_again(factory):
    review_id = _row(factory, ai_suggested_answer="以商品页为准")
    approve(factory, FakeIngestor(), review_id)
    second_ingestor = FakeIngestor()

    result = approve(factory, second_ingestor, review_id)

    assert result.ok is False and result.message == "already_approved"
    assert second_ingestor.calls == []


def test_concurrent_approve_ingests_only_once(factory):
    """两个审核员同时点通过：状态列要等入库后才写，光靠它拦不住重复入库。"""
    review_id = _row(factory, ai_suggested_answer="以商品页为准")

    class BlockingIngestor(FakeIngestor):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def ingest_pairs(self, pairs, *, source_ref=None):
            self.entered.set()
            assert self.release.wait(timeout=5)
            return super().ingest_pairs(pairs, source_ref=source_ref)

    first_ingestor = BlockingIngestor()
    first: dict = {}
    thread = threading.Thread(target=lambda: first.setdefault("result", approve(factory, first_ingestor, review_id)))
    thread.start()
    try:
        assert first_ingestor.entered.wait(timeout=5)
        second = approve(factory, FakeIngestor(), review_id)
    finally:
        first_ingestor.release.set()
        thread.join(timeout=5)

    assert second.ok is False and second.message == "in_progress"
    assert first["result"].ok is True
    assert len(first_ingestor.calls) == 1


def test_approve_missing_row_and_empty_answer(factory):
    assert approve(factory, FakeIngestor(), 999).message == "not_found"
    review_id = _row(factory)
    result = approve(factory, FakeIngestor(), review_id)
    assert result.ok is False and result.message == "empty_answer"


def test_reject_blocks_approved_row(factory):
    review_id = _row(factory, ai_suggested_answer="以商品页为准")
    assert reject(factory, review_id, reason="重复") is True
    assert reject(factory, review_id) is True          # 重复驳回是幂等的
    approve(factory, FakeIngestor(), review_id, answer="满 99 包邮")
    assert reject(factory, review_id) is False         # 已通过 → 不能再驳回（知识库已经写了）
    assert reject(factory, 999) is False


def test_list_queue_filters_and_paginates(factory):
    _row(factory, occurrence_count=5)
    _row(factory, normalized_question="退货流程", occurrence_count=9, review_status="驳回")

    rows, total = list_queue(factory)
    assert total == 2 and [row["occurrence_count"] for row in rows] == [9, 5]

    rows, total = list_queue(factory, status="待审")
    assert total == 1 and rows[0]["normalized_question"] == "运费怎么算"

    rows, total = list_queue(factory, limit=1, offset=1)
    assert total == 2 and len(rows) == 1


def test_queue_detail_includes_merged_originals_and_chunks(factory):
    review_id = _row(factory)
    with factory() as session:
        session.add(LowConfidenceQuestion(
            conversation_id=3, raw_question="邮费多少啊", source="user_feedback",
            reason="feedback:message_id=11", matched_review_id=review_id,
            retrieved_chunks=[{"question": "运费", "answer": "满 99 包邮", "score": 0.42}]))
        session.add(LowConfidenceQuestion(conversation_id=4, raw_question="没进队列的",
                                          source="retrieval_low_conf"))
        session.commit()

    detail = queue_detail(factory, review_id)

    assert detail["id"] == review_id and detail["review_status"] == "待审"
    assert len(detail["sources"]) == 1
    source = detail["sources"][0]
    assert source["raw_question"] == "邮费多少啊" and source["source"] == "user_feedback"
    assert source["retrieved_chunks"][0]["score"] == 0.42
    assert queue_detail(factory, 999) is None
