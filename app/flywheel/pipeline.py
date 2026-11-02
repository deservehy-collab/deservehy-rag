import asyncio
import logging
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.flywheel.dedupe import find_match
from app.flywheel.standardize import Standardized, standardize_batch
from app.models import LowConfidenceQuestion, ReviewQueue

logger = logging.getLogger(__name__)

# 同一进程内不重叠跑：两个 drain 同时读到同一批 matched_review_id IS NULL 的行，
# 会把同一个缺口插成两行待审。用模块级布尔标志而不是 asyncio.Lock：Lock 第一次 acquire 会
# 绑定当前事件循环，pytest 里几十个 asyncio.run() 各建一个循环，换循环再 acquire 会抛
# 「is bound to a different event loop」。检查与置位之间没有 await，单事件循环下标志已足够。
_RUNNING = False


@dataclass(frozen=True)
class DrainReport:
    pending: int = 0
    standardized: int = 0
    merged: int = 0
    created: int = 0
    failed: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


async def drain(session_factory: sessionmaker, model, *, limit: int = 50,
                batch_size: int = 10, candidates: int = 20) -> DrainReport:
    rows, contexts = _pending(session_factory, limit)
    if not rows:
        return DrainReport()
    standardized = await standardize_batch(model, rows, batch_size=batch_size, contexts=contexts)
    by_pool_id = {item.pool_id: item for item in standardized}
    merged = created = failed = 0
    for pool_id, _question in rows:
        item = by_pool_id.get(pool_id)
        if item is None:
            logger.warning("池行 %s 没有拿到标准化结果，保持待处理。", pool_id)
            failed += 1
            continue
        try:
            review_id, is_new = await _place(session_factory, model, item, candidates)
        except Exception:
            logger.warning("池行 %s 归并失败，保持待处理。", pool_id, exc_info=True)
            failed += 1
            continue
        if is_new:
            created += 1
        else:
            merged += 1
    return DrainReport(pending=len(rows), standardized=len(standardized),
                       merged=merged, created=created, failed=failed)


def _pending(session_factory: sessionmaker, limit: int) -> tuple[list[tuple[int, str]], dict]:
    """池行按 id 升序取；召回快照单独收成 contexts，喂给标准化写 sample_answer（规格 §9.1）。"""
    with session_factory() as session:
        rows = session.execute(
            select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question,
                   LowConfidenceQuestion.retrieved_chunks)
            .where(LowConfidenceQuestion.matched_review_id.is_(None))
            .order_by(LowConfidenceQuestion.id)
            .limit(limit)
        ).all()
    pairs = [(row.id, row.raw_question) for row in rows]
    contexts = {row.id: row.retrieved_chunks or [] for row in rows}
    return pairs, contexts


async def _place(session_factory: sessionmaker, model, item: Standardized,
                 candidates: int) -> tuple[int, bool]:
    question = item.normalized_question[:512]
    with session_factory() as session:
        rows = session.execute(
            select(ReviewQueue.id, ReviewQueue.normalized_question)
            .where(ReviewQueue.review_status == "待审")
            .order_by(ReviewQueue.occurrence_count.desc(), ReviewQueue.id)
            .limit(candidates)
        ).all()
    pairs = [(row.id, row.normalized_question) for row in rows]
    # 无条件调：候选为空时 find_match 自己早退（一次模型都不调），但调用方的假替身抛异常
    # 也要能被 drain 计成 failed，不能在这里先短路掉。
    matched = await find_match(model, question, pairs)
    with session_factory() as session:
        target = session.get(ReviewQueue, matched) if matched is not None else None
        if target is not None and target.review_status != "待审":
            target = None
        if target is None:
            review = ReviewQueue(normalized_question=question,
                                 ai_suggested_answer=item.sample_answer,
                                 occurrence_count=1, review_status="待审")
            session.add(review)
            session.flush()
            review_id, is_new = review.id, True
        else:
            target.occurrence_count += 1
            if not target.ai_suggested_answer and item.sample_answer:
                target.ai_suggested_answer = item.sample_answer
            review_id, is_new = target.id, False
        pool_row = session.get(LowConfidenceQuestion, item.pool_id)
        if pool_row is not None:
            pool_row.matched_review_id = review_id
        session.commit()
    return review_id, is_new


async def drain_later(session_factory: sessionmaker, model, **kwargs) -> None:
    global _RUNNING
    if _RUNNING:
        logger.debug("飞轮已在排空，本次触发跳过。")
        return
    _RUNNING = True
    try:
        try:
            report = await drain(session_factory, model, **kwargs)
        except Exception:
            logger.exception("飞轮排空失败，池行留待下一趟重试。")
            return
    finally:
        _RUNNING = False
    if report.pending:
        logger.info("飞轮排空：%s", report.as_dict())


class FlywheelTrigger:
    """三个入池口共用的后台触发器；关着、模型缺失或已有 drain 在跑时 schedule() 返回 False。"""

    def __init__(self, session_factory: sessionmaker, model, *, enabled: bool = True,
                 limit: int = 50, batch_size: int = 10, candidates: int = 20):
        self.session_factory = session_factory
        self.model = model
        self.enabled = enabled
        self.limit = limit
        self.batch_size = batch_size
        self.candidates = candidates
        self._tasks: set[asyncio.Task] = set()

    def schedule(self) -> bool:
        if not self.enabled or self.model is None or _RUNNING:
            return False
        task = asyncio.create_task(
            drain_later(self.session_factory, self.model, limit=self.limit,
                        batch_size=self.batch_size, candidates=self.candidates)
        )
        # create_task 只留弱引用，不自己攥着会被 GC 掉（任务跑到一半凭空消失）。
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def wait(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
