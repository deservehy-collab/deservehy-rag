# ch09 设计规格：可观测性（Langfuse）与低置信问题数据飞轮

- 日期：2026-10-31
- 父提交：`1399be1`（第八次提交 ch08）
- 本章分支：`ch09-observability-flywheel`
- 过程留痕：`dev-notes/ch09.md`

## 1. 用户原话与目标

> 「我要用 Superpowers 模式给客服系统装上眼睛,再把答不上的问题变成改进燃料。」

六条验收：

1. Langfuse 里能点开任意一条请求看到完整链路（每个节点的 prompt、工具调用、检索结果、token 消耗、耗时）。
2. 问知识库没有的问题 → 兜底话术，且该问题出现在待审队列，审核页能看到用户原话和当时的召回片段。
3. 后台点通过后，同一个问题再问就能答对（飞轮转完一整圈）。
4. 聊天页点 👎 → 该用户问题落池、标准化查重后出现在待审队列。
5. 能拿出按意图汇总的 token 花销统计。
6. 评估流水线至少跑两轮，能看到指标趋势对比。

**本章不做**：低置信度问题按主题归类的微调分类器（用户明确说是下一步的事）。

## 2. 已确认的环境事实（设计的前置约束）

| 事实 | 实测来源 | 对设计的影响 |
|---|---|---|
| Docker Desktop VM 上限 8.19GB，VM 内现有 4 容器只用 0.8GB | `docker info` MemTotal=8189284352；`docker stats` | Langfuse 六容器必须显式限内存，合计 ≤3.6GB |
| 宿主只剩 3.9GB 空闲物理内存 | `Win32_OperatingSystem.FreePhysicalMemory` | **不抬 Docker 上限**（用户已确认走「限内存 + 按需启停」） |
| Langfuse 官方生产最低配 = web 4GiB + worker 4GiB + ClickHouse 8GiB + Postgres 4GiB + Redis 1.5GiB = 21.5GiB | langfuse.com/self-hosting/configuration/scaling | 只作参考；本地演示按限内存跑，跑不动就回来问用户，不自行换选型 |
| **端口冲突**：官方 compose 的 MinIO console 映射 `127.0.0.1:9091:9001`，而 Milvus 已占 `127.0.0.1:9091` | 官方 `docker-compose.yml:179` vs 本仓 `docker-compose.yml:72` | MinIO console 改映射 **9092** |
| 官方 compose 的 web `3000:3000`、minio `9090:9000` **没绑 127.0.0.1** | 官方 `docker-compose.yml:122,178` | 派生版一律绑 `127.0.0.1`，不对局域网暴露 |
| 官方 compose **完全没有 mem_limit** | grep 无命中 | 内存上限由本章自己加 |
| `LANGFUSE_INIT_*` 可预置 org/project/user/API keys | 官方 compose web 段 12-20 行 | **免掉手动在 UI 注册**：自己生成 `pk-lf-<uuid>` / `sk-lf-<uuid>` 写进 `.env`，容器起来就有账号和项目 |
| SDK 最新 **langfuse 5.1.0**；本机 langchain-core 1.7.0 / langchain 1.4.7 / Python 3.12.3 | `pip download` 后读 wheel 源码 | 满足 v5 要求的 `langchain-core>=1`；依赖只有 httpx/pydantic2/backoff/wrapt/packaging/opentelemetry-*（`>=1.45,<2`），与现有栈无冲突 |
| `CallbackHandler(*, public_key=None, trace_context=None)` 是关键字限定构造 | `langfuse/langchain/CallbackHandler.py:128-152` | user_id/session_id/tags **不能**从构造函数传 |
| trace 级属性只认 run metadata 里的 `langfuse_session_id` / `langfuse_user_id` / `langfuse_trace_name` / `langfuse_tags` | 同文件 `:478-502`、`:1848-1861` | invoke 时已知的三项走 `config["metadata"]` |
| **SDK 5.1.0 没有 `update_current_trace`**；`span.update()` 也没有 trace 级字段 | 全包 grep 无此符号；`_client/span.py:819-831` | intent 只能靠直写 OTEL 属性（见 §5.3） |
| trace 级 OTEL 键名：`langfuse.trace.name` / `user.id` / `session.id` / `langfuse.trace.tags` / `langfuse.trace.public` / `langfuse.trace.metadata` | `_client/attributes.py:29-35` | 直写这些键 |
| 指标接口 `GET /api/public/v2/metrics`，`query` 为 JSON 字符串，支持 `filters` 里 `type:"stringObject"+key:` 按 metadata 过滤；`userId`/`sessionId` 是高基数维度**只能当 filter 不能 group by** | 官方 `fern/apis/server/definition/metrics.yml:127` | 按意图统计 = 8 个意图各发一次带 metadata filter 的查询 |
| **`app/model.py:16` 是 `stream_usage=False`**，而主 Agent 走 `.astream()`（`app/agent/react.py:81`） | 读源码 | 不改则 Langfuse 记到的 token 全是 0，验收⑤落空 |
| DeepSeek 实测**支持**流式 usage：`stream_usage=True` 时最后一个 chunk 带 `usage_metadata={'input_tokens':32,'output_tokens':41,'total_tokens':73,'output_token_details':{'reasoning':39}}` | `/tmp/probe_usage.py` 真调一次（2026-10-31） | 改 `stream_usage=True`；成本报表要单列 reasoning token（deepseek-flash 的思考 token 计入 output 且不受 max_tokens 约束） |
| 非流式调用（`classifier.py:24`、`coref.py:76`、`expansion.py:47` 的 `.ainvoke`）本来就带 usage | 读源码 + 探针 `INVOKE usage=...` | 这三处不受影响 |
| `compile()` 不接受 callbacks（`app/workflow/graph.py:91`） | 读源码 | 用户说的「编译时挂一次」落地为「启动时构造一个 handler + 在 `graph.py:141` 唯一调用点传入」，全站一个接线点，效果等价 |
| `messages` 表不存 citations（`app/models.py:66-79`） | 读源码 | 用户已确认：给 assistant 行加 `retrieval_snapshot` JSON 列 |
| `init_db.py` 靠 `Base.metadata.create_all`，**不给已存在的表加列** | `scripts/init_db.py:9-21` | 必须另给幂等补列脚本（§7.3） |
| `IdType = mysql.BIGINT(unsigned=True).with_variant(Integer,"sqlite")`；既有 `low_confidence_questions.id` 在 MySQL 就是 `BIGINT UNSIGNED` | `models.py:19`、`004_rag_advanced.sql:4` | 用户 DDL 的 `matched_review_id BIGINT UNSIGNED` 外键类型正确 |
| Context7 MCP 本会话 0 个工具 | `mcp_list keyword=context7` | 改用官方文档 + 读发行包源码；两个错误 URL（`/self-hosting/infrastructure/overview`、`/integrations/native/langchain`）已 404，正确的是 `/self-hosting/deployment/docker-compose`、`/integrations/frameworks/langchain` |

