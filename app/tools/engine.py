import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.tools.audit import AuditRecord
from app.tools.formatting import MAX_SUMMARY, format_content, normalize_result, summarize
from app.tools.policy import decide
from app.tools.registry import ToolRegistry
from app.tools.validate import validate_arguments

logger = logging.getLogger(__name__)

WRITE_UNKNOWN_RESULT = "写操作结果未知：可能已经执行，请勿重复提交，稍后人工核对。"


@dataclass(frozen=True)
class ToolOutcome:
    """`ok=False` 有两种：业务空结果（code 是业务码，如 not_found）与执行失败
    （code 是 tool_error/tool_timeout）。调用方判「这轮工具是否失败」看 code，不要只看 ok。"""

    tool: str
    ok: bool
    code: str
    content: str
    summary: str
    duration_ms: int
    source: str = "builtin"
    mcp_server: str | None = None
    retry_count: int = 0
    audit_status: str = "成功"
    arguments: dict = field(default_factory=dict)
    tool_call_id: str | None = None


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class ToolEngine:
    """所有工具调用的唯一出口：查表 → 校验 → 权限 → 执行(超时/重试) → 格式化 → 审计。"""

    def __init__(self, registry: ToolRegistry, *, settings, audit=None) -> None:
        self.registry = registry
        self.settings = settings
        self.audit = audit
        self.timeouts = {"query_faq": settings.knowledge_tool_timeout_seconds}

    def _timeout_for(self, spec) -> float:
        return self.timeouts.get(spec.name, self.settings.tool_timeout_seconds)

    async def _call(self, spec, args: dict, call_id: str, limit: float):
        payload = {"type": "tool_call", "name": spec.name, "args": dict(args), "id": call_id}
        return await asyncio.wait_for(spec.tool.ainvoke(payload), limit)

    async def run(self, *, name: str, args: dict, tool_call_id: str | None = None,
                  conversation_id: int | None = None, confirmed: bool = False) -> ToolOutcome:
        started = time.perf_counter()
        args = dict(args or {})
        call_id = tool_call_id or f"engine-{uuid4().hex[:12]}"
        spec = self.registry.get(name)
        if spec is None:
            logger.warning("工具未登记：%s", name)
            return self._skip(name, "unknown_tool", f"未知工具 {name}", "失败", started, args)

        try:
            report = validate_arguments(spec.json_schema, args)
        except Exception:
            logger.exception("工具参数定义非法，无法校验：%s", name)
            return await self._reject(spec, "invalid_arguments", "这个工具的参数定义有问题，暂时无法调用。",
                                      "校验拦下", started, args, call_id, conversation_id)
        if not report.ok:
            return await self._reject(spec, "invalid_arguments", report.message, "校验拦下", started, args,
                                      call_id, conversation_id)

        decision = decide(spec, confirmed=confirmed, settings=self.settings)
        if not decision.allowed:
            if decision.needs_confirm:
                return self._skip(spec.name, "confirm_required",
                                  "该操作需要用户在页面上确认后才能执行。", "confirm_required",
                                  started, args, spec=spec, call_id=call_id)
            return await self._reject(spec, "permission_denied", decision.reason, "权限拒绝", started, args,
                                      call_id, conversation_id)

        write = spec.permission == "write"
        attempts = 1 if write else self.settings.tool_max_attempts
        limit = self._timeout_for(spec)
        code, message, result = "tool_error", "工具执行失败，请稍后重试。", None
        tries = 0
        for tries in range(1, attempts + 1):
            try:
                result = normalize_result(await self._call(spec, args, call_id, limit))
            except TimeoutError:
                # 写工具跑在 executor 线程里，取消 await 并不等于没执行，只能如实报「结果未知」。
                code = "tool_timeout"
                message = WRITE_UNKNOWN_RESULT if write else "工具响应超时，请稍后重试。"
            except asyncio.CancelledError:
                cancelled = WRITE_UNKNOWN_RESULT if write else "调用被取消，请稍后重试。"
                # 取消这一支只能同步落审计：任务已经在取消中，线程化的 await 会再被取消一次，留痕就丢了。
                self._audit(self._failure_outcome(spec, args, "tool_error", cancelled, started,
                                                  tries - 1, call_id),
                            call_id, conversation_id, error=cancelled)
                raise
            except ValidationError as exc:          # JSON Schema 表达不了的跨字段规则落这里
                return await self._reject(spec, "invalid_arguments",
                                          "参数校验未通过：" + _pydantic_text(exc) + "。请向用户确认后重新调用。",
                                          "校验拦下", started, args, call_id, conversation_id,
                                          retry_count=tries - 1)
            except SQLAlchemyError:
                code = "tool_error"
                message = WRITE_UNKNOWN_RESULT if write else "数据库暂时不可用，请稍后重试。"
            except Exception:
                logger.exception("工具执行异常：%s", spec.name)
                code = "tool_error"
                message = WRITE_UNKNOWN_RESULT if write else "工具执行失败，请稍后重试。"
            else:
                try:
                    outcome = self._success(spec, args, result, started, tries - 1, call_id)
                except Exception:
                    # 工具已经跑完了，坏在整理结果：绝不能重跑（写操作会执行两遍），如实报失败。
                    logger.exception("工具结果整理失败：%s", spec.name)
                    return await self._failure(
                        spec, args, "tool_error",
                        WRITE_UNKNOWN_RESULT if write else "工具结果无法整理，请稍后重试。",
                        started, tries - 1, call_id, conversation_id)
                await self._audit_off_thread(outcome, call_id, conversation_id,
                                             error=None if outcome.ok else outcome.summary)
                return outcome
        return await self._failure(spec, args, code, message, started, tries - 1, call_id, conversation_id)

    def _success(self, spec, args, result, started, retry_count, tool_call_id) -> ToolOutcome:
        payload = format_content(result, spec)
        if isinstance(result, dict) and result.get("ok") is False:
            message = str(result.get("message") or "工具没有返回结果。")
            return ToolOutcome(tool=spec.name, ok=False, code=str(result.get("error") or "tool_error"),
                               content=payload, summary=message[:MAX_SUMMARY],
                               duration_ms=_elapsed(started), source=spec.source,
                               mcp_server=spec.mcp_server, retry_count=retry_count,
                               audit_status="失败", arguments=dict(args),
                               tool_call_id=tool_call_id)
        return ToolOutcome(tool=spec.name, ok=True, code="ok", content=payload,
                           summary=summarize(spec, result), duration_ms=_elapsed(started),
                           source=spec.source, mcp_server=spec.mcp_server,
                           retry_count=retry_count, audit_status="成功", arguments=dict(args),
                           tool_call_id=tool_call_id)

    async def _failure(self, spec, args, code, message, started, retry_count, tool_call_id, conversation_id):
        outcome = self._failure_outcome(spec, args, code, message, started, retry_count, tool_call_id)
        await self._audit_off_thread(outcome, tool_call_id, conversation_id, error=message)
        return outcome

    def _failure_outcome(self, spec, args, code, message, started, retry_count, tool_call_id) -> ToolOutcome:
        return ToolOutcome(tool=spec.name, ok=False, code=code, content=_json_error(code, message),
                           summary=message[:MAX_SUMMARY], duration_ms=_elapsed(started),
                           source=spec.source, mcp_server=spec.mcp_server, retry_count=retry_count,
                           audit_status="超时" if code == "tool_timeout" else "失败",
                           arguments=dict(args), tool_call_id=tool_call_id)

    async def _reject(self, spec, code, message, status, started, args, call_id, conversation_id, *,
                      retry_count=0):
        outcome = ToolOutcome(tool=spec.name, ok=False, code=code, content=_json_error(code, message),
                              summary=message[:MAX_SUMMARY], duration_ms=_elapsed(started),
                              source=spec.source, mcp_server=spec.mcp_server, retry_count=retry_count,
                              audit_status=status, arguments=dict(args), tool_call_id=call_id)
        await self._audit_off_thread(outcome, call_id, conversation_id, error=message)
        return outcome

    def record_denial(self, *, name: str, args: dict, reason: str,
                      tool_call_id: str | None = None, conversation_id: int | None = None) -> None:
        """用户在卡片上点了「取消」：调用没发生，但第 5 条要求留痕——按「权限拒绝」落一行。"""
        spec = self.registry.get(name)
        if spec is None or spec.permission != "write":
            return
        call_id = tool_call_id or f"denied-{uuid4().hex[:12]}"
        outcome = self._skip(name, "permission_denied", reason, "权限拒绝",
                             time.perf_counter(), args, spec=spec, call_id=call_id)
        self._audit(outcome, call_id, conversation_id, error=reason)

    def _skip(self, key, code, message, status, started, args, *, spec=None, call_id=None):
        return ToolOutcome(tool=key, ok=False, code=code, content=_json_error(code, message),
                           summary=message[:MAX_SUMMARY], duration_ms=_elapsed(started),
                           source=(spec.source if spec else "unknown"),
                           mcp_server=(spec.mcp_server if spec else None), retry_count=0,
                           audit_status=status, arguments=dict(args or {}),
                           tool_call_id=call_id)

    async def _audit_off_thread(self, outcome, call_id, conversation_id, *, error) -> None:
        """审计是一次同步 INSERT+commit：热路径一律挪进线程，别把整条 SSE loop 卡在这一往返上。"""
        await asyncio.to_thread(self._audit, outcome, call_id, conversation_id, error=error)

    def _audit(self, outcome, call_id, conversation_id, *, error) -> None:
        # 只有真正放行过的调用会走到这里：未知工具与 confirm_required 在 run/record_denial 里早退，
        # 不再落库（§5 映射表：那次调用根本没执行）。
        if self.audit is None:
            return
        try:
            self.audit.record(AuditRecord(
                tool=outcome.tool, source=outcome.source, mcp_server=outcome.mcp_server,
                arguments=outcome.arguments, status=outcome.audit_status, summary=outcome.summary,
                error=error, retry_count=outcome.retry_count, duration_ms=outcome.duration_ms,
                tool_call_id=call_id, conversation_id=conversation_id,
            ))
        except Exception:
            logger.warning("审计写入失败，工具结果照常返回：tool=%s", outcome.tool, exc_info=True)


def _json_error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=False)


_TYPE_HINTS = {
    "missing": "缺少必填项",
    "string_too_short": "长度太短",
    "string_too_long": "长度太长",
    "string_type": "应为字符串",
    "int_type": "应为整数",
    "int_parsing": "无法解析为整数",
    "float_parsing": "无法解析为数字",
    "bool_type": "应为布尔值",
    "extra_forbidden": "带了多余的参数",
}


def _pydantic_text(exc: ValidationError) -> str:
    parts = []
    for item in exc.errors():
        loc = "、".join(str(x) for x in item.get("loc", ())) or "参数"
        hint = _TYPE_HINTS.get(str(item.get("type") or ""))
        msg = str(item.get("msg") or "").removeprefix("Value error, ").strip().rstrip("。")
        detail = f"{hint}：{msg}" if hint and msg and msg != hint else (msg or hint or "不符合校验规则")
        parts.append(f"{loc}：{detail}")
    return "；".join(parts)[:400]
