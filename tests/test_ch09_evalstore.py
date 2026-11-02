import pytest

from app.knowledge.evalstore import load_runs, record_eval_run
from tests.support import sqlite_factory


@pytest.fixture
def factory():
    with sqlite_factory() as session_factory:
        yield session_factory


METRICS = {"strategy": "hybrid_rerank", "recall@5": 0.8, "recall@10": 0.9, "mrr@10": 0.7,
           "refusal_rate": 1.0, "faithfulness": None, "confidence_min": 0.45}


def test_record_and_load_roundtrip(factory):
    run_id = record_eval_run(factory, triggered_by="手动", dataset_size=80, metrics=METRICS)

    rows = load_runs(factory)

    assert len(rows) == 1 and rows[0]["id"] == run_id
    assert rows[0]["triggered_by"] == "手动" and rows[0]["dataset_size"] == 80
    assert rows[0]["metrics"]["faithfulness"] is None      # null 要留得住，不能被丢掉
    assert rows[0]["metrics"]["confidence_min"] == 0.45


def test_load_runs_is_oldest_first_and_limited(factory):
    for index in range(5):
        record_eval_run(factory, triggered_by="定时", dataset_size=index, metrics=METRICS)

    rows = load_runs(factory, limit=3)

    assert [row["dataset_size"] for row in rows] == [2, 3, 4]


def test_load_runs_on_empty_table(factory):
    assert load_runs(factory) == []


@pytest.mark.parametrize("trigger", ["手动跑", "", "manual"])
def test_record_eval_run_rejects_unknown_trigger(factory, trigger):
    with pytest.raises(ValueError, match="triggered_by"):
        record_eval_run(factory, triggered_by=trigger, dataset_size=80, metrics=METRICS)

    assert load_runs(factory) == []