## 3. 架构：五个互相独立、接口清楚的单元

```
┌─ 1. app/observability/     Langfuse 接线（可整体关掉，关掉后零副作用）
│     tracing.py             build_handler / trace_config / set_intent_on_trace / flush
│     cost_report.py         按意图查 metrics API
├─ 2. app/knowledge/confidence.py   evidence_confidence 三信号打分（替换 top1<0.30）
├─ 3. app/knowledge/pool.py  落池（扩签名带快照）+ 三入口接线
├─ 4. app/flywheel/          标准化 → 查重 → review_queue（幂等可续跑）
│     standardize.py  dedupe.py  pipeline.py  review.py
└─ 5. scripts/               校准、飞轮排空、成本报表、评估趋势 + 007 SQL + 补列迁移
      前端 app/web/admin.*   （Vibe Coding，不套流程）
```

依赖方向单向：`3 → 4`（落池后触发飞轮），`1` 与 `2/3/4` 无耦合（观测关掉不影响业务），`5` 只调用 `1..4` 的公开函数。

## 4. Langfuse 自部署（deploy/langfuse/）

**文件**
- `deploy/langfuse/docker-compose.yml`：派生自官方 main 分支 compose，改动仅四类——① 所有端口绑 `127.0.0.1`；② MinIO console `9091→9092`；③ 每个服务加 `mem_limit`；④ ClickHouse 挂一份内存配置。服务、镜像 tag、卷名、healthcheck 全部照抄官方，不自创。
- `deploy/langfuse/clickhouse-memory.xml`：挂到 `/etc/clickhouse-server/config.d/`，设 `<max_server_memory_usage>1342177280</max_server_memory_usage>`（1.25GiB 绝对值，比 ratio 稳，不受 VM 内存识别影响）。
- `start-langfuse.bat` / `stop-langfuse.bat`：双击可跑。start 做四件事：`docker compose up -d` → 轮询 web 健康 → 打印 UI 地址与预置账号 → 提示把 `.env` 里的 key 填好。
- `.env.example` 增补：`LANGFUSE_ENABLED=false`、`LANGFUSE_HOST=http://127.0.0.1:3000`、`LANGFUSE_PUBLIC_KEY=`、`LANGFUSE_SECRET_KEY=`，以及部署侧的 `LANGFUSE_INIT_*`（含注释说明 key 格式必须 `pk-lf-<uuid>` / `sk-lf-<uuid>`，用 `python -c "import uuid;print('pk-lf-'+str(uuid.uuid4()))"` 生成）。

**内存预算（合计 3.6GiB，装得进 VM 里 6.8GB 空闲）**

