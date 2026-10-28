from pathlib import Path
from typing import Any, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore", str_strip_whitespace=True, hide_input_in_errors=True,
        env_file_encoding="utf-8",
    )

    openai_base_url: AnyHttpUrl
    openai_model: str = Field(min_length=1)
    openai_api_key: SecretStr
    openai_extra_body: dict[str, Any] = Field(default_factory=dict)
    context_window_tokens: int = Field(default=8192, gt=0)
    max_output_tokens: int = Field(default=1024, gt=0)
    token_safety_margin: int = Field(default=512, ge=0)
    request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    store_name: str = Field(default="本店", min_length=1, max_length=100)

    @field_validator("openai_api_key")
    @classmethod
    def require_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("OPENAI_API_KEY 不能为空；本地模型可填非空占位值。")
        return value

    @field_validator("openai_base_url")
    @classmethod
    def clean_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("上游地址不能包含账户、密钥、查询参数或片段。")
        return value

    @model_validator(mode="after")
    def require_input_budget(self) -> Self:
        if self.input_budget <= 0:
            raise ValueError("上下文窗口必须大于输出额度与安全余量之和。")
        return self

    @property
    def input_budget(self) -> int:
        return self.context_window_tokens - self.max_output_tokens - self.token_safety_margin

    @classmethod
    def from_env(cls) -> Self:
        return cls(_env_file=ENV_FILE)
