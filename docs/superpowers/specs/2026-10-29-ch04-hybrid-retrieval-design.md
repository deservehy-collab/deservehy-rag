# ch04 设计规格：混合检索、重排、生成控制与评估体系

- 日期：2026-10-29
- 范围：在 ch03 向量知识库基础上，加入 Milvus 原生 BM25、RRF 混合检索、bge-reranker-v2-m3 重排、元数据过滤、Query 改写与检索侧同义词扩展、引用/拒答/低置信度池、四策略评估与聊天页引用/反馈。
- 不做：指代消解、多轮改写；用户反馈只做前端采集，不落库；关键词检索不在 MySQL 侧兜底。

## 1. 目标与验收

1. 四策略（纯向量、纯 BM25、混合、混合+重排）对比报告能跑出数字。
2. 带具体型号的问题，BM25 单路能命中。
3. 回答引用编号可映射回来源 chunk，聊天页点引用能看到原文与章节路径。
4. 知识库没有的内容得到明确拒答，问题进入 `low_confidence_questions`。

## 2. 已确认决策

| 决策 | 选择 | 理由 |
|---|---|---|
| Query 改写 | LLM 改写 + 静态同义词表 | 口语归一靠模型，同义词可控且只影响检索侧 |
| 低置信度入池 | 检索闸门 + 生成自评 | `retrieval_low_conf` 与 `self_check` 两个来源都用上 |
| 评估成本 | 四策略检索全量；Faithfulness 只跑 hybrid_rerank 子集 | 检索指标无模型成本，生成与裁判按子集控制调用量 |
| 评估集 | 本项目写 80 题，每桶 20 | 覆盖政策、型号、口语、多意图，含难度梯度与不可答题 |

## 3. 总体架构

```
POST /api/chat
  └─ 模型首轮：选择工具（query_order / query_faq / ...）
       └─ query_faq(question, category?)
            ├─ QueryRewriter（LLM，JSON，缓存，失败回退原话）
            ├─ 同义词扩展（仅 BM25 查询串）
            ├─ Milvus dense Top-50 + BM25 Top-50
            ├─ hybrid_search + RRFRanker(k=60) → 候选
            ├─ CrossEncoder 精排 → Top-10
            ├─ 证据按「最相关在首尾」重排并编号
            └─ 检索闸门：无证据或 Top-1 低于阈值 → low_confidence
  └─ 命中 query_faq 且 ok=true：
       ├─ 发送 citations 事件（证据快照）
       ├─ 生成前自评 useful（LLM JSON）
       ├─ useful=false → 固定拒答 + low_confidence_questions(self_check)
       └─ useful=true → 第二轮流式生成（带 [n] 引用）
  └─ 检索闸门失败：固定拒答 + low_confidence_questions(retrieval_low_conf)
```

MySQL 是唯一事实源：`knowledge_chunks` 存文本与状态，`conversations/messages` 存对话。Milvus 只存 `vector_id`、dense 向量、BM25 文本与 sparse 向量、过滤字段。

## 4. Milvus 集合与入库

### 4.1 集合结构

集合名沿用 `knowledge`，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `vector_id` | VARCHAR(64) PK | 等于 MySQL `knowledge_chunks.id` |
| `embedding` | FLOAT_VECTOR(1024) | BGE-M3 dense |
| `text` | VARCHAR(8192) | 用于 BM25 的拼接文本：`category\nquestions\nanswer` |
| `bm25_sparse` | SPARSE_FLOAT_VECTOR | `FunctionType.BM25` 的 `text` 输出 |
| `category` | VARCHAR(255) | 过滤 |
| `content_type` | VARCHAR(32) | 过滤/展示 |
| `section_path` | VARCHAR(512) | 引用展示 |
| `is_key_clause` | BOOL | 元数据 |