| 服务 | 上限 | 依据 |
|---|---|---|
| clickhouse | 1536M | 官方生产建议 8GiB；本地演示按 1.25G 服务内存 + 余量 |
| langfuse-web | 800M | 用 `NODE_OPTIONS=--max-old-space-size=640` 环境变量限堆，**不覆盖 command**（覆盖会顶掉镜像自带 CMD，起不来） |
| langfuse-worker | 600M | 同上，`NODE_OPTIONS=--max-old-space-size=480` |
| postgres | 256M | 只存元数据 |
| minio | 256M | 只存事件 blob |
| redis | 128M | 只加 `mem_limit`，**不加 `--maxmemory-policy allkeys-lru`**：Langfuse 用 Redis 存摄入队列，LRU 淘汰等于静默丢 trace。撞上限时 Langfuse 会记录摄入失败，本地演示可接受 |

**启停策略**：按需。日常开发/跑测试**不启**（`LANGFUSE_ENABLED=false` 是默认值），要做观测演示时才 `start-langfuse.bat`。这样 Milvus 与 Langfuse 不同时吃满内存。

**已知风险与处置**：ClickHouse 首次启动要跑迁移建表，是内存峰值点。若 OOM，处置顺序是——先降到 1024M 重试；仍不行则**停下来问用户**是否抬 Docker 上限（用户已选「跑不动再回来问」），**不自行换成 Langfuse Cloud 或降级 v2**。

## 5. 追踪接线

### 5.1 开关与构造（app/observability/tracing.py）

```python
def build_handler(settings) -> CallbackHandler | None
```
`LANGFUSE_ENABLED=false` 或 key 不全 → 返回 `None` 并记一条 warning；**不做任何 import 副作用**（`langfuse` 的 import 放在函数内部），保证没装 langfuse 的环境（比如只跑单测的 CI）也能启动。

```python
def trace_config(settings, conversation_id, handler) -> dict
```
返回要并进 LangGraph `config` 的片段：`{"callbacks": [handler], "metadata": {"langfuse_session_id": f"conv-{id}", "langfuse_user_id": f"conv-{id}", "langfuse_trace_name": "chat-turn"}}`；`handler is None` 时 `callbacks` 为空列表。

`user_id` 用会话 id 而不是真实用户：本项目没有账号体系，编造用户 id 是假数据。**这点在 README 里写明**，避免看起来像真的用户维度分析。

### 5.2 接线点（唯一一处）

`app/workflow/graph.py:141-145`，把 `trace_config(...)` 的结果并进现有 `{"configurable": {"thread_id": ...}}`。全站只有这一个 `astream` 调用点，所以「挂一次」成立。lifespan 收尾（`app/main.py`）调 `flush()`。

### 5.3 intent 进 trace 元数据（本章唯一有技术不确定性的地方）

intent 在图内 `input_nodes.py:9-23` 才算出来，而 SDK 5.1.0 没有 trace 级更新 API。方案：在 intent 节点末尾调

```python
def set_intent_on_trace(intent: str, confidence: float) -> bool
```
内部 `opentelemetry.trace.get_current_span()`，对当前 span 写 `langfuse.trace.tags = ["intent:<值>"]`、`langfuse.trace.metadata.intent`、`langfuse.trace.metadata.intent_confidence`。span 无效（`NonRecordingSpan`）或没装 otel → 返回 `False`，静默跳过，绝不抛。

**这条必须实测，不许想当然**：T2 写一次性探针 `scripts/probe_trace.py`（留档、不进主流程）——真跑一轮对话 → flush → 用 public API 拉回这条 trace，断言 trace 级 `tags` 含 `intent:...` 且 `metadata.intent` 有值，并打印 generation observation 的 usage/cost。

**退回方案（探针失败才走，且要写进 dev-notes）**：intent 只写在 intent 节点自己的 span 上（`get_client().update_current_span(metadata={"intent": ...})`，SDK 确定支持），成本统计改为 `GET /api/public/traces` + `GET /api/public/observations` 按 `traceId` 在本地 join 后按 intent 汇总。业务代码只改 `set_intent_on_trace` 一个函数和 `cost_report` 的取数方式，不影响其余设计。

**探针实测结论（2026-11-01，本机 Langfuse 4.72.0 `events_only` 部署，走上面这条退回方案，选型不变）**：
① 图内 `get_current_span()` 拿到的是 ASGI 的 `POST /api/chat` span，和那轮 Langfuse trace 同 traceId
但从不是被导出的 observation（该轮 14 行 `tags` 全 `[]`）——**事件管线只认 `scope.name=langfuse-sdk`
的 span**，所以「写在 intent 节点当前 span 上」根本不落地；② `POST /api/public/ingestion` 的
`trace-create` 被服务端拒（`events_only` 下只收 score 类事件），事后补 trace 标签这条路不存在；
③ 自己用 `client.start_as_current_observation(trace_context={"trace_id": ...})` 建**每轮根 span**
并写 `langfuse.trace.tags` / `langfuse.trace.metadata.*` ⇒ 该行确实带上 tags 与 `metadata.intent`，
`GET /api/public/v2/observations?name=…&isRootObservation=true` 能单独捞到；但 tag **不摊给同 trace
的兄弟行**，而 token 都在兄弟行上 ⇒ `groupBy tags` 归不出整轮 token。
结论：intent 的载体 = 「我们自建的每轮根 observation」，成本统计走 **Observations v2 取
`traceId → intent` + Metrics v2 按 `traceId` groupBy（带 `config.row_limit` + `orderBy` 才可）
取 token，本地 join**——即本节退回方案的形状，只把 v1 的 `traces`/`observations` 接口换成本机可用的
v2 接口（v1 两个接口在 events_only 部署下 404）。报表列与验收⑤口径不变。

