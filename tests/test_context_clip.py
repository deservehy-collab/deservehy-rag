import pytest

from app.context import ContextBudgetError, build_model_messages
from tests.support import _settings


def _plan(layer1, layer2, summary=""):
    return {"current_id": 99, "layer1": layer1, "layer2": layer2, "summary": summary}


def test_fallback_clipping_never_drops_the_current_question():
    settings = _settings({"context_window_tokens": 1200, "max_output_tokens": 100,
                          "token_safety_margin": 0})
    layer1 = [{"id": index, "role": "assistant", "content": "答" * 200} for index in range(1, 20)]
    plan = _plan(layer1, [])
    try:
        messages = build_model_messages(plan, "这单最后能退多少钱", "", settings)
    except ContextBudgetError:
        return
    assert any(message.content == "这单最后能退多少钱" for message in messages)


def test_fallback_clipping_raises_when_only_the_question_is_left():
    settings = _settings({"context_window_tokens": 1200, "max_output_tokens": 100,
                          "token_safety_margin": 0})
    plan = _plan([], [], summary="摘" * 1200)
    with pytest.raises(ContextBudgetError):
        build_model_messages(plan, "当前这句问话本身不算长", "【知识库证据】" + "据" * 1200, settings)
