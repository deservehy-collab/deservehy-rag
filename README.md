# deservehy-rag

电商智能客服：FastAPI + LangChain，SSE 流式多轮对话、售后诉求结构化提取、让模型自己决定调用业务工具的 Function Calling 链路，Milvus 原生 BM25 + BGE-M3 混合检索、bge-reranker 重排与引用拒答的知识库，以及 LangGraph 工作流编排（意图分流 + 强制检索闸门 + 手写 ReAct 主 Agent）。

## 功能

- `POST /api/chat`：SSE 流式客服对话。普通问题一次流式返回；需要查数据时，模型先调工具，页面显示工具徽章，再用工具结果收敛回答。
- `POST /api/after-sales/extract`：从售后描述里提取订单号、诉求类型和期望方案。
- 五个业务工具：查订单、查商品、查物流（内置 mock 数据）、查知识库、建工单（写库）。
- 知识库检索：Query 改写 + 检索侧同义词 → dense 与 Milvus 原生 BM25 各取 Top-50 → RRF 融合 → bge-reranker-v2-m3 重排 Top-10 → 证据首尾编排并编号；支持 `category` 元数据过滤。
- 生成控制：回答用 `[n]` 标注来源并随 SSE 下发 `citations`；证据不足时固定拒答；生成前自评不过也拒答，并把原始问题写入 `low_confidence_questions`；System Prompt 明确负面知识（不承诺到账日期、必达时间、审核必过等）。
- 评估：80 题标注集，对比 dense / BM25 / hybrid / hybrid+rerank 四策略的 Recall@5、Recall@10、MRR@10 与不可答题拒答率，并对 12 题做忠实度裁判，编造个案写入 `faith_cases`。
- 知识库：Markdown 文档按标题层级切分入库，FAQ 与会话挖掘问答同库；MySQL 存正文与状态，Milvus 存 dense 向量、BM25 文本与过滤字段，双写可断点续跑。
- 会话挖掘：从最近历史会话抽取可复用问答，精确 + 语义去重后入库；默认关闭，可用脚本或服务内定时器触发。
- 八张表：`conversations`、`messages`、`faq`、`tickets`、`knowledge_chunks`、`qa_extraction_staging`、`low_confidence_questions`、`faith_cases`。
- 聊天页面：`http://127.0.0.1:8000/`，支持桌面与手机宽度。
- Workflow 编排：LangGraph 图负责意图分流（七意图 → 知识/业务/投诉/闲聊四出口），知识类强制检索后过置信闸门，业务类直连主 Agent；主 Agent 是手写 ReAct 循环（最多 4 轮工具 + 1 次收敛，token 预算 6000）。
- 投诉回合不进 Agent、不生成正文：固定安抚话术 + 前端「转人工」「建工单」两个独立按钮；转人工是前端模拟，建工单走 `POST /api/tickets` 写 `tickets` 表，重复点击幂等。（意图分类本身仍会调用一次模型；分类失败按默认意图 `售后` 走业务路由。）
- 状态由 LangGraph checkpointer 持久化（默认 `.cache/langgraph/ch05.sqlite`），跨轮保留轮次与待确认操作。

不做正式意图识别与指代消解的工程化（coref 直通、指代靠模型在 prompt 里自己看历史）；不做 MCP 接入与飞轮回灌。

## 环境

- Python 3.12
- Docker Desktop：MySQL 8.0（`127.0.0.1:3307`）、Milvus 2.6 standalone（`127.0.0.1:19530`，含 etcd 与 MinIO 容器）
- 可选 NVIDIA GPU：`EMBEDDING_DEVICE=cuda`；不可用时自动回退 CPU。GPU 版 torch 需从 cu128 wheel 源安装，`start.bat` 已带 `-f https://mirrors.aliyun.com/pytorch-wheels/cu128/`

## 配置

复制 `.env.example` 为 `.env`，填写：

