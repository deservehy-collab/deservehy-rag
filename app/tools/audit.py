import logging
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from app.models import ToolAuditLog

logger = logging.getLogger(__name__)

MAX_SUMMARY_COLUMN = 2000
MAX_ERROR_COLUMN = 512
STATUSES = frozenset({"成功", "失败", "超时", "校验拦下", "权限拒绝"})


@dataclass
class AuditRecord:
    tool: str
    source: str
    mcp_server: str | None
    arguments: dict
    status: str
    summary: str
    error: str | None
    retry_count: int
    duration_ms: int
    tool_call_id: str | None
    conversation_id: int | None


class ToolAuditWriter:
    """审计是留痕不是闸门：任何失败只记日志，工具调用结果照常返回（规格 §15）。"""

    def __init__(self, session_factory, *, enabled: bool = True) -> None:
        self.session_factory = session_factory
        self.enabled = enabled

    def record(self, item: AuditRecord) -> None:
        if not self.enabled or self.session_factory is None:
            return
        if item.status not in STATUSES:
            logger.warning("工具审计写入失败（未知状态）：tool=%s status=%r", item.tool, item.status)
            return
        for attempt in (1, 2):
            try:
                # 每次尝试都重建行对象：commit 阶段失败过的实例会带着 identity 变 detached，
                # 再 add 会被当成 UPDATE，第二次必抛 StaleDataError（评审实测）。
                row = self._row(item, drop_arguments=attempt == 2)
            except Exception:
                logger.warning("工具审计写入失败（字段无法落库）：tool=%s status=%s",
                               item.tool, item.status, exc_info=True)
                return
            try:
                committed = False
                with self.session_factory() as session:
                    session.add(row)
                    session.commit()
                    committed = True
                return
            except SQLAlchemyError:
                # commit 已经成功、只是 session 收尾时抛错的话不能再插一次：那会留两行审计。
                if committed:
                    return
                logger.warning("工具审计写入失败（数据库）：tool=%s status=%s attempt=%s",
                               item.tool, item.status, attempt, exc_info=True)
            except Exception:
                logger.warning("工具审计写入失败：tool=%s status=%s", item.tool, item.status, exc_info=True)
                return

    @staticmethod
    def _row(item: AuditRecord, *, drop_arguments: bool = False) -> ToolAuditLog:
        if item.source in ("builtin", "mcp"):
            source = item.source
        else:
            source = "mcp" if item.mcp_server else "builtin"
        return ToolAuditLog(
            conversation_id=item.conversation_id,
            tool_call_id=(item.tool_call_id or None) and str(item.tool_call_id)[:64],
            tool_name=item.tool[:128],
            tool_source=source,
            mcp_server=(item.mcp_server or None) and str(item.mcp_server)[:64],
            arguments=None if drop_arguments else (item.arguments if isinstance(item.arguments, dict) else None),
            result_summary=(item.summary or "")[:MAX_SUMMARY_COLUMN] or None,
            status=item.status,
            error_message=(item.error or None) and str(item.error)[:MAX_ERROR_COLUMN],
            retry_count=max(0, int(item.retry_count or 0)),
            duration_ms=max(0, int(item.duration_ms or 0)),
        )
