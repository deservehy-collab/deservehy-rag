import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.observability.cost_report as cost_report
from app.observability.cost_report import (
    IntentCost,
    ROOT_OBSERVATION_NAME,
    batch_trace_ids,
    build_grouped_query,
    build_intent_query,
    build_join_metrics_query,
    build_observations_query,
    parse_grouped,
    parse_join,
    parse_single,
    render_markdown,
)

GROUPED = {"data": [
    {"tags": "intent:物流", "sum_totalTokens": 1200, "sum_inputTokens": 900,
     "sum_outputTokens": 300, "sum_totalCost": 0.012, "count_count": 4},
    {"tags": ["intent:商品咨询"], "sum_totalTokens": 800, "sum_inputTokens": 600,
     "sum_outputTokens": 200, "sum_totalCost": 0.008, "count_count": 2},
    {"tags": "intent:闲聊", "sum_totalTokens": 0, "sum_inputTokens": 0,
     "sum_outputTokens": 0, "sum_totalCost": 0.0, "count_count": 0},
    {"tags": "some-other-tag", "sum_totalTokens": 99, "sum_inputTokens": 99,
     "sum_outputTokens": 0, "sum_totalCost": 0.0, "count_count": 1},
]}

ROOTS = [
    {"traceId": "trace-logistics-1",
     "metadata": '{"intent": "物流", "intent_confidence": 0.91}',
     "tags": ["intent:物流"]},
    {"traceId": "trace-logistics-2", "metadata": {"intent": "物流"}, "tags": []},
    {"traceId": "trace-product-1",
     "metadata": '{"intent": "商品咨询"}', "tags": ["intent:商品咨询"]},
    {"traceId": "trace-unknown", "metadata": {"intent": "hh"}, "tags": ["intent:hh"]},
]

JOIN_METRICS = {"data": [
    {"traceId": "trace-logistics-1", "sum_totalTokens": 1200, "sum_inputTokens": 900,
     "sum_outputTokens": 300, "sum_totalCost": 0.0, "count_count": 4},
    {"traceId": "trace-logistics-2", "sum_totalTokens": 300, "sum_inputTokens": 200,
     "sum_outputTokens": 100, "sum_totalCost": 0.0, "count_count": 2},
    {"traceId": "trace-product-1", "sum_totalTokens": 800, "sum_inputTokens": 600,
     "sum_outputTokens": 200, "sum_totalCost": 0.0, "count_count": 3},
    # 没有对应根 observation 的 trace 不计入任何意图
    {"traceId": "trace-orphan", "sum_totalTokens": 999, "sum_inputTokens": 999,
     "sum_outputTokens": 0, "sum_totalCost": 0.0, "count_count": 1},
]}


def test_parse_grouped_reads_both_shapes_and_skips_non_intent_tags():
    # parse_grouped 永远返回全部八个意图（见下一条用例），所以按 intent 取，不能按列表顺序断言。
    rows = {row.intent: row for row in parse_grouped(GROUPED)}

    assert (rows["物流"].requests, rows["物流"].total_tokens) == (4, 1200)
    assert rows["商品咨询"].total_tokens == 800          # tags 是数组形状也要认
    assert rows["闲聊"].requests == 0                     # 有行但 count=0，保留
    assert "some-other-tag" not in rows                   # 非意图 tag 不产生行


def test_parse_grouped_fills_missing_intents_with_zero():
    rows = {row.intent: row for row in parse_grouped(GROUPED)}

    assert rows["投诉"].requests == 0 and rows["投诉"].cost == 0.0
    assert len(rows) == 8                      # 八个意图全在，没数据的补 0


def test_parse_grouped_empty_payload():
    assert parse_grouped({"data": []}) == []
    assert parse_grouped({}) == []


def test_parse_grouped_aggregates_repeated_tag_rows():
    payload = {"data": [
        {"tags": ["intent:物流"], "sum_totalTokens": 100, "sum_inputTokens": 80,
         "sum_outputTokens": 20, "sum_totalCost": 0.001, "count_count": 1},
        {"tags": ["intent:物流"], "sum_totalTokens": 200, "sum_inputTokens": 150,
         "sum_outputTokens": 50, "sum_totalCost": 0.002, "count_count": 2},
    ]}

    row = {row.intent: row for row in parse_grouped(payload)}["物流"]

    assert (row.requests, row.input_tokens, row.output_tokens, row.total_tokens) == (3, 230, 70, 300)
    assert row.cost == pytest.approx(0.003)


