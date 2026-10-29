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
