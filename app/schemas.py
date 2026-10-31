from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=12000)]


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    content: Text


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    message: Text
    history: list[HistoryMessage] = Field(default_factory=list, max_length=100)
    client_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")] | None = None
    conversation_id: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def complete_turns(self) -> Self:
        if len(self.history) % 2 or any(
            message.role != ("user" if index % 2 == 0 else "assistant")
            for index, message in enumerate(self.history)
        ):
            raise ValueError("历史必须是完整的 user、assistant 交替问答。")
        return self


class ResumeRequest(BaseModel):
    """确认卡片的一次性凭据：`pending_id` 是 T13 生成的 nonce，过期或重放都拿不到执行权。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    conversation_id: Annotated[int, Field(ge=1)]
    client_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")]
    decision: Literal["accept", "cancel"]
    pending_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[a-f0-9]+")]


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    description: Text


class AfterSales(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    order_id: Annotated[str, Field(min_length=1, max_length=200)] | None
    request_type: Literal["refund", "return_refund", "exchange", "repair", "logistics", "other", "unknown"]
    expected_solution: Annotated[str, Field(min_length=1, max_length=1000)] | None


Intent = Literal["物流", "订单", "商品咨询", "退款退货", "售后", "投诉", "其他", "闲聊"]


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    intent: Intent
    confidence: Annotated[float, Field(ge=0, le=1)]


class ResolvedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    question: Annotated[str, Field(min_length=1, max_length=200)]


class QueryExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    queries: Annotated[list[Annotated[str, Field(min_length=1, max_length=120)]], Field(min_length=1, max_length=3)]


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    conversation_id: Annotated[int, Field(ge=1)]
    client_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")]
    turn: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=40)]
    note: Annotated[str, Field(max_length=200)] | None = None

    @field_validator("reason")
    @classmethod
    def known_reason(cls, value: str) -> str:
        from app.workflow.actions import REFUND_REASONS

        if value not in REFUND_REASONS:
            raise ValueError("退款原因不在可选范围内。")
        return value


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    conversation_id: Annotated[int, Field(ge=1)]
    client_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")]
    turn: Annotated[int, Field(ge=1)]
