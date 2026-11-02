"""构建 ch10 主题分类语料：生成、预标、改写、抽审、真实池、切分。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.models import LowConfidenceQuestion
from app.topics.clean import clean_text, mask_pii
from app.topics.dataset import (
    Sample, apply_corrections, assemble_splits, dedupe, make_sample, read_jsonl, trigrams,
    write_jsonl,
)
from app.topics.prompts import (
    generate_prompt, paraphrase_prompt, parse_payload, prelabel_prompt, typo_prompt,
)
from app.topics.taxonomy import LABELS, OTHER, order_labels

QUOTAS = {label: (80 if label == OTHER else 70) for label in LABELS}
COMBO_EACH = 30
TOTAL = 1200
COMBOS = (
    ("退换货", "尺码"), ("运费", "物流"), ("价保", "优惠活动"), ("发票", "订单修改"),
    ("会员积分", "运费"), ("库存补货", "保修维修"), ("质量问题", "退换货"),
    ("支付", "订单修改"), ("商品信息", "库存补货"), ("账号", "会员积分"),
)
PII = re.compile(r"1[3-9]\d{9}|\d{17}[\dXx]|\d{6,}")


def plan_generation() -> list[dict]:
    """300 条多标签 + 配额缺口单标签 + 轮转补齐到 1200 条。"""
    support: Counter[str] = Counter()
    for combo in COMBOS:
        for label in combo:
            support[label] += COMBO_EACH
    plan = [{"labels": list(combo), "count": COMBO_EACH} for combo in COMBOS]
    used = COMBO_EACH * len(COMBOS)
    for label in LABELS:
        deficit = max(0, QUOTAS[label] - support[label])
        if deficit:
            plan.append({"labels": [label], "count": deficit})
            used += deficit
    extra = TOTAL - used
    if extra < 0:
        raise ValueError(f"配额总和 {used} 超过 {TOTAL}，请检查 COMBOS 与 QUOTAS")
    base, remainder = divmod(extra, len(LABELS))
    for index, label in enumerate(LABELS):
        count = base + (1 if index < remainder else 0)
        if count:
            plan.append({"labels": [label], "count": count})
    return plan


def _ask(llm, prompt: str) -> str:
    result = llm.invoke(prompt)
    return str(getattr(result, "content", result))


def valid_question(text: str) -> bool:
    return 4 <= len(text) <= 60 and not PII.search(text)


def generate_group(llm, labels: list[str], count: int, batch: int = 10,
                   retries: int = 1) -> list[str]:
    questions: list[str] = []
    while len(questions) < count:
        need = min(batch, count - len(questions))
        accepted = None
        for _ in range(retries + 1):
            try:
                payload = parse_payload(_ask(llm, generate_prompt(labels, need)))
                items = [clean_text(str(row.get("question", ""))) for row in payload.get("items", [])]
                items = [mask_pii(item) for item in items if valid_question(item)]
                if items:
                    accepted = items[:need]
                    break
            except Exception:
                continue
        if not accepted:
            raise RuntimeError(f"生成失败：{labels} 还差 {count - len(questions)} 条")
        questions.extend(accepted)
    return questions


def run_generate(llm, out: Path, report_dir: Path, *, seed: int = 42) -> list[Sample]:
    samples = []
    counter = 1
    seen_grams: list[set[str]] = []

    def duplicate(text: str) -> bool:
        grams = trigrams(text)
        if not grams:
            return True
        if any(len(grams & other) / len(grams | other) >= 0.9 for other in seen_grams):
            return True
        seen_grams.append(grams)
        return False

    for group in plan_generation():
        need = group["count"]
        texts: list[str] = []
        attempts = 0
        while len(texts) < need:
            attempts += 1
            if attempts > 3:
                raise RuntimeError(f"生成失败：{group['labels']} 去重后仍差 {need - len(texts)} 条")
            # 多要 2 条做缓冲；近似重复在收集阶段就丢弃，避免最终 dedupe 打乱各类配额。
            for text in generate_group(llm, group["labels"], need - len(texts) + 2):
                if duplicate(text):
                    continue
                texts.append(text)
                if len(texts) == need:
                    break
        for text in texts:
            samples.append(make_sample(id=f"syn-{counter:04d}", text=text,
                                       labels=group["labels"], target_labels=group["labels"]))
            counter += 1
    kept, dropped = dedupe(samples)
    if dropped:
        raise RuntimeError(f"生成后去重掉 {dropped} 条，不足 {TOTAL}；请重跑 --generate")
    if len(kept) != TOTAL:
        raise RuntimeError(f"生成条数 {len(kept)} != {TOTAL}")
    write_jsonl(out, kept)
    write_report(report_dir, "dataset", _dataset_stats(kept, {}, {}))
    print(f"生成完成：{len(kept)} 条 → {out}")
    return kept


def _dataset_stats(samples: list[Sample], splits: dict, extra: dict) -> dict:
    counts = Counter(label for item in samples for label in item.labels)
    multi = sum(1 for item in samples if len(item.labels) > 1)
    agree = sum(1 for item in samples if item.prelabels and set(item.prelabels) == set(item.labels))
    compared = sum(1 for item in samples if item.prelabels)
    stats = {
        "total": len(samples),
        "per_label": {label: counts[label] for label in LABELS},
        "multi_label": multi,
        "prelabel_agree": f"{agree}/{compared}" if compared else "未预标",
        "splits": {name: len(items) for name, items in splits.items()},
        **extra,
    }
    return stats


def write_report(report_dir: Path, kind: str, stats: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"ch10-{kind}-{stamp}.md"
    lines = [f"# ch10 {kind} 报告", "", f"- 生成时间：{stamp}", ""]
    for key, value in stats.items():
        lines.append(f"- {key}：{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_prelabel(llm, path: Path, batch: int = 20) -> list[Sample]:
    samples = read_jsonl(path)
    if not samples:
        print(f"预标完成：0 条（{path} 为空）")
        return []
    updated = []
    succeeded = 0
    for start in range(0, len(samples), batch):
        chunk = samples[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, prelabel_prompt([(s.id, s.text) for s in chunk])))
            got = {str(row.get("id")): row.get("labels", []) for row in payload.get("items", [])}
            succeeded += 1
        except Exception:
            got = {}
        for item in chunk:
            labels = order_labels([label for label in got.get(item.id, []) if label in LABELS])
            updated.append(replace(item, prelabels=labels))
    if succeeded == 0:
        # LLM 全挂时不能拿空预标覆盖已有文件，否则人工抽审过的结果会被清掉。
        print("预标失败：所有批次均未成功，保留原文件不覆盖。", file=sys.stderr)
        return samples
    write_jsonl(path, updated)
    agree = sum(1 for item in updated if set(item.prelabels) == set(item.labels))
    print(f"预标完成：{len(updated)} 条，与目标标签一致 {agree} 条（{agree / len(updated):.1%}）")
    return updated


def run_paraphrase(llm, in_path: Path, out_path: Path, batch: int = 10) -> tuple[int, int]:
    seeds = read_jsonl(in_path)
    rewrites: dict[str, str] = {}
    for start in range(0, len(seeds), batch):
        chunk = seeds[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, paraphrase_prompt([(s.id, s.text) for s in chunk])))
        except Exception:
            continue
        known = {s.id: s for s in chunk}
        for row in payload.get("items", []):
            seed_id = str(row.get("id", ""))
            text = clean_text(str(row.get("text", "")))
            if seed_id in known and text and text != known[seed_id].text:
                rewrites[seed_id] = text
    candidates = [(f"par-{index:04d}", seed_id, rewrites[seed_id])
                  for index, seed_id in enumerate([s.id for s in seeds if s.id in rewrites], 1)]
    labels_by_id: dict[str, list[str]] = {}
    for start in range(0, len(candidates), 20):
        chunk = [(item_id, text) for item_id, _, text in candidates[start:start + 20]]
        try:
            payload = parse_payload(_ask(llm, prelabel_prompt(chunk)))
        except Exception:
            continue
        for row in payload.get("items", []):
            labels_by_id[str(row.get("id"))] = order_labels(
                [label for label in row.get("labels", []) if label in LABELS])
    seed_by_id = {s.id: s for s in seeds}
    kept, dropped = [], 0
    for item_id, seed_id, text in candidates:
        labels = labels_by_id.get(item_id, [])
        seed = seed_by_id[seed_id]
        if not labels or set(labels) != set(seed.labels):
            dropped += 1
            continue
        kept.append(make_sample(id=item_id, text=text, labels=labels, origin="paraphrase",
                                target_labels=seed.labels, prelabels=labels, seed_id=seed_id))
    write_jsonl(out_path, kept)
    print(f"改写完成：保留 {len(kept)} 条，丢弃 {dropped} 条（预标不一致）")
    return len(kept), dropped


def export_audit(samples: list[Sample], out: Path, per_class: int = 2,
                 max_disagree: int = 20, seed: int = 42) -> int:
    rng = random.Random(seed)
    picked: dict[str, Sample] = {}
    for label in LABELS:
        pool = [item for item in samples if label in item.labels]
        for item in rng.sample(pool, min(per_class, len(pool))):
            picked[item.id] = item
    disagree = [item for item in samples if set(item.prelabels) != set(item.labels)]
    for item in rng.sample(disagree, min(max_disagree, len(disagree))):
        picked.setdefault(item.id, item)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "prelabels", "correct_labels", "note"])
        for item in sorted(picked.values(), key=lambda row: row.id):
            writer.writerow([item.id, item.text, ",".join(item.prelabels), "", ""])
    print(f"审计样本已导出：{len(picked)} 条 → {out}（请人工填 correct_labels 与 note）")
    return len(picked)


def _read_corrections(path: Path) -> dict[str, list[str]]:
    corrections = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            corrections[str(row["id"])] = row["labels"]
    return corrections


def run_apply_audit(path: Path, audit: Path) -> tuple[int, int]:
    samples = read_jsonl(path)
    corrections = _read_corrections(audit)
    fixed = apply_corrections(samples, corrections)
    changed = sum(1 for before, after in zip(samples, fixed) if before.labels != after.labels)
    write_jsonl(path, fixed)
    print(f"审计修正已合并：{len(corrections)} 条修正，实际改动 {changed} 条")
    return len(corrections), changed


def run_pull_real(settings: Settings, llm, out_csv: Path, batch: int = 20) -> int:
    engine = build_engine(build_database_url(settings))
    try:
        with build_session_factory(engine)() as session:
            rows = session.execute(
                select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question)
                .order_by(LowConfidenceQuestion.id)
            ).all()
    finally:
        engine.dispose()
    seen, unique = set(), []
    for question_id, raw in rows:
        text = clean_text(mask_pii(raw or ""))
        if text and text not in seen:
            seen.add(text)
            unique.append((f"real-{question_id}", text))
    fixed: dict[str, str] = {}
    for start in range(0, len(unique), batch):
        chunk = unique[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, typo_prompt(chunk)))
        except Exception:
            continue
        for row in payload.get("items", []):
            # 模型可能把抱怨里的号码“补”回来；写 CSV 前再脱敏一次。
            text = clean_text(mask_pii(str(row.get("text", ""))))
            if text:
                fixed[str(row.get("id"))] = text
    pairs = [(item_id, fixed.get(item_id, text)) for item_id, text in unique]
    labels: dict[str, list[str]] = {}
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, prelabel_prompt(chunk)))
        except Exception:
            continue
        for row in payload.get("items", []):
            labels[str(row.get("id"))] = order_labels(
                [label for label in row.get("labels", []) if label in LABELS])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "prelabels", "correct_labels", "note"])
        for item_id, text in pairs:
            writer.writerow([item_id, text, ",".join(labels.get(item_id, [])), "", ""])
    print(f"真实池已导出：{len(pairs)} 条不同问法 → {out_csv}（全部需人工确认）")
    return len(pairs)


def run_apply_real_audit(audit: Path, out: Path) -> int:
    samples = []
    with audit.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            # 真实行没有“目标标签”，人工留空视为同意预标；预标也空才跳过
            raw = (row.get("correct_labels") or row.get("prelabels") or "")
            raw = raw.replace("，", ",").replace("、", ",")
            labels = [part.strip() for part in raw.split(",") if part.strip()]
            if not labels:
                continue
            samples.append(make_sample(id=row["id"], text=row["text"], labels=labels,
                                       origin="real", target_labels=labels))
    kept, dropped = dedupe(samples)
    write_jsonl(out, kept)
    print(f"真实训练集已写入：{len(kept)} 条（去重 {dropped} 条）→ {out}")
    return len(kept)


def run_split(synthetic_path: Path, out_dir: Path, *, real_path: Path | None = None,
              paraphrase_path: Path | None = None, seed: int = 42,
              report_dir: Path | None = None) -> dict[str, list[Sample]]:
    synthetic, synthetic_dropped = dedupe(read_jsonl(synthetic_path))
    real = read_jsonl(real_path) if real_path and real_path.exists() else []
    real, real_dropped = dedupe(real)
    paraphrases = read_jsonl(paraphrase_path) if paraphrase_path and paraphrase_path.exists() else []
    splits = assemble_splits(synthetic, real=real, paraphrases=paraphrases, seed=seed)
    for name, items in splits.items():
        write_jsonl(out_dir / f"{name}.jsonl", items)
    if report_dir is not None:
        write_report(report_dir, "dataset", _dataset_stats(
            synthetic, splits, {"real_train": len(real), "paraphrase_pool": len(paraphrases),
                                "dedupe_synthetic": synthetic_dropped,
                                "dedupe_real": real_dropped}))
    print("切分完成：" + "，".join(f"{name}={len(items)}" for name, items in splits.items()))
    return splits


def build_llm():
    from app.model import build_model

    return build_model(Settings.from_env())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ch10 主题分类语料构建")
    group = parser.add_mutually_exclusive_group(required=True)
    for flag in ("generate", "prelabel", "paraphrase", "audit-sample", "apply-audit",
                 "pull-real", "apply-real-audit", "split"):
        group.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--in", dest="in_path", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--real", type=Path, default=Path("data/topics/real_train.jsonl"))
    parser.add_argument("--paraphrase-path", dest="paraphrase_path", type=Path,
                        default=Path("data/topics/paraphrase_v1.jsonl"))
    parser.add_argument("--report-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--count", type=int, default=TOTAL)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.generate:
        if args.count != TOTAL:
            parser.error(f"--count 固定为 {TOTAL}")
        run_generate(build_llm(), args.out or Path("data/topics/synthetic_v1.jsonl"),
                     args.report_dir, seed=args.seed)
    elif args.prelabel:
        run_prelabel(build_llm(), args.in_path or Path("data/topics/synthetic_v1.jsonl"))
    elif args.paraphrase:
        run_paraphrase(build_llm(), args.in_path or Path("data/topics/synthetic_v1.jsonl"),
                       args.out or Path("data/topics/paraphrase_v1.jsonl"))
    elif args.audit_sample:
        export_audit(read_jsonl(args.in_path or Path("data/topics/synthetic_v1.jsonl")),
                     args.out or Path("evals/topics/audit_sample.csv"), seed=args.seed)
    elif args.apply_audit:
        run_apply_audit(args.in_path or Path("data/topics/synthetic_v1.jsonl"),
                        args.audit or Path("evals/topics/audit_corrections.jsonl"))
    elif args.pull_real:
        run_pull_real(Settings.from_env(), build_llm(),
                      args.out or Path("data/topics/real_audit.csv"))
    elif args.apply_real_audit:
        run_apply_real_audit(args.audit or Path("data/topics/real_audit.csv"),
                             args.out or args.real)
    else:
        run_split(args.in_path or Path("data/topics/synthetic_v1.jsonl"),
                  args.out or Path(".cache/topics/splits"), real_path=args.real,
                  paraphrase_path=args.paraphrase_path, seed=args.seed, report_dir=args.report_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
