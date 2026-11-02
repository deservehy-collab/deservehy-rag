"""按意图汇总 Langfuse 里的 token 花销（ch09 T16）。

取数口径由 T2 探针与 2026-11-02 真机复核共同定死：

1. 本机是 `events_only` 部署，根 observation 上的 `intent:*` tag **不会**摊给同 trace 的
   兄弟 observation，token 全在兄弟行上。因此主路径是两段 join（`mode="join"`）：
   Observations v2 取 `name=chat-turn&isRootObservation=true` 的根行，读出 intent；
   Metrics v2 按 `traceId` 分组取这些 trace 的 token，再在本地按 traceId join。
2. `grouped`（按 tags 分组）与 `per_intent`（按 tags 过滤）保留为降级路径，解析器与测试都在。
   实测 grouped 的 `tags=[]` 行拿走了全部 token，`intent:*` 行 token 恒为 0，所以在该部署上
   必然退到 join。

已实测的接口形状（2026-11-02，Langfuse v4.72.0）：

- `GET /api/public/v2/observations?name=chat-turn&isRootObservation=true&fields=core,basic,
  metadata,trace_context&limit=100&fromStartTime=…&toStartTime=…` 返回 200；
  分页是游标式：响应 `meta.cursor`，下一页把它作为 `cursor` query 参数传回。`page` 参数会被忽略。
- `GET /api/public/v2/metrics?query=<JSON>`：`fromTimestamp`/`toTimestamp` 必填；
  `dimensions` 必须是对象数组（`[{"field":"tags"}]`）。`traceId` 是高基数维度，必须同时带
  `config.row_limit` 和 `orderBy:[{"field":"sum_totalTokens","direction":"desc"}]`，否则 400。
  `traceId` 过滤的 `type` 必须是 `"stringOptions"`、`operator` 是 `"any of"`。
- 返回里的度量键名就是 `<aggregation>_<measure>`：`sum_totalTokens` / `sum_inputTokens` /
  `sum_outputTokens` / `sum_totalCost` / `count_count`，与假设一致。
- `tags` 过滤的 `type` 是 `arrayOptions`、`operator` 是 `any of`、`value` 是数组。
- 成本只用 Langfuse 的 `sum_totalCost`。自部署实例没有模型价格表，该值恒为 0；
  不拿 token 编价格（`Settings.token_unit_price_*` 在本仓库不存在）。

本模块是旁路报表：Langfuse 没开或连不上时返回空行 + 人话 note，**绝不抛**。
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.workflow.routing import INTENTS

logger = logging.getLogger(__name__)

TAG_PREFIX = "intent:"
ROOT_OBSERVATION_NAME = "chat-turn"
ROOT_FIELDS = "core,basic,metadata,trace_context"
TRACE_ID_BATCH = 50
OBSERVATION_PAGE_LIMIT = 100

MEASURES = (
    ("totalTokens", "sum"),
    ("inputTokens", "sum"),
    ("outputTokens", "sum"),
    ("totalCost", "sum"),
    ("count", "count"),
)


@dataclass(frozen=True)
class IntentCost:
    intent: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    reasoning_tokens: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _window(since=None, until=None) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = since or (now - timedelta(days=7))
    end = until or now
    return _iso(start), _iso(end)


def _iso(value) -> str:
    """把 datetime / ISO8601 字符串统一成 UTC 的秒级 ISO 字符串。

    字符串不再原样透传：CLI 的 `--from/--to` 如果写错，应该在发请求前就报一个能看懂的
    `ValueError`（带原值），而不是让 Langfuse 回一个语焉不详的 400 `Invalid ISO datetime`。
    naive 值按 UTC 解释，避免本机时区把时间窗悄悄挪走。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"无法解析时间 {value!r}：请用 ISO8601（如 2026-11-01T00:00:00+00:00）"
            ) from None
    else:
        raise ValueError(f"无法解析时间 {value!r}：需要 datetime 或 ISO8601 字符串")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def build_observations_query(*, since=None, until=None, cursor: str | None = None,
                             limit: int = OBSERVATION_PAGE_LIMIT) -> dict:
    """Observations v2 的查询参数。时间窗参数名实测是 `fromStartTime`/`toStartTime`，
    分页参数是 `cursor`（响应 `meta.cursor`），不是 `page`。"""
    start, end = _window(since, until)
    query = {
        "name": ROOT_OBSERVATION_NAME,
        "isRootObservation": "true",
        "fields": ROOT_FIELDS,
        "limit": limit,
        "fromStartTime": start,
        "toStartTime": end,
    }
    if cursor:
        query["cursor"] = cursor
    return query


