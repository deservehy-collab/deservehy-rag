import asyncio

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.topics.scheduler import TopicScheduler


def test_topic_scheduler_starts_and_stops_cleanly():
    async def scenario():
        scheduler = TopicScheduler(lambda: None, 5)
        await scheduler.start()
        try:
            assert scheduler._scheduler is not None and scheduler._scheduler.running
            job = scheduler._scheduler.get_job("topic-classify")
            assert job is not None and job.next_run_time is not None
            # 上一轮没跑完不要叠起来，错过的那次合并成一次（同 EvalScheduler 的取舍）。
            assert job.max_instances == 1
            assert job.coalesce is True
        finally:
            scheduler.shutdown()
        assert scheduler._scheduler is None
        # 重复 shutdown 必须无害：lifespan finally 里可能已经关过一次。
        scheduler.shutdown()

    asyncio.run(scenario())


def test_topic_scheduler_add_job_failure_leaves_shutdown_safe(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(AsyncIOScheduler, "add_job", boom)

    scheduler = TopicScheduler(lambda: None, 5)
    with pytest.raises(RuntimeError):
        asyncio.run(scheduler.start())

    # 修复前 start() 可能已经把 self._scheduler 指向未启动的实例，这里再 shutdown 会抛
    # SchedulerNotRunningError，把 lifespan finally 里后面的关停步骤全部打断（ch09 踩过）。
    assert scheduler._scheduler is None
    scheduler.shutdown()
