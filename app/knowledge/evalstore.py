from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import EvalRun

# eval_runs.triggered_by 的合法取值；趋势报告按这两类分开看，写错会在报表里变成第三类。
TRIGGERS = ("定时", "手动")


def record_eval_run(session_factory: sessionmaker, *, triggered_by: str,
                    dataset_size: int, metrics: dict) -> int:
    if triggered_by not in TRIGGERS:
        raise ValueError(f"triggered_by 只能是 {'/'.join(TRIGGERS)}，收到 {triggered_by!r}")
    with session_factory() as session:
        row = EvalRun(triggered_by=triggered_by, dataset_size=dataset_size, metrics=metrics)
        session.add(row)
        session.commit()
        return row.id


def load_runs(session_factory: sessionmaker, limit: int = 10) -> list[dict]:
    with session_factory() as session:
        rows = session.scalars(
            select(EvalRun).order_by(EvalRun.id.desc()).limit(limit)
        ).all()
    return [
        {"id": row.id, "triggered_by": row.triggered_by, "dataset_size": row.dataset_size,
         "metrics": row.metrics or {},
         "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else None}
        for row in reversed(rows)          # 取最近 limit 轮，但按时间升序交给趋势报告
    ]