### 5.4 让 token 数变成真的

`app/model.py:16` 改 `stream_usage=True`。风险点：主 Agent 的 chunk 累积逻辑（`app/agent/react.py:81` 起）会多收到一个只带 usage、content 为空的 chunk。**必须确认它不会因此多发一个空 token 帧或污染累积文本**——T2 先补一条针对性测试（假模型吐 [文本 chunk…, 空 content + usage chunk]，断言 SSE 文本不变、usage 被记录），再改这一行。

### 5.5 观测不能拖垮业务

Langfuse 是旁路：handler 抛错、网络不通、容器没起，都**不许**影响回复。处置——`build_handler` 失败返回 `None`；SDK 自身是后台批量导出（OTEL BatchSpanProcessor），导出失败只记 SDK 日志；`set_intent_on_trace` 全包 try/except。测试里要有一条「handler 抛异常时对话照常返回」的用例。

## 6. evidence_confidence：把最简闸升级成正式置信度检查

### 6.1 打分（app/knowledge/confidence.py）

```python
@dataclass(frozen=True)
class EvidenceConfidence:
    value: float          # 0..1
    signals: dict         # {"top1": .., "effective_count": .., "margin": ..}
    reason: str           # 落池 reason 用，人话 + 数字

def score(items, *, w_top1, w_count, w_margin, effective_floor) -> EvidenceConfidence
```

三个信号（用户点名的）：
- `top1` = `items[0].scores["rerank"]`（精排 Top1 相关性分，本身已是 0..1 的 sigmoid 输出）
- `effective_count` = `rerank >= effective_floor` 的条数；归一化 `min(count/3, 1.0)`（3 条及以上视为证据充分）
- `margin` = `top1 - top2`；**只有一条命中时 margin 记为 `top1` 本身**（没有竞争者，等价于「全部相关性都归它」），这条规则要写进 docstring 和测试

`value = clamp01(w_top1*top1 + w_count*norm_count + w_margin*margin)`。

**权重固定为一组可解释的值，不参与校准**：`w_top1=0.7, w_count=0.2, w_margin=0.1`。理由——80 题的标注集太小，三维权重加阈值一起扫必然过拟合；top1 本来就是 ch04 验证过 R@5=0.993 的主信号，另两维只做修正。**只校准 `confidence_min`（总阈值）和 `effective_floor`**，这两个是用户要求「拿评估集校准、不拍脑袋」的对象。

### 6.2 校准（scripts/calibrate_confidence.py）

- 输入：`evals/retrieval_qa.json` 80 题（含 `answerable` 标记），策略 `hybrid_rerank`。
- 对每题算 `value`，然后扫网格：`effective_floor ∈ {0.10,0.15,0.20,0.25,0.30}` × `confidence_min ∈ 0.00..0.95 步长 0.05`。
- 每格算两个数：**不可答拒答率**（`answerable=false` 的题里被拦下的比例）、**可答误拒率**（`answerable=true` 的题里被拦下的比例）。
- 选择规则（写死，不留人工挑）：先过滤出「不可答拒答率 = 1.000」的格子（ch04 已达成的水平，**不许退步**），在其中取可答误拒率最小者；若并列，取 `confidence_min` 较大者（更保守）。
- 产出：`evals/reports/ch09-confidence-{stamp}.md`（网格全表 + 选中格 + 选中理由）和 `.json`；选中的两个数写进 `app/config.py` 的默认值，`.env.example` 注明「由 ch09 校准脚本得出，改前请重跑校准」。
- 若无任何格子满足不可答拒答率 1.000 → 脚本非零退出并打印最接近的几格，**停下来问用户**，不擅自放宽约束。

### 6.3 接入（闸的位置不动）

- `app/knowledge/retrieval.py`：`retrieve()`（`:136-138`）和 `retrieve_expanded()`（`:223-225`）两处把 `top < self.rerank_min_score` 换成 `conf = score(...)` / `low = conf.value < self.confidence_min`；`RetrievalResult` 加两个字段：`confidence: EvidenceConfidence | None`、`snapshot: list[dict]`。
- **删掉 `rerank_min_score`**（`:64`）——升级不是并存，两个闸会让「为什么被拦」说不清。README 里按用户的收尾规矩标新旧两行（旧：`top1<0.30`；新：校准出的 `confidence_min`），不抹掉旧值。
- `gate_selector`（`knowledge_node.py:68-70`）判据仍是 `retrieval["low_confidence"]` 布尔，位置仍在检索之后、Agent 之前，**不动**。节点只是把 state 里的 `retrieval` 字典多带 `confidence`（value + signals + reason）和 `retrieval_snapshot`。
- 非 `hybrid_rerank` 策略没有精排分：`score()` 遇到 `items[0].scores` 无 `rerank` 键时返回 `value=1.0, reason="no_rerank"`（等价于不拦），保持与今天一致的行为。

