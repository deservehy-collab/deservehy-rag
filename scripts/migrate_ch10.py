"""ch10 升级已有库：只补 topic_classifications，重复执行安全。

跑法：
    python scripts/migrate_ch10.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import build_database_url, build_engine  # noqa: E402
from app.models import Base  # noqa: E402

NEW_TABLES = ("topic_classifications",)


def migrate(engine) -> list[str]:
    done: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in NEW_TABLES:
                continue
            missing = not inspect(conn).has_table(table.name)
            table.create(conn, checkfirst=True)
            if missing:
                done.append(f"表 {table.name}")
    return done


def main() -> int:
    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    try:
        done = migrate(engine)
    finally:
        engine.dispose()
    print("ch10 迁移完成：" + ("；".join(done) if done else "结构已是最新，无需变更"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
