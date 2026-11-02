import asyncio
import logging

from app.knowledge.mining import ConversationMiner

logger = logging.getLogger(__name__)


class MiningScheduler:
    """进程内定时挖掘；只在单 worker 部署下启用，避免多进程重复抽取。"""

    def __init__(self, miner: ConversationMiner, interval_minutes: int):
        self.miner = miner
        self.interval_minutes = interval_minutes
        self._scheduler = None

    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._scheduler.add_job(
            self._run,
            "interval",
            minutes=self.interval_minutes,
            id="knowledge-mining",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info("知识挖掘定时任务已启动，间隔 %s 分钟。", self.interval_minutes)

    async def _run(self) -> None:
        try:
            result = await asyncio.to_thread(self.miner.mine)
            logger.info("知识挖掘完成：%s", result)
        except Exception:
            logger.exception("知识挖掘失败，等待下一轮重试。")

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None


class EvalScheduler:
    """进程内定时评估；EVAL_SCHEDULE_CRON 为空时不启动。"""

    def __init__(self, job, cron: str):
        self.job = job
        self.cron = cron
        self._scheduler = None

    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        # 先在局部建好，add_job/start 都成功才交给 self：cron 写法有误时 start() 抛 ValueError，
        # 若此时 self._scheduler 已经指向一个没启动的实例，lifespan 的 finally 再 shutdown()
        # 会抛 SchedulerNotRunningError，把后面的关停步骤全部打断。
        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        try:
            scheduler.add_job(
                self._run, CronTrigger.from_crontab(self.cron, timezone="Asia/Shanghai"),
                id="retrieval-eval", max_instances=1, coalesce=True,
            )
            scheduler.start()
        except Exception:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            raise
        self._scheduler = scheduler
        logger.info("定时评估已启动，cron=%s。", self.cron)

    async def _run(self) -> None:
        try:
            await asyncio.to_thread(self.job)
        except Exception:
            logger.exception("定时评估失败，等待下一轮。")

    def shutdown(self) -> None:
        from apscheduler.schedulers.base import SchedulerNotRunningError

        scheduler, self._scheduler = self._scheduler, None
        if scheduler is None:
            return
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            # start() 失败（cron 非法）或进程退出路径重复调用：没有在跑就没什么可关的。
            logger.debug("定时评估调度器未运行，跳过 shutdown。")