### 6.4 召回快照

```python
def snapshot_of(items, limit=5) -> list[dict]
# [{"n", "chunk_id", "question", "answer"(截 500 字), "section_path", "scores": {"rerank","rrf"/"dense"/"bm25"}}]
```
低置信时 `result.items` 依然保留（`retrieval.py:139-143` 只清空 `evidence`），所以**快照从 `items` 取，不重跑检索**。

## 7. 数据模型与迁移

### 7.1 新表（照用户给的 DDL）

`review_queue`、`eval_runs` 完全按用户 DDL 落 `scripts/init_sql/007_flywheel.sql`，ORM 侧在 `app/models.py` 加 `ReviewQueue`、`EvalRun`，类型用既有 `IdType`/`CountType` 变体以保 SQLite 测试库能建。`ENUM('待审','通过','驳回')` 与 `ENUM('定时','手动')` 在 ORM 里用 `Enum(...)`，与 ch08 `tool_audit_logs.status` 同一套写法。

### 7.2 加列

| 表 | 新列 | 用途 |
|---|---|---|
| `low_confidence_questions` | `retrieved_chunks JSON NULL` | 落池当时的召回片段快照；没走检索的入口为 NULL |
| `low_confidence_questions` | `matched_review_id BIGINT UNSIGNED NULL` + FK → `review_queue.id ON DELETE SET NULL` | 归并落点 |
| `messages` | `retrieval_snapshot JSON NULL` | assistant 行存当轮 Top-5 片段，供 👎 事后回捞（用户已确认） |

### 7.3 迁移路径（本章第一个需要 ALTER 既有表的章节）

`create_all` 不给已存在的表加列，所以两条路都要有：
- `scripts/init_sql/007_flywheel.sql`：全新库用（含 `CREATE TABLE` × 2 + `ALTER TABLE` × 2）。
- `scripts/migrate_ch09.py`：**幂等补列**，查 `information_schema.COLUMNS` 判断列是否存在再 `ALTER`，可重复执行；对 SQLite 测试库同样可用（走 `PRAGMA table_info`）。README 的「升级已有库」小节指向它。

## 8. 三入口落池

`LowConfidencePool.record(...)` 扩签名为 `record(conversation_id, raw_question, source, reason=None, retrieved_chunks=None)`；异步包装 `record_low_confidence(...)` 同步扩参（`knowledge_node.py:11-17`）。三处调用点：

| 入口 | source | 位置 | 快照来源 |
|---|---|---|---|
| 检索证据置信度低 | `retrieval_low_conf` | `knowledge_node.py:76-77`（refuse 节点）、`agent_node.py:86-87`（query_faq 低置信） | state 的 `retrieval_snapshot` |
| 生成阶段自评知识不够答 | `self_check` | `agent_node.py:100-101`（ch04 已在落池，本章只补快照） | 手边的 `items` |
| 用户反馈没解决 | `user_feedback` | 新增 `POST /api/feedback` | 从 `messages.retrieval_snapshot` 按 `message_id` 回捞 |

`source` 的三个 ENUM 值 ch04 就已预留（`models.py:208`），「各自标好入池入口」天然满足。

**`POST /api/feedback`**：body `{conversation_id: int, message_id: int, value: "up"|"down"}`。
- `down` → 按 `message_id` 取该 assistant 行，再取**同一会话中它前面最近的一条 user 消息**作为 `raw_question`（这才是要落池的「用户问题」），快照取该 assistant 行的 `retrieval_snapshot`（NULL 就是「那一轮真没走检索」）→ 落池 `user_feedback`。
- `up` → 只返回 ok，不落库（避免为点赞建表；前端已有 localStorage 一次性语义）。
- 校验：`message_id` 不属于该 `conversation_id` → 403（沿用 ch05 建工单的归属校验口径）；找不到 → 404；重复提交同一 `message_id` 的 `down` → 幂等，不重复落池。
- **幂等判据要写死成精确匹配，不能用 LIKE**：`reason` 固定写成 `feedback:message_id=<id>`，查重条件是 `source='user_feedback' AND reason = 该完整字符串`（`reason` 是 Text 列，等值比较成立）。用 LIKE 会让 `message_id=12` 匹配上 `message_id=123`。
- 落池成功后同样触发飞轮（见 §9.3），所以 👎 这条会自己走进待审队列，验收④不需要人工跑脚本。
- **`done` 帧要带 `message_id`**：`reply_nodes.py:43` 已经拿到 `add_message` 的返回值，只是没往外发。前端点 👎 时才有办法指名哪一条。这是对既有 SSE 协议的扩展，向后兼容（老前端忽略新键）。

