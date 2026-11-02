import asyncio
from types import SimpleNamespace

import pytest

from app.knowledge.scheduler import EvalScheduler
from scripts.evaluate_retrieval import _metrics_of

OVERALL = {"recall@5": 0.8, "recall@10": 0.9, "mrr@10": 0.7, "refusal_rate": 1.0}
# eval_runs.metrics 的契约：固定七个键，趋势报告按这七个直接取。
KEYS = {"strategy", "recall@5", "recall@10", "mrr@10", "refusal_rate", "faithfulness", "confidence_min"}


def report(faithfulness=None):
    body = {"retrieval": {"hybrid_rerank": {"overall": OVERALL}}}
    if faithfulness is not None:
        body["faithfulness"] = faithfulness
    return body


def test_metrics_of_keeps_exact_seven_keys_with_faithfulness():
    metrics = _metrics_of(
        report({"judged": 12, "faithful": 9, "rate": 0.75}),
        SimpleNamespace(confidence_min=0.45),
    )

    assert set(metrics) == KEYS
    assert metrics["strategy"] == "hybrid_rerank"
    assert metrics["recall@5"] == 0.8 and metrics["mrr@10"] == 0.7
    assert metrics["faithfulness"] == 0.75
    assert metrics["confidence_min"] == 0.45


def test_metrics_of_without_faithfulness_is_none_not_missing():
    metrics = _metrics_of(report(), SimpleNamespace(confidence_min=0.10))

    assert set(metrics) == KEYS
    assert metrics["faithfulness"] is None


def test_metrics_of_zero_judged_is_none_not_zero():
    # 裁判一行都没判出来时 run_faithfulness 的 rate 是 0.0，但这不是「忠实度 0」。
    metrics = _metrics_of(
        report({"judged": 0, "faithful": 0, "rate": 0.0}),
        SimpleNamespace(confidence_min=0.10),
    )

    assert metrics["faithfulness"] is None


def test_eval_scheduler_valid_cron_starts_and_stops_cleanly():
    async def scenario():
        scheduler = EvalScheduler(lambda: None, "30 3 * * 0")
        await scheduler.start()
        try:
            assert scheduler._scheduler is not None and scheduler._scheduler.running
            job = scheduler._scheduler.get_job("retrieval-eval")
            assert job is not None and job.next_run_time is not None
        finally:
            scheduler.shutdown()
        assert scheduler._scheduler is None

    asyncio.run(scenario())


def test_eval_scheduler_bad_cron_leaves_shutdown_safe():
    scheduler = EvalScheduler(lambda: None, "bad cron")

    with pytest.raises(ValueError):
        asyncio.run(scheduler.start())

    # 修复前 start() 已经把 self._scheduler 指向未启动的实例，这里会抛
    # SchedulerNotRunningError，把 lifespan finally 里后面的关停步骤全部打断。
    scheduler.shutdown()
    assert scheduler._scheduler is None
