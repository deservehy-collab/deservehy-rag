"""ch09 表结构契约：SQLite 建表/列、数据库侧默认值、ORM 列契约、MySQL DDL、007 SQL、迁移幂等。"""

from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models import Base, EvalRun, ReviewQueue

SQL_FILE = Path(__file__).resolve().parents[1] / "scripts/init_sql/007_flywheel.sql"


def _engine(tmp_path):
    # 用文件库不用 sqlite://：inspect/create_all 各开连接，内存库换连接就换库
    return create_engine(f"sqlite:///{tmp_path / 'schema.db'}")


def test_new_tables_are_created_in_sqlite(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    names = set(inspect(engine).get_table_names())
    assert {"review_queue", "eval_runs"} <= names


def test_new_columns_exist(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    lcq = {c["name"] for c in inspect(engine).get_columns("low_confidence_questions")}
    assert {"retrieved_chunks", "matched_review_id"} <= lcq
    msg = {c["name"] for c in inspect(engine).get_columns("messages")}
    assert "retrieval_snapshot" in msg


def test_review_queue_defaults_match_ddl(tmp_path):
    """测的是 ORM **Python 侧** `default=`——落库时 SQLAlchemy 已自己填了值，不是 server_default 的证据。

    server_default 由 `test_db_side_defaults_come_from_the_database` 用裸 SQL 覆盖。
    """
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ReviewQueue(normalized_question="怎么查物流"))
        session.add(EvalRun(dataset_size=80, metrics={}))
        session.flush()
        row = session.query(ReviewQueue).one()
        assert row.review_status == "待审"
        assert row.occurrence_count == 1
        assert session.query(EvalRun).one().triggered_by == "定时"


def test_db_side_defaults_come_from_the_database(tmp_path):
    """绕开 ORM 的 Python 侧 default，走裸 SQL 插入——server_default 才算真的被测到。"""
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO review_queue (normalized_question) VALUES ('怎么查物流')")
        conn.exec_driver_sql("INSERT INTO eval_runs (dataset_size) VALUES (80)")
        status, count, updated = conn.exec_driver_sql(
            "SELECT review_status, occurrence_count, updated_at FROM review_queue").one()
        assert status == "待审" and count == 1 and updated is not None
        assert conn.exec_driver_sql("SELECT triggered_by FROM eval_runs").one()[0] == "定时"


def test_orm_column_contract():
    from app.models import EvalRun, LowConfidenceQuestion, Message, ReviewQueue

    rq = ReviewQueue.__table__.c
    assert rq["normalized_question"].type.length == 512
    assert rq["normalized_question"].nullable is False
    assert rq["review_status"].type.enums == ["待审", "通过", "驳回"]
    assert rq["review_status"].server_default.arg == "待审"
    assert rq["occurrence_count"].nullable is False
    assert rq["occurrence_count"].server_default.arg == "1"

    er = EvalRun.__table__.c
    assert er["triggered_by"].type.enums == ["定时", "手动"]
    assert er["triggered_by"].server_default.arg == "定时"
    assert er["dataset_size"].nullable is False

    lcq = LowConfidenceQuestion.__table__.c
    assert lcq["retrieved_chunks"].nullable is True
    # ON DELETE SET NULL 要求可空；改成 NOT NULL 在 MySQL 上加 FK 会失败
    assert lcq["matched_review_id"].nullable is True
    fk = next(iter(lcq["matched_review_id"].foreign_keys))
    assert fk.column.table.name == "review_queue"
    assert fk.ondelete == "SET NULL"
    assert Message.__table__.c["retrieval_snapshot"].nullable is True


def test_mysql_ddl_matches_sql_file():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    from app.models import EvalRun, LowConfidenceQuestion, Message, ReviewQueue

    rq = str(CreateTable(ReviewQueue.__table__).compile(dialect=mysql.dialect()))
    assert "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" in rq
    assert "normalized_question VARCHAR(512) NOT NULL" in rq
    assert "occurrence_count INTEGER UNSIGNED NOT NULL DEFAULT '1'" in rq
    assert "review_status ENUM('待审','通过','驳回') NOT NULL DEFAULT '待审'" in rq
    assert "CHARSET=utf8mb4" in rq

    er = str(CreateTable(EvalRun.__table__).compile(dialect=mysql.dialect()))
    assert "triggered_by ENUM('定时','手动') NOT NULL DEFAULT '定时'" in er
    assert "dataset_size INTEGER UNSIGNED NOT NULL" in er
    assert "metrics JSON" in er

    lcq = str(CreateTable(LowConfidenceQuestion.__table__).compile(dialect=mysql.dialect()))
    assert "retrieved_chunks JSON" in lcq
    assert "matched_review_id BIGINT UNSIGNED" in lcq
    assert "CONSTRAINT fk_lcq_review FOREIGN KEY(matched_review_id) " \
           "REFERENCES review_queue (id) ON DELETE SET NULL" in lcq

    msg = str(CreateTable(Message.__table__).compile(dialect=mysql.dialect()))
    assert "retrieval_snapshot JSON" in msg


def test_sql_file_matches_orm_columns():
    import re

    sql = SQL_FILE.read_text(encoding="utf-8")
    assert "CREATE TABLE review_queue" in sql and "CREATE TABLE eval_runs" in sql
    assert "ADD COLUMN retrieved_chunks JSON NULL" in sql
    assert "ADD COLUMN matched_review_id" in sql
    assert "ADD COLUMN retrieval_snapshot" in sql
    for column in ("normalized_question", "ai_suggested_answer", "occurrence_count",
                   "review_status", "approved_answer"):
        assert re.search(rf"\b{column}\b", sql), column


def test_sql_file_locks_key_ddl_lines():
    """007 里那几行「改了不会有人发现」的 DDL：ENUM 值序、默认值、无符号、FK 动作、ON UPDATE。"""
    sql = SQL_FILE.read_text(encoding="utf-8")
    assert "ENUM('待审','通过','驳回') NOT NULL DEFAULT '待审'" in sql
    assert "ENUM('定时','手动') NOT NULL DEFAULT '定时'" in sql
    assert "ADD COLUMN matched_review_id BIGINT UNSIGNED NULL" in sql
    assert "ON DELETE SET NULL" in sql
    assert "ON UPDATE CURRENT_TIMESTAMP" in sql


def test_migrate_creates_new_tables_once(tmp_path):
    from scripts.migrate_ch09 import migrate

    engine = _engine(tmp_path)
    first = migrate(engine)
    assert sorted(first) == ["表 eval_runs", "表 review_queue"]
    assert migrate(engine) == []          # 第二遍必须如实报「无」
    assert {"review_queue", "eval_runs"} <= set(inspect(engine).get_table_names())
