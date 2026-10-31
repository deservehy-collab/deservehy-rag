import asyncio
import json
import logging
import re
import time

from app.tools import list_orders
from app.tools.wiring import build_engine, build_registry
from app.workflow.events import emit

logger = logging.getLogger(__name__)

MISSING_ORDER_TEXT = "我想先确认是哪一笔订单。你可以在下面点选，也可以直接发订单号。"
NOT_FOUND_TEXT = "我没有找到订单 {order_id}。请确认订单号，或从下面选择一笔订单。"
ORDER_WITH_PREFIX = re.compile(
    r"(?:订单号|订单|单号)\s*[:：]?\s*([0-9]{4,12})"
    r"(?![0-9A-Za-z])"
    r"(?!\s*(?:元|块|个|件|只|台|套|%|年|月|日|天|次|kg|g|ml|l))"
)
BARE_ORDER = re.compile(
    r"(?<![0-9A-Za-z\-./])([0-9]{4,8})(?![0-9A-Za-z\-./])"
    r"(?!\s*(?:元|块|个|件|只|台|套|%|年|月|日|天|次|kg|g|ml|l))"
)


def extract_order_id(text: str) -> str | None:
    match = ORDER_WITH_PREFIX.search(text or "")
    if match:
        return match.group(1)
    match = BARE_ORDER.search(text or "")
    return match.group(1) if match else None


def make_order_node(deps):
    async def order_node(state) -> dict:
        conversation_id = state["conversation_id"]
        resolved = state.get("resolved_question") or state["question"]
        order_id = extract_order_id(resolved)
        started = time.perf_counter()
        if order_id:
            registry = build_registry(deps.store, conversation_id, deps.knowledge, settings=deps.settings)
            spec = registry.get("query_order")
            emit("status", {"phase": "start", "tool": "query_order",
                            "label": spec.label if spec else "查询订单",
                            "source": spec.source if spec else "builtin",
                            "mcp_server": spec.mcp_server if spec else None})
            engine = build_engine(registry, settings=deps.settings,
                                  session_factory=deps.store.session_factory)
            outcome = await engine.run(name="query_order", args={"order_id": order_id},
                                       conversation_id=conversation_id)
            try:
                payload = json.loads(outcome.content)
            except (TypeError, ValueError):
                payload = {}
            order = payload.get("data") if payload.get("ok") else None
            emit("status", {"phase": "end", "tool": "query_order", "label": "查询订单",
                            "ok": bool(order), "summary": outcome.summary,
                            "duration_ms": int((time.perf_counter() - started) * 1000),
                            "source": outcome.source, "mcp_server": outcome.mcp_server})
            if order:
                logger.info("workflow policy_order conv=%s order_id=%s found=1", conversation_id, order_id)
                return {"order": order}
            text = NOT_FOUND_TEXT.format(order_id=order_id)
            reason = "order_not_found"
        else:
            text = MISSING_ORDER_TEXT
            reason = "missing_order"
        logger.info("workflow policy_order conv=%s order_id=%s found=0", conversation_id, order_id)
        emit("token", {"content": text})
        emit("orders", {"items": list_orders(), "reason": reason})
        return {"order": None, "reply": text, "actions": [], "offer": None, "finish_reason": "stop",
                "pending": {"question": resolved, "intent": state.get("intent") or ""}}

    return order_node


def policy_selector(state) -> str:
    return "retrieve" if state.get("order") else "log"


def make_policy_retrieve_node(deps):
    async def retrieve_node(state) -> dict:
        resolved = state.get("resolved_question") or state["question"]
        start = time.perf_counter()
        queries = [resolved]
        if deps.expander is not None:
            emit("status", {"phase": "start", "tool": "expand_query", "label": "扩展检索问法"})
            try:
                queries = await deps.expander.expand(resolved)
            except Exception:
                logger.exception("问题扩写失败，按原问题检索。")
                queries = [resolved]
            emit("status", {"phase": "end", "tool": "expand_query", "label": "扩展检索问法",
                            "ok": True, "summary": f"生成 {len(queries)} 条检索问法",
                            "duration_ms": int((time.perf_counter() - start) * 1000)})
        started = time.perf_counter()
        emit("status", {"phase": "start", "tool": "retrieve", "label": "检索知识库"})
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(deps.knowledge.retrieve_expanded, queries),
                deps.settings.knowledge_tool_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("策略检索超时（%s 秒），按低置信处理。", deps.settings.knowledge_tool_timeout_seconds)
            emit("status", {"phase": "end", "tool": "retrieve", "label": "检索知识库", "ok": False,
                            "summary": "知识库响应超时",
                            "duration_ms": int((time.perf_counter() - started) * 1000)})
            return {"queries": list(queries),
                    "retrieval": {"low_confidence": True, "reason": "retrieval_timeout",
                                  "canonical_query": "", "strategy": ""},
                    "evidence": [], "citations": []}
        except Exception:
            logger.exception("策略检索失败，按低置信处理。")
            emit("status", {"phase": "end", "tool": "retrieve", "label": "检索知识库", "ok": False,
                            "summary": "知识库暂时不可用",
                            "duration_ms": int((time.perf_counter() - started) * 1000)})
            return {"queries": list(queries),
                    "retrieval": {"low_confidence": True, "reason": "retrieval_error",
                                  "canonical_query": "", "strategy": ""},
                    "evidence": [], "citations": []}
        evidence = [item.item() for item in result.evidence]
        citations = [item.citation() for item in result.evidence]
        if citations:
            emit("citations", {"items": citations})
        emit("status", {"phase": "end", "tool": "retrieve", "label": "检索知识库",
                        "ok": not result.low_confidence,
                        "summary": f"命中 {len(evidence)} 条知识" if evidence else (result.reason or "没有命中"),
                        "duration_ms": int((time.perf_counter() - started) * 1000)})
        logger.info("workflow policy_retrieve conv=%s queries=%s low_conf=%s reason=%s",
                    state.get("conversation_id"), queries, result.low_confidence, result.reason)
        return {"queries": list(queries),
                "retrieval": {"low_confidence": result.low_confidence, "reason": result.reason,
                              "canonical_query": result.canonical_query, "strategy": result.strategy},
                "evidence": evidence, "citations": citations}

    return retrieve_node
