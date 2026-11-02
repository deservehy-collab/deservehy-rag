"""旁路主题分类推理服务；默认 CPU，端口 8933。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.topics import model as topic_model
from app.topics.limits import MAX_BATCH, MAX_CHARS
from app.topics.model import ModelNotReady
from app.topics.taxonomy import LABELS, order_labels

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path(".cache/models/topic-classifier")


class _TopicSettings(BaseSettings):
    """旁路服务只认 TOPIC_*；进程环境优先于 .env，和主应用的读取顺序一致。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    topic_model_dir: str = Field(default=str(DEFAULT_MODEL_DIR), min_length=1, max_length=255)
    topic_device: Literal["cpu", "cuda"] = "cpu"
    topic_max_len: int = Field(default=128, ge=32, le=256)


def _topic_env() -> tuple[Path, str, int]:
    settings = _TopicSettings()
    return Path(settings.topic_model_dir), settings.topic_device, settings.topic_max_len


class ClassifyRequest(BaseModel):
    texts: list[str]


def create_app(model_dir=None, predictor=None, *, settings=None) -> FastAPI:
    if settings is not None:
        topic_dir = Path(settings.topic_model_dir)
        device, max_len = settings.topic_device, settings.topic_max_len
    else:
        topic_dir, device, max_len = _topic_env()
    if model_dir:
        topic_dir = Path(model_dir)
    state = {"predictor": predictor}
    if predictor is None:
        try:
            state["predictor"] = topic_model.TopicModel(topic_dir, device=device, max_len=max_len)
        except ModelNotReady as exc:
            logger.warning("topic model not ready: %s", exc)

    app = FastAPI(title="ch10 主题分类推理服务")

    @app.get("/health")
    def health():
        return {
            "status": "ok" if state["predictor"] is not None else "not_ready",
            "model_dir": str(topic_dir),
            "labels": len(LABELS),
            "device": device,
        }

    @app.post("/classify")
    def classify(body: ClassifyRequest):
        texts = [text.strip() for text in body.texts]
        if (not 1 <= len(texts) <= MAX_BATCH or any(not text for text in texts)
                or sum(len(text) for text in texts) > MAX_CHARS):
            raise HTTPException(
                status_code=422,
                detail=f"texts 需要 1~{MAX_BATCH} 条非空文本，总长度不超过 {MAX_CHARS} 字符")
        if state["predictor"] is None:
            raise HTTPException(status_code=503, detail={
                "code": "model_not_ready",
                "message": "分类模型还没训练好，请先运行 scripts/train_topic_classifier.py。",
            })
        try:
            predictions = state["predictor"].predict(texts)
            return {"items": [
                {"labels": order_labels(prediction.labels),
                 "scores": {label: prediction.scores[label] for label in LABELS}}
                for prediction in predictions
            ]}
        except Exception:
            logger.exception("topic classify failed")
            raise HTTPException(status_code=503, detail={
                "code": "predict_failed",
                "message": "分类推理失败，请查看推理服务日志。",
            }) from None

    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 主题分类推理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8933)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(create_app(args.model_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
