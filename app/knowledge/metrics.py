def recall_at(sections, expected, k):
    if not expected:
        return 0.0
    return len(expected.intersection(sections[:k])) / len(expected)


def reciprocal_rank(sections, expected, k=10):
    for index, section in enumerate(sections[:k], 1):
        if section in expected:
            return 1.0 / index
    return 0.0


def summarize(rows):
    answerable = [row for row in rows if row.get("answerable")]
    unanswerable = [row for row in rows if not row.get("answerable")]
    count = len(answerable)
    return {
        "count": len(rows),
        "answerable": count,
        "recall@5": sum(row["recall@5"] for row in answerable) / count if count else 0.0,
        "recall@10": sum(row["recall@10"] for row in answerable) / count if count else 0.0,
        "mrr@10": sum(row["mrr@10"] for row in answerable) / count if count else 0.0,
        "refusal_rate": sum(1 for row in unanswerable if row["refused"]) / len(unanswerable) if unanswerable else 0.0,
    }
