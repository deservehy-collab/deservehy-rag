import asyncio
import json
import logging

from langgraph.types import interrupt

from app.prompts import (TICKET_CANCELLED_TEXT, TICKET_CREATED_TEXT, TICKET_FAILED_TEXT,
                         TICKET_TIMEOUT_TEXT)
from app.tools.builtin import TOOL_LABELS
from app.tools.wiring import build_engine, build_registry, fetch_mcp_specs
from app.workflow.events import emit

logger = logging.getLogger(__name__)

CANCELLATION_REASON = "用户在预览卡片取消了这次操作。"
FIELD_LABELS = {"ticket_type": "工单类型", "description": "问题描述"}


def write_preview(state) -> dict:
    """卡片只展示已经确定的东西：参数原样列出来，外加这一轮的会话与订单。"""
    pending = state.get("pending_write") or {}
    args = pending.get("args") or {}
    fields = [{"label": FIELD_LABELS.get(key, key), "value": str(value)} for key, value in args.items()]
    order = state.get("order") or {}
    if order.get("order_id"):
        fields.append({"label": "关联订单", "value": str(order["order_id"])})
    fields.append({"label": "所属会话", "value": f"#{state.get('conversation_id')}"})
    return {"pending_id": pending.get("pending_id"), "tool": pending.get("tool"),
            "tool_call_id": pending.get("tool_call_id"),
            # 中文名只有一个来源：react 那次调用查到的 spec.label，卡片与徽章才不会各叫各的。
            "label": pending.get("label") or TOOL_LABELS.get(pending.get("tool") or "",
                                                             pending.get("tool") or "写操作"),
            "turn": state.get("turn_count") or 1, "question": state.get("question") or "",
            "fields": fields, "note": "确认后才会真正执行，取消则什么都不会发生。"}


def make_confirm_ticket_node():
    def confirm_ticket(state) -> dict:
        # resume 时这个节点从头重跑：除了这一句 interrupt，这里什么都不许做。
        decision = interrupt(write_preview(state))
        return {"write_decision": str(decision or "cancel")}

    return confirm_ticket


def agent_selector(state) -> str:
    return "confirm" if state.get("pending_write") else "log"


def decision_selector(state) -> str:
    return "execute" if state.get("write_decision") == "accept" else "cancel"


async def _engine(deps, conversation_id):
    """写操作执行时要的注册表／引擎只能现搭：内置工具是按会话闭包生成的（规格 §6.2）。"""
    registry = build_registry(deps.store, conversation_id, deps.knowledge, settings=deps.settings,
                              mcp_specs=await fetch_mcp_specs(getattr(deps, "mcp", None)))
    return registry, build_engine(registry, settings=deps.settings, session_factory=deps.store.session_factory)


def make_execute_write_node(deps):
    async def execute_write(state) -> dict:
        pending = state.get("pending_write") or {}
        conversation_id = state["conversation_id"]
        name = pending.get("tool") or ""
        registry, engine = await _engine(deps, conversation_id)
        spec = registry.get(name)
        label = spec.label if spec else name
        source = spec.source if spec else "unknown"
        server = spec.mcp_server if spec else None
        emit("status", {"phase": "start", "tool": name, "label": label,
                        "source": source, "mcp_server": server})
        outcome = await engine.run(name=name, args=pending.get("args") or {},
                                   tool_call_id=pending.get("tool_call_id"),
                                   conversation_id=conversation_id, confirmed=True)
        ticket_no = _ticket_no(outcome.content) if outcome.ok else None
        # 徽章与正文必须共用一个判据：引擎说成功但拿不到工单号，也不能亮绿勾说「已创建工单」。
        emit("status", {"phase": "end", "tool": name, "label": label, "ok": bool(ticket_no),
                        "summary": outcome.summary, "duration_ms": outcome.duration_ms,
                        "source": source, "mcp_server": server})
        reply = TICKET_CREATED_TEXT.format(ticket_no=ticket_no) if ticket_no else TICKET_FAILED_TEXT
        stopped_by = None if ticket_no else "write_failed"
        if not ticket_no and outcome.code == "tool_timeout":
            # 写工具跑在 executor 线程里，超时不等于没执行：文案和状态都必须说「结果未知」，
            # 否则用户会以为一定失败，回头又建一张重复工单。
            reply, stopped_by = TICKET_TIMEOUT_TEXT, "write_timeout"
        logger.info("workflow node=execute_write conv=%s tool=%s engine_ok=%s code=%s ticket=%s",
                    conversation_id, name, outcome.ok, outcome.code, ticket_no)
        emit("token", {"content": reply})
        return {"pending_write": None, "reply": reply, "finish_reason": "stop",
                "stopped_by": stopped_by}

    return execute_write


def make_write_cancelled_node(deps):
    async def write_cancelled(state) -> dict:
        pending = state.get("pending_write") or {}
        conversation_id = state["conversation_id"]
        _, engine = await _engine(deps, conversation_id)
        # 取消留痕是一次同步 commit，留在事件循环里会把整个 SSE loop 堵住的。
        await asyncio.to_thread(engine.record_denial, name=pending.get("tool") or "",
                                args=pending.get("args") or {}, reason=CANCELLATION_REASON,
                                tool_call_id=pending.get("tool_call_id"), conversation_id=conversation_id)
        logger.info("workflow node=write_cancelled conv=%s tool=%s", conversation_id, pending.get("tool"))
        emit("token", {"content": TICKET_CANCELLED_TEXT})
        return {"pending_write": None, "reply": TICKET_CANCELLED_TEXT, "finish_reason": "stop",
                "stopped_by": "cancelled_by_user"}

    return write_cancelled


def _ticket_no(content) -> str | None:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data.get("ticket_no") or None
