import pytest
from pydantic import ValidationError

from app.config import Settings


def make(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m",
            "openai_api_key": "k"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_topic_defaults():
    settings = make()
    assert settings.topic_classify_enabled is False
    assert settings.topic_service_url == "http://127.0.0.1:8933"
    assert settings.topic_classify_interval_minutes == 10
    assert settings.topic_classify_min_pending == 20
    assert settings.topic_classify_batch == 50
    assert settings.topic_model_dir == ".cache/models/topic-classifier"
    assert settings.topic_device == "cpu"
    assert settings.topic_max_len == 128


def test_topic_bounds_reject_bad_values():
    for field, value in (("topic_classify_interval_minutes", 0),
                         ("topic_classify_min_pending", 0),
                         ("topic_classify_batch", 0),
                         ("topic_classify_batch", 201),
                         ("topic_max_len", 16),
                         ("topic_max_len", 257),
                         ("topic_device", "mps")):
        with pytest.raises(ValidationError):
            make(**{field: value})
