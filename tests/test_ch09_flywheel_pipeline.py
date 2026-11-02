import asyncio

import pytest
from sqlalchemy import select

from app.config import Settings
from app.flywheel import pipeline
from app.flywheel.pipeline import DrainReport, FlywheelTrigger, drain
from app.models import LowConfidenceQuestion, ReviewQueue
from app.workflow.knowledge_node import record_low_confidence
from tests.support import sqlite_factory


def test_flywheel_settings_defaults():
    # 不用 tests.support._settings()：那里会把 flywheel_auto_drain 默认关掉（见下一条），
    # 这里要验的是生产默认值。传齐三个必填项、_env_file=None 免得读到仓库 .env。
    settings = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1",
                        openai_model="test-model", openai_api_key="test-secret-not-real")
    assert settings.flywheel_auto_drain is True
    assert settings.flywheel_batch_size == 10
    assert settings.flywheel_dedupe_candidates == 20


@pytest.fixture
def factory():
    with sqlite_factory() as session_factory:
        yield session_factory


def _pool(factory, question="邮费多少", *, chunks=None):
    with factory() as session:
        row = LowConfidenceQuestion(conversation_id=1, raw_question=question,
                                    source="retrieval_low_conf", reason="low",
                                    retrieved_chunks=chunks)
        session.add(row)
        session.commit()
        return row.id


def test_drain_creates_one_review_row_and_backfills(monkeypatch, factory):

    seen = {}

    async def fake_standardize(model, rows, *, batch_size=10, contexts=None):
        seen["contexts"] = contexts
        return [pipeline.Standardized(pool_id, "运费怎么算", "以商品页为准") for pool_id, _ in rows]

    async def fake_match(model, question, candidates):
        return None

    monkeypatch.setattr(pipeline, "standardize_batch", fake_standardize)
    monkeypatch.setattr(pipeline, "find_match", fake_match)
    pool_id = _pool(factory, chunks=[{"question": "运费", "answer": "满 99 包邮", "score": 0.4}])

    report = asyncio.run(drain(factory, object(), limit=50))

    assert report == DrainReport(pending=1, standardized=1, merged=0, created=1, failed=0)
    # 召回片段必须从池行带进标准化调用，否则 sample_answer 只能是通用常识（规格 §9.1）。
    assert seen["contexts"] == {pool_id: [{"question": "运费", "answer": "满 99 包邮", "score": 0.4}]}
    with factory() as session:
        row = session.scalar(select(ReviewQueue))
        assert row.normalized_question == "运费怎么算"
        assert row.ai_suggested_answer == "以商品页为准"
        assert row.occurrence_count == 1 and row.review_status == "待审"
        assert session.get(LowConfidenceQuestion, pool_id).matched_review_id == row.id


def test_drain_merges_into_existing_pending_row(monkeypatch, factory):

    async def fake_standardize(model, rows, *, batch_size=10, contexts=None):
        return [pipeline.Standardized(pool_id, "运费怎么算", None) for pool_id, _ in rows]

    async def fake_match(model, question, candidates):
        assert candidates == [(7, "运费怎么算")]
        return 7

    monkeypatch.setattr(pipeline, "standardize_batch", fake_standardize)
    monkeypatch.setattr(pipeline, "find_match", fake_match)
    with factory() as session:
        session.add(ReviewQueue(id=7, normalized_question="运费怎么算",
                                ai_suggested_answer="以商品页为准", occurrence_count=2))
        session.commit()
    pool_id = _pool(factory)

    report = asyncio.run(drain(factory, object()))

    assert (report.merged, report.created) == (1, 0)
    with factory() as session:
        assert session.get(ReviewQueue, 7).occurrence_count == 3
        assert session.get(LowConfidenceQuestion, pool_id).matched_review_id == 7


def test_drain_is_idempotent_on_second_pass(monkeypatch, factory):

    async def fake_standardize(model, rows, *, batch_size=10, contexts=None):
        return [pipeline.Standardized(pool_id, "运费怎么算", None) for pool_id, _ in rows]

    async def fake_match(model, question, candidates):
        return None

    monkeypatch.setattr(pipeline, "standardize_batch", fake_standardize)
    monkeypatch.setattr(pipeline, "find_match", fake_match)
    _pool(factory)
    asyncio.run(drain(factory, object()))
    second = asyncio.run(drain(factory, object()))

    assert second.pending == 0
    with factory() as session:
        assert len(session.scalars(select(ReviewQueue)).all()) == 1


