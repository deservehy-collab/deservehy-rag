import json
import logging
from datetime import datetime

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import LowConfidenceQuestion, TopicClassification
from app.topics.limits import MAX_CHARS
from app.topics.stats import distribution
from app.topics.taxonomy import LABELS
from app.topics.worker import (
    ClassifyReport, TopicClient, pending_count, pending_questions, run_batch, store_classifications,
)
from tests.support import sqlite_factory


def seed(factory, text="怎么查物流"):
    with factory() as session:
        row = LowConfidenceQuestion(raw_question=text, source="self_check")
        session.add(row)
        session.commit()
        return row.id


def test_pending_only_unclassified():
    with sqlite_factory() as factory:
        first = seed(factory, "怎么查物流")
        second = seed(factory, "运费怎么算")
        seed(factory, "   ")  # 空白文本无法归类，不能占 pending 名额
        assert pending_count(factory) == 2
        assert [qid for qid, _ in pending_questions(factory, 10)] == [first, second]
        store_classifications(factory, {first: ["物流"]})
        assert pending_count(factory) == 1
        assert [qid for qid, _ in pending_questions(factory, 10)] == [second]
        assert {qid for qid, _ in pending_questions(factory, 10, force=True)} == {first, second}


def test_pending_ignores_all_unicode_blank_variants():
    with sqlite_factory() as factory:
        for blank in ("   ", "\t", "\n", "\u3000", "\u00a0"):
            seed(factory, blank)
        qid = seed(factory, "怎么查物流")
        assert pending_count(factory) == 1
        assert pending_questions(factory, 10) == [(qid, "怎么查物流")]
        assert pending_questions(factory, 10, force=True) == [(qid, "怎么查物流")]
        # 分布池总量必须与 worker 口径一致，否则 force 批次永远清不完。
        assert distribution(factory)["pending"] == 1


def test_store_is_idempotent_and_force_updates():
    with sqlite_factory() as factory:
        qid = seed(factory)
        assert store_classifications(factory, {qid: ["物流"]}) == 1
        assert store_classifications(factory, {qid: ["运费"]}) == 0
        with factory() as session:
            assert session.query(TopicClassification).one().labels == ["物流"]
        assert store_classifications(factory, {qid: ["运费"]}, force=True) == 1
        with factory() as session:
            assert session.query(TopicClassification).one().labels == ["运费"]


def test_force_refresh_really_updates_classified_at():
    with sqlite_factory() as factory:
        qid = seed(factory)
        assert store_classifications(factory, {qid: ["物流"]}) == 1
        with factory() as session:
            row = session.query(TopicClassification).one()
            row.classified_at = datetime(2000, 1, 1)
            session.commit()
        assert store_classifications(factory, {qid: ["运费"]}, force=True) == 1
        with factory() as session:
            row = session.query(TopicClassification).one()
            assert row.labels == ["运费"]
            assert row.classified_at > datetime(2000, 1, 1)


def test_store_unique_conflict_skips_only_the_failing_row(monkeypatch):
    with sqlite_factory() as factory:
        conflict, keep = seed(factory, "第一问"), seed(factory, "第二问")
        original_commit = Session.commit

        def flaky_commit(self):
            for row in self.new:
                if isinstance(row, TopicClassification) and row.question_id == conflict:
                    raise IntegrityError("INSERT INTO topic_classifications", {},
                                         Exception("UNIQUE constraint failed"))
            return original_commit(self)

        monkeypatch.setattr(Session, "commit", flaky_commit)
        assert store_classifications(
            factory, {conflict: ["物流"], keep: ["运费"]}, force=True) == 1
        with factory() as session:
            rows = {row.question_id: row.labels for row in session.query(TopicClassification)}
        assert rows == {keep: ["运费"]}


def test_store_empty_labels_falls_back_to_other_and_skips_unknown(caplog):
    with sqlite_factory() as factory:
        qid = seed(factory)
        store_classifications(factory, {qid: []})
        with factory() as session:
            assert session.query(TopicClassification).one().labels == ["其他"]
        with caplog.at_level(logging.WARNING, logger="app.topics.worker"):
            assert store_classifications(factory, {seed(factory, "第二问"): ["退款"]}) == 0
        assert "主题标签无效" in caplog.text
        with factory() as session:
            assert session.query(TopicClassification).count() == 1


def test_run_batch_skips_below_threshold_and_handles_failure():
    with sqlite_factory() as factory:
        seed(factory)
        calls = []
        report = run_batch(factory, lambda texts: calls.append(texts) or [["物流"]], min_pending=2)
        assert report == ClassifyReport(pending=1) and calls == []

        def boom(texts):
            raise RuntimeError("service down")

        report = run_batch(factory, boom, min_pending=1)
        assert report.failed == 1 and report.classified == 0


