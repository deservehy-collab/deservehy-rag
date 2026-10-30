import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas import IntentResult, QueryExpansion, RefundRequest, ResolvedQuestion
from app.workflow.actions import REFUND_REASONS


def test_settings_expose_router_knobs():
    settings = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1",
                        openai_model="test-model", openai_api_key="test")
    assert settings.workflow_intent_min_confidence == 0.6
    assert settings.workflow_understanding_enabled is True
    assert settings.workflow_understanding_timeout_seconds == 10
    assert settings.workflow_query_expansion is True
    assert settings.workflow_query_expansion_max == 3


def test_intent_result_requires_confidence_and_eight_classes():
    assert IntentResult(intent="其他", confidence=0.4).intent == "其他"
    with pytest.raises(ValidationError):
        IntentResult(intent="其他")
    with pytest.raises(ValidationError):
        IntentResult(intent="其他", confidence=1.5)
    with pytest.raises(ValidationError):
        IntentResult(intent="不存在", confidence=0.5)


def test_resolved_question_and_expansion_limits():
    assert ResolvedQuestion(question="订单 1001 能退吗").question.startswith("订单")
    assert len(QueryExpansion(queries=["a", "b", "c"]).queries) == 3
    with pytest.raises(ValidationError):
        QueryExpansion(queries=[])
    with pytest.raises(ValidationError):
        QueryExpansion(queries=["a", "b", "c", "d"])


def test_refund_request_only_accepts_known_reason():
    body = RefundRequest(conversation_id=1, client_id="anon-refund-1", turn=2,
                         reason=REFUND_REASONS[0], note="杯口有裂纹")
    assert body.reason == REFUND_REASONS[0]
    with pytest.raises(ValidationError):
        RefundRequest(conversation_id=1, client_id="anon-refund-1", turn=2, reason="随便写写")