def test_one_bad_row_does_not_block_the_batch(monkeypatch, factory):

    async def fake_standardize(model, rows, *, batch_size=10, contexts=None):
        return [pipeline.Standardized(pool_id, "运费怎么算", None) for pool_id, _ in rows]

    async def flaky_match(model, question, candidates):
        raise RuntimeError("模型超时")

    monkeypatch.setattr(pipeline, "standardize_batch", fake_standardize)
    monkeypatch.setattr(pipeline, "find_match", flaky_match)
    _pool(factory)

    report = asyncio.run(drain(factory, object()))

    assert (report.pending, report.failed, report.created) == (1, 1, 0)
    with factory() as session:
        assert session.scalar(select(LowConfidenceQuestion)).matched_review_id is None


def test_missing_standardized_row_counts_as_failed(monkeypatch, factory):

    async def fake_standardize(model, rows, *, batch_size=10, contexts=None):
        return []

    monkeypatch.setattr(pipeline, "standardize_batch", fake_standardize)
    _pool(factory)

    report = asyncio.run(drain(factory, object()))

    assert (report.pending, report.standardized, report.failed) == (1, 0, 1)


def test_stale_candidate_falls_back_to_new_row(monkeypatch, factory):
    """查重期间候选被审核掉：宁可新建一行，也不要归并到已终结的缺口上。"""

    async def fake_standardize(model, rows, *, batch_size=10, contexts=None):
        return [pipeline.Standardized(pool_id, "运费怎么算", None) for pool_id, _ in rows]

    async def fake_match(model, question, candidates):
        # 候选查询时行 7 还是「待审」；查重等待期间被人审掉了，再返回 7 才能逼出 _place 的复查。
        with factory() as session:
            session.get(ReviewQueue, 7).review_status = "通过"
            session.commit()
        return 7

    monkeypatch.setattr(pipeline, "standardize_batch", fake_standardize)
    monkeypatch.setattr(pipeline, "find_match", fake_match)
    with factory() as session:
        session.add(ReviewQueue(id=7, normalized_question="运费怎么算"))
        session.commit()
    _pool(factory)

    report = asyncio.run(drain(factory, object()))

    assert (report.created, report.merged) == (1, 0)
    with factory() as session:
        rows = session.scalars(select(ReviewQueue).order_by(ReviewQueue.id)).all()
        assert [row.review_status for row in rows] == ["通过", "待审"]


def test_trigger_disabled_or_busy_returns_false(factory, monkeypatch):
    assert FlywheelTrigger(factory, object(), enabled=False).schedule() is False
    monkeypatch.setattr(pipeline, "_RUNNING", True)
    assert FlywheelTrigger(factory, object()).schedule() is False


def test_trigger_schedule_runs_drain(monkeypatch, factory):
    calls = []

    async def fake_later(session_factory, model, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(pipeline, "drain_later", fake_later)

    async def go():
        trigger = FlywheelTrigger(factory, object(), batch_size=3, candidates=4)
        assert trigger.schedule() is True
        await trigger.wait()

    asyncio.run(go())
    assert calls == [{"limit": 50, "batch_size": 3, "candidates": 4}]


class FakePool:
    def __init__(self):
        self.rows = []

    def record(self, conversation_id, question, source, reason, retrieved_chunks=None):
        self.rows.append((conversation_id, question, source, reason, retrieved_chunks))


def test_record_low_confidence_schedules_flywheel():
    class Trigger:
        def __init__(self):
            self.calls = 0

        def schedule(self):
            self.calls += 1
            return True

    pool = FakePool()
    trigger = Trigger()
    asyncio.run(record_low_confidence(pool, 1, "邮费多少", "retrieval_low_conf", "low",
                                      flywheel=trigger))
    assert pool.rows == [(1, "邮费多少", "retrieval_low_conf", "low", None)]
    assert trigger.calls == 1


def test_record_low_confidence_skips_flywheel_when_pooling_failed():
    class Trigger:
        def __init__(self):
            self.calls = 0

        def schedule(self):
            self.calls += 1

    class BrokenPool:
        def record(self, *args):
            raise RuntimeError("库断了")

    trigger = Trigger()
    asyncio.run(record_low_confidence(BrokenPool(), 1, "邮费多少", "retrieval_low_conf", "low",
                                      flywheel=trigger))
    assert trigger.calls == 0