def test_run_batch_counts_unknown_labels_as_failed():
    with sqlite_factory() as factory:
        first, second = seed(factory, "第一问"), seed(factory, "第二问")
        report = run_batch(factory, lambda texts: [["物流"], ["退款"]], min_pending=1, limit=2)
        assert report.classified == 1 and report.failed == 1
        with factory() as session:
            assert session.query(TopicClassification).one().question_id == first


def test_run_batch_counts_mismatched_return_as_failed():
    with sqlite_factory() as factory:
        seed(factory)
        report = run_batch(factory, lambda texts: [], min_pending=1)
        assert report.classified == 0 and report.failed == 1
        with factory() as session:
            assert session.query(TopicClassification).count() == 0


def test_run_batch_splits_by_char_budget_and_writes_all():
    with sqlite_factory() as factory:
        for index in range(3):
            seed(factory, f"{index}" + "长" * 7999)  # 每条 8000 字，3 条必分两块
        sizes = []

        def classify(texts):
            sizes.append(sum(len(text) for text in texts))
            return [["物流"] for _ in texts]

        report = run_batch(factory, classify, min_pending=1, limit=64)
        assert sizes == [16000, 8000]
        assert report.classified == 3 and report.failed == 0
        with factory() as session:
            assert session.query(TopicClassification).count() == 3


def test_run_batch_truncates_oversized_single_text():
    with sqlite_factory() as factory:
        seed(factory, "长" * (MAX_CHARS + 1))
        received = []

        def classify(texts):
            received.extend(len(text) for text in texts)
            return [["物流"] for _ in texts]

        report = run_batch(factory, classify, min_pending=1)
        assert received == [MAX_CHARS]
        assert report.classified == 1 and report.failed == 0


def test_run_batch_clamps_limit_to_service_cap(caplog):
    # 推理服务单次上限 64：limit 必须在查库前夹紧，否则 65+ 条必被 422。
    with sqlite_factory() as factory:
        for index in range(70):
            seed(factory, f"第{index}问怎么查物流")
        sizes = []

        def classify(texts):
            sizes.append(len(texts))
            return [["物流"] for _ in texts]

        with caplog.at_level(logging.WARNING, logger="app.topics.worker"):
            report = run_batch(factory, classify, min_pending=1, limit=200)
        assert "超过单次上限" in caplog.text
        assert sizes == [64]
        assert report.pending == 70
        assert report.classified == 64 and report.failed == 0
        with factory() as session:
            assert session.query(TopicClassification).count() == 64


def test_topic_client_maps_labels_via_http():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [{"labels": ["运费", "物流"]}, {"labels": None}]})

    client = TopicClient("http://topic.invalid", transport=httpx.MockTransport(handler))
    try:
        assert client(["怎么查物流", "运费多少"]) == [["运费", "物流"], []]
    finally:
        client.close()
    assert [(request.method, request.url.path) for request in requests] == [("POST", "/classify")]
    assert json.loads(requests[0].content) == {"texts": ["怎么查物流", "运费多少"]}


def test_topic_client_raises_on_http_error():
    def handler(request):
        return httpx.Response(503, json={"detail": "model_not_ready"})

    client = TopicClient("http://topic.invalid", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client(["怎么查物流"])
    finally:
        client.close()


def test_topic_client_rejects_mismatched_item_count():
    def handler(request):
        return httpx.Response(200, json={"items": [{"labels": ["物流"]}]})

    client = TopicClient("http://topic.invalid", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError, match="条数不匹配"):
            client(["第一问", "第二问"])
    finally:
        client.close()


def test_distribution_fixed_order_and_counts():
    with sqlite_factory() as factory:
        first, second, third = seed(factory, "第一问"), seed(factory, "第二问"), seed(factory, "第三问")
        store_classifications(factory, {first: ["物流", "运费"], second: ["其他"]})
        data = distribution(factory)
        assert data["total_classified"] == 2
        assert data["pending"] == 1
        assert data["multi_label"] == 1
        assert [row["label"] for row in data["classes"]] == list(LABELS)
        by_label = {row["label"]: row for row in data["classes"]}
        assert by_label["物流"]["count"] == 1 and by_label["物流"]["pct"] == 0.5
        assert by_label["其他"]["count"] == 1


def test_distribution_empty_database():
    with sqlite_factory() as factory:
        data = distribution(factory)
        assert data["total_classified"] == 0 and data["pending"] == 0
        assert data["multi_label"] == 0 and data["last_classified_at"] is None
        assert len(data["classes"]) == 17
        assert all(row["count"] == 0 and row["pct"] == 0.0 for row in data["classes"])
