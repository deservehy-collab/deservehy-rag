"""ch09 升级已有库：只补新表/新列/新索引/新外键，重复执行安全。

MySQL 走 information_schema 先查再改；SQLite（测试库）由 Base.metadata.create_all 直接建全，
所以脚本对 SQLite 只做 create_all。跑法：
    python scripts/migrate_ch09.py

注意：CLI 的 build_database_url 固定 MySQL，所以 SQLite 分支只在测试里直接调用
migrate(engine) 时可达，命令行怎么跑都走不到。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import build_database_url, build_engine  # noqa: E402
from app.models import Base  # noqa: E402

NEW_TABLES = ("review_queue", "eval_runs")

# (表, 列, MySQL 片段)。AFTER 子句只对 MySQL 有意义——SQLite 走不到这里（函数提前 return）。
COLUMNS = [
    ("low_confidence_questions", "retrieved_chunks",
     "ADD COLUMN retrieved_chunks JSON NULL AFTER reason"),
    ("low_confidence_questions", "matched_review_id",
     "ADD COLUMN matched_review_id BIGINT UNSIGNED NULL AFTER retrieved_chunks"),
    ("messages", "retrieval_snapshot",
     "ADD COLUMN retrieval_snapshot JSON NULL AFTER tool_call_id"),
]

INDEXES = [("low_confidence_questions", "idx_lcq_matched_review_id",
            "ADD KEY idx_lcq_matched_review_id (matched_review_id)")]

FOREIGN_KEYS = [("low_confidence_questions", "fk_lcq_review",
                 "ADD CONSTRAINT fk_lcq_review FOREIGN KEY (matched_review_id) "
                 "REFERENCES review_queue (id) ON DELETE SET NULL")]


def existing_columns(conn, dialect, table) -> set[str]:
    if dialect == "sqlite":
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
    rows = conn.execute(
        text("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
             "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": table}).fetchall()
    return {row[0] for row in rows}


def existing_indexes(conn, table) -> set[str]:
    rows = conn.execute(
        text("SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
             "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": table}).fetchall()
    return {row[0] for row in rows}


def existing_constraints(conn, table) -> set[str]:
    rows = conn.execute(
        text("SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
             "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
             "AND CONSTRAINT_TYPE = 'FOREIGN KEY'"), {"t": table}).fetchall()
    return {row[0] for row in rows}


def migrate(engine) -> list[str]:
    dialect = engine.dialect.name
    done: list[str] = []
    with engine.begin() as conn:
        # 新表：checkfirst=True 对已存在的表是 no-op，天然幂等；只有真补建才记进 done。
        for table in Base.metadata.sorted_tables:
            if table.name in NEW_TABLES:
                missing = not inspect(conn).has_table(table.name)
                table.create(conn, checkfirst=True)
                if missing:
                    done.append(f"表 {table.name}")
        if dialect == "sqlite":
            return done
        for table, column, mysql_sql in COLUMNS:
            if column not in existing_columns(conn, dialect, table):
                conn.execute(text(f"ALTER TABLE {table} {mysql_sql}"))
                done.append(f"列 {table}.{column}")
        for table, index, mysql_sql in INDEXES:
            if index not in existing_indexes(conn, table):
                conn.execute(text(f"ALTER TABLE {table} {mysql_sql}"))
                done.append(f"索引 {table}.{index}")
        for table, name, mysql_sql in FOREIGN_KEYS:
            if name not in existing_constraints(conn, table):
                conn.execute(text(f"ALTER TABLE {table} {mysql_sql}"))
                done.append(f"外键 {table}.{name}")
    return done


def main() -> int:
    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    try:
        done = migrate(engine)
    finally:
        engine.dispose()
    print("ch09 迁移完成：" + ("；".join(done) if done else "结构已是最新，无需变更"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
