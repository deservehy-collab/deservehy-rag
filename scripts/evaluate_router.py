"""用真实模型跑多轮指代消解 + 意图标注集，验证「物流→退款→物流」这类串味场景。"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.model import build_model
from app.workflow.classifier import IntentClassifier
from app.workflow.coref import QuestionResolver
from app.workflow.routing import fast_chitchat

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/multiturn_cases.jsonl"
REPORT_DIR = ROOT / "evals/reports"
INTENT_GATE = 1.00
SPAN_GATE = 1.00


def load_cases() -> list[dict]:
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise SystemExit("多轮评估集为空")
    return rows


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    resolver = QuestionResolver(model, timeout=settings.workflow_understanding_timeout_seconds)
    classifier = IntentClassifier(model, timeout=settings.workflow_intent_timeout_seconds)
    rows = load_cases()

    intent_hits = intent_total = span_hits = span_total = 0
    failures = []
    print(f"模型：{settings.openai_model}；场景：{len(rows)}")
    for case in rows:
        previous = None
        print(f"\n[{case['name']}]")
        for index, turn in enumerate(case["turns"], start=1):
            resolved = await resolver.resolve(turn["text"], previous_question=previous)
            if fast_chitchat(resolved):
                intent, confidence = "闲聊", 1.0
            else:
                result = await classifier.classify(resolved, previous)
                intent, confidence = result.intent, float(result.confidence)
            expected = turn["intent"]
            missing = [piece for piece in turn.get("contains") or [] if piece not in resolved]
            intent_ok = intent == expected
            intent_total += 1
            intent_hits += intent_ok
            span_total += len(turn.get("contains") or [])
            span_hits += len(turn.get("contains") or []) - len(missing)
            mark = "OK " if intent_ok and not missing else "ERR"
            print(f"  {mark} 第{index}轮：{turn['text']} → {resolved}｜{intent}（{confidence:.2f}）")
            if not intent_ok or missing:
                failures.append((case["name"], index, turn["text"], resolved, expected, intent, missing))
            previous = resolved
        print()

    intent_accuracy = intent_hits / intent_total if intent_total else 0.0
    span_rate = (span_hits / span_total) if span_total else 1.0
    passed = intent_accuracy >= INTENT_GATE and span_rate >= SPAN_GATE
    lines = [
        "# ch06 多轮指代与意图评估",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{settings.openai_model}",
        f"- 场景：{len(rows)}；轮次：{intent_total}",
        f"- 意图准确率：{intent_accuracy:.3f}（门槛 {INTENT_GATE:.2f}）",
        f"- 片段命中率：{span_rate:.3f}（门槛 {SPAN_GATE:.2f}）",
        f"- 结论：{'通过' if passed else '未达标，先改 prompt/示例，不改路由'}",
        "",
        "## 错例",
        "",
    ]
    if failures:
        for name, index, text, resolved, expected, actual, missing in failures:
            detail = f"，缺少片段 {'、'.join(missing)}" if missing else ""
            lines.append(f"- {name} 第 {index} 轮 `{text}`：补全为 `{resolved}`，意图 {actual}（期望 {expected}）{detail}")
    else:
        lines.append("- 无")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"ch06-multiturn-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"意图准确率 {intent_accuracy:.3f}；片段命中率 {span_rate:.3f}；报告：{report}")

    await model.root_async_client.close()
    model.root_client.close()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
