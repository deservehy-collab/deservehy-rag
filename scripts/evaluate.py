import asyncio
import json
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.context import ContextBudgetError
from app.model import build_model
from app.schemas import ChatRequest, ExtractRequest
from app.service import CustomerService, ModelFailure

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("order_id", "request_type", "expected_solution")


def score_fields(result: dict, expected: dict) -> dict[str, bool]:
    return {field: result[field] in expected[field] for field in FIELDS}


async def evaluate(settings: Settings) -> tuple[dict, bool]:
    model = build_model(settings)
    service = CustomerService(settings, model)
    extraction_results, chat_results = [], []
    try:
        cases = json.loads((ROOT / "evals/after_sales.json").read_text(encoding="utf-8"))
        for case in cases:
            try:
                result = (await service.extract(ExtractRequest(description=case["description"]))).model_dump()
                fields = score_fields(result, case["expected"])
                extraction_results.append({"id": case["id"], "result": result, "fields": fields, "exact_match": all(fields.values())})
            except (ModelFailure, ContextBudgetError) as exc:
                extraction_results.append({"id": case["id"], "error": str(exc), "fields": dict.fromkeys(FIELDS, False), "exact_match": False})
        chat_cases = json.loads((ROOT / "evals/chat.json").read_text(encoding="utf-8"))
        for case in chat_cases:
            answer, finished = "", False
            try:
                messages = service.prepare_chat(ChatRequest(message=case["message"], history=case.get("history", [])))
                async for event, payload in service.stream_chat(messages):
                    if event == "token":
                        answer += payload["content"]
                    elif event == "done":
                        finished = payload["finish_reason"] == "stop"
                passed = finished and all(any(word in answer for word in group) for group in case["required_any"]) and not any(word in answer for word in case["forbidden"])
                chat_results.append({"id": case["id"], "answer": answer, "heuristic_pass": passed, "review_criteria": case["review_criteria"]})
            except ContextBudgetError as exc:
                chat_results.append({"id": case["id"], "error": str(exc), "heuristic_pass": False})
    finally:
        await model.root_async_client.close()
        model.root_client.close()
    report = {
        "model": settings.openai_model,
        "extraction_cases": len(extraction_results),
        "field_accuracy": {field: sum(result["fields"][field] for result in extraction_results) / len(extraction_results) for field in FIELDS},
        "exact_match_rate": sum(result["exact_match"] for result in extraction_results) / len(extraction_results),
        "extraction_results": extraction_results,
        "chat_results": chat_results,
        "chat_review_required": True,
        "note": "客服关键词检查仅作筛查，必须按 review_criteria 人工核对回答；不代表完整语义评估通过。",
    }
    passed = all(result["exact_match"] for result in extraction_results) and all(result["heuristic_pass"] for result in chat_results)
    return report, passed


def main() -> int:
    try:
        settings = Settings.from_env()
    except ValidationError:
        print(json.dumps({"status": "blocked", "reason": "请先填写项目 .env 的上游地址、模型名和密钥，并检查预算配置。"}, ensure_ascii=False))
        return 2
    report, passed = asyncio.run(evaluate(settings))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
