import asyncio
import json
import logging

from app.agent.guards import ACTION_LABELS, AgentStream
from app.agent.react import run_react
from app.context import build_model_messages, estimate_tokens, summary_log_lines
from app.knowledge.quality import REFUSAL_TEXT
from app.prompts import AGENT_ADDENDUM, KNOWLEDGE_HINT, POLICY_HINT
from app.tools import ToolExecutor, make_tools
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


def make_agent_node(deps):
    async def on_tool_outcome(name, outcome, guard, conversation_id, question):
        if name != "query_faq":
            return None
        if not outcome.ok:
            if outcome.code == "low_confidence":
                await record_low_confidence(deps.pool, conversation_id, question,
                                            "retrieval_low_conf", _error_reason(outcome))
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
                await record_low_confidence(deps.pool, conversation_id, question,
                                            "self_check", reason or "self_check")
                return REFUSAL_TEXT
        return None

    async def agent_node(state) -> dict:
        conversation_id = state["conversation_id"]
        question = state["question"]
        route = state.get("route") or "agent"
        tools = make_tools(deps.store, conversation_id, deps.knowledge)
        if route in ("knowledge", "policy"):
            tools.pop("query_faq", None)
        executor = ToolExecutor(
            tools, timeout=deps.settings.tool_timeout_seconds, attempts=deps.settings.tool_max_attempts,
            timeouts={"query_faq": deps.settings.knowledge_tool_timeout_seconds},
        )
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

        async def hook(name, outcome):
            return await on_tool_outcome(name, outcome, guard, conversation_id, question)

        result = await run_react(
            model=deps.model, tools=tools, executor=executor, messages=messages,
            on_token=lambda text: emit("token", {"content": text}),
            on_status=lambda payload: emit("status", payload),
            max_steps=deps.settings.workflow_agent_max_steps,
            token_budget=deps.settings.workflow_agent_token_budget,
            timeout=deps.settings.request_timeout_seconds,
            guard=guard, on_tool_outcome=hook,
            store=deps.store, conversation_id=conversation_id,
            tool_result_max_tokens=deps.settings.tool_result_max_tokens,
        )

        actions = [name for name in guard.actions.actions if name in ACTION_LABELS]
        if "refund" in actions and not (route == "policy" and state.get("order")):
            actions = [name for name in actions if name != "refund"]
        offer = None
        if actions:
            turn = state.get("turn_count") or 1
            offer = build_offer(turn, actions, question, state.get("intent") or "",
                                state.get("order") if route == "policy" else None)
            emit("actions", actions_payload(actions, turn, offer["refund"]))
        return {"reply": result.reply, "actions": actions, "offer": offer,
                "finish_reason": result.finish_reason, "steps": result.steps,
                "tool_calls": result.tool_calls, "tokens": result.tokens,
                "stopped_by": result.stopped_by}

    return agent_node