- `text` 字段 `enable_analyzer=True`，`analyzer_params={"type": "chinese"}`。
- dense 索引：HNSW / COSINE / M=16 / efConstruction=200；sparse 索引：`SPARSE_INVERTED_INDEX` / `BM25` / `{"bm25_k1": 1.2, "bm25_b": 0.75}`。
- 写入只提供 `vector_id/embedding/text/category/content_type/section_path/is_key_clause`，sparse 由 BM25 函数自动生成。
- 集合结构校验：启动时读取集合字段，缺 `text`/`bm25_sparse` 时抛 `CollectionSchemaMismatch`，提示执行 `python -m scripts.ingest_knowledge --rebuild --yes`。不自动删除集合。

### 4.2 入库

- `KnowledgeIngestor.vectorize_pending` 与 `ingest_*` 逻辑保持幂等与断点续跑，upsert 增加文本与元数据。
- `rebuild` 清空 `knowledge_chunks` 与 Milvus 集合后重建；只影响知识库，不碰会话/工单。
- 新增 `examples/knowledge/04-型号与兼容性.md`，提供型号、配件型号、适配关系，支撑「具体型号」验收与 B 桶评估；`03` 起仍按 manual 处理。

## 5. 检索与重排

### 5.1 Query 改写

- `app/knowledge/rewrite.py`：输入用户原话，输出标准问法。
- 提示词要求只输出 JSON：`{"canonical_query": "..."}`；不得添加事实、不得改变诉求。
- 结果做 LRU 缓存（默认 256 条）；解析失败/超时/上游错误回退原话并记 warning。
- 开关 `KNOWLEDGE_QUERY_REWRITE=true`；关闭时直接用原话。

### 5.2 同义词扩展

- `app/knowledge/synonyms.py` 维护固定词组：邮费/运费/快递费、退款/退钱、退货/退、保修/质保、发票/开票、到账/多久到、包邮/免运费、时效/几天到 等。
- 只把命中的同义词追加到 BM25 查询串；dense 与 reranker 只用规范问法。
- 不在 `knowledge_chunks` 中拆存同义问法。

### 5.3 召回与融合

- dense：`embedding` 字段，Top-50。
- BM25：`bm25_sparse` 字段，`AnnSearchRequest(data=[expanded], anns_field="bm25_sparse", param={"metric_type": "BM25"}, limit=50)`。
- 融合：`hybrid_search(reqs=[dense_req, sparse_req], ranker=RRFRanker(k=60), limit=50, output_fields=[...], consistency_level="Strong")`。
- `category` 过滤：Milvus 表达式 `category == "..."`（转义引号/反斜杠），回表时再校验一次。

### 5.4 重排

- `app/knowledge/rerank.py`：`CrossEncoder` 加载 `bge-reranker-v2-m3`，GPU + fp16 + batch 8；CUDA OOM 回退 CPU并记 warning。
- 打分对 `(canonical_query, section_path + "\n" + questions + "\n" + answer)`；sigmoid 归一后排序，取 Top-10。
- Top-1 分数 < `KNOWLEDGE_RERANK_MIN_SCORE`（默认 0.30）→ 低置信。
- 重排模型缺失/加载失败：`query_faq` 返回 `knowledge_unavailable`，不静默退化为未重排结果。

### 5.5 证据编排

- 按相关性名次 `[1st, 3rd, 5th, ..., 6th, 4th, 2nd]` 重排，使最相关位于首尾。
- 重排后编号 `n=1..N`，`n` 与引用角标一一对应。
- 返回结构：`{"n", "id", "question", "answer", "category", "section_path", "content_type", "scores": {"dense", "bm25", "rrf", "rerank"}}`。

### 5.6 工具契约

- `query_faq(keyword, category=None)`：保留原 `keyword` 字段，新增可选 `category`。
- 成功：`{"ok": true, "data": {"items": [...], "canonical_query": "...", "low_confidence": false}}`。
- 检索闸门：`{"ok": false, "error": "low_confidence", "message": "知识库没有足够依据回答这个问题。"}`。
- 知识库/重排不可用：`{"ok": false, "error": "knowledge_unavailable", ...}`。
- FAQ 工具超时单独使用 `KNOWLEDGE_TOOL_TIMEOUT_SECONDS`（默认 20），其他工具仍用 5 秒。

## 6. 生成控制

### 6.1 引用

