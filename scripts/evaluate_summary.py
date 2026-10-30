"""用真实模型跑摘要标注集，检查事实命中、寒暄不残留与长度区间。"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.model import build_model
from app.summary import Summarizer

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/summary_cases.jsonl"
REPORT_DIR = ROOT / "evals/reports"
GATE = 0.90
MIN_CHARS = 20
MAX_CHARS = 300


def check(text, case):
    missing = [word for word in case["must_contain"] if not text or word not in text]
    banned = [word for word in case["must_not_contain"] if text and word in text]
    length_ok = bool(text) and MIN_CHARS <= len(text) <= MAX_CHARS
    return missing, banned, length_ok, not (missing or banned) and length_ok


def report_line(case, text, missing, banned, length_ok, ok):
    detail = []
    if missing:
        detail.append(f"缺少事实 {missing}")
    if banned:
        detail.append(f"残留寒暄/客套 {banned}")
    if not length_ok:
        detail.append(f"长度 {len(text or '')} 不在 {MIN_CHARS}–{MAX_CHARS}")
    return f"- {'OK ' if ok else 'ERR'} `{case['id']}`（{len(text or '')} 字）{text or '（无输出）'}" + (
        f"｜{'；'.join(detail)}" if detail else "")


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    summarizer = Summarizer(model, settings.summary_timeout_seconds)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]

    print(f"模型：{settings.openai_model}；样本：{len(cases)}")
    rows = []
    for case in cases:
        text = await summarizer.summarize(case["turns"], case.get("previous") or [])
        result = check(text, case)
        rows.append((case, text, *result))
        print(report_line(case, text, *result))

    score = sum(1 for row in rows if row[-1]) / len(rows) if rows else 0.0
    passed = score >= GATE
    lines = [
        "# ch07 会话摘要评估",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{settings.openai_model}",
        f"- 样本：{len(rows)}；通过：{sum(1 for row in rows if row[-1])}",
        f"- 准确率：{score:.3f}（门槛 {GATE:.2f}）",
        f"- 长度要求：{MIN_CHARS}–{MAX_CHARS} 字",
        f"- 结论：{'通过' if passed else '未达标，先改摘要 prompt，不改触发与锚点逻辑'}",
        "",
        "## 逐例结果",
        "",
        *(report_line(case, text, missing, banned, length_ok, ok)
          for case, text, missing, banned, length_ok, ok in rows),
        "",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"ch07-summary-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"准确率 {score:.3f}；报告：{report}")

    await model.root_async_client.close()
    model.root_client.close()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
