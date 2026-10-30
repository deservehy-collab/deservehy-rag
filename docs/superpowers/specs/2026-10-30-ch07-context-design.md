# ch07 上下文管理设计规格

日期：2026-10-30
状态：定稿
范围：单个会话内的历史记忆；不跨会话，不存用户画像

## 1. 目标

把第一版「超预算就删最老一轮」升级为三层上下文管理：

1. 最近几轮保留原文，一个字不压。
2. 中间层按规则截短，仍可被当前轮直接看到。
3. 更早的历史由后台异步压成一段段梗概，逐段追加，不重写旧段。

同时保证：调模型时 system 前缀稳定、历史预算从窗口倒推、完整历史落检查点、每轮上下文可观测、前端能切换多个会话。

## 2. 范围

### 2.1 本章做

- 三层结构与两个消息 id 锚点。
- 后台异步摘要（触发、调度、落分段、推进锚点、生命周期日志）。
- 固定拼装顺序与 token 预算公式。
- 中文 token 估算口径与启动自检。
- `conversations` / `messages` 两表读历史，LangGraph `State.messages` 挂 `add_messages`。
- `log/app.log` 中的 `history_ctx` / `model_ctx` 与摘要日志。
- 只读会话接口与前端会话侧栏。
- 摘要标注集与评估脚本。

### 2.2 本章不做

- 语义检索捞历史、主题重要度保留关键事实。
- 跨会话长期记忆、用户画像。
- tool 行进入模型上下文（库里保留，用于审计/回放）。
- 摘要段重写、合并或投影到 `conversations` 表。
- 多进程摘要协调；按单进程教学项目处理。

## 3. 现状与差距

| 现状 | ch07 要求 |
| --- | --- |
| `app/context.py` 用 UTF-8 字节估 token，超预算按轮删除 | 中文口径 + 三层裁剪 + 摘要注入 |
| 历史由前端 `history` 字段回传 | 从 `messages` 表按会话加载 |
| `State.messages` 无 reducer，每轮整体替换 | `add_messages`，按稳定消息 id 合并 |
| 路由提示与证据拼进 `SystemMessage` | system 恒定，提示与证据移到当前用户之后 |
| 无摘要表、无锚点列、无后台摘要 | 分段表 + 两个锚点 + 异步任务 |
| 无文件日志、无会话列表接口 | `log/app.log` + 两个只读接口 |
| 左侧栏是固定介绍 | 会话列表、切换回载、移动端入口 |

## 4. 数据模型与锚点

### 4.1 `conversations`

新增两列，均可空：

```sql
ALTER TABLE conversations
  ADD COLUMN summary_upto_msg_id BIGINT UNSIGNED NULL COMMENT '摘要已覆盖到的消息 id（含）',
  ADD COLUMN layer1_from_msg_id BIGINT UNSIGNED NULL COMMENT '层 1 起始消息 id（含）';
```

- `NULL` 按 `0` 处理。
- 只用于定位边界，不复制消息内容。
- 降级只更新 `layer1_from_msg_id`；摘要完成只更新 `summary_upto_msg_id`。

### 4.2 `conversation_summaries`

