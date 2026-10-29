import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.llm_json import extract_json_object
from app.knowledge.metrics import recall_at, reciprocal_rank, summarize
from app.knowledge.pool import FaithCaseLedger
from app.knowledge.rerank import BgeReranker
from app.knowledge.retrieval import KnowledgeRetriever, STRATEGIES
from app.knowledge.rewrite import QueryRewriter
from app.knowledge.vector_store import MilvusVectorStore
from app.model import build_model
from app.models import KnowledgeChunk
from app.prompts import JUDGE_SYSTEM, chat_system

ROOT = Path(__file__).resolve().parents[1]
FAITH_IDS = ("A05", "A09", "A16", "B05", "B09", "B14", "C02", "C11", "C18", "E02", "E07", "E10")


def validate(cases, known_sections):
    if len(cases) != 80:
        raise SystemExit(f"评估集应为 80 题，实际 {len(cases)} 题")
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("评估集 id 有重复")
    counts = Counter(case["bucket"] for case in cases)
    if set(counts) != {"A_policy", "B_model", "C_colloquial", "E_multi"} or any(value != 20 for value in counts.values()):
        raise SystemExit(f"四个桶应各 20 题，实际 {counts}")
    missing = sorted({section for case in cases for section in case["expected_sections"] if section not in known_sections})
    if missing:
        print("以下 expected_sections 不在当前知识库中：")
        for section in missing:
            print(" -", section)
        raise SystemExit(2)
    for case in cases:
        if case["answerable"] != bool(case["expected_sections"]):
            raise SystemExit(f"{case['id']} 的 answerable 与 expected_sections 不一致")


def run_retrieval(retriever, cases):
    results = {}
    for case in cases:
        canonical = retriever.rewrite(case["question"])
        results[case["id"]] = {}
        for strategy in STRATEGIES:
            result = retriever.retrieve(case["question"], strategy=strategy, canonical=canonical)
            sections = [item.section_path for item in result.items if item.section_path]
            expected = set(case["expected_sections"])
            results[case["id"]][strategy] = {
                "sections": sections,
                "refused": result.low_confidence,
                "low_reason": result.reason,
                "recall@5": recall_at(sections, expected, 5),
                "recall@10": recall_at(sections, expected, 10),
                "mrr@10": reciprocal_rank(sections, expected, 10),
            }
    return results


def summarize_all(cases, retrieval, strategy):
    rows, by_bucket = [], {}
    for case in cases:
        row = {**retrieval[case["id"]][strategy], "answerable": case["answerable"], "bucket": case["bucket"]}
        rows.append(row)
        by_bucket.setdefault(case["bucket"], []).append(row)
    return {"overall": summarize(rows), "buckets": {name: summarize(items) for name, items in by_bucket.items()}}


def evidence_snapshot(evidence):
    return [{"n": item.n, "chunk_id": item.id, "section_path": item.section_path,
             "question": item.question, "answer": item.answer} for item in evidence]


def evidence_text(evidence):
    return "\n\n".join(
        f"[{item.n}] 章节：{item.section_path or '无'}\n问：{item.question}\n答：{item.answer}"
        for item in evidence
    )


def generate(model, settings, question, evidence):
    system = chat_system(settings.store_name) + "\n本次只能依据编号证据回答，事实句末尾标注 [n]，不得引用证据外的信息。"
    human = f"证据：\n{evidence_text(evidence)}\n\n用户问题：{question}"
    return model.invoke([SystemMessage(content=system), HumanMessage(content=human)]).content


def judge(model, question, answer, evidence):
    payload = json.dumps({"question": question, "answer": answer,
                          "evidence": evidence_snapshot(evidence)}, ensure_ascii=False)
    try:
        reply = model.invoke([SystemMessage(content=JUDGE_SYSTEM), HumanMessage(content=payload)])
        parsed = extract_json_object(reply.content)
        faithful = parsed.get("faithful")
        if not isinstance(faithful, bool):
            raise ValueError("faithful is not bool")
        return faithful, str(parsed.get("reason") or ""), parsed.get("unsupported") or []
    except Exception:
        return None, "judge_parse_failed", []


