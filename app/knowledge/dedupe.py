import hashlib
import math
import re
import unicodedata

_PUNCTUATION = re.compile(r"[\s，。！？、；：,.!?;:（）()\[\]【】“”‘’\"'`~·…—_-]+")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    return _PUNCTUATION.sub("", unicodedata.normalize("NFKC", text).lower())


def content_key(category: str, questions: str, answer: str) -> str:
    raw = "\x1f".join((normalize(category), normalize(questions), normalize(answer)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pair_key(question: str, answer: str) -> str:
    raw = "\x1f".join((normalize(question), normalize(answer)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("向量维度不一致")
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def is_semantic_duplicate(vector: list[float], existing: list[list[float]], threshold: float) -> bool:
    return any(cosine(vector, other) >= threshold for other in existing)
