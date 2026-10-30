import asyncio
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.guards import ACTION_LABELS, AgentStream
from app.agent.react import run_react
from app.knowledge.quality import REFUSAL_TEXT
from app.prompts import AGENT_ADDENDUM, KNOWLEDGE_HINT, POLICY_HINT
from app.tools import ToolExecutor, make_tools
from app.workflow.actions import actions_payload, build_offer
from app.workflow.events import emit
from app.workflow.knowledge_node import record_low_confidence

logger = logging.getLogger(__name__)


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
        messages = list(state["messages"])
        resolved = state.get("resolved_question") or question
        if resolved != question:
            # 指代消解的结果必须进入模型上下文；否则选单后的「订单 1001」会被答成订单查询。
            for index in range(len(messages) - 1, -1, -1):
                if isinstance(messages[index], HumanMessage):
                    messages[index] = HumanMessage(content=resolved)
                    break
        base = messages[0].content if messages else ""
        extra = KNOWLEDGE_HINT + "\n" + evidence_block(state.get("evidence") or []) \
            if route == "knowledge" else AGENT_ADDENDUM
        if route == "policy":
            extra = (POLICY_HINT + "\n" + order_block(state.get("order") or {}) + "\n"
                     + evidence_block(state.get("evidence") or []))
        messages[0] = SystemMessage(content=f"{base}\n\n{extra}")

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