def test_parse_grouped_finds_intent_in_multi_tag_row():
    payload = {"data": [
        {"tags": ["some-other-tag", "intent:物流"], "sum_totalTokens": 100,
         "sum_inputTokens": 80, "sum_outputTokens": 20, "sum_totalCost": 0.0,
         "count_count": 1},
    ]}

    row = {row.intent: row for row in parse_grouped(payload)}["物流"]

    assert (row.requests, row.total_tokens) == (1, 100)


def test_parse_single_reads_one_intent():
    payload = {"data": [{"sum_totalTokens": 500, "sum_inputTokens": 400,
                         "sum_outputTokens": 100, "sum_totalCost": 0.005, "count_count": 3}]}

    row = parse_single("物流", payload)

    assert row == IntentCost("物流", 3, 400, 100, 500, 0.005)


def test_parse_single_tolerates_missing_measures():
    row = parse_single("闲聊", {"data": [{}]})

    assert row == IntentCost("闲聊", 0, 0, 0, 0, 0.0)


def test_queries_are_wellformed():
    # 实测（2026-11-02）：dimensions 必须是对象；时间窗用 fromTimestamp/toTimestamp；
    # tags 过滤的 type 是 arrayOptions、operator 是 any of、value 是数组。
    grouped = build_grouped_query(since="2026-10-25T00:00:00+00:00", until=None)

    assert grouped["view"] == "observations"
    assert grouped["dimensions"] == [{"field": "tags"}]
    assert {"measure": "totalTokens", "aggregation": "sum"} in grouped["metrics"]
    assert grouped["fromTimestamp"] == "2026-10-25T00:00:00+00:00"
    assert "toTimestamp" in grouped
    assert "timeDimension" not in grouped

    single = build_intent_query("物流", since="2026-10-25T00:00:00+00:00", until=None)

    assert single["dimensions"] == []
    tag_filter = single["filters"][0]
    assert tag_filter == {"column": "tags", "type": "arrayOptions",
                          "operator": "any of", "value": ["intent:物流"]}


def test_observations_query_is_wellformed():
    query = build_observations_query(since="2026-11-02T00:00:00+00:00",
                                     until="2026-11-03T00:00:00+00:00")

    assert query["name"] == ROOT_OBSERVATION_NAME
    assert query["isRootObservation"] == "true"
    assert query["fields"] == "core,basic,metadata,trace_context"
    assert query["limit"] == 100
    assert query["fromStartTime"] == "2026-11-02T00:00:00+00:00"
    assert query["toStartTime"] == "2026-11-03T00:00:00+00:00"
    assert "cursor" not in query
    assert build_observations_query(cursor="abc")["cursor"] == "abc"


def test_join_metrics_query_is_wellformed():
    query = build_join_metrics_query(["trace-1", "trace-2"],
                                     since="2026-11-02T00:00:00+00:00",
                                     until="2026-11-03T00:00:00+00:00")

    assert query["view"] == "observations"
    assert query["dimensions"] == [{"field": "traceId"}]
    assert query["filters"] == [{"column": "traceId", "operator": "any of",
                                 "type": "stringOptions",
                                 "value": ["trace-1", "trace-2"]}]
    assert query["config"] == {"row_limit": 50}
    assert query["orderBy"] == [{"field": "sum_totalTokens", "direction": "desc"}]
    assert query["fromTimestamp"] == "2026-11-02T00:00:00+00:00"
    assert query["toTimestamp"] == "2026-11-03T00:00:00+00:00"


def test_batch_trace_ids_splits_at_fifty():
    ids = [f"trace-{index}" for index in range(51)]

    batches = batch_trace_ids(ids)

    assert [len(batch) for batch in batches] == [50, 1]
    assert batch_trace_ids([]) == []


def test_parse_join_aggregates_by_intent():
    rows = {row.intent: row for row in parse_join(ROOTS, JOIN_METRICS)}

    assert len(rows) == 8
    assert rows["物流"].requests == 2
    assert (rows["物流"].total_tokens, rows["物流"].input_tokens,
            rows["物流"].output_tokens) == (1500, 1100, 400)
    assert rows["商品咨询"].requests == 1
    assert rows["商品咨询"].total_tokens == 800
    # 未知 intent 与没有根 observation 的 trace 都不计入
    assert rows["其他"].requests == 0
    assert sum(row.total_tokens for row in rows.values()) == 2300


def test_parse_join_empty_roots():
    assert parse_join([], JOIN_METRICS) == []
    assert parse_join([], {"data": []}) == []


def test_render_markdown_lists_every_intent_and_total():
    rows = parse_grouped(GROUPED)

    text = render_markdown(rows, since="2026-10-25", until="2026-11-01", mode="grouped")

    assert "| 意图 | 请求数 | 输入 token | 输出 token | 总 token | 估算花费 |" in text
    assert "| 物流 | 4 | 900 | 300 | 1200 |" in text
    assert "| 合计 |" in text and "2000" in text
    assert "grouped" in text
    assert "Langfuse totalCost" in text


