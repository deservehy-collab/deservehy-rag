"""证据置信度：把 ch05 的「rerank top1 单阈值」升级成三信号打分（规格 §6）。

权重固定（w_top1=0.7/w_count=0.2/w_margin=0.1）——80 题标注撑不起四维网格，
只校准 confidence_min 与 effective_floor 两个数，见 scripts/calibrate_confidence.py。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvidenceConfidence:
    value: float
    signals: dict = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict:
        return {"value": round(self.value, 4), "signals": self.signals, "reason": self.reason}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def score(items, *, w_top1: float = 0.7, w_count: float = 0.2, w_margin: float = 0.1,
          effective_floor: float = 0.15) -> EvidenceConfidence:
    """三信号加权。items 为空 → value=0（必拦）；无 rerank 分 → value=1（不拦，兼容非精排策略）。

    只有一条命中时 margin 记为 top1 本身：没有竞争者，等价于「全部相关性都归它」（规格 §6.1）。

    signals 里的 ``norm_count`` 不是第四个独立信号，而是由 ``effective_count`` 派生：
    ``min(effective_count / 3, 1.0)``，即「3 条及以上视为证据充分」的归一化分量。
    它单独出现在 signals 里只为 T7 校准与 T9 落池时能直接看到归一化后的分量，权重仍只有
    top1 / count / margin 三项。

    调用方须保证同一批 items 的 rerank 分口径一致（例如同一精排模型的输出）；混合分数时
    没有 rerank 的条目会被排除在计数之外，但 items 非空仍会把结果推进打分分支而不是
    ``no_evidence``。正常路径 ``reason=""``；两个短路值是机器短码 ``no_evidence`` /
    ``no_rerank``，落池要的人话由 T9 拼。``EvidenceConfidence`` 里装的是 dict，**不可 hash**；
    要拿它当缓存 key 请用 ``(case_id, effective_floor)`` 这类原始入参。
    """
    items = list(items)
    reranks = [float(item.scores["rerank"]) for item in items if "rerank" in (item.scores or {})]
    if not reranks:
        if items:
            return EvidenceConfidence(1.0, {"top1": 0.0, "effective_count": 0, "margin": 0.0,
                                            "norm_count": 0.0}, "no_rerank")
        return EvidenceConfidence(0.0, {"top1": 0.0, "effective_count": 0, "margin": 0.0,
                                       "norm_count": 0.0}, "no_evidence")
    reranks.sort(reverse=True)
    top1 = reranks[0]
    effective_count = sum(1 for value in reranks if value >= effective_floor)
    norm_count = min(effective_count / 3, 1.0)
    margin = top1 - reranks[1] if len(reranks) > 1 else top1
    signals = {"top1": round(top1, 4), "effective_count": effective_count,
               "margin": round(margin, 4), "norm_count": round(norm_count, 4)}
    value = _clamp01(w_top1 * top1 + w_count * norm_count + w_margin * margin)
    return EvidenceConfidence(value, signals, "")


def snapshot_of(items, limit: int = 5, answer_chars: int = 500) -> list[dict]:
    """落池/落消息的召回快照：低置信时 items 仍在（retrieval.py 只清 evidence），不重跑检索。"""
    items = list(items)
    rows = []
    for index, item in enumerate(items[: max(0, limit)], 1):
        rows.append({
            "n": index,
            "chunk_id": item.id,
            "question": item.question,
            "answer": (item.answer or "")[:answer_chars],
            "section_path": item.section_path,
            "scores": dict(item.scores or {}),
        })
    return rows
