"""ch09 置信度阈值校准（规格 §6.2）：只扫 effective_floor × confidence_min 两个数。

权重不进网格（w_top1=0.7/w_count=0.2/w_margin=0.1 固定），80 题标注不够拟合 4 个自由参数。

跑法：
    python scripts/calibrate_confidence.py                 # 现拉现算
    python scripts/calibrate_confidence.py --cache evals/reports/ch09-calibrate-cache.json
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import build_database_url, build_engine, build_session_factory  # noqa: E402
from app.knowledge.confidence import score  # noqa: E402
from app.knowledge.embedding import BgeM3Embedder  # noqa: E402
from app.knowledge.metrics import summarize  # noqa: E402
from app.knowledge.rerank import BgeReranker  # noqa: E402
from app.knowledge.retrieval import KnowledgeRetriever  # noqa: E402
from app.knowledge.vector_store import MilvusVectorStore  # noqa: E402
from app.models import KnowledgeChunk  # noqa: E402

FLOORS = (0.10, 0.15, 0.20, 0.25, 0.30)
THRESHOLDS = tuple(round(0.05 * step, 2) for step in range(0, 20))  # 0.00..0.95
WEIGHTS = {"w_top1": 0.7, "w_count": 0.2, "w_margin": 0.1}


RETRIEVAL_KEYS = ("knowledge_strategy", "knowledge_candidates", "knowledge_top_k", "knowledge_rrf_k",
                  "knowledge_rerank_model", "embedding_model")


def dataset_digest(cases) -> str:
    """评估集指纹：改了题面/期望段落就变，用来判断缓存还能不能复用。"""
    canonical = json.dumps(cases, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def cache_meta(settings, cases) -> dict:
    meta = {"dataset_digest": dataset_digest(cases), "dataset_size": len(cases)}
    meta.update({key: getattr(settings, key) for key in RETRIEVAL_KEYS})
    return meta


def load_cache(path: Path, cases, *, expected: dict | None = None) -> dict | None:
    """读缓存；评估集指纹或检索参数对不上就返回 None 让调用方重算，不静默拿旧数出报告。

    返回 {"meta": {...}, "per_case": [...]}；旧格式（裸列表）没有 meta，meta 如实留空，
    由调用方在报告里标「未记录」（不拿当前设置冒充）。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"缓存 {path} 读不出来或不是合法 JSON，重算。")
        return None
    if isinstance(payload, dict):
        rows = payload.get("per_case")
        meta = payload.get("meta") or {}
        if not isinstance(rows, list):
            print(f"缓存 {path} 缺少 per_case 列表，重算。")
            return None
        if meta.get("dataset_digest") != dataset_digest(cases):
            print(f"缓存 {path} 与当前评估集不是同一份（dataset_digest 不一致），重算。")
            return None
        if expected:
            changed = [key for key in RETRIEVAL_KEYS if meta.get(key) != expected.get(key)]
            if changed:
                print(f"缓存 {path} 的检索参数已变（{', '.join(changed)}），重算。")
                return None
    elif isinstance(payload, list):
        rows, meta = payload, {}
        print(f"缓存 {path} 是旧格式（无 meta），只校验题目；检索参数未记录。")
    else:
        print(f"缓存 {path} 结构不认识，重算。")
        return None
    if not all(isinstance(row, dict) for row in rows) \
            or [row.get("id") for row in rows] != [case["id"] for case in cases]:
        print(f"缓存 {path} 的题目列表与当前评估集对不上，重算。")
        return None
    return {"meta": meta, "per_case": rows}


def select_cell(grid):
    """先要求不可答拒答率=1.000（ch04 已达成的水平，不许退步），再取误拒最小，平手取阈值较大。"""
    qualified = [row for row in grid if row["unanswerable_refusal_rate"] >= 1.0]
    if not qualified:
        return None
    return min(qualified, key=lambda row: (row["answerable_false_refusal_rate"], -row["confidence_min"]))


