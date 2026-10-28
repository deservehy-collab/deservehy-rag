from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=12000)]


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    content: Text


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    message: Text
    history: list[HistoryMessage] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def complete_turns(self) -> Self:
        if len(self.history) % 2 or any(
            message.role != ("user" if index % 2 == 0 else "assistant")
            for index, message in enumerate(self.history)
        ):
            raise ValueError("历史必须是完整的 user、assistant 交替问答。")
        return self


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    description: Text


class AfterSales(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    order_id: Annotated[str, Field(min_length=1, max_length=200)] | None
    request_type: Literal["refund", "return_refund", "exchange", "repair", "logistics", "other", "unknown"]
    expected_solution: Annotated[str, Field(min_length=1, max_length=1000)] | None
