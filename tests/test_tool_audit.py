import pytest
from sqlalchemy import create_engine, select

from app.database import build_session_factory
from app.models import Base, ToolAuditLog
from app.tools.audit import AuditRecord, ToolAuditWriter


@pytest.fixture()
def writer(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield ToolAuditWriter(factory), factory
    engine.dispose()


def record(**overrides):
    payload = {"tool": "query_order", "source": "builtin", "mcp_server": None,
               "arguments": {"order_id": "1001"}, "status": "成功", "summary": "订单 1001 已发货",
               "error": None, "retry_count": 0, "duration_ms": 12,
               "tool_call_id": "call_1", "conversation_id": 7}
    payload.update(overrides)
    return AuditRecord(**payload)


def test_every_call_lands_exactly_one_row(writer):
    audit, factory = writer
    for status in ("成功", "失败", "超时", "校验拦下", "权限拒绝"):
        audit.record(record(status=status))
    with factory() as session:
        rows = list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))
    assert [row.status for row in rows] == ["成功", "失败", "超时", "校验拦下", "权限拒绝"]
    assert rows[0].tool_source == "builtin" and rows[0].arguments == {"order_id": "1001"}
    assert rows[0].created_at is not None


def test_mcp_rows_carry_the_server_name(writer):
    audit, factory = writer
    audit.record(record(source="mcp", mcp_server="logistics", tool="query_logistics"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (row.tool_source, row.mcp_server) == ("mcp", "logistics")


def test_null_conversation_is_allowed(writer):
    audit, factory = writer
    audit.record(record(conversation_id=None, tool_call_id=None))
    with factory() as session:
        assert session.scalars(select(ToolAuditLog)).one().conversation_id is None


def test_disabled_writer_writes_nothing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'off.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    ToolAuditWriter(factory, enabled=False).record(record())
    with factory() as session:
        assert list(session.scalars(select(ToolAuditLog))) == []
    engine.dispose()


def test_failure_to_write_never_raises(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad.db'}")
    Base.metadata.create_all(engine)

    class Broken:
        def __call__(self):
            raise RuntimeError("库挂了")

    with caplog.at_level("WARNING"):
        ToolAuditWriter(Broken()).record(record())        # 不许抛
        ToolAuditWriter(None).record(record())            # 没 factory 也不许抛
    assert "审计写入失败" in caplog.text
    engine.dispose()


def test_database_error_is_swallowed_and_retried_once(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.db'}")   # 故意不建表
    factory = build_session_factory(engine)
    calls = []

    def counting_factory():
        calls.append(1)
        return factory()

    with caplog.at_level("WARNING"):
        ToolAuditWriter(counting_factory).record(record())
    assert len(calls) == 2
    assert "attempt=2" in caplog.text
    engine.dispose()


def test_long_summary_and_error_are_truncated(writer):
    audit, factory = writer
    audit.record(record(summary="长" * 5000, error="错" * 5000))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert len(row.result_summary) <= 2000
    assert len(row.error_message) <= 512


def test_full_row_round_trips_every_operational_field(writer):
    """T17 要按这些列核对验收，默认值能落库不代表字段真写进去了。"""
    audit, factory = writer
    audit.record(record(source="mcp", mcp_server="logistics", tool="query_logistics",
                        conversation_id=42, tool_call_id="call_42", retry_count=2,
                        duration_ms=321, summary="2 条物流记录，最新：已揽收"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (row.tool_name, row.tool_source, row.mcp_server) == ("query_logistics", "mcp", "logistics")
    assert (row.conversation_id, row.tool_call_id) == (42, "call_42")
    assert (row.retry_count, row.duration_ms) == (2, 321)
    assert row.result_summary == "2 条物流记录，最新：已揽收"


def test_source_is_inferred_from_server_when_the_source_string_is_unknown(writer):
    audit, factory = writer
    audit.record(record(source="外部", mcp_server="aftersale"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert row.tool_source == "mcp"


def test_unserialisable_arguments_are_dropped_on_the_retry_so_the_status_row_survives(writer):
    audit, factory = writer
    audit.record(record(arguments={"x": object()}, summary="长" * 5000))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert row.arguments is None and row.status == "成功"
    assert len(row.result_summary) == 2000          # 降级只丢载荷，不丢摘要


def test_string_columns_are_clamped_to_their_declared_widths(writer):
    audit, factory = writer
    audit.record(record(tool="t" * 200, tool_call_id="c" * 100, mcp_server="m" * 100,
                        source="mcp", error="e" * 1000, summary="短"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (len(row.tool_name), len(row.tool_call_id), len(row.mcp_server)) == (128, 64, 64)
    assert len(row.error_message) == 512


def test_unknown_status_is_dropped_with_a_warning(writer, caplog):
    """MySQL 的 status 是 ENUM：写进去只会整行失败，重试也救不回，不如在本地就拦住。"""
    audit, factory = writer
    with caplog.at_level("WARNING"):
        audit.record(record(status="乱写"))
    assert rows_of(factory) == []
    assert "未知状态" in caplog.text


def rows_of(factory):
    with factory() as session:
        return list(session.scalars(select(ToolAuditLog)))


def test_commit_phase_failure_rebuilds_the_row_and_lands_once(tmp_path):
    """commit 阶段失败过的实例带着 identity 变 detached，复用它会变成 UPDATE 并抛 StaleDataError（评审实测）。"""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'commit.db'}")
    Base.metadata.create_all(engine)
    state = {"first": True}

    class FlakySession(Session):
        def commit(self):
            if state["first"]:
                state["first"] = False
                super().flush()                      # 行已 INSERT、主键已回填，随后 commit 失败
                raise OperationalError("commit", {}, Exception("连接断了"))
            return super().commit()

    class FlakyFactory:
        def __call__(self):
            return FlakySession(bind=engine)

    ToolAuditWriter(FlakyFactory()).record(record())
    with engine.connect() as connection:
        count = connection.execute(select(ToolAuditLog)).all()
    assert len(count) == 1
    engine.dispose()


def test_none_counters_fall_back_to_zero(writer):
    audit, factory = writer
    audit.record(record(retry_count=None, duration_ms=None))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (row.retry_count, row.duration_ms) == (0, 0)


def test_a_committed_row_is_not_inserted_twice_when_the_session_close_fails(tmp_path):
    """commit 已经成功、只是 session 收尾抛错：这时再「重试一次」就是同一次调用落两行审计（评审 Minor 1）。"""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'close.db'}")
    Base.metadata.create_all(engine)
    commits = []

    class ClosingBadly(Session):
        def commit(self):
            commits.append(1)
            return super().commit()

        def close(self):
            super().close()
            raise OperationalError("close", {}, Exception("连接回收失败"))

    class Factory:
        def __call__(self):
            return ClosingBadly(bind=engine)

    ToolAuditWriter(Factory()).record(record())
    assert len(commits) == 1                          # 没有第二次尝试
    with engine.connect() as connection:
        assert len(connection.execute(select(ToolAuditLog)).all()) == 1
    engine.dispose()
