from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, LowConfidenceQuestion, TopicClassification

SQL_FILE = Path(__file__).resolve().parents[1] / "scripts/init_sql/008_topics.sql"


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'topic-schema.db'}")


def _seed_pool(session, text="运费怎么算"):
    row = LowConfidenceQuestion(raw_question=text, source="retrieval_low_conf")
    session.add(row)
    session.flush()
    return row.id


def test_table_is_created_in_sqlite_and_columns_match(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    assert "topic_classifications" in inspect(engine).get_table_names()
    columns = {c["name"]: c for c in inspect(engine).get_columns("topic_classifications")}
    assert set(columns) == {"id", "question_id", "labels", "classified_at"}
    assert columns["question_id"]["nullable"] is False
    assert columns["labels"]["nullable"] is False


def test_unique_question_id_and_foreign_key(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        question_id = _seed_pool(session)
        session.add(TopicClassification(question_id=question_id, labels=["运费"]))
        session.commit()
        session.add(TopicClassification(question_id=question_id, labels=["物流"]))
        with pytest.raises(IntegrityError):
            session.commit()


def test_mysql_ddl_matches_user_ddl():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(TopicClassification.__table__).compile(dialect=mysql.dialect()))
    assert "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" in ddl
    assert "labels JSON NOT NULL" in ddl
    assert "classified_at DATETIME NOT NULL" in ddl
    assert "CONSTRAINT uk_question_id UNIQUE (question_id)" in ddl
    assert "CONSTRAINT fk_topic_question FOREIGN KEY(question_id) " \
           "REFERENCES low_confidence_questions (id)" in ddl
    assert "CHARSET=utf8mb4" in ddl


def test_sql_file_contains_user_ddl_lines():
    sql = SQL_FILE.read_text(encoding="utf-8")
    assert "CREATE TABLE topic_classifications" in sql
    assert "UNIQUE KEY uk_question_id (question_id)" in sql
    assert "CONSTRAINT fk_topic_question FOREIGN KEY (question_id)" in sql
    assert "REFERENCES low_confidence_questions (id)" in sql
    assert "SET NAMES utf8mb4;" in sql
    assert "COMMENT='主题分类结果'" in sql


def test_migrate_is_idempotent(tmp_path):
    from scripts.migrate_ch10 import migrate

    engine = _engine(tmp_path)
    assert migrate(engine) == ["表 topic_classifications"]
    assert migrate(engine) == []
