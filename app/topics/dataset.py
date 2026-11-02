"""语料读写、去重与迭代分层切分。"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from app.topics.taxonomy import LABELS, order_labels, validate_labels

SPLITS = ("train", "val", "test")


@dataclass(frozen=True)
class Sample:
    id: str
    text: str
    labels: list[str]
    origin: str = "synthetic"
    target_labels: list[str] = field(default_factory=list)
    prelabels: list[str] = field(default_factory=list)
    seed_id: str = ""

    def to_dict(self) -> dict:
        payload = {
            "id": self.id, "text": self.text, "labels": list(self.labels),
            "origin": self.origin, "target_labels": list(self.target_labels),
            "prelabels": list(self.prelabels),
        }
        if self.seed_id:
            payload["seed_id"] = self.seed_id
        return payload


def make_sample(*, id: str, text: str, labels: Sequence[str],
                origin: str = "synthetic", target_labels: Sequence[str] | None = None,
                prelabels: Sequence[str] | None = None, seed_id: str = "") -> Sample:
    cleaned = " ".join(str(text).split())
    if not cleaned:
        raise ValueError("样本文本为空")
    return Sample(
        id=id,
        text=cleaned,
        labels=validate_labels(labels),
        origin=origin,
        target_labels=validate_labels(target_labels if target_labels else labels),
        prelabels=order_labels([label for label in (prelabels or []) if label in LABELS]),
        seed_id=seed_id,
    )


def read_jsonl(path: Path | str) -> list[Sample]:
    samples = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            samples.append(make_sample(**json.loads(line)))
    return samples


def write_jsonl(path: Path | str, samples: Sequence[Sample]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in samples),
        encoding="utf-8",
    )


def normalize_for_dedupe(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _trigrams(text: str) -> set[str]:
    normalized = normalize_for_dedupe(text)
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[i:i + 3] for i in range(len(normalized) - 2)}


def trigrams(text: str) -> set[str]:
    """3-gram 集合，供生成阶段做增量近似去重。"""
    return _trigrams(text)


def jaccard(a: str, b: str) -> float:
    left, right = _trigrams(a), _trigrams(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def dedupe(samples: Sequence[Sample], threshold: float = 0.9) -> tuple[list[Sample], int]:
    """精确去重后做字符 3-gram 近似去重，保留先出现者。"""
    kept: list[Sample] = []
    seen: set[str] = set()
    dropped = 0
    for item in samples:
        normalized = normalize_for_dedupe(item.text)
        if not normalized or normalized in seen:
            dropped += 1
            continue
        if any(jaccard(item.text, other.text) >= threshold for other in kept):
            dropped += 1
            continue
        seen.add(normalized)
        kept.append(item)
    return kept, dropped


def iterative_split(samples: Sequence[Sample], ratios: tuple[float, float, float] = (0.8, 0.1, 0.1),
                    seed: int = 42) -> dict[str, list[Sample]]:
    """按标签迭代分配：稀有标签先分，每次放进该类最缺且尚有余量的分片。"""
    rng = random.Random(seed)
    shuffled = list(samples)
    rng.shuffle(shuffled)
    total = len(shuffled)
    caps = {name: int(total * ratio) for name, ratio in zip(SPLITS, ratios)}
    caps["train"] += total - sum(caps.values())
    buckets: dict[str, list[Sample]] = {name: [] for name in SPLITS}
    assigned: set[str] = set()

    support = Counter(label for item in shuffled for label in item.labels)
    for label in sorted(support, key=lambda name: (support[name], LABELS.index(name))):
        members = [item for item in shuffled if label in item.labels]
        for item in members:
            if item.id in assigned:
                continue
            candidates = [name for name in SPLITS if len(buckets[name]) < caps[name]]
            if not candidates:
                break
            target = {name: support[label] * ratio for name, ratio in zip(SPLITS, ratios)}
            current = {name: sum(1 for other in buckets[name] if label in other.labels)
                       for name in SPLITS}
            # 余量相同按 train→val→test 顺序取；最后用 rng 打破平局，保证同 seed 可复现
            best = max(candidates, key=lambda name: (target[name] - current[name],
                                                     -SPLITS.index(name), rng.random()))
            buckets[best].append(item)
            assigned.add(item.id)

    for item in shuffled:
        if item.id in assigned:
            continue
        candidates = [name for name in SPLITS if len(buckets[name]) < caps[name]]
        best = max(candidates, key=lambda name: (caps[name] - len(buckets[name]), -SPLITS.index(name)))
        buckets[best].append(item)
        assigned.add(item.id)

    # 硬保证：样本数 ≥10 的标签在 val/test 至少出现一次；从 train 挪一条过去。
    for label, count in support.items():
        if count < 10:
            continue
        for name in ("val", "test"):
            if any(label in item.labels for item in buckets[name]):
                continue
            donor = next((item for item in buckets["train"] if label in item.labels), None)
            if donor is not None:
                buckets["train"].remove(donor)
                buckets[name].append(donor)
    return buckets


SYNONYMS: tuple[tuple[str, str], ...] = (
    ("快递", "物流"),
    ("包邮", "免运费"),
    ("退换", "退换货"),
    ("开票", "开发票"),
    ("优惠券", "券"),
    ("价保", "保价"),
    ("补货", "到货"),
    ("积分", "会员分"),
)


def apply_synonyms(text: str, rng: random.Random, max_replacements: int = 2) -> str:
    result = text
    changed = 0
    for source, target in rng.sample(list(SYNONYMS), len(SYNONYMS)):
        if changed >= max_replacements:
            break
        if source in result and target not in result:
            result = result.replace(source, target, 1)
            changed += 1
    return result


def attach_paraphrases(train: Sequence[Sample], paraphrases: Sequence[Sample], *,
                       ratio: float = 0.3, seed: int = 42) -> list[Sample]:
    """变体与 seed 同组：seed 不在训练集就丢弃，避免验证/测试泄漏。"""
    rng = random.Random(seed)
    train_ids = {item.id for item in train}
    grouped: dict[str, list[Sample]] = {}
    for para in paraphrases:
        if para.seed_id in train_ids:
            grouped.setdefault(para.seed_id, []).append(para)
    chosen = sorted(grouped)
    sample_size = min(len(chosen), max(0, round(len(train) * ratio)))
    picked = set(rng.sample(chosen, sample_size)) if sample_size else set()
    merged = list(train)
    for seed_id in sorted(picked):
        merged.extend(grouped[seed_id])
    return merged


def apply_corrections(samples: Sequence[Sample], corrections: dict[str, list[str]]) -> list[Sample]:
    fixed = []
    for item in samples:
        labels = corrections.get(item.id, item.labels)
        fixed.append(make_sample(id=item.id, text=item.text, labels=labels,
                                 origin=item.origin, target_labels=item.target_labels,
                                 prelabels=item.prelabels, seed_id=item.seed_id))
    return fixed


def assemble_splits(synthetic: Sequence[Sample], *, real: Sequence[Sample] = (),
                    paraphrases: Sequence[Sample] = (), seed: int = 42) -> dict[str, list[Sample]]:
    unique, _ = dedupe(synthetic)
    splits = iterative_split(unique, seed=seed)
    train = list(splits["train"])
    if real:
        # 真实问题若与验证/测试集重复，会变成同题跨集合泄漏；宁可丢样本也不污染评测。
        held = [*splits["val"], *splits["test"]]
        fresh_real = [item for item in real
                      if all(jaccard(item.text, other.text) < 0.9 for other in held)]
        train.extend(fresh_real)
    rng = random.Random(seed)
    augmented = [make_sample(id=f"{item.id}-syn", text=apply_synonyms(item.text, rng),
                             labels=item.labels, origin="synonym",
                             target_labels=item.target_labels)
                 for item in train]
    originals = {item.id: item.text for item in train}
    train.extend(item for item in augmented
                 if item.text != originals[item.id.removesuffix("-syn")])
    if paraphrases:
        train = attach_paraphrases(train, paraphrases, seed=seed)
    kept, _ = dedupe(train)
    return {"train": kept, "val": list(splits["val"]), "test": list(splits["test"])}
