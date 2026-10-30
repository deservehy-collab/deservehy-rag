# deservehy-rag

电商智能客服：FastAPI + LangChain，SSE 流式多轮对话、售后诉求结构化提取、让模型自己决定调用业务工具的 Function Calling 链路，Milvus 原生 BM25 + BGE-M3 混合检索、bge-reranker 重排与引用拒答的知识库，LangGraph 工作流编排（指代消解 + 八类意图分流 + 强制检索闸门 + 退款售后子流程 + 手写 ReAct 主 Agent），以及三层上下文管理（原文 / 规则截短 / 异步梗概 + token 预算 + 多会话侧栏）。

## 功能

- `POST /api/chat`：SSE 流式客服对话。普通问题一次流式返回；需要查数据时，模型先调工具，页面显示工具徽章，再用工具结果收敛回答。
- `POST /api/after-sales/extract`：从售后描述里提取订单号、诉求类型和期望方案。
- 五个业务工具：查订单、查商品、查物流（内置 mock 数据）、查知识库、建工单（写库）。
- 知识库检索：Query 改写 + 检索侧同义词 → dense 与 Milvus 原生 BM25 各取 Top-50 → RRF 融合 → bge-reranker-v2-m3 重排 Top-10 → 证据首尾编排并编号；支持 `category` 元数据过滤。
- 生成控制：回答用 `[n]` 标注来源并随 SSE 下发 `citations`；证据不足时固定拒答；生成前自评不过也拒答，并把原始问题写入 `low_confidence_questions`；System Prompt 明确负面知识（不承诺到账日期、必达时间、审核必过等）。
- 评估：80 题标注集，对比 dense / BM25 / hybrid / hybrid+rerank 四策略的 Recall@5、Recall@10、MRR@10 与不可答题拒答率，并对 12 题做忠实度裁判，编造个案写入 `faith_cases`。
- 知识库：Markdown 文档按标题层级切分入库，FAQ 与会话挖掘问答同库；MySQL 存正文与状态，Milvus 存 dense 向量、BM25 文本与过滤字段，双写可断点续跑。
- 会话挖掘：从最近历史会话抽取可复用问答，精确 + 语义去重后入库；默认关闭，可用脚本或服务内定时器触发。
- 九张表：`conversations`、`messages`、`conversation_summaries`、`faq`、`tickets`、`knowledge_chunks`、`qa_extraction_staging`、`low_confidence_questions`、`faith_cases`。
- 聊天页面：`http://127.0.0.1:8000/`，支持桌面与手机宽度。
- Workflow 编排：LangGraph 图 `coref → intent → route`。先做指代消解（「它能退吗」补成「订单 1001 的商品能退吗」，已经明确的问题原样透传），再按八类意图（物流、订单、商品咨询、退款退货、售后、投诉、其他、闲聊）强制 JSON 输出 `intent` 与 `confidence`，低于置信阈值改判「其他」。知识类强制检索后过置信闸门，业务类直连主 Agent；主 Agent 是手写 ReAct 循环（最多 4 轮工具 + 1 次收敛，token 预算 6000）。
- 退款退货 / 售后子流程：先取订单（缺号或查无都不让模型猜，下发订单卡片让用户点选），再把「这一单能不能退」做 2–3 条问法扩写并强制检索政策，最后把订单与政策证据交给主 Agent 判断。扩写只用于退款退货与售后，且在检索侧现查现用，知识库只保留一份原文。
- 槽位与前端：订单号只从用户消息或演示订单数据里提取；退款原因不追问，提交时从固定 6 类原因里下拉选。订单选择器是聊天流里可点选的卡片，点中后自动回填「订单 1001」续跑；退款单是简单表单，走 `POST /api/refunds` 写 `tickets`（`ticket_type` 为「售后」）。
- 投诉回合不进 Agent、不生成正文：固定安抚话术 + 前端「转人工」「建工单」两个独立按钮；转人工是前端模拟，建工单走 `POST /api/tickets` 写 `tickets` 表，重复点击幂等。（意图分类本身仍会调用一次模型；分类失败按默认意图 `其他` 走业务路由。）
- 状态由 LangGraph checkpointer 持久化（默认 `.cache/langgraph/ch05.sqlite`），跨轮保留轮次与待确认操作；`State.messages` 挂 `add_messages` reducer，节点只吐新消息，完整历史靠 checkpoint 落盘，调模型前另整理裁过带摘要的精简版。
- 三层上下文（只管当前会话）：按离当前轮的远近切三段。层 1 最近几轮留原文一个字不压；层 2 按规则截短（用户原话不动、客服答复只留开头几十字、大块工具结果换成一行标识）；层 3 更早的异步压成一段段梗概。两个锚点 `summary_upto_msg_id`、`layer1_from_msg_id` 只挪 id、不搬数据，梗概单独存 `conversation_summaries`。
- 异步摘要：层 2 攒到超预算就在后台起摘要任务，压成新段追加进摘要表、边界追到层 1 起点，不阻塞当前回复。一段段攒、压完不回头重写，旧梗概只作背景不参与合并；一批最多 20 条，积压超过一批时从最老的一端往后分批，**锚点只推到本批真正喂进模型的最后一行**，绝不越过摘要器没看过的消息。触发只看 token 用量，不数条数；摘要失败或超时不推进锚点，下一轮继续触发。
- 调模型的固定顺序：system 人设 / 红线 / 工具定义打头且每轮一样（吃到前缀缓存）→ 层 2 截短对话 → 最近几轮原文 → 当前用户这句话 → 早期梗概与检索证据合成一条尾随块挂在那句之后。梗概不单独占一条 system，否则上游把 system 上提会让前缀缓存作废。
- token 预算从窗口倒推：扣掉系统提示、检索证据、注入梗概、输出预留、安全余量、当前轮 ReAct 瞬时峰值，剩下归历史；历史预算取「想留轮数 × 每轮稳态占用」与「窗口匀得出的」较小者，再按层 1 七成 / 层 2 三成分配。启动自检，连一轮都装不下就打报警日志；中文按 1 字 ≈ 1 token、其他 3.5 字符 ≈ 1 token 折算，与预算同一口径。
- 上下文可观测：每轮调模型前把实际上下文原样写进 `log/app.log`——主力 Agent 打 `model_ctx`（含 `messages=` 完整载荷、`window=`、`layer2=`/`layer1=`/`summary_tokens=`/`total_tokens=`），指代消解与意图共用的历史打 `history_ctx`（每轮必打，闲聊兜底轮也可见）；后台摘要的 trigger / start / done / skip / failed 全留痕，带覆盖边界与耗时。
- 多会话侧栏：聊天页左侧列该用户全部会话（新在前、首问预览、`已摘要` 或消息条数），点击切换并回载历史原文继续聊；「新对话」只开新会话，不动历史。手机宽度改成抽屉，加载失败静默降级、不影响聊天。数据来自两个只读接口 `GET /api/conversations`、`GET /api/conversations/{id}/messages`（按 `client_id` 鉴权，别人的会话一律 403）。