def run_faithfulness(model, settings, retriever, cases, ledger, retrieval):
    by_id = {case["id"]: case for case in cases}
    judged = faithful_count = 0
    rows = []
    for eval_id in FAITH_IDS:
        case = by_id[eval_id]
        canonical = retriever.rewrite(case["question"])
        result = retriever.retrieve(case["question"], strategy="hybrid_rerank", canonical=canonical)
        if not result.evidence:
            rows.append({"id": eval_id, "faithful": None, "reason": "no_evidence", "answer": ""})
            continue
        answer = generate(model, settings, case["question"], result.evidence)
        faithful, reason, unsupported = judge(model, case["question"], answer, result.evidence)
        rows.append({"id": eval_id, "bucket": case["bucket"], "faithful": faithful,
                     "reason": reason, "unsupported": unsupported, "answer": answer})
        if faithful is None:
            continue
        judged += 1
        faithful_count += int(faithful)
        if not faithful:
            ledger.record(eval_id, case["bucket"], case["question"], "hybrid_rerank", answer,
                          reason, evidence_snapshot(result.evidence), settings.openai_model)
    return {"model": settings.openai_model, "cases": len(rows), "judged": judged,
            "faithful": faithful_count, "rate": faithful_count / judged if judged else 0.0, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="ch04 四策略检索评估与忠实度抽检。")
    parser.add_argument("--no-faith", action="store_true", help="只跑检索指标，不调用生成与裁判")
    parser.add_argument("--report-dir", default="evals/reports", help="报告输出目录")
    args = parser.parse_args()
    settings = Settings.from_env()
    cases = json.loads((ROOT / "evals/retrieval_qa.json").read_text(encoding="utf-8"))
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    store = MilvusVectorStore(settings.milvus_uri, settings.milvus_collection)
    embedder = BgeM3Embedder(settings.embedding_model, device=settings.embedding_device,
                             batch_size=settings.embedding_batch_size)
    model = build_model(settings)
    reranker = BgeReranker(settings.knowledge_rerank_model, device=settings.knowledge_rerank_device,
                           batch_size=settings.knowledge_rerank_batch_size)
    retriever = KnowledgeRetriever(
        factory, embedder, store, candidates=settings.knowledge_candidates, top_k=settings.knowledge_top_k,
        rrf_k=settings.knowledge_rrf_k, rewrite=QueryRewriter(model), reranker=reranker,
        strategy=settings.knowledge_strategy, rerank_min_score=settings.knowledge_rerank_min_score,
    )
    try:
        with factory() as session:
            known = {row for row in session.scalars(select(KnowledgeChunk.section_path)) if row}
        validate(cases, known)
        retrieval = run_retrieval(retriever, cases)
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": settings.model_dump(mode="json", exclude={"openai_api_key", "db_password"}),
            "dataset": {"total": len(cases), "buckets": dict(Counter(case["bucket"] for case in cases))},
            "retrieval": {strategy: summarize_all(cases, retrieval, strategy) for strategy in STRATEGIES},
            "cases": retrieval,
        }
        if not args.no_faith:
            report["faithfulness"] = run_faithfulness(model, settings, retriever, cases,
                                                      FaithCaseLedger(factory), retrieval)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = Path(args.report_dir)
        if not directory.is_absolute():
            directory = ROOT / directory
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"ch04-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# ch04 检索与忠实度评估", "", f"- 生成时间：{report['generated_at']}", "",
                 "| 策略 | Recall@5 | Recall@10 | MRR@10 | 正确拒答率 |", "|---|---|---|---|---|"]
        for strategy, data in report["retrieval"].items():
            overall = data["overall"]
            lines.append(f"| {strategy} | {overall['recall@5']:.3f} | {overall['recall@10']:.3f} | "
                         f"{overall['mrr@10']:.3f} | {overall['refusal_rate']:.3f} |")
        if "faithfulness" in report:
            faith = report["faithfulness"]
            lines += ["", f"Faithfulness：{faith['faithful']}/{faith['judged']}（{faith['rate']:.3f}），模型 {faith['model']}。"]
        (directory / f"ch04-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for strategy, data in report["retrieval"].items():
            overall = data["overall"]
            print(f"{strategy:14s} R@5={overall['recall@5']:.3f} R@10={overall['recall@10']:.3f} "
                  f"MRR={overall['mrr@10']:.3f} 拒答={overall['refusal_rate']:.3f}")
        print(f"报告：evals/reports/ch04-{stamp}.json|md")
        return 0
    finally:
        store.close()
        if model is not None:
            model.root_client.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
