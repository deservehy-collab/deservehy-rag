# deservehy-rag

电商智能客服：FastAPI + LangChain，SSE 流式多轮对话、售后诉求结构化提取、让模型自己决定调用业务工具的 Function Calling 链路，以及 BGE-M3 + Milvus 的向量知识库。

## 功能

- `POST /api/chat`：SSE 流式客服对话。普通问题一次流式返回；需要查数据时，模型先调工具，页面显示工具徽章，再用工具结果收敛回答。
- `POST /api/after-sales/extract`：从售后描述里提取订单号、诉求类型和期望方案。
- 五个业务工具：查订单、查商品、查物流（内置 mock 数据）、查知识库（Milvus 语义检索）、建工单（写库）。
- 知识库：Markdown 文档按标题层级切分入库，FAQ 与会话挖掘问答同库；MySQL 存正文与状态，Milvus 存向量，双写可断点续跑。
- 会话挖掘：从最近历史会话抽取可复用问答，精确 + 语义去重后入库；默认关闭，可用脚本或服务内定时器触发。
- 六张表：`conversations`、`messages`（含 tool 角色与工具调用记录）、`faq`、`tickets`、`knowledge_chunks`、`qa_extraction_staging`。
- 聊天页面：`http://127.0.0.1:8000/`，支持桌面与手机宽度。

本章仍是单轮工具调用（一次工具结果后收敛），不做 Agent 循环、关键词召回、混合检索与 rerank。

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
| `KNOWLEDGE_MIN_SCORE` | 检索相似度下限，默认 0.35；过低会引入无关内容，过高会召回变少 |
| `KNOWLEDGE_MINING_ENABLED` | 是否在服务内启动会话挖掘定时器，默认关闭 |

其余检索条数、批次大小、挖掘间隔、去重阈值都有默认值。

### 准备嵌入模型

权重约 2.3GB。能访问 HuggingFace 时可留空让 sentence-transformers 自动下载；国内网络建议用 ModelScope：

```bat
.venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ modelscope
.venv\Scripts\modelscope download --model BAAI/bge-m3 --local_dir .cache\models\bge-m3
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

依次问三句并检查工具调用：

- 「订单 1001 的物流到哪了？」→ 调 `query_logistics`，回答里有顺丰运单和上海分拨中心节点；
- 「退货政策是什么？」→ 调 `query_faq`，命中退换货文档；
- 「邮费是多少？」→ 调 `query_faq`，向量检索命中配送与运费文档，回答给出邮费规则。

知识库单独验收与维护：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo_knowledge            :: 邮费问题召回检查 + 待向量化统计
.venv\Scripts\python.exe -X utf8 -m scripts.ingest_knowledge           :: 幂等导入文档与 FAQ，补齐 pending 向量
.venv\Scripts\python.exe -X utf8 -m scripts.ingest_knowledge --rebuild --yes
.venv\Scripts\python.exe -X utf8 -m scripts.mine_knowledge --limit 50  :: 从历史会话挖掘问答
```

其他命令：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo       :: ch01 两轮对话 + 售后提取
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate   :: 标注集评估（需真实上游）
.venv\Scripts\python.exe -X utf8 -m pytest -q          :: 自动测试（或双击 test.bat）
```

## 知识库说明

- 文档放 `examples/knowledge/*.md`：`00`–`02` 按 policy 处理，`03` 起按 manual 处理；每个分块记录父级标题路径（`section_path`）与所属分类。
- 切分上限 500 字、目标 400 字、重叠 80 字；重叠只在句子边界截断，长表格按行拆分并重复表头。
- 写入顺序：MySQL `pending` → Milvus → 回填 `vector_id` 与 `done`；中途失败重跑只补没完成的。
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
- `token`：正文增量；
- `status`：工具开始/结束（`phase`、`tool`、`label`，结束帧带 `ok`、`summary`、`duration_ms`）；
- `done`：`finish_reason`；
- `error`：`code`、`message`（例如 `database_unavailable`）。

ch03 未改动 SSE 协议与页面交互。

## 目录

```
app/          配置、模型、提示词、SSE 服务、工具、存储、ORM 模型、知识库模块、网页
scripts/      初始化数据库、知识导入/挖掘/演示、评估、初始化 SQL
examples/     知识库示例文档
tests/        SQLite 自动测试 + MySQL/Milvus 集成检查（不可用时跳过）
dev-notes/    分阶段开发留痕
docs/         规格与实施计划
```