| 变量 | 说明 |
| --- | --- |
| `OPENAI_BASE_URL` | 上游 OpenAI 兼容地址 |
| `OPENAI_MODEL` | 模型名 |
| `OPENAI_API_KEY` | 密钥（不要提交到 git） |
| `DB_PASSWORD` | MySQL root 密码，同时供 Docker 与服务使用 |
| `EMBEDDING_MODEL` | 默认 `.cache/models/bge-m3`（本地权重）；也可填 `BAAI/bge-m3` 交给 sentence-transformers 下载 |
| `EMBEDDING_DEVICE` | `cuda` 或 `cpu` |
| `MILVUS_URI` | 默认 `http://127.0.0.1:19530` |
| `KNOWLEDGE_RERANK_MODEL` | 重排权重路径，默认 `.cache/models/bge-reranker-v2-m3` |
| `KNOWLEDGE_RERANK_MIN_SCORE` | 重排 top1 低于该值判低置信并拒答，默认 0.30 |
| `KNOWLEDGE_CANDIDATES` / `KNOWLEDGE_TOP_K` / `KNOWLEDGE_RRF_K` | 双路召回条数、重排后保留条数、RRF 常数，默认 50 / 10 / 60 |
| `KNOWLEDGE_QUERY_REWRITE` | 是否用 LLM 改写口语问题，默认开启；失败自动回退原话 |
| `KNOWLEDGE_SELF_CHECK` | 生成前证据自评，默认开启；自评失败按可用放行 |
| `KNOWLEDGE_TOOL_TIMEOUT_SECONDS` | `query_faq` 专属超时，默认 20 秒 |
| `KNOWLEDGE_MINING_ENABLED` | 是否在服务内启动会话挖掘定时器，默认关闭 |
| `WORKFLOW_CHECKPOINT_PATH` | LangGraph 检查点路径，默认 `.cache/langgraph/ch05.sqlite`；填 `:memory:` 则只在进程内保存 |
| `WORKFLOW_AGENT_MAX_STEPS` | 主 Agent 最多几轮生成（含工具往返），默认 4 |
| `WORKFLOW_AGENT_TOKEN_BUDGET` | 单轮 token 预算，超预算提前收敛，默认 6000 |
| `WORKFLOW_INTENT_TIMEOUT_SECONDS` | 意图分类单次调用超时，超时按默认意图 `售后` 兜底，默认 10 秒 |

其余检索条数、批次大小、挖掘间隔、去重阈值都有默认值。

### 准备嵌入与重排模型

两份权重各约 2.3GB。能访问 HuggingFace 时可留空让 sentence-transformers 自动下载；国内网络建议用 ModelScope：

```bat
.venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ modelscope
.venv\Scripts\modelscope download --model BAAI/bge-m3 --local_dir .cache\models\bge-m3
.venv\Scripts\modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir .cache\models\bge-reranker-v2-m3
```

## 启动

双击 `start.bat`：建虚拟环境、装依赖、`docker compose up -d`、初始化数据库、导入知识库，然后启动服务。

手动等价命令：

```bat
docker compose up -d
.venv\Scripts\python.exe -X utf8 -m scripts.init_db
.venv\Scripts\python.exe -X utf8 -m scripts.ingest_knowledge
.venv\Scripts\python.exe -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`scripts.init_db` 与 `scripts.ingest_knowledge` 均可重复执行：前者建库建表补 FAQ 种子，后者按内容幂等导入并在必要时补向量。

## 演示与验收

服务启动后，另开终端：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo_tools
```

依次问五句并检查工具调用：

- 「订单 1001 的物流到哪了？」→ 调 `query_logistics`，回答里有顺丰运单和上海分拨中心节点；
- 「退货政策是什么？」→ 调 `query_faq`，命中退换货文档；
- 「邮费是多少？」→ 调 `query_faq`，混合检索命中配送与运费文档；
- 「G500 杯盖能装在 B750 上吗？」→ 调 `query_faq`，BM25 命中型号条款，回答含 G500 与 B750；
- 「你们支持货到付款吗？」→ 调 `query_faq` 失败，回答为固定拒答文案。

