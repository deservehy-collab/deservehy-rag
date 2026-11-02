import asyncio
import json
import logging

from app.agent.guards import ACTION_LABELS, AgentStream
from app.agent.react import run_react
from app.context import build_model_messages, estimate_tokens, summary_log_lines
from app.knowledge.quality import REFUSAL_TEXT
from app.prompts import AGENT_ADDENDUM, KNOWLEDGE_HINT, POLICY_HINT, TICKET_CONFIRM_TEXT
from app.tools.wiring import build_engine, build_registry, fetch_mcp_specs
from app.workflow.actions import actions_payload, build_offer
from app.workflow.events import emit
from app.workflow.knowledge_node import record_low_confidence

logger = logging.getLogger(__name__)

ORCHESTRATION = "【本轮编排】\n"


def _fallback_plan(state) -> dict:
    """没有服务层计划时（例如单元测试直接跑图），退化成「State 消息原样进层 1」。"""
    rows = []
    messages = [item for item in state.get("messages") or [] if item.type in ("human", "ai")]
    if messages and messages[-1].type == "human":
        messages = messages[:-1]
    for index, item in enumerate(messages):
        try:
            row_id = int(item.id)
        except (TypeError, ValueError):
            row_id = index
        rows.append({"id": row_id, "role": "user" if item.type == "human" else "assistant",
                     "content": str(item.content)})
    return {"summary": "", "layer2": [], "layer1": rows,
            "current_id": state.get("current_message_id") or 0}


def evidence_block(evidence: list[dict]) -> str:
    lines = ["【知识库证据】"]
    for item in evidence:
        lines.append(
            f"[{item['n']}] 小节：{item.get('section_path') or ''}｜问题：{item['question']}\n原文：{item['answer']}"
        )
    lines.append("【证据结束】")
    return "\n".join(lines)


def order_block(order: dict) -> str:
    items = "、".join(f"{item.get('name')}×{item.get('quantity')}"
                     for item in order.get("items") or [])
    return (f"【订单信息】\n订单号：{order.get('order_id')}｜状态：{order.get('status')}｜"
            f"金额：{order.get('amount')}｜下单：{order.get('created_at')}\n商品：{items}")


def trailing_block(route: str, state: dict) -> str:
    """规格 §7：摘要→证据→订单→本轮编排，摘要由 build_model_messages 负责。"""
    if route == "knowledge":
        parts = [evidence_block(state.get("evidence") or []), ORCHESTRATION + KNOWLEDGE_HINT]
    elif route == "policy":
        parts = [evidence_block(state.get("evidence") or []),
                 order_block(state.get("order") or {}), ORCHESTRATION + POLICY_HINT]
    else:
        parts = [ORCHESTRATION + AGENT_ADDENDUM]
    return "\n\n".join(parts)


def _citation(item: dict) -> dict:
    return {"n": item["n"], "chunk_id": item["id"], "question": item["question"],
            "answer": item["answer"], "category": item["category"],
            "section_path": item.get("section_path")}


def _error_reason(outcome) -> str:
    try:
        payload = json.loads(outcome.content)
    except (TypeError, ValueError):
        return outcome.code
    return str(payload.get("reason") or outcome.code)


def _context_snapshot(items: list[dict], limit: int) -> list[dict]:
    """业务路由的 query_faq 不走 retrieve 节点，快照只能从刚返回的 items 现拼。"""
    rows = []
    for index, item in enumerate(items[:max(0, limit)], 1):
        rows.append({"n": index, "chunk_id": item.get("id"),
                     "question": item.get("question"),
                     "answer": (item.get("answer") or "")[:500],
                     "section_path": item.get("section_path"),
                     "scores": dict(item.get("scores") or {})})
    return rows


