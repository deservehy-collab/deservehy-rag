"""进程内定时主题归类；照 EvalScheduler 的形状。"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TopicScheduler:
    def __init__(self, job, interval_minutes: int):
        self.job = job
        self.interval_minutes = interval_minutes
        self._scheduler = None

    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        try:
            scheduler.add_job(self._run, "interval", minutes=self.interval_minutes,
                              id="topic-classify", max_instances=1, coalesce=True)
            scheduler.start()
        except Exception:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            raise
        self._scheduler = scheduler
        logger.info("主题归类定时任务已启动，间隔 %s 分钟。", self.interval_minutes)

    async def _run(self) -> None:
        try:
            await asyncio.to_thread(self.job)
        except Exception:
            logger.exception("主题归类失败，等待下一轮。")

    def shutdown(self) -> None:
        from apscheduler.schedulers.base import SchedulerNotRunningError

        scheduler, self._scheduler = self._scheduler, None
        if scheduler is None:
            return
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            logger.debug("主题归类调度器未运行，跳过 shutdown。")