ch06 不做：不训练意图分类模型（用 prompt + 强制 JSON + few-shot）、不做跨会话长期记忆，也不做多级意图降级链路；不做 MCP 接入与飞轮回灌。

ch07 不做：只用同会话内的三层结构，不做语义检索捞历史、不按主题重要度留关键事实；不跨会话、不存用户画像。

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
| `WORKFLOW_INTENT_TIMEOUT_SECONDS` | 意图分类单次调用超时，超时按默认意图 `其他` 兜底，默认 10 秒 |
| `WORKFLOW_INTENT_MIN_CONFIDENCE` | 意图置信度低于该值改判「其他」，默认 0.6 |
| `WORKFLOW_UNDERSTANDING_ENABLED` | 是否启用指代消解，默认开启；失败或超时退回原问题 |
| `WORKFLOW_UNDERSTANDING_TIMEOUT_SECONDS` | 指代消解与问法扩写的单次调用超时，默认 10 秒 |
| `WORKFLOW_QUERY_EXPANSION` | 是否在退款退货 / 售后子流程做问法扩写，默认开启 |
| `WORKFLOW_QUERY_EXPANSION_MAX` | 每次扩写最多几条问法，默认 3（范围 2–3） |
| `MODEL_CONTEXT_WINDOW` | 模型窗口，默认 32768（兼容旧名 `CONTEXT_WINDOW_TOKENS`） |
| `MAX_OUTPUT_TOKENS` | 输出预留，默认 1024 |
| `TOKEN_SAFETY_MARGIN` | 安全余量，默认 316（与 System Prompt 实测 1234 合计 1550，按演示配置校准） |
| `MAX_USER_INPUT_TOKENS` | 当前问题上限，默认 2000；超了直接 422，不启动 SSE |
| `TOOL_RESULT_MAX_TOKENS` | 单次工具结果峰值预留，默认 1200 |
| `MAX_AGENT_STEPS` | ReAct 步数（预算按步数 × 工具结果峰值），默认 4（兼容 `WORKFLOW_AGENT_MAX_STEPS`） |
| `RERANK_TOP_K` | 证据条数，预算按 400/条，默认 10（兼容 `KNOWLEDGE_TOP_K`） |
| `HISTORY_KEEP_ROUNDS` | 想留的历史轮数，默认 20 |
| `HISTORY_ROUND_TOKENS` | 每轮稳态占用，默认 600（范围 100–4000） |
| `CONTEXT_SUMMARY_ENABLED` | 后台摘要开关，默认开启；关闭后层 2 超预算只做层 1 降级 |
| `SUMMARY_TIMEOUT_SECONDS` | 单次摘要调用超时，默认 20 秒；超时不推进锚点 |
| `LOG_FILE` | 上下文日志路径，默认 `log/app.log`（自动建目录）；填 `:memory:` 关闭落盘 |