def make_agent_node(deps):
    async def on_tool_outcome(name, outcome, guard, conversation_id, question, retrieved_chunks=None):
        if name != "query_faq":
            return None
        if not outcome.ok:
            if outcome.code == "low_confidence":
                # 工具把这次召回的完整快照挂在内部字段 `_snapshot` 上（不进模型上下文）；
                # 旧 checkpoint 的 state 快照可能过期，只有工具没给时才退回。
                snapshot = (outcome.raw or {}).get("_snapshot")
                await record_low_confidence(deps.pool, conversation_id, question,
                                            "retrieval_low_conf", _error_reason(outcome),
                                            snapshot or retrieved_chunks,
                                            flywheel=deps.flywheel)
                return REFUSAL_TEXT
            return None
        payload = json.loads(outcome.content)
        data = payload.get("data") or {}
        items = data.get("items") or []
        if items:
            emit("citations", {"items": [_citation(item) for item in items]})
            guard.enable_citations({item["n"] for item in items if isinstance(item.get("n"), int)})
        if deps.selfcheck is not None:
            useful, reason = await asyncio.to_thread(
                deps.selfcheck.check, question, data.get("canonical_query") or "", items)
            if not useful:
                # 工具在原始结果里留了带 scores 的快照；投影后的 items 没有分数，只能当退路。
                snapshot = (outcome.raw or {}).get("_snapshot")
                await record_low_confidence(deps.pool, conversation_id, question,
                                            "self_check", reason or "self_check",
                                            snapshot or _context_snapshot(items, deps.settings.snapshot_top_n),
                                            flywheel=deps.flywheel)
                return REFUSAL_TEXT
        return None

    async def agent_node(state) -> dict:
        conversation_id = state["conversation_id"]
        question = state["question"]
        route = state.get("route") or "agent"
        mcp = getattr(deps, "mcp", None)
        registry = build_registry(deps.store, conversation_id, deps.knowledge, settings=deps.settings,
                                  mcp_specs=await fetch_mcp_specs(mcp),
                                  exclude={"query_faq"} if route in ("knowledge", "policy") else ())
        engine = build_engine(registry, settings=deps.settings,
                              session_factory=deps.store.session_factory)
        resolved = state.get("resolved_question") or question
        plan = state.get("context_plan") or _fallback_plan(state)
        messages = build_model_messages(plan, resolved, trailing_block(route, state), deps.settings)
        logger.info(
            "model_ctx conv=%s turn=%s route=%s layer2=%s layer1=%s window=%s summary_tokens=%s "
            "total_tokens=%s summary=%s messages=%s",
            conversation_id, state.get("turn_count"), route,
            len(plan.get("layer2") or []), len(plan.get("layer1") or []),
            len(plan.get("layer2") or []) + len(plan.get("layer1") or []),
            plan.get("summary_tokens", 0), estimate_tokens(messages),
            summary_log_lines(plan.get("summary_segments")),
            [{"role": m.type, "content": str(m.content)} for m in messages],
        )

        guard = AgentStream()
        if route in ("knowledge", "policy"):
            guard.enable_citations({item["n"] for item in state.get("evidence") or []
                                    if isinstance(item.get("n"), int)})

        last_snapshot: list[dict] = []

        async def hook(name, outcome):
            if name == "query_faq":
                snapshot = (outcome.raw or {}).get("_snapshot")
                if snapshot:
                    # 业务路由不跑检索节点；把这次召回的片段交给 log 节点写进回复行，
                    # 👎 事后回捞才有证据，而不是空快照。
                    last_snapshot[:] = snapshot
            return await on_tool_outcome(name, outcome, guard, conversation_id, question,
                                         state.get("retrieval_snapshot"))

        result = await run_react(
            model=deps.model, registry=registry, engine=engine, messages=messages,
            on_token=lambda text: emit("token", {"content": text}),
            on_status=lambda payload: emit("status", payload),
            max_steps=deps.settings.workflow_agent_max_steps,
            token_budget=deps.settings.workflow_agent_token_budget,
            timeout=deps.settings.request_timeout_seconds,
            guard=guard, on_tool_outcome=hook,
            store=deps.store, conversation_id=conversation_id,
            tool_result_max_tokens=deps.settings.tool_result_max_tokens,
        )

        if result.pending_write:
            # 图马上在 confirm_ticket 挂起，log 节点本轮跑不到：这一轮的正文由这里落库。
            text = result.reply or TICKET_CONFIRM_TEXT
            await asyncio.to_thread(deps.store.add_message, conversation_id, "assistant", content=text)
            if not result.reply:
                emit("token", {"content": text})
            logger.info("workflow node=agent conv=%s paused=confirm_write tool=%s",
                        conversation_id, result.pending_write["tool"])
            update = {"reply": text, "actions": [], "offer": None,
                      "finish_reason": result.finish_reason, "steps": result.steps,
                      "tool_calls": result.tool_calls, "tokens": result.tokens,
                      "stopped_by": result.stopped_by, "pending_write": result.pending_write}
            if last_snapshot:
                # 空值不回写：知识路由的 retrieve 节点已经把快照放进 state，不能在这里清掉。
                update["retrieval_snapshot"] = last_snapshot
            return update

        actions = [name for name in guard.actions.actions if name in ACTION_LABELS]
        if "refund" in actions and not (route == "policy" and state.get("order")):
            actions = [name for name in actions if name != "refund"]
        offer = None
        if actions:
            turn = state.get("turn_count") or 1
            offer = build_offer(turn, actions, question, state.get("intent") or "",
                                state.get("order") if route == "policy" else None)
            emit("actions", actions_payload(actions, turn, offer["refund"]))
        update = {"reply": result.reply, "actions": actions, "offer": offer,
                  "finish_reason": result.finish_reason, "steps": result.steps,
                  "tool_calls": result.tool_calls, "tokens": result.tokens,
                  "stopped_by": result.stopped_by, "pending_write": result.pending_write}
        if last_snapshot:
            # 空值不回写：知识路由的 retrieve 节点已经把快照放进 state，不能在这里清掉。
            update["retrieval_snapshot"] = last_snapshot
        return update

    return agent_node
