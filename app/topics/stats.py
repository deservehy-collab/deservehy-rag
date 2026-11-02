"""主题分布统计；JSON 在 Python 侧解析，MySQL/SQLite 同路。"""

from __future__ import annotations

from app.models import LowConfidenceQuestion, TopicClassification
from app.topics.taxonomy import LABELS, OTHER, order_labels
from app.topics.worker import has_text


def distribution(session_factory) -> dict:
    with session_factory() as session:
        rows = session.query(TopicClassification).all()
        # 与 worker.pending_count 口径一致：空白问题不参与待归类统计。
        pool_total = (session.query(LowConfidenceQuestion)
                      .filter(has_text(LowConfidenceQuestion.raw_question)).count())
    counts = {label: 0 for label in LABELS}
    multi_label = 0
    last = None
    for row in rows:
        labels = order_labels([label for label in (row.labels or []) if label in counts]) or [OTHER]
        multi_label += 1 if len(labels) > 1 else 0
        for label in labels:
            counts[label] += 1
        if row.classified_at is not None and (last is None or row.classified_at > last):
            last = row.classified_at
    total = len(rows)
    return {
        "ok": True,
        "total_classified": total,
        "pending": max(pool_total - total, 0),
        "multi_label": multi_label,
        "last_classified_at": last.isoformat() if last else None,
        "classes": [
            {"label": label, "count": counts[label],
             "pct": round(counts[label] / total, 4) if total else 0.0}
            for label in LABELS
        ],
    }
