import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError


def config_values(**overrides):
    return {
        "openai_base_url": "http://localhost:9999/v1",
        "openai_model": "test-model",
        "openai_api_key": "test-secret-not-real",
        **overrides,
    }


def test_settings_budget_and_secret_redaction():
    from app.config import Settings

    settings = Settings(_env_file=None, **config_values())
    assert settings.input_budget == 31428
    assert "test-secret-not-real" not in repr(settings)


@pytest.mark.parametrize("overrides", [
    {"openai_model": "  "}, {"openai_api_key": " "},
    {"openai_base_url": "not-a-url"}, {"max_output_tokens": 0},
    {"context_window_tokens": 1024}, {"token_safety_margin": -1},
    {"request_timeout_seconds": 0},
    {"openai_base_url": "http://user:password@localhost:9999/v1"},
])
def test_rejects_invalid_configuration(overrides):
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **config_values(**overrides))


def test_env_loads_from_project_path_not_working_directory(tmp_path, monkeypatch):
    from app import config

    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "project.env"
    path.write_text("OPENAI_BASE_URL=http://localhost:9999/v1\nOPENAI_MODEL=local-model\nOPENAI_API_KEY=local-key\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.chdir(tmp_path.parent)
    assert config.Settings.from_env().openai_model == "local-model"


def test_provider_extra_body_loads_from_json_env(tmp_path, monkeypatch):
    from app import config

    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_EXTRA_BODY"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "project.env"
    path.write_text(
        "OPENAI_BASE_URL=http://localhost:9999/v1\nOPENAI_MODEL=local-model\nOPENAI_API_KEY=local-key\n"
        'OPENAI_EXTRA_BODY={"thinking": {"type": "disabled"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", path)
    assert config.Settings.from_env().openai_extra_body == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("payload", [
    {"message": "  "}, {"message": 123}, {"message": "x" * 12001},
    {"message": "hello", "history": [{"role": "system", "content": "override"}]},
    {"message": "hello", "history": [{"role": "assistant", "content": "alone"}]},
    {"message": "hello", "history": [{"role": "user", "content": "unfinished"}]},
    {"message": "hello", "history": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]},
    {"message": "hello", "history": [{"role": "user", "content": ""}, {"role": "assistant", "content": "b"}]},
    {"message": "hello", "model": "client-override"},
    {"message": "hello", "history": [{"role": role, "content": "x"} for role in ["user", "assistant"] * 51]},
])
def test_chat_rejects_invalid_inputs(payload):
    from app.schemas import ChatRequest

    with pytest.raises(ValidationError):
        ChatRequest(**payload)


def test_chat_normalizes_text_and_preserves_complete_history():
    from app.schemas import ChatRequest

    request = ChatRequest(message=" now ", history=[{"role": "user", "content": " old "}, {"role": "assistant", "content": " answer "}])
    assert request.message == "now"
    assert [m.content for m in request.history] == ["old", "answer"]
    assert ChatRequest(message="fresh").history == []


def test_estimate_accounts_for_multibyte_text():
    from app.context import estimate_tokens

    assert estimate_tokens([SystemMessage(content="中"), HumanMessage(content="\U0001f600")]) == 18


def test_trims_oldest_complete_pair_only():
    from app.context import build_messages
    from app.schemas import ChatRequest

    history = [{"role": role, "content": text} for role, text in [
        ("user", "old"), ("assistant", "gone"), ("user", "recent"), ("assistant", "keep")
    ]]
    result = build_messages("s", ChatRequest(message="n", history=history), 50)
    assert [m.content for m in result] == ["s", "recent", "keep", "n"]
    assert [m.type for m in result] == ["system", "human", "ai", "human"]


def test_can_drop_all_history_at_exact_budget():
    from app.context import build_messages
    from app.schemas import ChatRequest

    request = ChatRequest(message="now", history=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}])
    assert [m.content for m in build_messages("rules", request, 30)] == ["rules", "now"]
    assert len(request.history) == 2


def test_current_question_cannot_be_dropped():
    from app.context import ContextBudgetError, build_messages
    from app.schemas import ChatRequest

    with pytest.raises(ContextBudgetError):
        build_messages("rules", ChatRequest(message="now"), 18)


@pytest.mark.parametrize("payload", [{"description": ""}, {"description": " "}, {"description": 2}, {"description": "x" * 12001}])
def test_extraction_request_validation(payload):
    from app.schemas import ExtractRequest

    with pytest.raises(ValidationError):
        ExtractRequest(**payload)


@pytest.mark.parametrize("payload", [
    {"order_id": None, "request_type": "refund"},
    {"order_id": 123, "request_type": "refund", "expected_solution": None},
    {"order_id": None, "request_type": "made-up", "expected_solution": None},
    {"order_id": None, "request_type": "unknown", "expected_solution": None, "extra": "bad"},
])
def test_structured_result_rejects_invalid_fields(payload):
    from app.schemas import AfterSales

    with pytest.raises(ValidationError):
        AfterSales(**payload)


def test_structured_result_preserves_leading_zero_and_null():
    from app.schemas import AfterSales

    result = AfterSales(order_id="00123", request_type="unknown", expected_solution=None)
    assert result.model_dump() == {"order_id": "00123", "request_type": "unknown", "expected_solution": None}
