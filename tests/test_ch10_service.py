import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.topics.model import TopicPrediction
from app.topics.service import _topic_env, create_app
from app.topics.taxonomy import LABELS
from tests.support import _settings


def test_topic_env_reads_dotenv_and_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TOPIC_MODEL_DIR=models/from-dotenv\nTOPIC_DEVICE=cuda\nTOPIC_MAX_LEN=64\n",
        encoding="utf-8")
    for key in ("TOPIC_MODEL_DIR", "TOPIC_DEVICE", "TOPIC_MAX_LEN"):
        monkeypatch.delenv(key, raising=False)

    assert _topic_env() == (Path("models/from-dotenv"), "cuda", 64)

    monkeypatch.setenv("TOPIC_MODEL_DIR", "models/from-env")
    assert _topic_env() == (Path("models/from-env"), "cuda", 64)


@pytest.mark.parametrize(
    ("key", "value"),
    [("TOPIC_DEVICE", "gpu"), ("TOPIC_MAX_LEN", "16"), ("TOPIC_MAX_LEN", "abc")],
    ids=["device", "max-len-range", "max-len-type"],
)
def test_topic_env_rejects_invalid_values(tmp_path, monkeypatch, key, value):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    for name in ("TOPIC_MODEL_DIR", "TOPIC_DEVICE", "TOPIC_MAX_LEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError):
        _topic_env()


class FakePredictor:
    def __init__(self):
        self.calls = []

    def predict(self, texts):
        self.calls.append(texts)
        return [TopicPrediction(labels=["尺码", "退换货"],
                                scores={label: 0.5 for label in LABELS}) for _ in texts]


def test_health_and_classify_order_labels():
    fake = FakePredictor()
    client = TestClient(create_app(predictor=fake))
    assert client.get("/health").json()["status"] == "ok"
    body = client.post("/classify", json={"texts": ["买大了想退", "运费怎么算"]}).json()
    assert body["items"][0]["labels"] == ["退换货", "尺码"]
    assert list(body["items"][0]["scores"]) == list(LABELS)
    assert fake.calls == [["买大了想退", "运费怎么算"]]


def test_validation_returns_422():
    client = TestClient(create_app(predictor=FakePredictor()))
    for texts in ([], ["   "], ["x"] * 65, ["长" * 20001]):
        assert client.post("/classify", json={"texts": texts}).status_code == 422


def test_validation_boundaries():
    client = TestClient(create_app(predictor=FakePredictor()))
    assert client.post("/classify", json={"texts": ["x"] * 64}).status_code == 200
    assert client.post("/classify", json={"texts": ["x"] * 65}).status_code == 422
    assert client.post("/classify",
                       json={"texts": ["长" * 10000, "长" * 10000]}).status_code == 200
    assert client.post("/classify",
                       json={"texts": ["长" * 10000, "长" * 10001]}).status_code == 422


def test_not_ready_model_returns_503(tmp_path):
    settings = _settings({"topic_model_dir": str(tmp_path)})
    client = TestClient(create_app(model_dir=tmp_path, settings=settings))
    assert client.get("/health").json()["status"] == "not_ready"
    response = client.post("/classify", json={"texts": ["在吗"]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "model_not_ready"


def test_predictor_failure_returns_503_without_leaking_exception(caplog):
    class BoomPredictor:
        def predict(self, texts):
            raise RuntimeError("internal-secret-trace")

    client = TestClient(create_app(predictor=BoomPredictor()))
    with caplog.at_level(logging.ERROR, logger="app.topics.service"):
        response = client.post("/classify", json={"texts": ["在吗"]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "predict_failed"
    assert "internal-secret-trace" not in response.text
    assert "topic classify failed" in caplog.text


def test_injected_predictor_skips_model_load_and_main_settings(monkeypatch):
    import app.config
    import app.topics.model as topic_model

    model_calls = []

    class ExplodingModel:
        def __init__(self, *args, **kwargs):
            model_calls.append((args, kwargs))
            raise AssertionError("injected predictor must not load TopicModel")

    def fail_from_env(cls):
        raise AssertionError("injected predictor must not read main Settings")

    monkeypatch.setattr(topic_model, "TopicModel", ExplodingModel)
    monkeypatch.setattr(app.config.Settings, "from_env", classmethod(fail_from_env))
    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    fake = FakePredictor()
    client = TestClient(create_app(predictor=fake))
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/classify", json={"texts": ["在吗"]}).status_code == 200
    assert model_calls == []


def test_create_app_without_env_file_or_openai_settings(monkeypatch, tmp_path):
    import app.config

    def fail_from_env(cls):
        raise AssertionError("service must not read main Settings")

    monkeypatch.setattr(app.config.Settings, "from_env", classmethod(fail_from_env))
    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOPIC_MODEL_DIR", str(tmp_path))

    client = TestClient(create_app())
    health = client.get("/health").json()
    assert health["status"] == "not_ready"
    assert health["model_dir"] == str(tmp_path)
    assert client.post("/classify", json={"texts": ["在吗"]}).status_code == 503