def per_case_signals(cases, retriever) -> list[dict]:
    rows = []
    for case in cases:
        canonical = retriever.rewrite(case["question"])
        result = retriever.retrieve(case["question"], strategy="hybrid_rerank", canonical=canonical)
        reranks = [item.scores["rerank"] for item in result.items if "rerank" in (item.scores or {})]
        rows.append({"id": case["id"], "bucket": case["bucket"], "answerable": case["answerable"],
                     "reranks": [round(value, 6) for value in reranks],
                     "expected_sections": sorted(set(case["expected_sections"])),
                     "hit_sections": [item.section_path for item in result.items if item.section_path]})
        print(f"  {case['id']} reranks={len(reranks)}条 top1={reranks[0]:.3f}" if reranks
              else f"  {case['id']} 无命中")
    return rows


def blocked(reranks, *, floor, threshold) -> bool:
    class Row:
        def __init__(self, value):
            self.scores = {"rerank": value}

    return score([Row(value) for value in reranks], effective_floor=floor, **WEIGHTS).value < threshold


def grid_rows(per_case: list[dict]) -> list[dict]:
    unanswerable = [row for row in per_case if not row["answerable"]]
    answerable = [row for row in per_case if row["answerable"]]
    grid = []
    for floor in FLOORS:
        for threshold in THRESHOLDS:
            refused_un = sum(1 for row in unanswerable if blocked(row["reranks"], floor=floor,
                                                                  threshold=threshold))
            refused_ans = sum(1 for row in answerable if blocked(row["reranks"], floor=floor,
                                                                 threshold=threshold))
            grid.append({
                "effective_floor": floor,
                "confidence_min": threshold,
                "unanswerable_refusal_rate": round(refused_un / len(unanswerable), 4) if unanswerable else 0.0,
                "answerable_false_refusal_rate": round(refused_ans / len(answerable), 4) if answerable else 0.0,
                "false_refusal_count": refused_ans,
            })
    return grid


def selected_scores(per_case, *, floor, threshold) -> dict:
    """选中格子的连带影响：误拒的题按「检索结果不可用」记 0（不拿「放宽阈值」换指标好看）。

    返回 recall@10 与 mrr@10 两个数——函数名与返回值必须对得上，别叫 recall_at_10 却返回 mrr。
    """
    from app.knowledge.metrics import recall_at, reciprocal_rank

    rows = []
    for case in per_case:
        if not case["answerable"]:
            continue
        refused = blocked(case["reranks"], floor=floor, threshold=threshold)
        sections = [] if refused else case["hit_sections"]
        expected = set(case["expected_sections"])
        rows.append({"answerable": True,
                     "recall@5": recall_at(sections, expected, 5),
                     "recall@10": recall_at(sections, expected, 10),
                     "mrr@10": reciprocal_rank(sections, expected, 10),
                     "refused": refused})
    summary = summarize(rows)
    return {"recall@10": summary["recall@10"], "mrr@10": summary["mrr@10"]}


