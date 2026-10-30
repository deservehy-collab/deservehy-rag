import json
import logging
import math
from dataclasses import asdict, dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.messages.utils import trim_messages

from app.prompts import chat_system
from app.schemas import ChatRequest
from app.tools import FaqArgs, OrderArgs, ProductArgs, TicketArgs

logger = logging.getLogger(__name__)

CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


class ContextBudgetError(ValueError):
    pass


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if any(start <= ord(char) <= end for start, end in CJK_RANGES))
    other = len(text) - cjk
    return math.ceil(cjk + other / 3.5)


def estimate_tokens(messages) -> int:
    total = 0
    for message in messages:
        content = message.content
        text = content if isinstance(content, str) else str(content or "")
        total += 8 + estimate_text_tokens(text)
        if getattr(message, "tool_calls", None):
            total += 16
    return total


def clip_text(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or estimate_text_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def tool_schema_tokens() -> int:
    schemas = [item.model_json_schema() for item in (OrderArgs, ProductArgs, FaqArgs, TicketArgs, OrderArgs)]
    return estimate_text_tokens(json.dumps(schemas, ensure_ascii=False))


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output: int
    user_input: int
    system: int
    evidence: int
    react_peak: int
    safety: int
    summary: int
    wanted: int
    available: int
    history: int
    layer1: int
    layer2: int

    @classmethod
    def from_settings(cls, settings, *, summaries_tokens: int = 0) -> "ContextBudget":
        system = estimate_text_tokens(chat_system(settings.store_name)) + tool_schema_tokens()
        evidence = settings.knowledge_top_k * 400
        react_peak = settings.workflow_agent_max_steps * (settings.tool_result_max_tokens + 400)
        available = (
            settings.context_window_tokens
            - settings.max_output_tokens
            - settings.max_user_input_tokens
            - system
            - evidence
            - react_peak
            - settings.token_safety_margin
            - summaries_tokens
        )
        wanted = settings.history_keep_rounds * settings.history_round_tokens
        history = max(0, min(wanted, available))
        layer2 = int(history * 0.3)
        layer1 = max(0, history - layer2 - 1)
        return cls(
            settings.context_window_tokens,
            settings.max_output_tokens,
            settings.max_user_input_tokens,
            system,
            evidence,
            react_peak,
            settings.token_safety_margin,
            summaries_tokens,
            wanted,
            available,
            history,
            layer1,
            layer2,
        )

    def startup_problem(self) -> str | None:
        if self.history <= 0 or self.layer1 <= 0:
            return (
                f"上下文预算不足：窗口 {self.window}，历史预算 {self.history}，"
                f"层 1 {self.layer1}。请调大窗口或缩小输出/步骤预留。"
            )
        return None


def build_messages(system: str, request: ChatRequest, budget: int) -> list[BaseMessage]:
    messages = [SystemMessage(content=system)]
    messages.extend(
        HumanMessage(content=item.content) if item.role == "user" else AIMessage(content=item.content)
        for item in request.history
    )
    messages.append(HumanMessage(content=request.message))
    while estimate_tokens(messages) > budget and len(messages) > 2:
        del messages[1:3]
    if estimate_tokens(messages) > budget:
        raise ContextBudgetError("当前问题超过上下文预算，请缩短后重试。")
    return messages


ASSISTANT_CLIP_CHARS = 40


def _as_message(row: dict) -> BaseMessage:
    kwargs = {"content": row["content"], "id": str(row["id"])}
    return HumanMessage(**kwargs) if row["role"] == "user" else AIMessage(**kwargs)


def _render_layer2(row: dict) -> dict:
    content = row["content"]
    if row["role"] == "assistant" and len(content) > ASSISTANT_CLIP_CHARS:
        content = content[:ASSISTANT_CLIP_CHARS] + "…"
    return {"id": row["id"], "role": row["role"], "content": content}


@dataclass(frozen=True)
class ContextPlan:
    conversation_id: int
    turn: int
    current_id: int
    summary_upto: int
    layer1_from: int
    summary: str
    summary_tokens: int
    summary_segments: tuple[dict, ...]
    layer1: list[dict]
    layer2: list[dict]
    layer1_tokens: int
    layer2_tokens: int
    layer2_raw_tokens: int
    layer2_budget: int
    degraded: tuple[int, int] | None
    layer2_over: bool
    dropped: int

    def as_dict(self) -> dict:
        data = asdict(self)
        data["degraded"] = list(self.degraded) if self.degraded else None
        return data


class ContextBuilder:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def build(self, conversation_id: int, messages: list[dict], current_id: int, turn: int) -> ContextPlan:
        summaries = self.store.summaries(conversation_id)
        summary = "\n".join(item["content"] for item in summaries)
        # 梗概会拼进尾随块占真实窗口，所以先按它的体积扣一轮预算，再算三层。
        budget = ContextBudget.from_settings(self.settings, summaries_tokens=estimate_text_tokens(summary))
        summary_upto, layer1_from = self.store.anchors(conversation_id)

        candidates = [row for row in messages if layer1_from <= row["id"] < current_id]
        trimmed = trim_messages(
            [_as_message(row) for row in candidates],
            max_tokens=budget.layer1,
            token_counter=estimate_tokens,
            strategy="last",
            allow_partial=False,
            start_on="human",
            include_system=False,
        )
        if trimmed:
            new_layer1_from = int(trimmed[0].id)
        else:
            # 一轮都装不下时不再保留原文；层 2 仍按渲染规则兜底。
            new_layer1_from = current_id
        degraded = None
        if new_layer1_from != layer1_from:
            self.store.set_layer1_from(conversation_id, new_layer1_from)
            if layer1_from and new_layer1_from > layer1_from:
                degraded = (layer1_from, new_layer1_from)
                logger.info("layer1 降级 %s→%s conv=%s", layer1_from, new_layer1_from, conversation_id)

        layer1 = [{"id": int(message.id), "role": "user" if message.type == "human" else "assistant",
                   "content": str(message.content)} for message in trimmed]
        layer2_rows = [row for row in messages if summary_upto < row["id"] < new_layer1_from]
        layer2_raw_tokens = estimate_tokens([_as_message(row) for row in layer2_rows])
        layer2 = [_render_layer2(row) for row in layer2_rows]
        dropped = 0
        while layer2 and estimate_tokens([_as_message(row) for row in layer2]) > budget.layer2:
            layer2.pop(0)
            dropped += 1
        if dropped:
            logger.info("layer2 丢弃 %s 条 conv=%s", dropped, conversation_id)

        return ContextPlan(
            conversation_id=conversation_id,
            turn=turn,
            current_id=current_id,
            summary_upto=summary_upto,
            layer1_from=new_layer1_from,
            summary=summary,
            summary_tokens=estimate_text_tokens(summary),
            summary_segments=tuple({"seq": item["seq"], "content": item["content"]} for item in summaries),
            layer1=layer1,
            layer2=layer2,
            layer1_tokens=estimate_tokens([_as_message(row) for row in layer1]),
            layer2_tokens=estimate_tokens([_as_message(row) for row in layer2]),
            layer2_raw_tokens=layer2_raw_tokens,
            layer2_budget=budget.layer2,
            degraded=degraded,
            layer2_over=layer2_raw_tokens > budget.layer2,
            dropped=dropped,
        )


def build_model_messages(plan: dict, current_text: str, trailing: str, settings) -> list[BaseMessage]:
    system = SystemMessage(content=chat_system(settings.store_name))
    history = [_as_message(row) for row in (plan.get("layer2") or []) + (plan.get("layer1") or [])]
    current = HumanMessage(content=current_text, id=str(plan["current_id"]))
    summary = (plan.get("summary") or "").strip()
    trailing = "\n\n".join(part for part in (
        f"【会话摘要】\n{summary}" if summary else "",
        trailing,
    ) if part)
    tail = [HumanMessage(content=trailing)] if trailing else []
    messages = [system, *history, current, *tail]
    while history and estimate_tokens(messages) > settings.input_budget:
        # 只裁历史：从层 2 最老一条开始丢；当前问题与尾随块永不参与裁剪。
        history.pop(0)
        messages = [system, *history, current, *tail]
    if estimate_tokens(messages) > settings.input_budget:
        raise ContextBudgetError("当前问题超过上下文预算，请缩短后重试。")
    return messages


def history_window(messages: list[dict], limit: int = 6) -> list[dict]:
    return [{"id": row["id"], "role": row["role"], "content": row["content"]}
            for row in messages[-limit:]]


def summary_log_lines(segments) -> list[str]:
    """把摘要段渲染成日志里的 `seq=N 正文`，没有摘要时是空列表。"""
    return [f"seq={item.get('seq')} {item.get('content')}" for item in segments or []]

