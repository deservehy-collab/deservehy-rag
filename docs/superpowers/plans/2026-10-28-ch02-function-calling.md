# ch02 实施计划：Function Calling 工具链

规格：`docs/superpowers/specs/2026-10-28-ch02-function-calling-design.md`

全局约束：
- 每个任务先写失败测试，再写最小实现，最后跑全量测试。
- 不引入规格外的抽象；错误文案用中文、面向使用者。
- 任何涉及 SQLAlchemy / LangChain 的接口以已安装版本（SQLAlchemy 2.1.6、langchain-core 1.7.0）源码为准。
- 每个任务完成即追加 `dev-notes/ch02.md`。

## Task 1 数据库层

文件：`requirements.txt`、`app/database.py`、`app/models.py`、`scripts/init_db.py`、`tests/test_db.py`

1. 测试：四张表可在 SQLite 建出；`messages` 有 `tool_calls`/`tool_call_id`；`tickets.ticket_no` 为主键；FAQ 种子 8 条且幂等；MySQL 集成（可用时）四表齐全。
2. 实现：
   - `database.py`：`build_database_url(settings, name=None)` 用 `URL.create`（避免密码转义问题）；`build_engine`、`build_session_factory`、`ensure_database`。
   - `models.py`：`Base` + 四个模型，`BigInteger().with_variant(Integer, "sqlite")`，Enum/JSON/FK/索引/表选项齐全。
   - `init_db.py`：建库（MySQL）→ `create_all` → `seed_faq`（仅当表空）；`--test` 同时初始化测试库。
3. 验证：`pytest tests/test_db.py -q`；`python scripts/init_db.py` 后 `show tables`。

## Task 2 存储层

文件：`app/storage.py`、`tests/test_storage.py`

1. 测试：新建会话写 user 消息；带 `conversation_id` 且 `user_id` 一致时续接；不一致时新建；`add_message` 支持 tool_calls/tool_call_id；`next_ticket_no` 形如 `TYYYYMMDD001` 且递增；`search_faq("退货")` 命中、`search_faq("邮费")` 为空；LIKE 通配符被转义。
2. 实现 `ConversationStore`：`start_turn`、`add_message`、`create_ticket`、`search_faq`，每个方法短会话 + commit。
3. 验证：`pytest tests/test_storage.py -q`。

## Task 3 五个工具与执行器

文件：`app/tools.py`、`tests/test_tools.py`

1. 测试：注册表恰好 5 个且名称固定；`query_order("1001")` 有状态；未知订单 not_found；`query_logistics("1001")` 至少 1 个节点；`query_product` 至少一个参数；`query_faq("退货")` 非空、`query_faq("邮费")` 为空；`create_ticket` 落库且编号格式正确；执行器：未知工具、非法参数、超时重试 2 次、成功路径。
2. 实现：args_schema、mock 数据、`make_tools(session_factory, conversation_id)`、`ToolRegistry`、`ToolExecutor.execute`。
3. 验证：`pytest tests/test_tools.py -q`。

## Task 4 SSE 与落库

文件：`app/schemas.py`、`app/prompts.py`、`app/config.py`、`app/service.py`、`app/main.py`、`tests/support.py`、`tests/test_chat_tools.py`

1. 测试：
   - 无工具：事件 `meta → token… → done`，落库 user+assistant；
   - 有工具：`meta → status(start) → status(end) → token… → done`，落库 user、assistant(tool_calls)、tool、assistant；
   - 第二轮带 `conversation_id` 写入同一会话；
   - 工具未命中时模型收到 `ok:false` 的工具结果；
   - 上游错误仍映射为 `error`；DB 不可用映射为 `database_unavailable`。
2. 实现：`ChatRequest` 增 `client_id`/`conversation_id`；提示词改写；`CustomerService.respond`；`create_app` 注入 session factory；错误映射补充。
3. 验证：`pytest -q` 全绿（含 ch01 回归）。

## Task 5 前端徽章

文件：`app/web/chat.js`、`app/web/chat.css`、`app/web/index.html`

1. 实现：`client_id`（localStorage）、`conversation_id`（`meta` 帧）、`status` 帧渲染徽章、新对话重置；图标与样式。
2. 验证：浏览器（桌面 + 手机）三种状态截图；控制台零报错；无工具问答外观不变。

## Task 6 真实验收与交付

1. `docker compose up -d` + `python scripts/init_db.py`。
2. 重启服务（真实 DeepSeek），依次问三个验收问题，记录事件顺序与落库行。
3. `python scripts/demo_tools.py`。
4. `pytest -q`、`pip check`、`compileall`。
5. 更新 `dev-notes/ch02.md`（每阶段追加）、README 运行说明。
