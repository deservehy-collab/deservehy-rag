"""用真实模型跑扩写标注集，检查 JSON 结构、问法条数和关键词覆盖。"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.knowledge.expansion import QueryExpander
from app.model import build_model

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/expansion_cases.jsonl"
REPORT_DIR = ROOT / "evals/reports"
COVERAGE_GATE = 0.90


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    expander = QueryExpander(
        model,
        enabled=settings.workflow_query_expansion,
        max_queries=settings.workflow_query_expansion_max,
        timeout=settings.workflow_understanding_timeout_seconds,
    )
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]

    groups_total = groups_hit = 0
    failures = []
    print(f"模型：{settings.openai_model}；样本：{len(rows)}")
    for row in rows:
        queries = await expander.expand(row["text"])
        joined = " ".join(queries)
        hits = [any(word in joined for word in group) for group in row["expect_keywords"]]
        groups_total += len(hits)
        groups_hit += sum(hits)
        missing = [group for group, hit in zip(row["expect_keywords"], hits) if not hit]
        count_ok = 2 <= len(queries) <= settings.workflow_query_expansion_max
        print(f"{'OK ' if not missing and count_ok else 'ERR'} {row['text']} → {len(queries)} 条：{' ｜ '.join(queries)}")
        if missing or not count_ok:
            failures.append((row["text"], queries, missing, count_ok))

    coverage = groups_hit / groups_total if groups_total else 0.0
    passed = coverage >= COVERAGE_GATE
    lines = [
        "# ch06 问法扩写评估",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{settings.openai_model}",
        f"- 样本：{len(rows)}；关键词组：{groups_total}",
        f"- 覆盖率：{coverage:.3f}（门槛 {COVERAGE_GATE:.2f}）",
        f"- 条数要求：2–{settings.workflow_query_expansion_max} 条",
        f"- 结论：{'通过' if passed else '未达标，先改 prompt，不改检索合并逻辑'}",
        "",
        "## 错例",
        "",
    ]
    if failures:
        for text, queries, missing, count_ok in failures:
            detail = f"缺少关键词组 {missing}" if missing else ""
            if not count_ok:
                detail = (detail + "；" if detail else "") + f"条数 {len(queries)} 不在范围内"
            lines.append(f"- `{text}` → {' ｜ '.join(queries)}（{detail}）")
    else:
        lines.append("- 无")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"ch06-expansion-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"关键词覆盖率 {coverage:.3f}；报告：{report}")

    await model.root_async_client.close()
    model.root_client.close()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