def test_render_markdown_without_langfuse_says_so():
    text = render_markdown([], since="a", until="b", mode="grouped",
                           note="Langfuse 没开或连不上，没有可汇总的数据。")

    assert "没有可汇总的数据" in text


# --------------------------------------------------------------------------------------
# collect() 编排契约（评审 IMPORTANT 1/3）：用假 helper 钉住 join → grouped → per_intent 的回退，
# 以及「取数失败」不能伪装成「没有数据」。
# --------------------------------------------------------------------------------------

SINCE = "2026-11-01T00:00:00+00:00"
UNTIL = "2026-11-02T00:00:00+00:00"


def _settings():
    return SimpleNamespace(
        langfuse_enabled=True,
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_host="http://langfuse.test",
    )


def _status_error(status: int, body: str = "upstream failed") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://langfuse.test/api/public/v2/observations")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _status_error(self.status_code)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append(dict(params or {}))
        item = self.pages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_collect_returns_join_when_join_has_tokens(monkeypatch):
    async def fake_roots(client, since, until, failures=None):
        return [{"traceId": "t1", "metadata": {"intent": "物流"}}]

    async def fake_join(client, roots, since, until, failures=None):
        return [{"traceId": "t1", "sum_totalTokens": 10, "sum_inputTokens": 8,
                 "sum_outputTokens": 2, "sum_totalCost": 0.0, "count_count": 1}]

    async def unexpected(client, query):
        raise AssertionError("join 成功时不应再查 metrics")

    monkeypatch.setattr(cost_report, "_fetch_roots", fake_roots)
    monkeypatch.setattr(cost_report, "_fetch_join_metrics", fake_join)
    monkeypatch.setattr(cost_report, "_get_metrics", unexpected)

    rows, mode, note = asyncio.run(cost_report.collect(_settings()))

    assert (mode, note) == ("join", "")
    assert {row.intent: row.total_tokens for row in rows}["物流"] == 10


def test_collect_falls_back_to_grouped_when_join_empty(monkeypatch):
    async def fake_roots(client, since, until, failures=None):
        return [{"traceId": "t1", "metadata": {"intent": "物流"}}]

    async def fake_join(client, roots, since, until, failures=None):
        return []

    async def fake_metrics(client, query):
        assert query["dimensions"] == [{"field": "tags"}]
        return {"data": [{"tags": ["intent:物流"], "sum_totalTokens": 55,
                          "sum_inputTokens": 40, "sum_outputTokens": 15,
                          "sum_totalCost": 0.0, "count_count": 1}]}

    monkeypatch.setattr(cost_report, "_fetch_roots", fake_roots)
    monkeypatch.setattr(cost_report, "_fetch_join_metrics", fake_join)
    monkeypatch.setattr(cost_report, "_get_metrics", fake_metrics)

    rows, mode, note = asyncio.run(cost_report.collect(_settings()))

    assert (mode, note) == ("grouped", "")
    assert {row.intent: row.total_tokens for row in rows}["物流"] == 55


def test_collect_falls_back_to_per_intent_when_grouped_empty(monkeypatch):
    async def fake_roots(client, since, until, failures=None):
        return [{"traceId": "t1", "metadata": {"intent": "物流"}}]

    async def fake_join(client, roots, since, until, failures=None):
        return []

    async def fake_metrics(client, query):
        if query["dimensions"] == [{"field": "tags"}]:
            return {"data": []}
        if query["filters"] and query["filters"][0].get("value") == ["intent:物流"]:
            return {"data": [{"sum_totalTokens": 7, "sum_inputTokens": 5,
                              "sum_outputTokens": 2, "sum_totalCost": 0.0,
                              "count_count": 1}]}
        return {"data": []}

    monkeypatch.setattr(cost_report, "_fetch_roots", fake_roots)
    monkeypatch.setattr(cost_report, "_fetch_join_metrics", fake_join)
    monkeypatch.setattr(cost_report, "_get_metrics", fake_metrics)

    rows, mode, note = asyncio.run(cost_report.collect(_settings()))

    assert (mode, note) == ("per_intent", "")
    assert {row.intent: row.total_tokens for row in rows}["物流"] == 7


def test_collect_all_zero_reports_empty_window(monkeypatch):
    async def fake_roots(client, since, until, failures=None):
        return []

    async def fake_metrics(client, query):
        return {"data": []}

    monkeypatch.setattr(cost_report, "_fetch_roots", fake_roots)
    monkeypatch.setattr(cost_report, "_get_metrics", fake_metrics)

    rows, mode, note = asyncio.run(cost_report.collect(_settings()))

    assert rows == []
    assert "没有任何带 intent 标签的 trace" in note


