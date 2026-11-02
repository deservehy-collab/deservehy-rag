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
    # 三信号权重固定（80 题撑不起四维网格，只校准 min/floor）；条数只影响快照长度。
    confidence_w_top1: float = Field(default=0.7, ge=0, le=1)
    confidence_w_count: float = Field(default=0.2, ge=0, le=1)
    confidence_w_margin: float = Field(default=0.1, ge=0, le=1)
    snapshot_top_n: int = Field(default=5, ge=1, le=10)
    knowledge_query_rewrite: bool = True
    knowledge_self_check: bool = True
    knowledge_tool_timeout_seconds: float = Field(default=20, gt=0, allow_inf_nan=False)
    knowledge_strategy: Literal["dense", "bm25", "hybrid", "hybrid_rerank"] = "hybrid_rerank"
    knowledge_mining_enabled: bool = False
    knowledge_mining_interval_minutes: int = Field(default=60, ge=0)
    knowledge_mining_lookback_days: int = Field(default=7, ge=1, le=365)
    knowledge_dedupe_threshold: float = Field(default=0.95, ge=0, le=1, allow_inf_nan=False)
    # 定时评估（空 = 关）。示例：每周日 03:30 跑一轮 → "30 3 * * 0"；默认只跑检索指标，不跑忠实度。
    eval_schedule_cron: str = Field(default="", max_length=100)
    # default 取自 evals/reports/ch09-confidence-20261101-221716.md 的「选中」行（80 题、8 题不可答）；
    # 改前请重跑 scripts/calibrate_confidence.py，别手调。
    confidence_min: float = Field(default=0.10, ge=0, le=1, alias="CONFIDENCE_MIN")
    confidence_effective_floor: float = Field(default=0.15, ge=0, le=1,
                                              alias="CONFIDENCE_EFFECTIVE_FLOOR")
    langfuse_enabled: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    langfuse_host: str = Field(default="http://127.0.0.1:3000", alias="LANGFUSE_HOST")
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    flywheel_auto_drain: bool = True
    flywheel_batch_size: int = Field(default=10, ge=1, le=50)
    flywheel_dedupe_candidates: int = Field(default=20, ge=1, le=50)
    # ch10 主题分类：后台批量归类默认关（与定时评估一致），演示时显式打开。
    # 用 AliasChoices 而不是 alias：测试用字段名构造 Settings，pydantic v2 的 alias 会忽略字段名。
    topic_classify_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("TOPIC_CLASSIFY_ENABLED", "topic_classify_enabled"))
    topic_service_url: str = Field(
        default="http://127.0.0.1:8933", min_length=1, max_length=255,
        validation_alias=AliasChoices("TOPIC_SERVICE_URL", "topic_service_url"))
    topic_classify_interval_minutes: int = Field(
        default=10, ge=1, le=1440,
        validation_alias=AliasChoices("TOPIC_CLASSIFY_INTERVAL_MINUTES", "topic_classify_interval_minutes"))
    topic_classify_min_pending: int = Field(
        default=20, ge=1,
        validation_alias=AliasChoices("TOPIC_CLASSIFY_MIN_PENDING", "topic_classify_min_pending"))
    topic_classify_batch: int = Field(
        default=50, ge=1, le=200,
        validation_alias=AliasChoices("TOPIC_CLASSIFY_BATCH", "topic_classify_batch"))
    topic_model_dir: str = Field(
        default=".cache/models/topic-classifier", min_length=1, max_length=255,
        validation_alias=AliasChoices("TOPIC_MODEL_DIR", "topic_model_dir"))
    topic_device: Literal["cpu", "cuda"] = Field(
        default="cpu", validation_alias=AliasChoices("TOPIC_DEVICE", "topic_device"))
    topic_max_len: int = Field(
        default=128, ge=32, le=256,
        validation_alias=AliasChoices("TOPIC_MAX_LEN", "topic_max_len"))
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