```sql
CREATE TABLE conversation_summaries (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NOT NULL,
  seq INT UNSIGNED NOT NULL,
  from_msg_id BIGINT UNSIGNED NOT NULL,
  upto_msg_id BIGINT UNSIGNED NOT NULL,
  content TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_conversation_seq (conversation_id, seq),
  KEY idx_conversation_upto (conversation_id, upto_msg_id),
  CONSTRAINT fk_summaries_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- 一段只覆盖一个 `(from_msg_id, upto_msg_id]` 区间。
- 追加时 `seq = max(seq) + 1`；不更新旧行。
- 上下文拼装按 `seq ASC` 拼全部段。

DDL 落 `scripts/init_sql/005_context.sql`；新库由 `Base.metadata.create_all` 直接建列建表。

## 5. 三层结构与降级

消息集合只包含 `role in (user, assistant)` 且内容非空的最终消息；`tool` 行与带 `tool_calls` 的中间 assistant 行不进入上下文。

### 5.1 边界

设 `S = summary_upto_msg_id`，`L1 = layer1_from_msg_id`，`C = 当前用户消息 id`：

- 层 1（原文）：`L1 <= id < C`。
- 层 2（截短）：`S < id < L1`。
- 已摘要：`id <= S`，只以梗概文本出现。

### 5.2 层 1 降级

1. 用 `trim_messages`（`strategy="last"`、`allow_partial=False`、`start_on="human"`、自定义 token 计数）把层 1 裁到 `layer1_budget`。
2. 裁掉后第一条保留消息的 id 成为新 `L1`。
3. 若新 `L1` 比旧值大，写回 `conversations.layer1_from_msg_id`，并记日志：
   `layer1 降级 {old}→{new}`。
4. 首次没有锚点时，把 `L1` 初始化为当前层 1 第一条消息 id，不记「降级」。
5. `trim_messages` 必须从用户消息开始，避免留下半轮。

### 5.3 层 2 渲染

按消息类型截短：

- user：保留原话全文。
- assistant：`content[:40] + "…"`（不超过 40 字时不加省略号）。
- tool：不进入上下文（用户决策）。

渲染后再估算 token：

- 若仍超过 `layer2_budget`，从最老开始丢弃层 2 消息，直到不超预算；记 `layer2 丢弃 N 条`。
- 丢弃只影响本轮模型上下文，不影响数据库、摘要批次的原始消息。

### 5.4 摘要触发

- 用层 2 **原始**（截短前）token 估算与 `layer2_budget` 比较。
- 超过则本轮标记 `layer2_over=True`，回复结束后调度摘要。
- 日志：`summary trigger ... layer2≈N > budget M from=X upto=Y`。
- 摘要覆盖区间取调度时的 `(S, L1]` 快照。

## 6. Token 预算

### 6.1 估算口径

`estimate_text_tokens`：

- CJK 字符（含中文标点）：1 字 ≈ 1 token。
- 其他字符：约 3.5 字符 ≈ 1 token。
- 向上取整。

`estimate_tokens(messages)`：每条消息额外 8 token；带 `tool_calls` 的消息额外 16 token。

该口径比原 UTF-8 字节法更贴近中文模型，并保守（不会低估）。

### 6.2 预算公式

输入：

- `W = context_window_tokens`
- `O = max_output_tokens`
- `U = max_user_input_tokens`
- `SYS = estimate(chat_system(store_name)) + estimate(5 个工具 JSON Schema)`（本机实测 704 + 530 = 1234；随提示词变化）
- `E = knowledge_top_k * 400`（每条证据预留 400）
- `R = workflow_agent_max_steps * (tool_result_max_tokens + 400)`（每步工具调用与脚手架峰值）
- `M = token_safety_margin`（默认 316，按 SYS 实测值校准，`SYS + M` 合计 1550）
- `SUM = estimate(所有摘要段)`
- `want = history_keep_rounds * history_round_tokens`

计算：

```text
available = W - O - U - SYS - E - R - M - SUM
history   = max(0, min(want, available))
layer2    = floor(history * 0.3)
layer1    = max(0, history - layer2 - 1)   # 1 token 作拼接余量
```

演示配置：

```env
MODEL_CONTEXT_WINDOW=18000
MAX_OUTPUT_TOKENS=2000
MAX_USER_INPUT_TOKENS=2000
MAX_AGENT_STEPS=3
TOOL_RESULT_MAX_TOKENS=1200
RERANK_TOP_K=5
```

- `want = 20 * 600 = 12000`
- `available = 18000 - 2000 - 2000 - 1234 - 2000 - 4800 - 316 = 5650`（`SUM=0` 时）
- 结果：`history=5650`、`layer1=3954`、`layer2=1695`。

默认窗口 32768、其余默认：

- `available = 32768 - 1024 - 2000 - 1234 - 4000 - 6400 - 316 = 17794`
- `history = min(12000, 17794) = 12000`
- `layer1=8399`、`layer2=3600`
- 20 轮常规对话（约 6000 token 历史）不触发降级或摘要。

### 6.3 启动自检

构造 app 时计算一次空历史预算：

- 若 `history <= 0`、`layer1 <= 0`，或 `SYS + max_user_input_tokens > input_budget`，记 ERROR 日志，说明连一轮都装不下。
- 不抛异常：用户仍可用更短问题；单条问题预检会返回 422。

### 6.4 单条问题预检

`/api/chat` 在启动 SSE 前：

- `estimate_text(message) > max_user_input_tokens` → 422 `context_budget_exceeded`。
- `SYS + estimate_text(message) > input_budget` → 422 `context_budget_exceeded`。
- 其余情况进入流式；历史层裁剪不会让当前问题被删。

## 7. 模型上下文拼装

固定顺序：

1. `SystemMessage(chat_system(store_name))`：只含人设、红线、工具使用规则；每轮完全一致。
2. 层 2 截短消息。
3. 层 1 原文消息。
4. 当前用户消息（`resolved_question`，保留消息 id）。
5. 一条尾随 `HumanMessage`：按顺序拼
   - `【会话摘要】` + 所有摘要段（无摘要时省略本节）；
   - `【知识库证据】`（knowledge/policy 路由）；
   - `【订单信息】`（policy 路由查到订单时）；
   - `【本轮编排】` + `AGENT_ADDENDUM` / `KNOWLEDGE_HINT` / `POLICY_HINT`。

约束：

- 不再把路由提示、证据、订单拼进 system。
- 摘要不单独占 system，也不单独占一条 system 位置。
- 工具定义仍由 `bind_tools` 传入，顺序稳定。
- 当前消息与尾随块都在 system 之后，前缀缓存只受历史裁剪影响。

## 8. 后台异步摘要

### 8.1 调度

- 回复流结束后（`done` 已发），service 若发现 `layer2_over=True`，调用 `SummaryService.schedule(conversation_id, from, upto)`。
- 调度只创建 `asyncio.Task` 并登记到任务集合，不等待模型。
- `context_summary_enabled=false` 时记 `summary skip disabled`，不建任务。
- 同一会话已有任务时记 `summary skip running`，不重复建。

### 8.2 执行

1. 记 `summary start`（from/upto）。
2. 从 DB 读 `(from, upto]` 的 user/assistant 消息。
3. 调摘要模型：结构化输出 `{"summary": "..."}`。
4. 成功后 `append_summary`：
   - 期望当前 `summary_upto_msg_id == from`，否则记 `summary skip stale` 并放弃（已有更新的摘要）。
   - `seq = max(seq)+1`；插入分段；把 `summary_upto_msg_id` 更新为 `upto`。
5. 记 `summary done`（seq/from/upto/chars/elapsed）。
6. 失败记 `summary failed`（异常类型，不含敏感内容），不推进锚点。

### 8.3 摘要 Prompt

只提炼事实与诉求：

- 问过哪款商品/型号；
- 报过的订单号、手机号等已提供信息；
- 明确诉求（退款/换货/维修/催物流等）；
- 已确认的结论；
- 未解决的问题。

禁止：

- 编造未出现的信息；
- 保留寒暄、致谢、告别；
- 写建议、评价或操作结果。

长度：几十到一两百字；schema 限制 20–400 字，目标 ≤300 字。
旧摘要只作为背景输入，输出只覆盖本批新区间。

## 9. State、数据库与检查点

- `WorkflowState.messages: Annotated[list[BaseMessage], add_messages]`。
- 每轮 service 从 `messages` 表加载当前会话全部 user/assistant 最终消息，附带稳定 id（`str(Message.id)`），并把当前用户消息一起传入图。
- `add_messages` 按 id 合并：同一消息重复传入只保留一份；新消息追加。
- 节点只新增自己的消息：
  - 输入已含当前 user；
  - `log_node` 写最终 assistant 后，把 `AIMessage(id=str(assistant_msg_id))` 返回给 state。
  - tool 行与中间 assistant 行仍写数据库，但不进 state 的模型上下文（可按需审计）。
- checkpoint 保存完整 state（含完整消息列表）；模型只收到第 7 节的裁剪版。
- 锚点、摘要段以数据库为准，不放进 state。

## 10. 上下文可观测

### 10.1 文件日志

- 新增 `log/app.log`，UTF-8，INFO 级别。
- 目录不存在时创建；`log/` 加入 `.gitignore`。
- 配置项 `LOG_FILE`，测试用 `:memory:` 关闭。

### 10.2 `history_ctx`（每轮必打）

service 建完计划后记录，闲聊/投诉等固定话术轮也记录：

```text
history_ctx conv=12 turn=7 summary=[seq=1 ...] window=[human:..., ai:...]
```

- `summary`：所有摘要段全文或 `[]`。
- `window`：最近 6 条原始 user/assistant 消息（角色 + 内容）。
- 内容原样，不做摘要或省略（日志只落本机）。

### 10.3 `model_ctx`（每次调模型前）

`agent_node` 调 `run_react` 前记录：

```text
model_ctx conv=12 turn=7 summary=[...] messages=[{"role":"system","content":"..."}, ...]
layer2=3 layer1=4 window=7 summary_tokens=120 total_tokens=4200
```

- `messages` 为实际送给模型的完整上下文（含尾随块）。
- 同时记录层 1/层 2 条数与 token 估算。

### 10.4 摘要日志

`summary trigger / schedule / start / done / skip / failed` 全部带 `conv`、边界、token 或耗时。

## 11. 只读会话接口

### 11.1 `GET /api/conversations?client_id=...`

返回当前用户全部会话，`updated_at DESC, id DESC`，最多 50：

```json
{"items": [{"id": 12, "title": "最开始那个订单后来怎么说", "message_count": 14,
            "summarized": true, "updated_at": "2026-10-30T12:00:00"}]}
