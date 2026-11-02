"""一次性探针：验证「每轮根 observation 承载 intent + Metrics v2 按 traceId 本地 join」这条真路。
不进主流程、不参与业务判据。
    python scripts/probe_trace.py --question "我要退款" --conversation-id 900001
    （仓库惯例等价的写法是 python -m scripts.probe_trace，两种都能跑，见下方 _bootstrap_path）
判据（T4 落地后重定义；三个 verdict 名字与退出码口径不变）：
    MAIN     —— 本轮根 observation（name=chat-turn、isRootObservation=true）的 metadata.intent
                已落上，**且**用它的 traceId 在 Metrics v2 里拿到该轮 sum_totalTokens > 0（退出码 0）
    OBS_ONLY —— 根 observation 带上了 intent，但 Metrics join 拿不到该轮 token
    FAILED   —— 根 observation 上也没有 intent
读路径说明（实测，不是猜的）：本机 v4.72.0 是 events_only 部署，GET /api/public/traces、
/api/public/sessions、GET /api/public/observations 都返回 404；官方指定的实时读路径是
GET /api/public/v2/observations 和 GET /api/public/v2/metrics。v2 observations 支持
`sessionId` 过滤，根 observation 行顶层就有 `sessionId` / `projectId` / `traceId`
（`trace_context` 是 null，别去里面找）。⇒「trace 级 tags + groupBy tags」在本部署不可用：
tag 只落我们自建的那一行根 observation，token 全在兄弟行上，所以只能按 traceId 在本地 join。
接线是 T4 已落进 `Workflow._drive` 的真实调用（`app/workflow/graph.py`）：每轮开 `turn_trace`
（根 span + 绑到同 trace 的 CallbackHandler），intent 节点就地写根行。探针不再 patch 任何业务
函数、不再包 classifier——「返回 True ≠ 落地」正是 T2 阶段 4 的教训，判据只看回读结果。
落盘红线（T2 评审修复轮 1 第 3 条）：报告先过红敏（剥掉 scope.attributes.* 与名字含
public_key / secret 的键），再扫序列化全文，命中 pk-lf- / sk-lf- ⇒ 拒写文件、退出码非零。
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_path() -> None:
    """`python scripts/probe_trace.py` 时 sys.path[0] 是 scripts/，import app 会炸。
    补上仓库根，使 brief 里那条命令与 `python -m scripts.probe_trace` 等价。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


_bootstrap_path()

from app.config import Settings  # noqa: E402
from app.observability.tracing import flush_tracing  # noqa: E402

ROOT_OBSERVATION_NAME = "chat-turn"