def build_join_metrics_query(trace_ids, *, since=None, until=None,
                             row_limit: int = TRACE_ID_BATCH) -> dict:
    """Metrics v2 按 traceId 分组的查询体。高基数维度必须带 row_limit + orderBy。"""
    start, end = _window(since, until)
    return {
        "view": "observations",
        "dimensions": [{"field": "traceId"}],
        "metrics": [{"measure": name, "aggregation": agg} for name, agg in MEASURES],
        "filters": [{"column": "traceId", "operator": "any of", "type": "stringOptions",
                     "value": list(trace_ids)}],
        "fromTimestamp": start,
        "toTimestamp": end,
        "config": {"row_limit": row_limit},
        "orderBy": [{"field": "sum_totalTokens", "direction": "desc"}],
    }


def build_grouped_query(*, since=None, until=None) -> dict:
    """降级路径 A：按 tags 分组。`dimensions` 实测必须是对象；时间窗用 from/toTimestamp。"""
    start, end = _window(since, until)
    return {
        "view": "observations",
        "dimensions": [{"field": "tags"}],
        "metrics": [{"measure": name, "aggregation": agg} for name, agg in MEASURES],
        "fromTimestamp": start,
        "toTimestamp": end,
    }


def build_intent_query(intent: str, *, since=None, until=None) -> dict:
    """降级路径 B：按单个 intent tag 过滤。tags 是 string[]，过滤 type 必须是 arrayOptions。"""
    start, end = _window(since, until)
    return {
        "view": "observations",
        "dimensions": [],
        "metrics": [{"measure": name, "aggregation": agg} for name, agg in MEASURES],
        "filters": [{"column": "tags", "type": "arrayOptions", "operator": "any of",
                     "value": [f"{TAG_PREFIX}{intent}"]}],
        "fromTimestamp": start,
        "toTimestamp": end,
    }


def batch_trace_ids(trace_ids, size: int = TRACE_ID_BATCH) -> list[list[str]]:
    """Metrics v2 的 traceId 过滤按 50 个一批，避免一次请求过重。"""
    values = list(trace_ids)
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [values[index:index + size] for index in range(0, len(values), size)]


def parse_grouped(payload: dict) -> list[IntentCost]:
    rows = (payload or {}).get("data") or []
    if not rows:
        return []
    found: dict[str, IntentCost] = {}
    for row in rows:
        intents = _intents_of(row.get("tags"))
        if not intents:
            continue
        if len(intents) > 1:
            # 一行同时带多个有效意图：分不清这笔 token 该算给谁，宁可跳过也不硬摊给第一个。
            logger.warning("忽略同时带多个意图 tag 的 grouped 行：%s", intents)
            continue
        intent = intents[0]
        _accumulate(found, intent, requests=_int(row.get("count_count")),
                    input_tokens=_int(row.get("sum_inputTokens")),
                    output_tokens=_int(row.get("sum_outputTokens")),
                    total_tokens=_int(row.get("sum_totalTokens")),
                    cost=_float(row.get("sum_totalCost")))
    return [found.get(intent) or IntentCost(intent) for intent in INTENTS]


def parse_single(intent: str, payload: dict) -> IntentCost:
    rows = (payload or {}).get("data") or []
    if not rows:
        return IntentCost(intent)
    return _row(intent, rows[0])


def parse_join(roots, metrics_payload) -> list[IntentCost]:
    """把根 observation（traceId → intent）与 Metrics v2（traceId → token）在本地 join。

    `requests` = 该 intent 的 traceId 个数；token 只累加能在 Metrics 响应里找到的 trace。
    没有根 observation 的 metrics 行、intent 不在八个值里的根行都不计入。
    """
    root_rows = _rows(roots)
    if not root_rows:
        return []
    metrics = {row.get("traceId"): row for row in _rows(metrics_payload)
               if row.get("traceId")}
    traces: dict[str, str] = {}
    for row in root_rows:
        trace_id = row.get("traceId")
        intent = _intent_of_root(row)
        if trace_id and intent:
            traces.setdefault(trace_id, intent)
    found: dict[str, IntentCost] = {}
    for trace_id, intent in traces.items():
        raw = metrics.get(trace_id) or {}
        _accumulate(found, intent, requests=1,
                    input_tokens=_int(raw.get("sum_inputTokens")),
                    output_tokens=_int(raw.get("sum_outputTokens")),
                    total_tokens=_int(raw.get("sum_totalTokens")),
                    cost=_float(raw.get("sum_totalCost")))
    return [found.get(intent) or IntentCost(intent) for intent in INTENTS]