def render_markdown(report: dict) -> str:
    lines = ["# ch09 置信度阈值校准", "",
             f"- 生成时间：{report['generated_at']}",
             f"- 数据集：{report['dataset_size']} 题（不可答 {report['unanswerable']} 题），策略 hybrid_rerank",
             f"- 固定权重：{report['weights']}",
             f"- 检索参数：{report.get('retrieval') or '未记录'}", "",
             "| effective_floor | confidence_min | 不可答拒答率 | 可答误拒率 | 误拒题数 |",
             "|---|---|---|---|---|"]
    selected_key = ((report["selected"] or {}).get("effective_floor"),
                    (report["selected"] or {}).get("confidence_min"))
    for row in report["grid"]:
        # 不能比对象同一性：报告从 JSON 读回来时 selected 与 grid 里的格子不是同一个对象。
        mark = " **← 选中**" if (row["effective_floor"], row["confidence_min"]) == selected_key else ""
        lines.append(f"| {row['effective_floor']:.2f} | {row['confidence_min']:.2f} | "
                     f"{row['unanswerable_refusal_rate']:.3f} | "
                     f"{row['answerable_false_refusal_rate']:.3f} | {row['false_refusal_count']}{mark} |")
    selected = report["selected"]
    lines += ["", f"选中：`confidence_min={selected['confidence_min']:.2f}`、"
                  f"`effective_floor={selected['effective_floor']:.2f}`；"
                  f"该格 Recall@10={report['recall@10']:.3f}、MRR@10={report['mrr@10']:.3f}"
                  "（被误拒的题按 0 计）。",
              "", "选择规则：只保留不可答拒答率=1.000 的格子 → 取可答误拒率最小 → 平手取阈值较大。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="ch09 置信度阈值校准。")
    parser.add_argument("--cache", help="缓存/复用逐题 rerank 分的 JSON 路径")
    args = parser.parse_args()

    cases = json.loads((ROOT / "evals/retrieval_qa.json").read_text(encoding="utf-8"))
    settings = Settings.from_env()
    meta = cache_meta(settings, cases)
    cache = Path(args.cache) if args.cache else None
    loaded = load_cache(cache, cases, expected=meta) if cache and cache.exists() else None
    if loaded is not None:
        per_case = loaded["per_case"]
        meta = loaded["meta"] or {}
        print(f"复用缓存：{cache}（{len(per_case)} 题；检索参数 {meta or '未记录（旧格式缓存）'}）")
    else:
        engine = build_engine(build_database_url(settings))
        factory = build_session_factory(engine)
        store = MilvusVectorStore(settings.milvus_uri, settings.milvus_collection)
        embedder = BgeM3Embedder(settings.embedding_model, device=settings.embedding_device,
                                 batch_size=settings.embedding_batch_size)
        reranker = BgeReranker(settings.knowledge_rerank_model,
                               device=settings.knowledge_rerank_device,
                               batch_size=settings.knowledge_rerank_batch_size)
        from app.model import build_model
        from app.knowledge.rewrite import QueryRewriter

        retriever = KnowledgeRetriever(
            factory, embedder, store, candidates=settings.knowledge_candidates,
            top_k=settings.knowledge_top_k, rrf_k=settings.knowledge_rrf_k,
            rewrite=QueryRewriter(build_model(settings), enabled=settings.knowledge_query_rewrite),
            reranker=reranker,
            strategy="hybrid_rerank",
        )
        try:
            with factory() as session:
                known = {row for row in session.scalars(select(KnowledgeChunk.section_path)) if row}
            missing = [case["id"] for case in cases
                       if not set(case["expected_sections"]) <= known]
            if missing:
                print(f"这些题的 expected_sections 不在知识库里：{missing}")
                return 2
            print(f"逐题检索 {len(cases)} 题（一次，网格只在缓存上扫）...")
            per_case = per_case_signals(cases, retriever)
        finally:
            store.close()
            engine.dispose()
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"meta": meta, "per_case": per_case},
                                        ensure_ascii=False, indent=2), encoding="utf-8")

    grid = grid_rows(per_case)
    selected = select_cell(grid)
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "dataset_size": len(per_case),
              "unanswerable": sum(1 for row in per_case if not row["answerable"]),
              "retrieval": meta, "weights": WEIGHTS, "grid": grid, "selected": selected,
              "per_case": per_case}
    if selected is None:
        near = sorted(grid, key=lambda row: (-row["unanswerable_refusal_rate"],
                                             row["answerable_false_refusal_rate"]))[:8]
        report["near_miss"] = near
        print("没有任何格子能把不可答拒答率做到 1.000。最接近的几格：")
        for row in near:
            print(f"  floor={row['effective_floor']:.2f} min={row['confidence_min']:.2f} "
                  f"拒答={row['unanswerable_refusal_rate']:.3f} 误拒={row['false_refusal_count']}")
        print("→ 按规格 §6.2：停下来问用户，不擅自放宽「不许退步」这条约束。")
        return 1

    report.update(selected_scores(per_case, floor=selected["effective_floor"],
                                  threshold=selected["confidence_min"]))
    directory = ROOT / "evals/reports"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (directory / f"ch09-confidence-{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / f"ch09-confidence-{stamp}.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"选中 confidence_min={selected['confidence_min']:.2f} "
          f"effective_floor={selected['effective_floor']:.2f} "
          f"误拒 {selected['false_refusal_count']} 题 "
          f"Recall@10={report['recall@10']:.3f} MRR@10={report['mrr@10']:.3f}")
    print(f"报告：evals/reports/ch09-confidence-{stamp}.md|json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
