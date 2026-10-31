from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
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
    context_window_tokens: int = Field(
        default=32768,
        gt=0,
        validation_alias=AliasChoices(
            "MODEL_CONTEXT_WINDOW", "CONTEXT_WINDOW_TOKENS", "context_window_tokens"
        ),
    )
    max_output_tokens: int = Field(default=1024, gt=0)
    max_user_input_tokens: int = Field(default=2000, gt=0)
    tool_result_max_tokens: int = Field(default=1200, gt=0)
    token_safety_margin: int = Field(default=316, ge=0)
    history_keep_rounds: int = Field(default=20, ge=1, le=100)
    history_round_tokens: int = Field(default=600, ge=100, le=4000)
    context_summary_enabled: bool = True
    summary_timeout_seconds: float = Field(default=20, gt=0, allow_inf_nan=False)
    log_file: str = Field(default="log/app.log", min_length=1, max_length=255)
    request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    store_name: str = Field(default="本店", min_length=1, max_length=100)
    db_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    db_port: int = Field(default=3307, ge=1, le=65535)
    db_user: str = Field(default="root", min_length=1, max_length=64)
    db_password: SecretStr = SecretStr("")
    db_name: str = Field(default="xiaolin_ch02", min_length=1, max_length=64)
    db_name_test: str = Field(default="xiaolin_ch02_test", min_length=1, max_length=64)
    tool_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    tool_max_attempts: int = Field(default=2, ge=1, le=3)
    mcp_servers: str = Field(default="logistics,aftersale", min_length=0, max_length=255)
    mcp_logistics_url: str = Field(default="http://127.0.0.1:8931/mcp", min_length=1, max_length=255)
    mcp_logistics_permission: Literal["read", "write"] = "read"
    mcp_aftersale_url: str = Field(default="http://127.0.0.1:8932/mcp", min_length=1, max_length=255)
    mcp_aftersale_permission: Literal["read", "write"] = "read"
    tool_write_allowlist: str = Field(default="create_ticket", min_length=0, max_length=255)
    tool_extra_modules: str = Field(default="", min_length=0, max_length=512)
    tool_audit_enabled: bool = True
    tool_discovery_cache_seconds: int = Field(default=60, ge=0, le=3600)
    tool_schema_reserve_tokens: int = Field(default=400, ge=0, le=8000)
    embedding_model: str = Field(default="BAAI/bge-m3", min_length=1, max_length=255)
    embedding_device: str = Field(default="cuda", min_length=1, max_length=16)
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    milvus_uri: str = Field(default="http://127.0.0.1:19530", min_length=1, max_length=255)
    milvus_collection: str = Field(default="knowledge", min_length=1, max_length=64)
    knowledge_doc_dir: str = Field(default="examples/knowledge", min_length=1, max_length=255)
    knowledge_top_k: int = Field(
        default=10,
        ge=1,
        le=20,
        validation_alias=AliasChoices("RERANK_TOP_K", "KNOWLEDGE_TOP_K", "knowledge_top_k"),
    )
    knowledge_candidates: int = Field(default=50, ge=1, le=100)
    knowledge_rrf_k: int = Field(default=60, ge=1, le=1000)
    knowledge_rerank_model: str = Field(default=".cache/models/bge-reranker-v2-m3", min_length=1, max_length=255)
    knowledge_rerank_device: str = Field(default="cuda", min_length=1, max_length=16)
    knowledge_rerank_batch_size: int = Field(default=8, ge=1, le=64)
    knowledge_rerank_min_score: float = Field(default=0.30, ge=0, le=1, allow_inf_nan=False)
    knowledge_query_rewrite: bool = True
    knowledge_self_check: bool = True
    knowledge_tool_timeout_seconds: float = Field(default=20, gt=0, allow_inf_nan=False)
    knowledge_strategy: Literal["dense", "bm25", "hybrid", "hybrid_rerank"] = "hybrid_rerank"
    knowledge_mining_enabled: bool = False
    knowledge_mining_interval_minutes: int = Field(default=60, ge=0)
    knowledge_mining_lookback_days: int = Field(default=7, ge=1, le=365)
    knowledge_dedupe_threshold: float = Field(default=0.95, ge=0, le=1, allow_inf_nan=False)
    workflow_checkpoint_path: str = Field(default=".cache/langgraph/ch05.sqlite", min_length=1, max_length=255)
    workflow_agent_max_steps: int = Field(
        default=4,
        ge=1,
        le=8,
        validation_alias=AliasChoices("MAX_AGENT_STEPS", "WORKFLOW_AGENT_MAX_STEPS", "workflow_agent_max_steps"),
    )
    workflow_agent_token_budget: int = Field(default=6000, ge=1000, le=60000)
    workflow_intent_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    workflow_intent_min_confidence: float = Field(default=0.6, ge=0, le=1, allow_inf_nan=False)
    workflow_understanding_enabled: bool = True
    workflow_understanding_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    workflow_query_expansion: bool = True
    workflow_query_expansion_max: int = Field(default=3, ge=2, le=3)

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