知识库单独验收与维护：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo_knowledge            :: 邮费问题召回检查 + 待向量化统计
.venv\Scripts\python.exe -X utf8 -m scripts.demo_retrieval --question "G500 杯盖能装在 B750 上吗？"
.venv\Scripts\python.exe -X utf8 -m scripts.ingest_knowledge           :: 幂等导入文档与 FAQ，补齐 pending 向量
.venv\Scripts\python.exe -X utf8 -m scripts.ingest_knowledge --rebuild --yes
.venv\Scripts\python.exe -X utf8 -m scripts.mine_knowledge --limit 50  :: 从历史会话挖掘问答
```

四策略评估（需真实上游与重排权重）：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_retrieval
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_retrieval --no-faith   :: 只跑检索指标
```

报告写到 `evals/reports/ch04-<时间戳>.json/.md`，含总表、分桶与逐题明细。

工作流与主 Agent（ch05）：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo_agent_loop    :: 不用框架的手写 ReAct 循环（祛魅对照）
.venv\Scripts\python.exe -X utf8 -m scripts.demo_workflow      :: 5 条验收场景，打印 SSE 事件；节点轨迹看日志里的 workflow node=
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_intent    :: 43 题意图标注集，报告写 evals/reports/ch05-intent-<时间戳>.md
```

`demo_workflow` 的 5 句分别是：知识类（强制检索 + 角标）、物流（Agent 自调工具）、投诉（两个操作按钮）、闲聊（不调模型）、多步（连查订单与物流）。

其他命令：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo       :: ch01 两轮对话 + 售后提取
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate   :: 标注集评估（需真实上游）
.venv\Scripts\python.exe -X utf8 -m pytest -q          :: 自动测试（或双击 test.bat）
```

## 评估结果

80 题（A 政策 20、B 型号 20、C 口语 20、E 多跳 20），其中 9 题不可答；`hybrid_rerank` 对 12 题做忠实度抽检。

| 策略 | Recall@5 | Recall@10 | MRR@10 | 不可答题正确拒答率 |
|---|---|---|---|---|
| dense | 0.972 | 0.972 | 0.935 | 0.000 |
| bm25 | 0.889 | 0.889 | 0.830 | 0.000 |
| hybrid | 0.965 | 0.965 | 0.905 | 0.000 |
| hybrid_rerank | 0.993 | 0.993 | 0.937 | 1.000 |

- 忠实度：11/11 被判为忠实；C11「快递卡住不动了」在检索闸门被判低置信（top1=0.282，低于 0.30），未进入生成，因此没有可裁判的回答。
- 前三行的拒答率 0.000 是因为 dense / bm25 / hybrid 不跑重排闸门（没有拒答机制），不是拒答失误；只有 `hybrid_rerank` 具备闸门。
- 已知风险：0.30 阈值偏保守，C11 这类口语问法可能被误拒；9 题不可答全部正确拒答，说明当前阈值在“宁可拒答”一侧。调阈值前先补测更多口语样本，不要只对这份评估集调参。
- 分桶看，BM25 在 C 口语与 E 多跳上明显弱于 dense，hybrid+rerank 把 C 口语 Recall@5 从 0.889 拉回 1.000，型号题（B）由 BM25 补足精确匹配。
- 详细逐题明细与分桶数字见 `evals/reports/ch04-<时间戳>.json`。报告不含密钥；生成时间 2026-10-29 16:09。

## 低置信池与编造台账

- `low_confidence_questions`：检索闸门（`retrieval_low_conf`）或生成前自评（`self_check`）拒绝时写入原始问题、会话、来源、原因与时间，供后续补文档或补 FAQ。
- `faith_cases`：忠实度裁判判为编造的个案，按 `eval_id` 幂等更新，复发 `seen_count++` 并把状态改回 `未解决`；核实修正后用 `FaithCaseLedger.resolve` 标记已解决。
- 查看 SQL：

