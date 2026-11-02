"""待归类查询、推理服务客户端、批量写表。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import LowConfidenceQuestion, TopicClassification
from app.topics.limits import MAX_BATCH, MAX_CHARS
from app.topics.taxonomy import OTHER, validate_labels

logger = logging.getLogger(__name__)

# Python 的 strip() 去所有 Unicode 空白，而 SQL 的 trim() 只去 U+0020；
# 逐字符 replace 才能让两端口径一致，且 MySQL/SQLite 都支持。
_BLANK_CHARS = ("\t", "\n", "\r", " ", "\u3000", "\u00a0")


def has_text(column):
    """跨库判断文本列是否含非空白字符。"""
    cleaned = column
    for blank in _BLANK_CHARS:
        cleaned = func.replace(cleaned, blank, "")
    return cleaned != ""


@dataclass(frozen=True)
class ClassifyReport:
    pending: int = 0
    classified: int = 0
    failed: int = 0


def _unclassified():
    # 空白文本永远无法归类，留在池里会让 pending_count 永远降不下去。
    return (select(LowConfidenceQuestion.id)
            .outerjoin(TopicClassification,
                       TopicClassification.question_id == LowConfidenceQuestion.id)
            .where(TopicClassification.id.is_(None))
            .where(has_text(LowConfidenceQuestion.raw_question)))


def pending_count(session_factory) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(_unclassified().subquery())) or 0


def pending_questions(session_factory, limit: int, *, force: bool = False) -> list[tuple[int, str]]:
    statement = (select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question)
                 .where(has_text(LowConfidenceQuestion.raw_question)))
    if not force:
        statement = statement.where(LowConfidenceQuestion.id.in_(_unclassified()))
    with session_factory() as session:
        result = session.execute(
            statement.order_by(LowConfidenceQuestion.id).limit(limit)).all()
    return [(int(qid), (raw or "").strip()) for qid, raw in result if (raw or "").strip()]


def store_classifications(session_factory, results: dict[int, list[str]], *,
                          force: bool = False) -> int:
    # 每行独立事务：一行撞唯一键只丢这一行，不能让整批成功结果跟着回滚。
    written = 0
    for question_id, labels in results.items():
        try:
            normalized = validate_labels(labels or [OTHER])
        except ValueError:
            logger.warning("问题 %s 的主题标签无效，跳过本行。", question_id)
            continue
        try:
            with session_factory() as session:
                row = session.scalar(
                    select(TopicClassification)
                    .where(TopicClassification.question_id == question_id))
                if row is None:
                    session.add(TopicClassification(question_id=question_id, labels=normalized))
                elif force:
                    row.labels = normalized
                    row.classified_at = func.now()
                else:
                    continue
                session.commit()
            written += 1
        except IntegrityError:
            logger.warning("问题 %s 写入撞唯一键，跳过本行。", question_id, exc_info=True)
    return written


class TopicClient:
    def __init__(self, base_url: str, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        # transport 只给测试注入 MockTransport 用，默认交给 httpx 自己建连接池。
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                    transport=transport)

    def __call__(self, texts: list[str]) -> list[list[str]]:
        response = self._client.post("/classify", json={"texts": texts})
        response.raise_for_status()
        items = response.json().get("items")
        if not isinstance(items, list) or len(items) != len(texts):
            raise ValueError("推理服务返回条数不匹配")
        return [list(item.get("labels") or []) for item in items]

    def close(self) -> None:
        self._client.close()


def _batches(rows: list[tuple[int, str]]):
    """按累计字符切块，保证每块总长不超过 MAX_CHARS；超长单条截断后再送。"""
    batch: list[tuple[int, str]] = []
    size = 0
    for question_id, text in rows:
        if len(text) > MAX_CHARS:
            logger.warning("问题 %s 长度 %s 超过单次预算 %s，截断后再送推理。",
                           question_id, len(text), MAX_CHARS)
            text = text[:MAX_CHARS]
        if batch and size + len(text) > MAX_CHARS:
            yield batch
            batch, size = [], 0
        batch.append((question_id, text))
        size += len(text)
    if batch:
        yield batch


def run_batch(session_factory, classify: Callable[[list[str]], list[list[str]]], *,
              min_pending: int = 20, limit: int = 50,
              force: bool = False) -> ClassifyReport:
    # 先夹紧再查库：limit=200 会在推理服务侧 422，本批一条都写不进去。
    requested = max(1, int(limit))
    if requested > MAX_BATCH:
        logger.warning("limit=%s 超过单次上限 %s，本批按 %s 条处理。",
                       requested, MAX_BATCH, MAX_BATCH)
    limit = min(requested, MAX_BATCH)
    pending = pending_count(session_factory)
    if not force and pending < min_pending:
        return ClassifyReport(pending=pending)
    rows = pending_questions(session_factory, limit, force=force)
    if not rows:
        return ClassifyReport(pending=pending)
    results: dict[int, list[str]] = {}
    failed = 0
    for batch in _batches(rows):
        try:
            labels = classify([text for _, text in batch])
        except Exception:
            logger.warning("主题归类服务调用失败，本块 %s 条跳过。", len(batch), exc_info=True)
            failed += len(batch)
            continue
        if not isinstance(labels, (list, tuple)) or len(labels) != len(batch):
            logger.warning("主题归类返回条数与本块不一致，本块 %s 条计失败。", len(batch))
            failed += len(batch)
            continue
        for (question_id, _), item in zip(batch, labels):
            try:
                results[question_id] = validate_labels(item or [OTHER])
            except ValueError:
                failed += 1
    written = store_classifications(session_factory, results, force=force)
    return ClassifyReport(pending=pending, classified=written,
                          failed=failed + (len(results) - written))
