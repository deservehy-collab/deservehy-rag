"""加载微调权重、阈值判定与批量预测；torch/transformers 只在构造时导入。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.topics.limits import MAX_BATCH
from app.topics.taxonomy import LABELS, OTHER, order_labels


class ModelNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class TopicPrediction:
    labels: list[str]
    scores: dict[str, float]


def apply_thresholds(probs: Sequence[float], thresholds: Sequence[float]) -> list[str]:
    if len(probs) != len(LABELS) or len(thresholds) != len(LABELS):
        raise ValueError("概率与阈值长度必须等于 17")
    hits = [LABELS[index] for index, (prob, threshold) in enumerate(zip(probs, thresholds))
            if prob >= threshold]
    return hits or [OTHER]


class TopicModel:
    def __init__(self, model_dir: Path | str, *, device: str = "cpu", max_len: int = 128):
        self.model_dir = Path(model_dir)
        self.max_len = max_len
        if not self.is_ready(self.model_dir):
            raise ModelNotReady(f"模型文件不完整：{self.model_dir}")
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except Exception as exc:  # 缺包或 DLL 损坏都应收敛成 not_ready，而不是 500
            raise ModelNotReady(f"torch/transformers 不可用：{exc}") from exc

        try:
            labels = json.loads((self.model_dir / "labels.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelNotReady(f"labels.json 无法读取：{self.model_dir}") from exc
        if labels != list(LABELS):
            raise ModelNotReady("labels.json 与 17 类术语表不一致")
        try:
            raw = json.loads((self.model_dir / "thresholds.json").read_text(encoding="utf-8"))
            self.thresholds = [float(raw[label]) for label in LABELS]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelNotReady(f"thresholds.json 无法读取：{self.model_dir}") from exc
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in self.thresholds):
            raise ModelNotReady("thresholds.json 的阈值必须是 0~1 之间的有限数")
        self._torch = torch
        self.device = device
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir, local_files_only=True)
            self.model = AutoModelForSequenceClassification.from_pretrained(
                self.model_dir, local_files_only=True).to(device)
        except Exception as exc:  # 半拷贝/损坏目录：任何加载失败都收敛成 ModelNotReady
            raise ModelNotReady(f"模型权重加载失败：{self.model_dir}") from exc
        self.model.eval()

    @staticmethod
    def is_ready(model_dir: Path | str) -> bool:
        root = Path(model_dir)
        has_meta = all((root / name).is_file() for name in ("labels.json", "thresholds.json"))
        has_weights = any((root / name).is_file()
                          for name in ("model.safetensors", "pytorch_model.bin"))
        return has_meta and has_weights

    def predict(self, texts: list[str]) -> list[TopicPrediction]:
        if not 1 <= len(texts) <= MAX_BATCH:
            raise ValueError(f"一次预测 1~{MAX_BATCH} 条")
        predictions = []
        with self._torch.no_grad():
            for start in range(0, len(texts), 16):
                batch = texts[start:start + 16]
                encoded = self.tokenizer(batch, padding=True, truncation=True,
                                         max_length=self.max_len, return_tensors="pt")
                encoded = {key: value.to(self.device) for key, value in encoded.items()}
                probs = self._torch.sigmoid(self.model(**encoded).logits).cpu().tolist()
                for row in probs:
                    predictions.append(TopicPrediction(
                        labels=order_labels(apply_thresholds(row, self.thresholds)),
                        scores={label: round(float(score), 4)
                                for label, score in zip(LABELS, row)},
                    ))
        return predictions
