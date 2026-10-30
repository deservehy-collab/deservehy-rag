"""用真实模型跑意图标注集，输出总准确率、平均置信度、每类准确率和错例。"""

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.model import build_model
from app.schemas import IntentResult
from app.workflow.classifier import IntentClassifier
from app.workflow.routing import INTENTS, fast_chitchat

CASES = Path("evals/intent_cases.jsonl")
REPORT_DIR = Path("evals/reports")


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    classifier = IntentClassifier(model, timeout=settings.workflow_intent_timeout_seconds)
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]

    hits, per_class, misses, fast = 0, defaultdict(lambda: [0, 0]), [], 0
    confidences, low_confidence = [], []
    for row in rows:
        expected = row["intent"]
        shortcut = fast_chitchat(row["text"])
        fast += shortcut
        result = (IntentResult(intent="闲聊", confidence=1.0) if shortcut
                  else await classifier.classify(row["text"]))
        actual = result.intent
        confidences.append(float(result.confidence))
        if result.confidence < settings.workflow_intent_min_confidence:
            low_confidence.append((row["text"], actual, result.confidence))
        per_class[expected][1] += 1
        if actual == expected:
            hits += 1
            per_class[expected][0] += 1
        else:
            misses.append((row["text"], expected, actual, result.confidence, row.get("note", "")))
        print(f"{'OK ' if actual == expected else 'ERR'} {row['text']} -> {actual}"
              f"（期望 {expected}，置信度 {result.confidence:.2f}）")

    accuracy = hits / len(rows)
    average = sum(confidences) / len(confidences) if confidences else 0.0
    lines = [
        "# ch06 意图分类评估",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{settings.openai_model}",
        f"- 样本：{len(rows)}；总体准确率：{accuracy:.3f}；门槛：0.90",
        f"- 平均置信度：{average:.3f}；低于 {settings.workflow_intent_min_confidence:.2f} 的样本：{len(low_confidence)}",
        f"- 分流：快速通道命中 {fast}；模型判定 {len(rows) - fast}",
        f"- 结论：{'通过' if accuracy >= 0.90 else '未达标，先改 prompt/示例，不改路由'}",
        "",
        "| 意图 | 正确 | 总数 | 准确率 |",
        "| --- | --- | --- | --- |",
    ]
    for intent in INTENTS:
        ok, total = per_class[intent]
        if total:
            lines.append(f"| {intent} | {ok} | {total} | {ok / total:.3f} |")
    lines.append("")
    lines.append("## 低置信样本")
    lines.append("")
    if low_confidence:
        for text, actual, confidence in low_confidence:
            lines.append(f"- `{text}`：{actual}（{confidence:.2f}）")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 错例")
    lines.append("")
    for text, expected, actual, confidence, note in misses:
        lines.append(f"- `{text}`：期望 {expected}，实际 {actual}（置信度 {confidence:.2f}，{note}）")
    if not misses:
        lines.append("- 无")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"ch06-intent-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n总体准确率 {accuracy:.3f}，平均置信度 {average:.3f}，报告：{report}")

    await model.root_async_client.close()
    model.root_client.close()


if __name__ == "__main__":
    asyncio.run(main())