## 9. 飞轮流水线（app/flywheel/）

### 9.1 标准化 standardize.py

`standardize(rows) -> list[Standardized]`，一次 LLM 调用吃 ≤`FLYWHEEL_BATCH_SIZE`（默认 10）条原话，强制 JSON 数组返回：`{"pool_id", "normalized_question", "sample_answer"}`。
- 强制 JSON 沿用 ch06 的写法（`response_format` + 解析失败重试一次 + 失败则整批降级为逐条）。
- `sample_answer` 由模型**基于该行的召回片段**写（片段为 NULL 时明确写「知识库无相关依据」）——给审核人一个起点，不是最终答案。
- prompt 要钉住：标准化问题是 FAQ 式书面问法、不带人称、不改原意、不臆造商品信息。
- **验证方式（替代 TDD）**：造 10 条口语原话的标注样例（`evals/flywheel_standardize.json`，含期望的标准化问法要点），跑一遍人工核对 + 断言「不改原意」的可检查项（长度、是否含臆造数字）。

### 9.2 查重 dedupe.py

`find_match(normalized_question, candidates) -> int | None`，候选 = `review_status='待审'` 的行，一次给模型 ≤20 条（按 `occurrence_count` 降序，热点优先），强制 JSON 返回 `{"matched_id": <int|null>, "why": "..."}`。
- **只和「待审」比**（用户原话）。已知行为并写进 README：如果某问题曾被**驳回**，它再次出现会新建一行待审——这是有意的（反复出现本身就是「这个缺口是真的」的证据）。
- 模型返回的 `matched_id` 必须在候选集合里，否则视为未命中（防止幻觉 id 造成错误归并）。

### 9.3 编排 pipeline.py

```python
def drain(session_factory, llm, *, limit=50) -> DrainReport  # 同步，供脚本/线程调用
async def drain_later(...)                                   # fire-and-forget 包装
```
单趟：取 `matched_review_id IS NULL` 的池行（id 升序，≤limit）→ 批量标准化 → 逐条查重 → 命中则 `review_queue.occurrence_count += 1`，未命中则 INSERT 一行（`review_status='待审'`）→ 两种情况都回写该池行的 `matched_review_id`。

- **幂等与续跑**：判据就是 `matched_review_id IS NULL`，处理过的行不会再被取到；中途失败（LLM 超时、库断）留下的行下一趟自动重试。
- **一条坏行不许堵住整批**：逐行 try/except，失败的行保持 NULL 并记 warning，`DrainReport` 里计数。
- **并发**：模块级 `asyncio.Lock`，同一进程内不重叠跑；触发点失败只记日志。
- **触发时机**：唯一触发点是 `record_low_confidence`（`knowledge_node.py:11-17`）——它是三个入口共用的异步包装，落池成功后 `asyncio.create_task(drain_later(...))`。这样三个入口自动都覆盖，不必在每处调用点各写一遍（漏一处就是「有的问题进了队列、有的没进」这种最难查的 bug）。任务与回复并发，但**三个入口的回复都是固定话术或根本没有回复**（检索拦下 → `REFUSAL_TEXT`；自评拦下 → `REFUSAL_TEXT`；👎 → 只是接口返回 ok），所以后台 drain 的模型调用不会和同一轮的生成抢额度，用户感知不到延迟（复用 ch07 后台摘要的模式）。开关 `FLYWHEEL_AUTO_DRAIN`（默认 **true**，因为验收②④要求「问了就出现在待审队列」，不能靠人手跑脚本）。
- **成本**：每条池行摊到 ≤0.1 次标准化调用 + 1 次查重调用（批量后更低）。`FLYWHEEL_AUTO_DRAIN=false` 时只落池不入队，用 `scripts/run_flywheel.py` 批量排空。

### 9.4 审核通过 → 写回知识库

`app/flywheel/review.py`：
```python
def approve(session_factory, ingestor, review_id, answer=None) -> ApproveResult
def reject(session_factory, review_id, reason=None) -> bool
```
- `approve`：终答 = `answer or row.approved_answer or row.ai_suggested_answer`；调 ch03 那条落库路 `KnowledgeIngestor.ingest_pairs([{"question": row.normalized_question, "answer": final}], source_ref=f"review_queue:{id}")` → `vectorize_pending()`（`app/knowledge/ingest.py:92-106`、`:108-155`）→ 更新行 `review_status='通过'`、`approved_answer=final`。
- **幂等**：已是「通过」的行再 approve → 返回冲突（HTTP 409），**不重复写知识库**；否则同一条 FAQ 会被灌进 Milvus 两遍，检索时自己跟自己竞争。
- ingest + 向量化是重活（要跑本地 embedding），接口里放 `asyncio.to_thread`。
- `reject`：只改状态，不动知识库。