```

- `title`：该会话第一条 user 消息前 40 字，换行压成空格。
- `summarized`：是否存在摘要段。
- `message_count`：user/assistant 最终消息数。
- 参数不合法 422；数据库异常 503 `database_unavailable`。

### 11.2 `GET /api/conversations/{id}/messages?client_id=...`

```json
{"conversation_id": 12,
 "items": [{"id": 31, "role": "user", "content": "…", "created_at": "…"}]}
```

- 只返回 user/assistant 且内容非空、无 `tool_calls` 的最终消息，按 id 升序。
- 会话不存在或不属于该 client：403 `forbidden`（不泄露存在性）。
- 数据库异常 503。

## 12. 前端会话侧栏

- 桌面：现有左侧栏在品牌下增加「最近对话」列表；每项显示首问预览、更新时间、已摘要标记。
- 移动端：侧栏顶部条新增「历史」按钮，打开抽屉，复用同一个列表渲染。
- 交互：
  - 点击会话：锁住输入时忽略；拉取 `/messages`，清空消息区并按原文渲染 user/assistant，恢复 `conversationId`，可继续聊。
  - 「新对话」：清空当前消息区、`conversationId=null`，不删除旧会话；旧会话仍在列表。
  - 收到 `meta` 或 `done` 后刷新列表（新会话/预览/已摘要）。
  - 列表接口失败：静默保留空列表或旧缓存，聊天不受影响。
- 回载的消息只渲染文本，不重建退款表单/订单卡片等一次性交互。
- 所有动态文本用 `textContent`。

## 13. 配置

新增/调整：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `MODEL_CONTEXT_WINDOW`（兼容 `CONTEXT_WINDOW_TOKENS`） | 32768 | 模型窗口 |
| `MAX_OUTPUT_TOKENS` | 1024 | 输出预留 |
| `TOKEN_SAFETY_MARGIN` | 316 | 安全余量（与 SYS 实测 1234 合计 1550，已按演示配置校准） |
| `MAX_USER_INPUT_TOKENS` | 2000 | 当前问题上限 |
| `TOOL_RESULT_MAX_TOKENS` | 1200 | 单次工具结果峰值预留 |
| `MAX_AGENT_STEPS`（兼容 `WORKFLOW_AGENT_MAX_STEPS`） | 4 | ReAct 步数 |
| `RERANK_TOP_K`（兼容 `KNOWLEDGE_TOP_K`） | 10 | 证据条数，预算按 300/条 |
| `HISTORY_KEEP_ROUNDS` | 20 | 想留轮数 |
| `HISTORY_ROUND_TOKENS` | 600 | 每轮稳态占用 |
| `CONTEXT_SUMMARY_ENABLED` | true | 后台摘要开关 |
| `SUMMARY_TIMEOUT_SECONDS` | 20 | 摘要超时 |
| `LOG_FILE` | log/app.log | 文件日志；`:memory:` 关闭 |

## 14. 错误与边界

- 当前问题超过 `max_user_input_tokens` 或窗口：返回 422，不启动 SSE。
- 历史裁剪永不影响当前问题；预算不足时先丢层 2，再丢层 1 最老，最后仍不足则抛 `ContextBudgetError`（不吞）。
- 摘要失败/超时：不推进锚点，下一轮仍会触发；回复不受影响。
- 摘要落库时锚点已变：放弃本次结果，记 stale。
- 数据库异常：会话接口 503；聊天链路沿用现有 `database_unavailable`。
- 日志不记录 API key；消息内容只写本机 `log/app.log`，不入库。

## 15. 测试与评估

### 15.1 单元/集成测试

- 估算：中文≈1 token、ASCII 折算、消息开销。
- 预算：演示配置精确得到 `5650/3954/1695`；默认 32768 得到 `12000/8399/3600`。
- 分层：层 1 原文、层 2 截短、边界 id、降级日志、层 2 丢弃。
- 拼装：system 恒定且无证据；尾随块顺序为摘要→证据→订单→本轮编排；resolved 当前问题。
- State：`add_messages` 同 id 不重复；log 节点回写 assistant id。
- 摘要：超预算才调度；成功推进锚点追加段；失败/stale 不推进；不阻塞回复。
- 存储：历史过滤 tool；摘要/锚点读写；会话列表预览、排序、已摘要标记；消息回载归属。
- 接口：列表/回载成功、403、503、422。
- 回归：全量 pytest 全绿。

### 15.2 摘要评估集

- `evals/summary_cases.jsonl`：每例给定多轮对话与必含事实（订单号、商品、诉求、未解决问题）。
- `scripts/evaluate_summary.py`：真实模型逐例摘要，检查事实命中、长度 20–300、寒暄词不出现、JSON 可解析；门槛 0.90，写 `evals/reports/ch07-summary-*.md`。

## 16. 验收

1. 连续聊 20 轮以上，token 不爆、系统不崩。
2. 演示配置 `18000/2000/2000/3/1200/5` 再聊一遍：日志看到 `layer1 降级 X→Y`、`summary trigger layer2≈N > budget 1695`、`summary done 第 N 段`；再问「最开始那个订单后来怎么说」能靠梗概里的订单号与诉求答对。
3. 默认窗口 32768 聊 20 轮，不触发降级与摘要。
4. 日志能看到早期轮次被后台压缩且未阻塞该轮 `done`；`grep model_ctx / history_ctx` 每轮都有。
5. 前端侧栏可开多个会话、切回旧会话历史完整回载、继续聊。

## 17. 依赖与文档核对

- 本会话 Context7 MCP 不可用（`mcp_list` 返回 0 个工具）。按既有约定核对本机安装源码：
  - `langchain-core 1.7.0`：`trim_messages(messages, *, max_tokens, token_counter, strategy, allow_partial, end_on, start_on, include_system, text_splitter)`。
  - `langgraph 1.2.18`：`add_messages` 按消息 `id` 合并/替换。
  - `trim_messages` 的 `token_counter` 接受 `Callable[[list[BaseMessage]], int]`，本设计传自定义 `estimate_tokens`。
- 未新增第三方依赖；`trim_messages`/`add_messages` 均已在依赖内。

## 18. 已知取舍

- 层 2 截短的 assistant 只留 40 字，可能丢掉细节；细节仍可由摘要段覆盖，摘要未追上时以数据库为准。
- 摘要触发按原始层 2 token，可能比截短后更早触发；这是为了不丢事实，接受提前调度。
- 上下文只含 user/assistant，前端回载不显示工具中间态；工具审计看数据库。
- 单进程摘要任务集合；多进程部署需要数据库级锁，超出本章。
- 1 token 拼接余量使 `layer1 + layer2 = history - 1`，是为了保证拼装后不越界；演示数字以此为准。