- SSE 新增事件 `citations`，在 tool 状态结束后、第二轮 token 前发送：
  `{"items": [{"n", "chunk_id", "question", "answer", "category", "section_path"}]}`。
- Prompt 要求：每个事实句末尾标注 `[n]`；只能使用证据列表中的编号；无依据不得编造。
- 回答入库时保留 `[n]` 原文；引用映射不写入 `messages`（本章页面不重载历史）。

### 6.2 拒答

- 检索闸门失败或自评 `useful=false` 时，服务端直接流式输出固定拒答，不进入第二轮生成：
  「这个问题我在店铺知识库里没有找到可靠依据，先不猜测回答。你可以换个说法，或者告诉我更多信息，我帮你转人工核实。」
- 拒答文本作为 assistant 消息入库；同时写低置信度池；入池失败只记日志，不影响回复。

### 6.3 生成前自评

- 输入：用户原话、规范问法、Top-10 证据（编号 + 文本）。
- 输出 JSON：`{"useful": true|false, "reason": "..."}`。
- 解析失败按 `useful=true` 放行并记日志，避免误拒。
- 开关 `KNOWLEDGE_SELF_CHECK=true`。

### 6.4 System Prompt

新增规则：
- 只依据证据回答，事实句带 `[n]`，不得引用列表外编号。
- 证据不足时明确拒答，不猜测、不硬编。
- 负面知识：不承诺退款到账时间/具体日期；不承诺快递必达时间；不承诺审核一定通过或一定赔付；不编造优惠、库存与价格。
- 保持 ch01–ch03 的安全与工具规则。

### 6.5 低置信度池

表 `low_confidence_questions`（按 DDL）：
- `conversation_id` 可空；`raw_question` 用户原话；`source` 为 `retrieval_low_conf` / `self_check` / `user_feedback`；`reason` 判不能原因；`created_at`。
- 本章写入 `retrieval_low_conf` 与 `self_check`；`user_feedback` 预留。

### 6.6 编造个案台账

表 `faith_cases`（按 DDL）：评估裁判判出不忠实时按 `eval_id` upsert：
- 新题插入，`seen_count=1`，`status="未解决"`，记录 `answer/reason/citations/judge_model/strategy`。
- 已存在则更新最近答案/理由/证据快照与 `last_seen_at`，`seen_count+1`；若原状态为「已解决」或「无需解决」，退回「未解决」并清空 `resolution`，保留 `resolved_at` 作为复发标记。
- `citations` 存本轮喂给模型的 Top-K 证据全集快照。

## 7. 评估体系

### 7.1 评估集

`evals/retrieval_qa.json`，80 题，字段：

```json
{
  "id": "A01",
  "bucket": "A_policy",
  "difficulty": "easy|medium|hard",
  "question": "口语原话",
  "expected_sections": ["配送与运费 › 运费标准 › 普通地区邮费"],
  "answerable": true,
  "note": "可选说明"
}
```

- A_policy：政策事实；B_model：型号/配件/兼容性；C_colloquial：口语与同义说法；E_multi：一个问题涉及两处政策。
- 每桶 20 题，含 easy/medium/hard 梯度；每桶 2–3 题 `answerable=false`。
- 装载时校验 `expected_sections` 能在当前知识库中找到，缺失即报错。

### 7.2 检索指标

- 四策略：`dense`、`bm25`、`hybrid`、`hybrid_rerank`。
- Recall@5 / Recall@10：命中 `expected_sections` 的比例；MRR@10：首个相关结果的倒数排名。
- 不可答题：统计「正确拒答率」（返回低置信/空结果视为正确）。
- 按桶与总体输出；改写结果在同一题的四策略间复用，BM25 使用同义词扩展，dense 只用规范问法。

### 7.3 Faithfulness

- 仅 `hybrid_rerank`，取 12 题（每桶 3 题，含 hard）。
- 生成：同一 System Prompt + 证据列表，非流式调用；要求带 `[n]`。
- 裁判：LLM JSON `{"faithful": bool, "reason": "...", "unsupported": ["..."]}`，逐句核对证据快照。
- 指标：Faithfulness = 忠实题数 / 子集题数；不忠实写 `faith_cases`。
- 报告落 `evals/reports/ch04-<时间>.json` 与 `.md`，含配置、模型名、逐题结果与分桶汇总。