def api_get(host: str, pk: str, sk: str, path: str) -> dict:
    token = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    request = urllib.request.Request(host.rstrip("/") + path,
                                     headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _intent_of(row: dict) -> tuple:
    """v2/observations 的 metadata 常是 JSON 字符串（偶为 dict），统一取 intent 两键。"""
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata.get("intent"), metadata.get("intent_confidence")


def _tokens_of(data, trace_id: str) -> tuple:
    """Metrics v2 按 traceId 分组的结果里取本轮的 sum_totalTokens（join 的另一半）。"""
    for row in data or []:
        if (row.get("traceId") or row.get("id")) == trace_id:
            return row.get("sum_totalTokens"), row
    return None, None


# --------------------------------------------------------------------------------------
# 隐私红敏（T2 评审修复轮 1 第 3 条）。对着活着的服务端实测：根 observation 的 metadata 里带
# `scope.attributes.public_key`，值就是真实项目 pk。报告是对外证据，落盘前过两道：
#   ① 结构红敏：丢掉 scope.attributes.* 与任何名字含 public_key / secret 的键，
#      保留 intent / intent_confidence 等业务键（递归，所以将来有人整行塞进报告也兜得住）；
#   ② 写盘前扫序列化全文，命中 pk-lf- / sk-lf- ⇒ 不写文件、打印一句、退出码非零。
#      探针允许大声失败（业务代码才要求绝不抛）。
# --------------------------------------------------------------------------------------

CREDENTIAL_PREFIXES = ("pk-lf-", "sk-lf-")


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower()
    return (lowered.startswith("scope.attributes.")
            or "public_key" in lowered or "secret" in lowered)


def redact_metadata(metadata):
    """剥掉敏感键，只留业务键。metadata 在 v2 接口里既可能是 dict 也可能是 JSON 字符串
    （`_intent_of` 同样两种都吃过），字符串就解析→过滤→再序列化回字符串，下游读法不变。"""
    is_string = isinstance(metadata, str)
    parsed = metadata
    if is_string:
        try:
            parsed = json.loads(metadata)
        except ValueError:
            return metadata          # 不是 JSON：原样留着，交给第二道全文扫描兜
    if not isinstance(parsed, dict):
        return metadata
    cleaned = {key: redact_report(value) for key, value in parsed.items()
               if not _is_sensitive_key(key)}
    return json.dumps(cleaned, ensure_ascii=False) if is_string else cleaned


def redact_report(node):
    """递归红敏整份报告：敏感键名整个丢掉，名为 metadata 的值额外过 `redact_metadata`。"""
    if isinstance(node, dict):
        cleaned: dict = {}
        for key, value in node.items():
            if _is_sensitive_key(key):
                continue
            if str(key).lower() == "metadata":
                cleaned[key] = redact_metadata(value)
            else:
                cleaned[key] = redact_report(value)
        return cleaned
    if isinstance(node, list):
        return [redact_report(item) for item in node]
    return node


def write_redacted_report(report: dict) -> tuple:
    """红敏 → 扫凭据 → 才落盘。返回 (路径或 None, 命中的凭据前缀或 None)。"""
    text = json.dumps(redact_report(report), ensure_ascii=False, indent=2)
    hit = next((prefix for prefix in CREDENTIAL_PREFIXES if prefix in text), None)
    if hit is not None:
        return None, hit
    path = f"evals/reports/ch09-trace-probe-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)
    return path, None


def probe_metrics(settings) -> int:
    """T16 取数方式的现场探针：只读，不跑对话、不改任何状态。

    每个请求先打印**未 URL 编码**的 query payload，再打印响应（响应里不含 key；
    observations 的 metadata 带 `scope.attributes.public_key`，先过 `redact_report` 再打印）。
    中文 JSON 交给 httpx 编码，不在 shell 里拼。

    返回退出码：至少一个端点 200 → 0；全都失败 → 1（探针不能把「全挂」报成成功）。
    """
    import httpx

    from app.observability.cost_report import (
        batch_trace_ids, build_grouped_query, build_intent_query, build_join_metrics_query,
        build_observations_query,
    )

    host = str(settings.langfuse_host).rstrip("/")
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
    succeeded = 0

    def fetch(url, *, params=None):
        try:
            return httpx.get(url, params=params, auth=auth, timeout=20)
        except httpx.HTTPError as exc:
            print(f"请求失败：{type(exc).__name__}: {exc}")
            return None

    print("== Observations v2：chat-turn 根 observation（metadata 已红敏）==")
    observations_query = build_observations_query()
    print("query:", json.dumps(observations_query, ensure_ascii=False))
    response = fetch(host + "/api/public/v2/observations", params=observations_query)
    if response is None:
        trace_ids = []
    else:
        print(f"status={response.status_code}")
        if response.status_code == 200:
            succeeded += 1
            payload = response.json()
            print("top-level keys:", sorted(payload.keys()), "rows:",
                  len(payload.get("data") or []))
            print(json.dumps(redact_report(payload), ensure_ascii=False)[:2000])
            roots = payload.get("data") or []
            for row in roots[:10]:
                intent, confidence = _intent_of(row)
                print(f"  trace={row.get('traceId')} start={row.get('startTime')} "
                      f"tags={json.dumps(row.get('tags'), ensure_ascii=False)} "
                      f"metadata.intent={intent!r} confidence={confidence!r}")
            trace_ids = [row.get("traceId") for row in roots if row.get("traceId")]
        else:
            print(response.text[:1000])
            trace_ids = []

    for label, query in (("grouped（tags 维度）", build_grouped_query()),
                         ("per_intent:物流（tags 过滤）", build_intent_query("物流"))):
        print(f"--- {label}")
        print("query:", json.dumps(query, ensure_ascii=False))
        url = f"{host}/api/public/v2/metrics?query={quote(json.dumps(query, ensure_ascii=False))}"
        response = fetch(url)
        if response is None:
            continue
        print(f"（payload 已 URL 编码）响应 -> {response.status_code}")
        print(response.text[:2000])
        if response.status_code == 200:
            succeeded += 1

    if trace_ids:
        batch = batch_trace_ids(trace_ids)[0]
        query = build_join_metrics_query(batch)
        print(f"--- join（{len(batch)} 个 traceId 按 traceId 分组）")
        print("query:", json.dumps(query, ensure_ascii=False))
        url = f"{host}/api/public/v2/metrics?query={quote(json.dumps(query, ensure_ascii=False))}"
        response = fetch(url)
        if response is not None:
            print(f"（payload 已 URL 编码）响应 -> {response.status_code}")
            print(response.text[:2000])
            if response.status_code == 200:
                succeeded += 1
    else:
        print("--- join 跳过：没有 chat-turn 根 observation。")

    if succeeded == 0:
        print("== 所有端点都失败：--metrics 退出码 1 ==")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ch09 每轮根 observation + 本地 join 探针")
    parser.add_argument("--question", default="我要退款")
    parser.add_argument("--conversation-id", type=int, default=900001)
    parser.add_argument("--metrics", action="store_true",
                        help="只探测 T16 的取数接口（Observations v2 / Metrics v2），不跑对话")
    args = parser.parse_args()

    settings = Settings.from_env()
    host, pk, sk = (settings.langfuse_host, settings.langfuse_public_key,
                    settings.langfuse_secret_key)
    if not (settings.langfuse_enabled and pk and sk):
        print("先跑 start-langfuse.bat，并把 pk/sk 填进项目根 .env 且 LANGFUSE_ENABLED=true。")
        return 2

    if args.metrics:
        return probe_metrics(settings)

    # 1) 真跑一轮：走和线上一样的 Workflow，保证观测路径一致。探针不 patch 任何业务函数，
    #    也不再包 classifier——T4 之后接线在 `Workflow._drive` 里，intent 由 intent 节点写根行。
    #    探针只把 MCP 摘掉（本机 8931/8932 没起，握手会拖慢整轮），Workflow 本体不动。
    #    `demo_client` 用真库建会话（不再造临时 sqlite），会话 id 由 `start_turn` 现建，
    #    只能从 meta 帧读——绝不要把 `--conversation-id` 直接当会话 id 传进请求。
    from scripts.demo_flywheel import demo_client, run_turn

    probe_settings = settings.model_copy(update={"mcp_servers": "", "log_file": ":memory:",
                                                 "workflow_checkpoint_path": ":memory:"})
    # 回读用「本轮开始时间」+ sessionId 卡住：sessionId 唯一，但同一会话可能有多轮，
    # 取 startTime 最新的一行就不会混进历史运行。
    started_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    with demo_client(probe_settings) as client:
        turn = run_turn(client, args.question, client_id=f"probe-{args.conversation_id}")
    flush_tracing()

    if turn["error"]:
        print("对话没跑通：", turn["error"])
        return 2
    print(f"对话 OK：conv={turn['conversation_id']} events={turn['events']} "
          f"text={turn['text'][:60]!r}")

    session_id = f"conv-{turn['conversation_id']}"

    # 2) 回读两步 join（v1 的 traces/observations 在 events_only 下 404，别用）：
    #    ① Observations v2 按 sessionId 拿本轮**根 observation**（name=chat-turn、
    #       isRootObservation=true）的 metadata.intent 与 traceId；② Metrics v2 按 traceId
    #       分组拿本轮 token。
    #    trace_context/metadata 投影有延迟（实测首轮读到空、隔一次重读才有值），边等边试（最长 6 分钟）。
    fields = "core,basic,metadata,trace_context"
    deadline = datetime.now(timezone.utc) + timedelta(minutes=6)
    root_row: dict | None = None
    rows_seen = 0
    while datetime.now(timezone.utc) < deadline:
        try:
            listed = api_get(host, pk, sk,
                             f"/api/public/v2/observations?name={ROOT_OBSERVATION_NAME}"
                             f"&isRootObservation=true&sessionId={session_id}"
                             f"&fields={fields}&limit=10"
                             f"&fromStartTime={quote(started_at.isoformat(), safe='')}")
            window = [r for r in (listed.get("data", []) or [])
                      if r.get("sessionId", session_id) == session_id]
            rows_seen = len(window)
            latest = (max(window, key=lambda r: str(r.get("startTime") or ""))
                      if window else None)
            if latest is not None:
                root_row = latest
                if _intent_of(latest)[0]:
                    break
        except Exception as error:
            print("observations 查询失败：", type(error).__name__, error)
        time.sleep(10)

    intent_on_root, confidence_on_root = _intent_of(root_row) if root_row else (None, None)
    trace_id = (root_row or {}).get("traceId")
    project_id = (root_row or {}).get("projectId")

    # ② Metrics v2：traceId 是高基数维度，必须带 config.row_limit + orderBy 才给 200；
    #    filter 的类型要写 stringOptions（写 string[] 会被 400）。
    metrics_query = {
        "view": "observations",
        "dimensions": [{"field": "traceId"}],
        "metrics": [{"measure": "totalTokens", "aggregation": "sum"},
                    {"measure": "count", "aggregation": "count"}],
        "filters": [{"column": "traceId", "operator": "any of", "type": "stringOptions",
                     "value": [trace_id]}] if trace_id else [],
        "fromTimestamp": started_at.isoformat(),
        "toTimestamp": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "config": {"row_limit": 50},
        "orderBy": [{"field": "sum_totalTokens", "direction": "desc"}],
    }
    metrics_data: object = None
    metrics_error: str | None = None
    tokens = None
    # token 是兄弟行导完之后才聚得出来的，投影同样有延迟：这一半最多再等 120 秒，
    # 别把 6 分钟窗口在同一件事上等两遍。
    metrics_deadline = datetime.now(timezone.utc) + timedelta(seconds=120)
    while trace_id:
        try:
            metrics_data = api_get(host, pk, sk,
                                   f"/api/public/v2/metrics?query={quote(json.dumps(metrics_query))}"
                                   ).get("data")
            metrics_error = None
            tokens, _ = _tokens_of(metrics_data, trace_id)
        except Exception as error:
            metrics_error = f"{type(error).__name__} {error}"
            print("metrics v2（按 traceId）失败：", metrics_error)
            break
        if (tokens or 0) > 0 or datetime.now(timezone.utc) >= metrics_deadline:
            break
        time.sleep(10)

    # 本轮各行的 usage（报表口径核对用；按 traceId 精确取，不靠 sessionId 免撞车）
    generation_usage: list = []
    if trace_id:
        try:
            every = api_get(host, pk, sk,
                            f"/api/public/v2/observations?traceId={trace_id}"
                            f"&fields=core,basic,usage,metadata&limit=100").get("data", []) or []
            generation_usage = [[r.get("name"), r.get("type"), r.get("totalUsage")]
                                for r in every if r.get("traceId") == trace_id]
        except Exception as error:
            print("本轮 observations 取 usage 失败：", type(error).__name__, error)

    intent_present = bool(intent_on_root)
    if intent_present and (tokens or 0) > 0:
        verdict = "MAIN"
    elif intent_present:
        verdict = "OBS_ONLY"
    else:
        verdict = "FAILED"

    print(f"回读：session={session_id} trace={trace_id} 窗口内根 observation 行数={rows_seen}")
    print(f"根 observation：name={(root_row or {}).get('name')} "
          f"tags={json.dumps((root_row or {}).get('tags') or [], ensure_ascii=False)} "
          f"metadata.intent={intent_on_root!r} metadata.intent_confidence={confidence_on_root!r}")
    print(f"Metrics join 该轮 sum_totalTokens={tokens!r}")
    if root_row is not None:
        # 实测地址：{host}/project/<projectId>/traces/<traceId>，projectId 就在根行上。
        print(f"trace URL：{host.rstrip('/')}/project/{project_id}/traces/{trace_id}")
    else:
        print("没有读到根 observation：先确认 Langfuse 容器与 key，再看本轮是否真的开了观测。")

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "verdict": verdict, "session_id": session_id,
              "root_lookup": {"via": "sessionId", "session_id": session_id,
                              "from_start_time": started_at.isoformat(timespec="seconds")},
              "turn": {k: turn[k] for k in ("conversation_id", "message_id", "events", "error")},
              "root_observation": {"name": (root_row or {}).get("name"),
                                   "tags": (root_row or {}).get("tags"),
                                   "metadata.intent": intent_on_root,
                                   "metadata.intent_confidence": confidence_on_root,
                                   "trace_id": trace_id,
                                   "project_id": project_id,
                                   "observation_id": (root_row or {}).get("id"),
                                   "start_time": (root_row or {}).get("startTime")},
              "metrics_join_query": {k: v for k, v in metrics_query.items()
                                     if k in ("view", "dimensions", "metrics", "filters",
                                              "config", "orderBy")},
              "metrics_join": metrics_data,
              "metrics_join_error": metrics_error,
              "tokens_this_turn": tokens,
              "generation_usage": generation_usage}
    os.makedirs("evals/reports", exist_ok=True)
    path, credential_hit = write_redacted_report(report)
    if credential_hit is not None:
        print("报告含凭据片段，已拒写")
        print(f"（命中前缀 {credential_hit}；verdict={verdict}，不落盘、不「先写再删」）")
        return 1

    print(f"探针结论：{verdict}（报告 {path}）")
    print("generation usage:", json.dumps(generation_usage, ensure_ascii=True)[:400])
    print("metrics join:", json.dumps(metrics_data, ensure_ascii=True)[:400])
    return 0 if verdict == "MAIN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
