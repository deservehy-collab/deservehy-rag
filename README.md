# deservehy-rag

电商智能客服：FastAPI + LangChain，SSE 流式多轮对话、售后诉求结构化提取，以及让模型自己决定调用业务工具的 Function Calling 链路。

## 功能

- `POST /api/chat`：SSE 流式客服对话。普通问题一次流式返回；需要查数据时，模型先调工具，页面显示工具徽章，再用工具结果收敛回答。
- `POST /api/after-sales/extract`：从售后描述里提取订单号、诉求类型和期望方案。
- 五个业务工具：查订单、查商品、查物流（内置 mock 数据）、查 FAQ（MySQL 关键词检索）、建工单（写库）。
- 四张表：`conversations`、`messages`（含 tool 角色与工具调用记录）、`faq`、`tickets`。
- 聊天页面：`http://127.0.0.1:8000/`，支持桌面与手机宽度。

本章只做单轮工具调用（一次工具结果后收敛），不做 Agent 循环与向量检索。

## 环境

- Python 3.12
- Docker Desktop（MySQL 8.0，映射到 `127.0.0.1:3307`）

## 配置

复制 `.env.example` 为 `.env`，填写：

| 变量 | 说明 |
| --- | --- |
| `OPENAI_BASE_URL` | 上游 OpenAI 兼容地址 |
| `OPENAI_MODEL` | 模型名 |
| `OPENAI_API_KEY` | 密钥（不要提交到 git） |
| `DB_PASSWORD` | MySQL root 密码，同时供 Docker 与服务使用 |

其余上下文预算、工具超时/重试参数有默认值，可按需改。

## 启动

双击 `start.bat`：建虚拟环境、装依赖、`docker compose up -d`、初始化数据库，然后启动服务。

手动等价命令：

```bat
docker compose up -d
.venv\Scripts\python.exe -X utf8 -m scripts.init_db
.venv\Scripts\python.exe -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`scripts.init_db` 可重复执行：建库、建表、补齐 FAQ 种子（不覆盖已有 FAQ）。首次启动 MySQL 时会执行 `scripts/init_sql/` 下的建表与种子脚本。

## 演示与验收

服务启动后，另开终端：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo_tools
```

依次问三句并检查工具调用：

- 「订单 1001 的物流到哪了？」→ 调 `query_logistics`，回答里有顺丰运单和上海分拨中心节点；
- 「退货政策是什么？」→ 调 `query_faq`，命中退货政策；
- 「邮费是多少？」→ 调 `query_faq`，FAQ 没有运费条目，徽章显示失败，回答如实说明查不到（留待 ch03 改进检索）。

其他命令：

```bat
.venv\Scripts\python.exe -X utf8 -m scripts.demo       :: ch01 两轮对话 + 售后提取
.venv\Scripts\python.exe -X utf8 -m scripts.evaluate   :: 标注集评估（需真实上游）
.venv\Scripts\python.exe -X utf8 -m pytest -q          :: 自动测试（或双击 test.bat）
```

## SSE 协议

`POST /api/chat`，请求体：

```json
{"message": "订单 1001 的物流到哪了", "history": [], "client_id": "web-xxxxxxxx"}
```

`history` 是完整的 user/assistant 交替问答；`client_id` 为 8–64 位字母数字下划线连字符；续接会话时再带 `conversation_id`。

事件顺序：

- `meta`：新会话号；
- `token`：正文增量；
- `status`：工具开始/结束（`phase`、`tool`、`label`、结束帧带 `ok`、`summary`、`duration_ms`）；
- `done`：`finish_reason`；
- `error`：`code`、`message`（例如 `database_unavailable`）。

## 目录

```
app/          配置、模型、提示词、SSE 服务、工具、存储、ORM 模型、网页
scripts/      初始化数据库、演示、评估、初始化 SQL
tests/        SQLite 自动测试 + MySQL 集成检查（不可用时跳过）
dev-notes/    分阶段开发留痕
docs/         规格与实施计划
```