预算不是配出来的一个数，是从窗口倒推：`历史预算 = min(HISTORY_KEEP_ROUNDS × HISTORY_ROUND_TOKENS, 窗口 − 输出 − 余量 − 系统 − 证据 − 梗概 − ReAct 峰值)`，再按层 1 七成 / 层 2 三成分配。演示配置（18000 / 2000 / 2000 / 3 步 / 1200 / Top-5）实算得 **历史 5650、层 1 3954、层 2 1695**；默认 32768 得 12000 / 8399 / 3600。

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

### 数据迁移（ch07）

新库直接 `python -m scripts.init_db`，ORM 里已带 `conversation_summaries` 表与 `conversations` 的两个锚点列。已有库要单独跑一次迁移：

```bat
docker compose exec -T mysql mysql -uroot -p%DB_PASSWORD% xiaolin_ch02 < scripts\init_sql\005_context.sql
```

这条只跑一次：`ALTER TABLE` 不加 `IF NOT EXISTS`，重复执行报 `duplicate column name`，那说明库已经升过，可以忽略。锚点列允许 NULL，老会话不用回填——第一次降级或摘要时按当前边界写进去。

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
```

`demo_workflow` 的 5 句分别是：知识类（强制检索 + 角标）、物流（Agent 自调工具）、投诉（两个操作按钮）、闲聊（固定话术，不生成正文）、多步（连查订单与物流）。

分流器、指代消解与退款子流程（ch06）：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo_router        :: 三轮：这个能退吗 → 订单 1001 → 那物流呢，看指代补全与子流程事件
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_intent    :: 47 题八类意图标注集，报告写 evals/reports/ch06-intent-<时间戳>.md
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_router    :: 4 个多轮场景共 11 轮，意图与指代片段双门槛 1.00
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_expansion :: 5 题问法扩写，检查 2–3 条与关键词覆盖，门槛 0.90
```

三个评估都需要真实上游，报告写 `evals/reports/ch06-*-<时间戳>.md`，不达标以非零码退出。

上下文管理（ch07）：

```bat
:: 演示配置：窗口 18000，28 轮同一会话，看三层级联 + 后台摘要 + 靠梗概答题
set MODEL_CONTEXT_WINDOW=18000&& set MAX_OUTPUT_TOKENS=2000&& set MAX_USER_INPUT_TOKENS=2000&& set MAX_AGENT_STEPS=3&& set TOOL_RESULT_MAX_TOKENS=1200&& set RERANK_TOP_K=5
.venv\Scripts\python.exe -X utf8 -m scripts.demo_context

:: 默认窗口 32768 跑 20 轮：不该出现降级与摘要
.venv\Scripts\python.exe -X utf8 -m scripts.demo_context --default-window --turns 20

:: 只对已有会话追问末问（梗概已落库时补验「靠梗概答题」，省上游额度）
.venv\Scripts\python.exe -X utf8 -m scripts.demo_context --only-final 89

:: 摘要评估集（5 例，门槛 0.90，需真实上游）
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate_summary
```