### 7.4 演示

`scripts/demo_retrieval.py --question "..."`：打印该问题在四策略下的 Top-5 与分数，用于验收型号命中；`scripts/demo_tools.py` 增加型号题与不可答题两条。

## 8. 前端

- 流结束后把正文中的 `[n]` 渲染为按钮；点击打开右侧抽屉（手机为底部面板），显示 chunk 原文、章节路径、分类。
- 抽屉可键盘关闭（Esc）、焦点可见；引用编号只对 `citations` 事件中存在的 n 生效。
- 每条客服回答左下角 👍/👎：点击后点亮、显示「已反馈」，同一条只能选一次，状态写 localStorage（`deservehy-feedback:<turn-id>`），刷新后保持。
- 不新增后端接口；不把反馈写库。

## 9. 配置新增

| 变量 | 默认 | 说明 |
|---|---|---|
| `KNOWLEDGE_CANDIDATES` | 50 | 单路召回条数（原 12 改为 50） |
| `KNOWLEDGE_TOP_K` | 10 | 重排后进入提示词的证据数（沿用原变量名，默认值由 5 改为 10） |
| `KNOWLEDGE_RRF_K` | 60 | RRF 常数 |
| `KNOWLEDGE_RERANK_MODEL` | `.cache/models/bge-reranker-v2-m3` | 本地权重或模型名 |
| `KNOWLEDGE_RERANK_DEVICE` | `cuda` | `cuda`/`cpu` |
| `KNOWLEDGE_RERANK_BATCH_SIZE` | 8 | 重排批大小 |
| `KNOWLEDGE_RERANK_MIN_SCORE` | 0.30 | 低置信阈值（sigmoid），固定值；评估显示明显偏差时再调 |
| `KNOWLEDGE_QUERY_REWRITE` | true | 改写开关 |
| `KNOWLEDGE_SELF_CHECK` | true | 自评开关 |
| `KNOWLEDGE_TOOL_TIMEOUT_SECONDS` | 20 | FAQ 工具超时 |
| `KNOWLEDGE_STRATEGY` | `hybrid_rerank` | 线上策略（评估脚本可覆盖） |

## 10. 错误处理

- Milvus/重排模型不可用：`knowledge_unavailable`，页面工具徽章显示失败，回答如实说明。
- 改写失败：回退原话，不阻断检索。
- 自评解析失败：放行生成并记日志。
- 入池失败：记日志，不影响回答。
- 引用编号越界：生成后校验，丢弃越界角标并在日志记录，不伪造来源。
- 低置信拒答固定文案，保证可复现验收。

## 11. 测试与验收

- 单测：改写解析/回退、同义词扩展、RRF 融合顺序、证据首尾编排、引用解析、自评分支、低置信入池、faith_cases upsert 计数/复发、评估指标计算、DDL/ORM 对齐。
- 真机：Milvus BM25 中文召回、hybrid 顺序、category 过滤；重排模型冒烟（无模型时跳过）。
- API：citations 事件、检索闸门拒答、自评拒答、工具徽章不回归。
- 前端：真实浏览器点引用抽屉与反馈锁定；桌面/手机宽度。
- 交付：`python -m scripts.evaluate_retrieval` 报告数字、`python -m scripts.demo_retrieval --question ...`、`python -m scripts.demo_tools`、`pytest -q`、`dev-notes/ch04.md`。

## 12. 风险

- 6GB 显存同时放 BGE-M3 与重排模型：重排 fp16、batch 可调，OOM 回退 CPU。
- 首次下载重排权重约 2.3GB：ModelScope 指引，缺失时服务可用但 FAQ 明确报不可用。
- 80 题评估集与 Faithfulness 调用耗时：检索跑分本地；生成子集控制在 12 题。
- LLM 改写增加 FAQ 延迟：单独 20 秒超时，缓存命中后无额外开销。