def test_collect_persistent_4xx_surfaces_status_in_note(monkeypatch):
    error = _status_error(401, "invalid public key")

    async def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(cost_report, "_fetch_roots", failing)
    monkeypatch.setattr(cost_report, "_get_metrics", failing)

    rows, mode, note = asyncio.run(cost_report.collect(_settings()))

    assert rows == []
    assert "401" in note
    assert "invalid public key" in note
    assert "取数失败" in note
    # 关键：认证失败绝不能被说成「这段时间窗没有数据」。
    assert "没有任何带 intent 标签" not in note


def test_fetch_roots_merges_two_cursor_pages_without_duplicates():
    client = _FakeClient([
        _FakeResponse({"data": [{"traceId": "a"}, {"traceId": "b"}],
                       "meta": {"cursor": "c1"}}),
        _FakeResponse({"data": [{"traceId": "b"}, {"traceId": "c"}], "meta": {}}),
    ])

    rows = asyncio.run(cost_report._fetch_roots(client, SINCE, UNTIL))

    assert [row["traceId"] for row in rows] == ["a", "b", "c"]
    assert client.calls[0].get("cursor") is None
    assert client.calls[1]["cursor"] == "c1"
    assert len(client.calls) == 2


def test_fetch_roots_keeps_first_page_when_next_page_fails():
    client = _FakeClient([
        _FakeResponse({"data": [{"traceId": "a"}], "meta": {"cursor": "c1"}}),
        _status_error(500, "db down"),
    ])
    failures = []

    rows = asyncio.run(cost_report._fetch_roots(client, SINCE, UNTIL, failures))

    assert [row["traceId"] for row in rows] == ["a"]
    assert failures and "500" in failures[0]


def test_fetch_join_metrics_keeps_successful_batches(monkeypatch):
    calls = []

    async def fake_metrics(client, query):
        calls.append(query)
        if len(calls) == 1:
            return {"data": [{"traceId": "t0", "sum_totalTokens": 3}]}
        raise _status_error(500, "batch failed")

    monkeypatch.setattr(cost_report, "_get_metrics", fake_metrics)
    roots = [{"traceId": f"t{index}", "metadata": {"intent": "物流"}} for index in range(51)]
    failures = []

    rows = asyncio.run(cost_report._fetch_join_metrics(object(), roots, SINCE, UNTIL, failures))

    assert [row["traceId"] for row in rows] == ["t0"]
    assert failures and "500" in failures[0]


# --------------------------------------------------------------------------------------
# 评审 MINOR 4/5/6
# --------------------------------------------------------------------------------------

def test_intent_of_skips_unknown_tag_and_keeps_scanning():
    assert cost_report._intent_of(["intent:不存在", "intent:物流"]) == "物流"
    assert cost_report._intent_of(["intent:不存在"]) is None


def test_parse_grouped_skips_row_with_two_different_valid_intents():
    payload = {"data": [
        {"tags": ["intent:物流", "intent:商品咨询"], "sum_totalTokens": 999,
         "sum_inputTokens": 999, "sum_outputTokens": 0, "sum_totalCost": 0.0,
         "count_count": 1},
        {"tags": ["intent:物流"], "sum_totalTokens": 50, "sum_inputTokens": 40,
         "sum_outputTokens": 10, "sum_totalCost": 0.0, "count_count": 1},
    ]}

    rows = {row.intent: row for row in parse_grouped(payload)}

    assert rows["物流"].total_tokens == 50
    assert rows["商品咨询"].total_tokens == 0


def test_iso_validates_cli_strings_and_normalizes_naive_to_utc():
    assert cost_report._iso("2026-10-25T00:00:00") == "2026-10-25T00:00:00+00:00"
    with pytest.raises(ValueError, match="无法解析时间") as excinfo:
        cost_report._iso("2026-13-99")
    assert "2026-13-99" in str(excinfo.value)


def test_render_markdown_appends_note_to_table_when_rows_present():
    rows = parse_grouped(GROUPED)

    text = render_markdown(rows, since="2026-10-25", until="2026-11-01", mode="grouped",
                           note="部分取数请求失败：HTTP 500")

    assert "| 意图 | 请求数 | 输入 token | 输出 token | 总 token | 估算花费 |" in text
    assert "| 物流 | 4 | 900 | 300 | 1200 |" in text
    assert "mode=grouped" in text
    assert "注意：部分取数请求失败：HTTP 500" in text