`demo_context` 每轮打印路由、层 2 / 层 1 / 滑窗条数、梗概与总 token、两个锚点 id，跑完把本轮新增的 `layer1 降级` / `summary trigger` / `summary done` 日志原文列出来，并断言末问「最开始那个订单后来怎么说？」靠梗概作答：梗概正文里必须出现 `1001`、分段首尾相接无空洞、首问那一行的 id 已落在 `summary_upto` 之前，演示模式还要求答出「退」；不满足以非零码退出。环境变量里已经给出的键优先，脚本不覆盖。

日志抽查（PowerShell / Git Bash）：

```bash
grep -c "history_ctx" log/app.log   # 每轮至少一条，含闲聊兜底轮
grep -c "model_ctx" log/app.log     # 主力 Agent 每调模型一次一条
grep "summary trigger\|summary done\|layer1 降级" log/app.log | tail -20
```

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

ch06 分流器（模型 deepseek-flash，2026-10-30）：

| 评估 | 样本 | 结果 | 门槛 |
|---|---|---|---|
| 八类意图 | 47 题 | 准确率 1.000，平均置信度 0.954，低于 0.60 的样本 0 | 0.90 |
| 多轮指代 + 意图 | 4 场景 11 轮 | 意图 1.000，指代片段命中 1.000 | 1.00 |
| 问法扩写 | 5 题 | 2–3 条合规率 / 关键词覆盖 1.000 | 0.90 |

- 意图评估里快速通道（确定性寒暄正则）命中 9 题，其余 38 题走模型；八类各自准确率均为 1.000，没有错例。
- 多轮场景覆盖「物流 → 退款 → 物流」：第三轮「那快递呢」只补回订单号，不把上一轮的退货主题拼进来。
- 扩写只对退款退货 / 售后生效；要求正好 2–3 条，超过 3 条会解析失败并回退原问题检索（宁可少扩，不错扩）。

ch07 上下文管理（模型 deepseek-flash，2026-10-30）：

演示配置 `MODEL_CONTEXT_WINDOW=18000 MAX_OUTPUT_TOKENS=2000 MAX_USER_INPUT_TOKENS=2000 MAX_AGENT_STEPS=3 TOOL_RESULT_MAX_TOKENS=1200 RERANK_TOP_K=5` → 历史预算 5650 / 层 1 3954 / 层 2 1695；同一会话（conv=89）连聊 28 轮：

| 项 | 实测 |
|---|---|
| 层 1 滑窗降级 | 6 次，锚点单向往前挪：576→578→584→592→604→610→612 |
| 层 2 丢弃 | 0 次（降级总在预算耗尽前先把最老一轮挤出层 1） |
| 后台摘要 | `summary trigger` 2 次；`summary done` 3 段 seq=1（0→603，361 字，2.26s）、seq=2（603→610，211 字，3.62s）、seq=3（610→612，165 字，1.36s）；积压 603 行按每批 ≤20 条从最老一端分开压，锚点只推到已喂进模型的最后一行 |
| 送模型峰值 | 6191 tok（窗口 18000），全程无 error 帧 |
| 靠梗概答题 | 末问「最开始那个订单后来怎么说？」答出订单 1001 的退货退款：`summary_upto=612` 已盖过首问行 576，`summary_tokens=674`，送模型 6176 tok，最近几轮里已看不到这条线索 |
| 默认窗口对照 | 窗口 32768（conv=88）连聊 22 轮：降级 0 次、trigger 0 次，峰值 7685 tok，末问仍认得 1001 |
| 摘要质量 | 评估集 5 例，准确率 1.000（门槛 0.90），逐例见 `evals/reports/ch07-summary-20261030-135330.md` |