def render_markdown(rows: list[IntentCost], *, since, until, mode: str, note: str = "") -> str:
    if not rows:
        # 没有行时 note 是主体；连 note 都没有才说一句默认的人话，避免渲染一张全 0 的假表。
        return (f"# ch09 按意图的 token 花销\n\n- 时间窗：{since} ~ {until}\n\n"
                f"{note or '没有可汇总的数据。'}\n")
    lines = ["# ch09 按意图的 token 花销", "",
             f"- 时间窗：{since} ~ {until}",
             f"- 取数方式：Metrics v2 / observations view，mode={mode}",
             "- `user_id` 与 `session.id` 都是会话 id（本项目没有账号体系），"
             "所以这里是「按会话意图」的花销，不是「按用户」。", "",
             "| 意图 | 请求数 | 输入 token | 输出 token | 总 token | 估算花费 |",
             "|---|---|---|---|---|---|"]
    totals = [0, 0, 0, 0, 0.0]
    for row in rows:
        lines.append(f"| {row.intent} | {row.requests} | {row.input_tokens} | {row.output_tokens} "
                     f"| {row.total_tokens} | {row.cost:.4f} |")
        for index, value in enumerate((row.requests, row.input_tokens, row.output_tokens,
                                       row.total_tokens, row.cost)):
            totals[index] += value
    lines.append(f"| 合计 | {totals[0]} | {totals[1]} | {totals[2]} | {totals[3]} "
                 f"| {totals[4]:.4f} |")
    lines.append("")
    lines.append("> 估算花费取自 Langfuse totalCost；自部署实例没有模型价格表时该值恒为 0，"
                 "token 数是可靠口径，不按 token 反推价格。")
    lines.append("> DeepSeek 的 reasoning token 已计入 output token，Langfuse 未提供单独的 "
                 "reasoning 度量，因此不单列。")
    if note:
        # 有数据但有部分请求失败（或触到翻页上限）时，note 不能吞掉：作为脚注挂在表后。
        lines.append(f"> 注意：{note}")
    return "\n".join(lines) + "\n"


async def collect(settings, *, since=None, until=None) -> tuple[list[IntentCost], str, str]:
    """返回 (rows, mode, note)；note 非空表示没有拿到完整数据，不能当成「花了 0 元」。

    顺序：join（主路径）→ grouped（按 tags 分组）→ per_intent（逐意图过滤）。
    只有拿到非零 token 才算成功——八意图全 0 的报表等于没数据。

    失败语义（评审 IMPORTANT 1/2）：
    - 每次 HTTP 失败都记下状态码与截断后的响应体；只有所有尝试过的请求都是 200 且真的为空，
      才允许说「这段时间窗没有任何带 intent 的 trace」。
    - 分页/分批不再 all-or-nothing：单页/单批失败只跳过它，成功取到的行保留；只有一页/一批
      都没成功时才把该路径判为失败。
    - 有数据但存在部分失败时，note 作为警告返回（render_markdown 会挂在表后），不丢信息。
    """
    if not settings.langfuse_enabled or not settings.langfuse_public_key \
            or not settings.langfuse_secret_key:
        return [], "", "Langfuse 没开（LANGFUSE_ENABLED=false 或 key 没填），没有可汇总的数据。"
    start, end = _window(since, until)
    host = str(settings.langfuse_host).rstrip("/")
    # langfuse_secret_key 在 T2 里定义成普通 str（不是 SecretStr），直接取用。
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
    failures: list[str] = []
    async with httpx.AsyncClient(base_url=host, auth=auth, timeout=20) as client:
        roots: list[dict] = []
        try:
            roots = await _fetch_roots(client, start, end, failures)
        except (httpx.HTTPError, ValueError) as exc:
            _record_failure(failures, exc)
            logger.warning("Observations v2 根 observation 查询失败，按意图花销退回降级路径。",
                           exc_info=True)

        join_rows: list[IntentCost] = []
        if roots:
            try:
                metrics_rows = await _fetch_join_metrics(client, roots, start, end, failures)
                join_rows = parse_join(roots, {"data": metrics_rows})
            except (httpx.HTTPError, ValueError) as exc:
                _record_failure(failures, exc)
                logger.warning("Metrics v2 按 traceId join 失败，按意图花销退回降级路径。",
                               exc_info=True)
        if any(row.total_tokens for row in join_rows):
            return join_rows, "join", _partial_note(failures)

        try:
            grouped_payload = await _get_metrics(client, build_grouped_query(since=start, until=end))
        except (httpx.HTTPError, ValueError) as exc:
            _record_failure(failures, exc)
            logger.warning("Metrics v2 按 tags 分组失败，按意图花销退回逐意图过滤。", exc_info=True)
            grouped_payload = {}
        rows = parse_grouped(grouped_payload)
        if any(row.total_tokens for row in rows):
            return rows, "grouped", _partial_note(failures)

        per_intent: list[IntentCost] = []
        for intent in INTENTS:
            try:
                payload = await _get_metrics(client, build_intent_query(intent, since=start,
                                                                        until=end))
            except (httpx.HTTPError, ValueError) as exc:
                _record_failure(failures, exc)
                logger.warning("意图 %s 的花销查询失败。", intent, exc_info=True)
                continue
            per_intent.append(parse_single(intent, payload))
        if any(row.total_tokens for row in per_intent):
            return per_intent, "per_intent", _partial_note(failures)

        if failures:
            # 至少有一次请求失败：不能把空结果说成「这段时间窗没有数据」。
            return [], "join" if roots else "", _failure_note(failures, roots=len(roots))
        if roots:
            return [], "join", (f"找到 {len(roots)} 轮带 intent 的根 observation，但 Metrics v2 "
                                "没返回任何 token（可能是投影延迟，或这段时间窗内没有生成）。"
                                "稍后重跑，或确认 Langfuse 容器与 OTEL 摄取正常。")
        return [], "per_intent", ("这段时间窗内没有任何带 intent 标签的 trace。"
                                  "先确认服务跑过真实对话、且 T2 探针是 MAIN 结论；"
                                  "Langfuse 其他 public API 有约 10 分钟延迟，但 Metrics v2 是实时的。")