```sql
SELECT id, conversation_id, raw_question, source, reason, created_at
FROM low_confidence_questions ORDER BY id DESC LIMIT 20;

SELECT eval_id, bucket, query, status, seen_count, reason, last_seen_at
FROM faith_cases ORDER BY last_seen_at DESC;
```

## 知识库说明

- 文档放 `examples/knowledge/*.md`：`00`–`02` 按 policy 处理，`03` 起按 manual 处理；每个分块记录父级标题路径（`section_path`）与所属分类。
- 切分上限 500 字、目标 400 字、重叠 80 字；重叠只在句子边界截断，长表格按行拆分并重复表头。
- 写入顺序：MySQL `pending` → Milvus → 回填 `vector_id` 与 `done`；中途失败重跑只补没完成的。
- 集合 schema 升级为 dense + BM25 稀疏字段后，旧集合必须先重建：`python -m scripts.ingest_knowledge --rebuild --yes`。该命令只影响知识库集合与分块，不碰会话表。
- `scripts/mine_knowledge` 抽出的问答先进 `qa_extraction_staging`，精确 + 语义去重后入库；旧的「查不到」类回答需要人工复核，确认过时后在 staging 里置 `discarded` 并删除对应分块。
- 服务内定时挖掘只在单 worker 下开启，避免多进程重复抽取；APScheduler 是进程内调度，服务重启后按间隔重新计时。

## SSE 协议

`POST /api/chat`，请求体：

```json
{"message": "订单 1001 的物流到哪了", "history": [], "client_id": "web-xxxxxxxx"}
```

`history` 是完整的 user/assistant 交替问答；`client_id` 为 8–64 位字母数字下划线连字符；续接会话时再带 `conversation_id`。

事件顺序：

- `meta`：新会话号；
- `status`：工具开始/结束（`phase`、`tool`、`label`，结束帧带 `ok`、`summary`、`duration_ms`）；
- `citations`：`query_faq` 成功后、第二轮正文前下发，`items` 含 `n`、`chunk_id`、`question`、`answer`、`category`、`section_path`；
- `token`：正文增量；
- `actions`：待用户决定的操作（投诉回合的 `{"items":[{"id":"human","label":"转人工"},{"id":"ticket","label":"建工单"}],"turn":1}`）；按钮只在当轮消息下生效；
- `done`：`finish_reason`；
- `error`：`code`、`message`（例如 `database_unavailable`）。

检索低置信或检索异常时不下发 `citations`，直接返回固定拒答正文并以 `done` 结束。`query_faq` 成功后会先下发 `citations` 再做证据自评（规格 §4.6），因此自评不通过的拒答仍带着已发出的来源；前端只在正文出现 `[n]` 时渲染来源，不影响阅读。

`POST /api/tickets`，请求体 `{"conversation_id": 12, "client_id": "web-xxxxxxxx", "turn": 1}`：只能对当轮投诉 `offer` 建单（`turn` 来自 `actions` 帧，原样带回）；他人会话 403，offer 失效或轮次不符 409，建单失败 502，重复点击直接返回同一工单号。成功后写 `tickets` 表，`ticket_type` 为 `投诉`，描述以 `用户投诉：` 开头。

## 目录

```
app/          配置、模型、提示词、SSE 服务、工具、存储、ORM 模型、知识库模块、workflow 图与主 Agent 循环、网页
scripts/      初始化数据库、知识导入/挖掘/演示、评估、初始化 SQL
examples/     知识库示例文档
evals/        80 题标注集与四策略评估报告
tests/        SQLite 自动测试 + MySQL/Milvus 集成检查（不可用时跳过）
dev-notes/    分阶段开发留痕
docs/         规格与实施计划
```