- 日志留痕：`history_ctx` 每轮必打（conv=89 28/28、conv=88 22/22）；`model_ctx` 在真正调生成模型的轮次打（分别 20、19 次），其余轮走固定话术或订单点选 offer，本来就不调模型。
- 历史增速实测约 180 token/轮（寒暄与拒答轮几乎不涨），所以演示把问题换成必然多要点的复合问法，28 轮才压得出级联；默认窗口下同样的 22 轮离降级线还很远。
- 上一版这里写的是 conv=87 / seq=1 一段 307 字「靠梗概答题成立」，代码评审推翻：那批只把 20 条喂给摘要器却把锚点推到整段末尾，最早那段历史同时离开三层，seq=1 正文里根本没有订单 1001。分批与锚点收敛后重跑，`seq=1` 正文以「用户反馈订单1001保温杯漏水，要求退货退款」开头，末问靠梗概答题才是实测而非推断。
- 回归：`pytest` 313 passed。

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
{"message": "订单 1001 的物流到哪了", "client_id": "web-xxxxxxxx"}
```

`client_id` 为 8–64 位字母、数字、下划线或连字符；不带 `conversation_id` 就开一个新会话，`meta` 帧回它的 id，接着聊把它带回去即可。历史由服务端从 `conversations` / `messages` 取并按三层裁剪，`history` 字段已废弃（保留兼容、不再参与上下文）。

两个只读接口给侧栏用：`GET /api/conversations?client_id=...` 回 `{items: [{conversation_id, first_question, updated_at, summarized, message_count}]}`（新在前）；`GET /api/conversations/{conversation_id}/messages?client_id=...` 回该会话的原文消息供回载。`client_id` 非法 422，会话不属于你或不存在一律 403（不拿状态码泄露别人的会话是否存在），数据库异常 503。

事件顺序：

- `meta`：新会话号；
- `status`：工具开始/结束（`phase`、`tool`、`label`，结束帧带 `ok`、`summary`、`duration_ms`）；
- `citations`：`query_faq` 成功后、第二轮正文前下发，`items` 含 `n`、`chunk_id`、`question`、`answer`、`category`、`section_path`；
- `token`：正文增量；
- `actions`：待用户决定的操作（投诉回合的 `{"items":[{"id":"human","label":"转人工"},{"id":"ticket","label":"建工单"}],"turn":1}`；退款回合额外带 `refund`：`{"order_id":"1001","reasons":["商品破损或质量问题", "..."]}`）；按钮只在当轮消息下生效；
- `orders`：退款子流程拿不到订单号或查无订单时下发 `{"items":[{"order_id":"1001","status":"...","amount":...,"created_at":"...","items":[...]}],"reason":"missing_order"}`，前端渲染成可点选卡片；
- `done`：`finish_reason`；
- `error`：`code`、`message`（例如 `database_unavailable`）。

检索低置信或检索异常时不下发 `citations`，直接返回固定拒答正文并以 `done` 结束。`query_faq` 成功后会先下发 `citations` 再做证据自评（规格 §4.6），因此自评不通过的拒答仍带着已发出的来源；前端只在正文出现 `[n]` 时渲染来源，不影响阅读。

`POST /api/tickets`，请求体 `{"conversation_id": 12, "client_id": "web-xxxxxxxx", "turn": 1}`：只能对当轮投诉 `offer` 建单（`turn` 来自 `actions` 帧，原样带回）；他人会话 403，offer 失效或轮次不符 409，建单失败 502，重复点击直接返回同一工单号。成功后写 `tickets` 表，`ticket_type` 为 `投诉`，描述以 `用户投诉：` 开头。

`POST /api/refunds`，请求体 `{"conversation_id": 12, "client_id": "web-xxxxxxxx", "turn": 2, "reason": "商品破损或质量问题", "note": "可选，最多 200 字"}`：同样只对当轮退款 `offer` 生效。`reason` 必须是固定 6 类原因之一（否则 422），他人会话 403，offer 失效或轮次不符 409，建单失败 502；`offer.refund_no` 与进程内缓存双保险，重复提交返回同一单号。成功后写 `tickets` 表，`ticket_type` 为 `售后`，描述以 `退款申请：订单 <号>｜原因：<原因>` 开头。

## 目录

```
app/          配置、模型、提示词、SSE 服务、工具、存储、ORM 模型、知识库模块、workflow 图与主 Agent 循环、网页
scripts/      初始化数据库、知识导入/挖掘/演示、评估、初始化 SQL
examples/     知识库示例文档
evals/        80 题检索标注集、ch06 意图/多轮/扩写标注集与评估报告
tests/        SQLite 自动测试 + MySQL/Milvus 集成检查（不可用时跳过）
dev-notes/    分阶段开发留痕
docs/         规格与实施计划
```