_STATUS_HINTS = {
    400: "查询参数被拒，检查 Metrics v2 查询形状",
    401: "认证失败，检查 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY",
    403: "无权限，检查 key 所属项目",
    404: "接口不存在，检查 LANGFUSE_HOST 是否支持 /api/public/v2/*",
    429: "被限流，稍后重试",
}


def _format_failure(exc: BaseException) -> str:
    """把一次失败压成一行可读文本：状态码 + 路径 + 截断响应体（不含 key / query）。"""
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        hint = _STATUS_HINTS.get(
            status, "Langfuse 服务端错误，稍后重试" if status >= 500 else "请求被拒")
        body = " ".join((response.text or "").split())[:200]
        path = response.request.url.path if response.request is not None else "?"
        return f"HTTP {status} {path}（{hint}）：{body or '（无响应体）'}"
    return f"{type(exc).__name__}: {exc}"


def _record_failure(failures: list[str] | None, exc: BaseException) -> None:
    if failures is None:
        return
    entry = _format_failure(exc)
    if entry not in failures:
        failures.append(entry)


def _partial_note(failures: list[str]) -> str:
    if not failures:
        return ""
    return (f"部分取数请求失败（共 {len(failures)} 次），已保留成功取到的数据；"
            f"最后一次：{failures[-1]}。重跑可补齐缺口。")


def _failure_note(failures: list[str], *, roots: int = 0) -> str:
    last = failures[-1]
    if roots:
        lead = (f"找到 {roots} 轮带 intent 的根 observation，但 token 取数失败，"
                "不能据此判断这段时间窗没有数据。")
    else:
        lead = "取数失败，不能当成「这段时间窗内没有数据」。"
    count = f"共 {len(failures)} 次请求失败；" if len(failures) > 1 else ""
    return (f"{lead}{count}最后一次失败：{last}。"
            "请检查 LANGFUSE_HOST、LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 与查询参数，"
            "或稍后重试。")


MAX_OBSERVATION_PAGES = 200