### 9.5 后台接口与页面

接口（沿用现状：内联在 `main.py`，无 APIRouter，无鉴权——本项目是本地演示，现有接口一律无鉴权，不单独给后台加一套假的登录）：
- `GET /api/admin/review-queue?status=&limit=&offset=` → 列表：标准化问题、出现次数、状态、示例答案、创建/更新时间
- `GET /api/admin/review-queue/{id}` → 详情 + 归并进来的原话（join `low_confidence_questions ON matched_review_id`，带 `source`、`created_at`、`retrieved_chunks`）
- `POST /api/admin/review-queue/{id}/approve`（body 可选 `answer`）、`POST /api/admin/review-queue/{id}/reject`（body 可选 `reason`）

页面 `app/web/admin.html` + `admin.js` + `admin.css`，路由 `/admin`（`main.py` 现有 `/` 返回 index.html 的同一套写法）。**前端走 Vibe Coding**：不套 brainstorm/TDD/code review，用户描述效果我改；只做后端接口的契约测试 + `node --check` 语法校验兜底。列表页显示标准化问题/出现次数/示例答案/操作；点详情展开抽屉，看到归并进来的用户原话和当时的召回片段（正文 + 精排分），审核人据此判断「知识库是真缺这块，还是有但没检到」。

## 10. 按意图的 token 花销（验收⑤）

`app/observability/cost_report.py` + `scripts/report_intent_cost.py`：
- 主路径：对 8 个意图各发一次 `GET /api/public/v2/metrics`，`view="observations"`，`metrics=[{"measure":"totalCost","aggregation":"sum"},{"measure":"totalTokens","aggregation":"sum"},{"measure":"count","aggregation":"count"}]`，`filters=[{"column":"metadata","type":"stringObject","key":"intent","operator":"=","value":<意图>}]`，时间窗由 `--from/--to` 给（默认最近 7 天）。Basic Auth = public:secret。
- 输出：终端表格 + `evals/reports/ch09-intent-cost-{stamp}.md`，列「意图 / 请求数 / 输入 token / 输出 token / 其中 reasoning / 估算花费」。
- **measure 名与 metadata filter 的可用性要在 T2 探针里一并验真**（fern 定义确认了接口和 filter 结构，但具体 measure 名如 `totalCost`/`totalTokens` 需对自建版本实测）；对不上就退回 §5.3 的 traces+observations 本地 join 方案，同一张报表口径不变。
- 若 §5.3 探针失败走了退回方案，这里也跟着走 join，**报表格式和验收口径不变**（用户要的是「能拿出按意图汇总的统计」，不是「必须用某个接口」）。

## 11. 评估流水线与趋势（验收⑥）

- `scripts/evaluate_retrieval.py` 的 `main()` 收尾（`:170-185` 写报告处）插一行 `eval_runs`：`triggered_by`（新增 `--trigger {定时,手动}`，默认「手动」）、`dataset_size`（本轮实际跑的题数）、`metrics`（JSON：`recall@5`、`recall@10`、`mrr@10`、`refusal_rate`、`faithfulness`）。**带 `--no-faith` 跑的那轮，`faithfulness` 键写 `null` 而不是省略**——趋势报告要能区分「这轮没测」和「这轮测出来是 0」。复用 ch04 的评估集和指标函数，**不改判据**（用户红线：不许为了演示好看去改判据）。
- `scripts/report_eval_trend.py`：读最近 N 轮，按时间输出对比表，每个指标带与上一轮的差值和 `↑/↓/=` 标记，下滑的行前置 `!!`，让人一眼看出哪个指标在掉；任一侧为 `null` 的差值显示 `n/a`，不算下滑。同时写 `evals/reports/ch09-eval-trend-{stamp}.md`。
- 定时：复用 ch03 那套进程内 APScheduler，新增 `EVAL_SCHEDULE_CRON`（默认空 = 关）。开着时按 cron 跑一轮并以 `triggered_by='定时'` 落表。
- 验收⑥要求至少两轮：手动连跑两次（中间可以把 `confidence_min` 调成不同值，让趋势真的有变化，但**不动评估集和指标定义**），产出趋势报告。

## 12. 配置项汇总（.env.example 增补）

| 键 | 默认 | 说明 |
|---|---|---|
| `LANGFUSE_ENABLED` | `false` | 关掉时全链路零副作用 |
| `LANGFUSE_HOST` | `http://127.0.0.1:3000` | 自部署地址 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 空 | `pk-lf-<uuid>` / `sk-lf-<uuid>` |
| `CONFIDENCE_MIN` | 校准值 | 总阈值，低于则拦 |
| `CONFIDENCE_EFFECTIVE_FLOOR` | 校准值 | 算「有效证据条数」的下限 |
| `CONFIDENCE_W_TOP1/W_COUNT/W_MARGIN` | `0.7/0.2/0.1` | 固定不校准（§6.1 理由） |
| `SNAPSHOT_TOP_N` | `5` | 召回快照条数 |
| `FLYWHEEL_AUTO_DRAIN` | `true` | 落池后是否自动跑飞轮 |
| `FLYWHEEL_BATCH_SIZE` | `10` | 标准化每批条数 |
| `FLYWHEEL_DEDUPE_CANDIDATES` | `20` | 查重每次给模型的候选上限 |
| `EVAL_SCHEDULE_CRON` | 空 | 定时评估，空 = 关 |

