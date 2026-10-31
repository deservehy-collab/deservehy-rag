import re
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Base, ToolAuditLog

ROOT = Path(__file__).resolve().parents[1]


def test_settings_expose_ch08_keys_with_defaults():
    settings = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1",
                        openai_model="m", openai_api_key="k")
    assert settings.mcp_servers == "logistics,aftersale"
    assert settings.mcp_logistics_url == "http://127.0.0.1:8931/mcp"
    assert settings.mcp_aftersale_url == "http://127.0.0.1:8932/mcp"
    assert settings.mcp_logistics_permission == "read"
    assert settings.mcp_aftersale_permission == "read"
    assert settings.tool_write_allowlist == "create_ticket"
    assert settings.tool_extra_modules == ""
    assert settings.tool_audit_enabled is True
    assert settings.tool_discovery_cache_seconds == 60
    assert settings.tool_schema_reserve_tokens == 400


def test_tool_audit_log_columns_and_no_foreign_key():
    table = ToolAuditLog.__table__
    columns = {item.name: str(item.type) for item in table.columns}
    assert set(columns) == {"id", "conversation_id", "tool_call_id", "tool_name", "tool_source",
                            "mcp_server", "arguments", "result_summary", "status",
                            "error_message", "retry_count", "duration_ms", "created_at"}
    assert columns["tool_name"] == "VARCHAR(128)"
    assert columns["tool_source"] == "VARCHAR(7)"
    assert columns["arguments"] == "JSON"
    assert columns["retry_count"] == "INTEGER"
    assert columns["duration_ms"] == "INTEGER"
    assert columns["created_at"] == "DATETIME"
    assert table.columns["conversation_id"].nullable
    assert table.columns["arguments"].nullable
    assert not table.columns["tool_name"].nullable
    assert not table.columns["status"].nullable
    assert not table.columns["retry_count"].nullable
    assert table.foreign_keys == set()
    # SQLAlchemy 2.1.6 的 Enum.type.enums 是 list[str]（不是带 .status 的对象）
    assert set(table.columns["status"].type.enums) == {"成功", "失败", "超时", "校验拦下", "权限拒绝"}
    assert {(idx.name, idx.columns[0].name) for idx in table.indexes} == {
        ("idx_conversation_id", "conversation_id"),
        ("idx_tool_name", "tool_name"),
        ("idx_status", "status"),
    }


def _audit_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    return engine


def test_sqlite_roundtrip_keeps_json_arguments_and_defaults(tmp_path):
    engine = _audit_engine(tmp_path)
    with Session(engine) as session:
        session.add(ToolAuditLog(conversation_id=7, tool_call_id="call_1", tool_name="query_logistics",
                                 tool_source="mcp", mcp_server="logistics",
                                 arguments={"order_id": "1001", "备注": "用户原话"},
                                 result_summary="2 条物流记录", status="成功", error_message=None))
        session.commit()
        row = session.scalars(select(ToolAuditLog)).one()
        # JSON 列原样回来（含中文不转义），两个计数列走 DDL 默认值而不是 Python 默认值
        assert row.arguments == {"order_id": "1001", "备注": "用户原话"}
        assert (row.retry_count, row.duration_ms) == (0, 0)
        assert row.created_at is not None


def test_006_sql_matches_orm_columns_and_indexes():
    ddl = (ROOT / "scripts" / "init_sql" / "006_tools.sql").read_text(encoding="utf-8")
    body = ddl.split("CREATE TABLE tool_audit_logs (", 1)[1].split(") ENGINE=InnoDB", 1)[0]
    ddl_columns = set(re.findall(r"^\s{2}([a-z_]+)\s+[A-Z]", body, re.MULTILINE))
    ddl_indexes = dict(re.findall(r"KEY (\w+) \((\w+)\)", body))
    table = ToolAuditLog.__table__

    assert ddl_columns == {item.name for item in table.columns}
    assert ddl_indexes == {idx.name: idx.columns[0].name for idx in table.indexes}
    assert "FOREIGN KEY" not in ddl.upper() and "REFERENCES" not in ddl.upper()


def test_status_enum_is_native_and_limited_to_the_five_documented_states():
    """生产库（MySQL）用原生 ENUM 拦住第六种状态；SQLite 侧实测只渲染成 VARCHAR(4) NOT NULL、
    没有 CHECK——所以执行引擎（T8）不能指望数据库帮忙校验状态值。"""
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(ToolAuditLog.__table__).compile(dialect=mysql.dialect()))
    assert "status ENUM('成功','失败','超时','校验拦下','权限拒绝') NOT NULL" in ddl
    assert "retry_count INTEGER NOT NULL DEFAULT 0" in ddl
    assert "duration_ms INTEGER NOT NULL DEFAULT 0" in ddl
    assert "FOREIGN KEY" not in ddl.upper()


def test_engine_insert_bypassing_python_defaults_still_satisfies_ddl(tmp_path):
    """裸 SQL 省略 retry_count/duration_ms 也要能落库：MySQL strict 模式下没有
    server_default 会报 1364，而审计失败不许拦住工具执行（规格 §5）。"""
    engine = _audit_engine(tmp_path)
    conn = engine.raw_connection()
    try:
        conn.cursor().execute(
            "INSERT INTO tool_audit_logs (tool_name, tool_source, status) VALUES (?,?,?)",
            ("create_ticket", "builtin", "权限拒绝"),
        )
        conn.commit()
        row = conn.cursor().execute("SELECT retry_count, duration_ms FROM tool_audit_logs").fetchone()
        assert tuple(row) == (0, 0)
    finally:
        conn.close()