async def _fetch_roots(client: httpx.AsyncClient, since: str, until: str,
                       failures: list[str] | None = None) -> list[dict]:
    """游标翻页取时间窗内的 chat-turn 根 observation。

    单页失败不丢已经取到的行；只有第一页就失败（零页成功）才把整条路径判为失败并抛出。
    按 traceId 去重，防止服务端游标重叠时同一轮被算两次。触到页数上限会显式告警。
    """
    rows: list[dict] = []
    seen_traces: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = None
    successes = 0
    last_error: BaseException | None = None
    for page_number in range(1, MAX_OBSERVATION_PAGES + 1):
        try:
            response = await client.get("/api/public/v2/observations",
                                        params=build_observations_query(since=since, until=until,
                                                                        cursor=cursor))
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            _record_failure(failures, exc)
            logger.warning("Observations v2 第 %d 页（cursor=%s）取数失败：%s",
                           page_number, cursor, _format_failure(exc))
            break
        successes += 1
        page = payload.get("data") or []
        for row in page:
            trace_id = row.get("traceId")
            if trace_id:
                if trace_id in seen_traces:
                    continue
                seen_traces.add(trace_id)
            rows.append(row)
        cursor = (payload.get("meta") or {}).get("cursor")
        if not page or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    else:
        message = (f"Observations v2 达到 {MAX_OBSERVATION_PAGES} 页上限，"
                   f"只取了前 {len(rows)} 行，结果可能被截断。")
        logger.warning(message)
        if failures is not None:
            failures.append(message)
    if successes == 0 and last_error is not None:
        raise last_error
    return rows


async def _fetch_join_metrics(client: httpx.AsyncClient, roots: list[dict],
                              since: str, until: str,
                              failures: list[str] | None = None) -> list[dict]:
    """按 50 个 traceId 一批取 token；单批失败只丢该批，成功批次的行走保留。"""
    trace_ids = list(dict.fromkeys(row.get("traceId") for row in roots if row.get("traceId")))
    batches = batch_trace_ids(trace_ids)
    metrics_rows: list[dict] = []
    successes = 0
    last_error: BaseException | None = None
    for index, batch in enumerate(batches, start=1):
        try:
            payload = await _get_metrics(client, build_join_metrics_query(batch, since=since,
                                                                          until=until))
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            _record_failure(failures, exc)
            logger.warning("Metrics v2 第 %d/%d 批（%d 个 traceId）取数失败：%s",
                           index, len(batches), len(batch), _format_failure(exc))
            continue
        successes += 1
        metrics_rows.extend(payload.get("data") or [])
    if batches and successes == 0 and last_error is not None:
        raise last_error
    return metrics_rows


async def _get_metrics(client: httpx.AsyncClient, query: dict) -> dict:
    response = await client.get("/api/public/v2/metrics",
                                params={"query": json.dumps(query, ensure_ascii=False)})
    response.raise_for_status()
    return response.json()


def _rows(value) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.get("data") or [])
    return list(value)


def _intents_of(tags) -> list[str]:
    """按出现顺序返回 tags 里所有有效的意图（去重）。

    遇到 `intent:<未知值>` 不提前收手：后面可能还跟着一个有效意图，跳过坏的那个继续找。
    """
    values = tags if isinstance(tags, list) else [tags]
    found: list[str] = []
    for value in values:
        if isinstance(value, str) and value.startswith(TAG_PREFIX):
            intent = value[len(TAG_PREFIX):]
            if intent in INTENTS and intent not in found:
                found.append(intent)
    return found


def _intent_of(tags) -> str | None:
    intents = _intents_of(tags)
    return intents[0] if intents else None


def _intent_of_root(row: dict) -> str | None:
    return _intent_of_metadata(row.get("metadata")) or _intent_of(row.get("tags"))


def _intent_of_metadata(metadata) -> str | None:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        return None
    intent = metadata.get("intent")
    return intent if intent in INTENTS else None


def _row(intent: str, raw: dict) -> IntentCost:
    return IntentCost(
        intent=intent,
        requests=_int(raw.get("count_count")),
        input_tokens=_int(raw.get("sum_inputTokens")),
        output_tokens=_int(raw.get("sum_outputTokens")),
        total_tokens=_int(raw.get("sum_totalTokens")),
        cost=_float(raw.get("sum_totalCost")),
    )


def _accumulate(found: dict[str, IntentCost], intent: str, *, requests: int = 0,
                input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
                cost: float = 0.0) -> None:
    previous = found.get(intent) or IntentCost(intent)
    found[intent] = IntentCost(
        intent=intent,
        requests=previous.requests + requests,
        input_tokens=previous.input_tokens + input_tokens,
        output_tokens=previous.output_tokens + output_tokens,
        total_tokens=previous.total_tokens + total_tokens,
        cost=previous.cost + cost,
    )


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