`stream_usage` 不是配置项，是 `app/model.py` 里直接改 `True`（没有理由让它可关：关掉就等于观测数据是假的）。

## 13. 测试策略

**单测（TDD，RED 先行）**
- `confidence.score`：给定 items → 期望 value/signals/reason；含「只有一条命中时 margin = top1」「无 rerank 键时 value=1.0 不拦」「clamp 到 0..1」
- `snapshot_of`：条数上限、答案截断 500 字、分数字段齐全
- `LowConfidencePool.record` 新签名：快照落 JSON 列、`retrieved_chunks` 为 NULL 的入口照常写
- `pipeline.drain`：幂等（重跑不新建行、命中累加 `occurrence_count`）、一条坏行不堵整批、`matched_review_id` 回写、只和「待审」候选比
- `dedupe.find_match`：模型返回幻觉 id → 视为未命中
- `review.approve`：调用 `ingest_pairs`+`vectorize_pending`、重复 approve → 409 且**不重复写知识库**、终答优先级 `answer > approved_answer > ai_suggested_answer`
- `POST /api/feedback`：👎 落池且回捞到快照、没走检索的轮次快照为 NULL、message_id 归属不符 → 403、重复 👎 幂等
- `done` 帧带 `message_id`
- `eval_runs`：跑完落一行，字段齐全；`report_eval_trend` 对造好的三行数据能算出差值和 ↓ 标记
- `tracing`：`build_handler` 在 enabled=false / key 缺失 / import 失败三种情况下都返回 None 且不抛；`set_intent_on_trace` 无 OTEL span 时返回 False；**handler 抛异常时对话照常返回**
- `stream_usage=True` 后：假模型吐「文本 chunk… + 空 content 带 usage 的 chunk」，断言 SSE 文本不变、不产生空 token 帧

**用标注集/评估集验证（替代 TDD 的部分，用户明确要求）**
- 校准脚本跑 80 题，产出网格报告；断言选中格满足「不可答拒答率 = 1.000」
- 标准化：10 条口语原话标注样例跑一遍，人工核对 + 可检查断言
- 评估流水线真跑两轮出趋势

**真机验收（6 条，对真服务 + 真模型 + 真 MySQL + 真 Langfuse）**
演示脚本 `scripts.demo_flywheel --case all`，六条一一对应用户的验收标准；Langfuse 那两条要打印 trace URL 让人能点开看。

## 14. 交付物

- `deploy/langfuse/`（compose + clickhouse 内存配置 + 两个 .bat）
- `app/observability/`、`app/knowledge/confidence.py`、`app/flywheel/`
- `scripts/`：`calibrate_confidence.py`、`run_flywheel.py`、`report_intent_cost.py`、`report_eval_trend.py`、`migrate_ch09.py`、`probe_trace.py`、`demo_flywheel.py`
- `scripts/init_sql/007_flywheel.sql`
- `app/web/admin.{html,js,css}` + `main.py` 的 `/admin` 与 `/api/admin/*`、`/api/feedback`
- README 新章节 + `dev-notes/ch09.md`
- 一次提交：「第九次提交（ch09）」，推送前告知用户开代理

## 15. 风险与未决

| 风险 | 处置 |
|---|---|
| ClickHouse 首次迁移 OOM | 降到 1024M 重试 → 仍不行**停下来问用户**是否抬 Docker 上限；不换 Cloud、不降 v2 |
| 非根 span 上的 `langfuse.trace.*` 不归并到 trace | T2 探针验真；失败走 §5.3 退回方案，报表口径不变 |
| metrics v2 的 measure 名对不上自建版本 | 同上，退回 traces+observations join |
| `stream_usage=True` 多出空 chunk 污染 SSE | 先补测试再改这一行（§5.4） |
| 拉 Langfuse 镜像要代理（docker 走 `http.docker.internal:3128`，本机 mihomo 在 7890） | 拉镜像前告知用户；必要时在 Docker Desktop 配代理或用镜像源，**不改全局 git config** |
| 校准网格无格子满足不可答拒答率 1.000 | 脚本非零退出 + 打印最接近的几格，**停下来问用户**，不擅自放宽 |
| 80 题上校准出的阈值可能对线上真实分布过窄 | 在 README 写明「阈值来自 80 题标注集，扩充标注集后需重跑校准」，并把校准脚本做成可重复执行 |
