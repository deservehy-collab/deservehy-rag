import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from app.agent.guards import (
    BUDGET_TEXT,
    FALLBACK_PLAIN,
    FALLBACK_TOOL,
    MAX_TOOL_CALLS,
    AgentStream,
)
from app.context import clip_text, estimate_tokens
from app.tools import TOOL_LABELS, ToolExecutor, ToolOutcome

StatusFn = Callable[[dict], None]
TokenFn = Callable[[str], None]
OutcomeFn = Callable[[str, ToolOutcome], Awaitable[str | None]]


@dataclass
class ReActResult:
    reply: str
    finish_reason: str | None
    steps: int
    tool_calls: int
    tokens: int
    stopped_by: str | None


async def run_react(
    *,
    model,
    tools: dict,
    executor: ToolExecutor,
    messages: list,
    on_token: TokenFn,
    on_status: StatusFn,
    max_steps: int = 4,
    token_budget: int = 6000,
    timeout: float = 60.0,
    guard: AgentStream | None = None,
    on_tool_outcome: OutcomeFn | None = None,
    store=None,
    conversation_id: int | None = None,
    tool_result_max_tokens: int | None = None,
) -> ReActResult:
    guard = guard or AgentStream()
    bound = model.bind_tools(list(tools.values()))
    work = list(messages)
    steps = total = 0
    finish_reason = None

    while True:
        if steps and estimate_tokens(work) > token_budget:
            tail = guard.finish()
            if tail:
                on_token(tail)
            on_token(BUDGET_TEXT)
            return ReActResult(guard.text + BUDGET_TEXT, "stop", steps, total,
                               estimate_tokens(work), "token_budget")

        use_tools = steps < max_steps
        reply = None
        raw_parts: list[str] = []
        step_finish = None
        stream = (bound if use_tools else model).astream(work)
        try:
            async with asyncio.timeout(timeout):
                async for item in stream:
                    reply = item if reply is None else reply + item
                    if item.text:
                        raw_parts.append(item.text)
                        safe = guard.feed(item.text)
                        if safe:
                            on_token(safe)
                    step_finish = item.response_metadata.get("finish_reason") or step_finish
        finally:
            await stream.aclose()
        if not isinstance(reply, AIMessageChunk):
            raise ValueError("Empty model reply")
        finish_reason = step_finish or finish_reason
        steps += 1
        calls = reply.tool_calls

        if not calls:
            if finish_reason not in {"stop", "length"}:
                raise ValueError("Missing text or valid finish reason")
            tail = guard.finish()
            if tail:
                on_token(tail)
            text = guard.text or (FALLBACK_TOOL if guard.blocked else FALLBACK_PLAIN)
            if not guard.text:
                on_token(text)
            return ReActResult(text, finish_reason, steps, total, estimate_tokens(work), None)

        if not use_tools:
            raise ValueError("Model asked for tools after the convergence step")
        if len(calls) > MAX_TOOL_CALLS:
            raise ValueError("Too many tool calls")

        stored = [
            {"name": item["name"], "args": item.get("args") or {},
             "id": item.get("id") or f"call_{index + 1}", "type": "tool_call"}
            for index, item in enumerate(calls)
        ]
        if store is not None and conversation_id is not None:
            await asyncio.to_thread(store.add_message, conversation_id, "assistant", tool_calls=stored)
        work.append(AIMessage(content="".join(raw_parts), tool_calls=stored))

        for item in stored:
            label = TOOL_LABELS.get(item["name"], item["name"])
            on_status({"phase": "start", "tool": item["name"], "label": label})
            outcome = await executor.execute(item["name"], item["args"])
            if store is not None and conversation_id is not None:
                await asyncio.to_thread(store.add_message, conversation_id, "tool",
                                        content=outcome.content, tool_call_id=item["id"])
            on_status({"phase": "end", "tool": item["name"], "label": label, "ok": outcome.ok,
                       "summary": outcome.summary, "duration_ms": outcome.duration_ms})
            total += 1
            forced = await on_tool_outcome(item["name"], outcome) if on_tool_outcome else None
            if forced is not None:
                tail = guard.finish()
                if tail:
                    on_token(tail)
                on_token(forced)
                guard.text += forced
                return ReActResult(guard.text, "stop", steps, total, estimate_tokens(work), "tool_refusal")
            work.append(ToolMessage(
                content=clip_text(outcome.content, tool_result_max_tokens) if tool_result_max_tokens else outcome.content,
                tool_call_id=item["id"], name=item["name"],
            ))
