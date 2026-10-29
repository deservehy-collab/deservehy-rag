# ch03 设计：客服知识库与向量语义检索

日期：2026-10-29
状态：已定稿（用户批准「全部实现一遍」）

## 1. 目标

把 `query_faq` 的内部实现从 MySQL 关键词 `LIKE` 查表升级为 BGE-M3 + Milvus 的语义检索，工具的入参 Schema 与出参结构保持不变。同时建立可重复构建的知识库：Markdown 文档结构切分、历史会话挖掘、MySQL/Milvus 双写与断点续跑。

验收：

1. 问「邮费是多少」→ `query_faq` 命中运费内容，回答正确（ch02 记录的关键词漏检）；
2. 构建过程中途中断，再次运行后缺失向量自动补齐，`pending` 清零。

## 2. 范围

做：

- 3 份示例 Markdown 文档（配送与运费、退换货与退款、商品使用与保修），含运费、长文和表格；
- 结构感知切分：标题层级、父路径 category、长文递归切分、overlap 对齐句号、表格拆行重复表头、关键条款标记；
- 知识入库：文档 + `faq` 表 8 条商品 FAQ + 挖掘问答对；
- MySQL `knowledge_chunks` 为权威源，Milvus `knowledge` 集合存向量；先 pending 后向量化，回填 `vector_id` 置 done；
- 历史会话定时挖掘：LLM 抽取问答对 → `qa_extraction_staging` → 精确 + 语义去重 → kept 入库；
- `query_faq` 内部替换为向量检索，契约不变。

不做：关键词召回、混合检索、重排；只跑 BGE-M3 dense 单路。不改 ch01/ch02 的对话协议、前端与其它四个工具。

## 3. 架构

```
examples/knowledge/*.md ─┐
faq 表（商品 FAQ）       ─┼→ app/knowledge/splitter.py → 组装行（pending）
qa_extraction_staging    ─┘                                   ↓
                                                   MySQL knowledge_chunks（权威）
                                                              ↓ 批量 encode
                                                   Milvus knowledge（vector_id=chunk id）
                                                              ↓ 回填 vector_id + done

用户提问 → query_faq(keyword) → embed → Milvus Top-12 → MySQL 取 done 行 → Top-5
```

新增模块：

| 文件 | 职责 |
|---|---|
| `app/knowledge/splitter.py` | Markdown 结构切分，返回 `Chunk` 数据类 |
| `app/knowledge/embedding.py` | `Embedder` 协议 + `BgeM3Embedder`（懒加载、GPU、批量、归一化） |
| `app/knowledge/vector_store.py` | `VectorStore` 协议 + `MilvusVectorStore`（建集合、upsert、search、删除） |
| `app/knowledge/ingest.py` | `KnowledgeIngestor`：文档/FAQ/挖掘入库、向量化、续跑 |
| `app/knowledge/mining.py` | `ConversationMiner`：拉会话、LLM 抽取、staging、去重、入库 |
| `app/knowledge/retrieval.py` | `KnowledgeRetriever.search()`，与 `search_faq` 返回结构一致 |
| `app/knowledge/scheduler.py` | APScheduler 定时挖掘 |
| `app/knowledge/dedupe.py` | 规范化、精确哈希、余弦语义去重 |

接线：`make_tools(store, conversation_id, knowledge=None)`；`CustomerService` 持有 `KnowledgeRetriever`；lifespan 构建 embedding/vector/retriever，Milvus 懒连接，不可用不阻塞应用启动。`create_app` 增加可选注入参数，测试用 fake。

## 4. 数据模型

严格按用户 DDL：`knowledge_chunks`、`qa_extraction_staging`（见 `scripts/init_sql/003_rag.sql`）。ORM 与 DDL 一一对应：

- `id BIGINT UNSIGNED` 自增，SQLite 用 `with_variant(Integer, "sqlite")`；
- `prev_chunk_id` / `next_chunk_id` 自引用外键 `ON DELETE SET NULL`；
- `vectorize_status` 为 `ENUM('pending','done')`，默认 pending；`vector_id` 可空；
- 索引：`idx_category`、`idx_vectorize_status`、`idx_batch_no`、`idx_status`；
- 表 `InnoDB` / `utf8mb4`；DDL 首行 `SET NAMES utf8mb4;`。

Milvus 集合 `knowledge`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `vector_id` | VARCHAR(64) 主键 | 等于 `str(knowledge_chunks.id)` |
| `embedding` | FLOAT_VECTOR(1024) | BGE-M3 dense，L2 归一化后 COSINE |

索引 HNSW：`M=16`、`efConstruction=200`，检索 `ef=64`；集合不存在时创建并加载，已存在直接复用。

## 5. 切分规则

输入 Markdown，输出 `Chunk(category, questions, answer, section_path, content_type, is_key_clause, order)`。

- 按 `#`~`######` 维护标题栈；每个最小节的 `questions` = 小节标题，`category` = 父级标题路径（` › ` 连接，根节取文档一级标题），`section_path` = 含自身的完整路径。
- 正文含 `【关键条款】` 标记 → `is_key_clause=1`，标记从 `answer` 剥离。
- 小节正文 ≤ 500 字：整节一块；超过则按段落→句子聚合，目标 400 字、重叠 80 字；重叠起点向前吸附到最近句号/问号/感叹号/换行，避免半句；单句超长才硬切。
- 表格：识别 `|` 表格块；表格超 500 字按数据行拆分，每块重复表头与分隔行，`answer` 带上下文说明；表格不参与句号 overlap。
- 代码围栏内不解析标题与表格。
- 同一文档内块按出现顺序编号，入库后按编号回填 `prev_chunk_id` / `next_chunk_id`。
- `content_type`：`policy`（政策）、`manual`（手册）、`faq`（商品 FAQ）、`mined_qa`（会话挖掘）。

## 6. 入库与双写

`KnowledgeIngestor.ingest_documents()`、`ingest_faq()`、`ingest_pairs()` 都只写 MySQL pending；`vectorize_pending()` 统一编码并写 Milvus：

1. 查询 `vectorize_status='pending'` 或 `vector_id IS NULL` 的行，按 id 升序分批（默认 16）；
2. 拼嵌入文本：`category + "\n" + questions + "\n" + answer`；
3. `embedder.encode(texts)` → `vector_store.upsert([{vector_id, embedding}])`；
4. 同事务把每行 `vector_id=str(id)`、`vectorize_status='done'`；
5. 下一批继续；任何一批失败保留 pending，异常上抛，脚本非零退出。

幂等：

- 入库前按内容键（`category` + 规范化 `questions` + 规范化 `answer` 的 SHA-256）查重，已存在则跳过；文档重复运行不产生新行；
- Milvus 主键确定性等于 MySQL id，upsert 同主键幂等；
- 崩溃在「Milvus 写成功、MySQL 未回填」之间时，下一轮重编码并 upsert 同主键，不产生重复向量；
- 崩溃在「MySQL 已写 pending、Milvus 未写」之间时，下一轮 pending 查询自然拾起。

CLI：`python -m scripts.ingest_knowledge [--no-faq] [--vectorize-only] [--rebuild]`。`--rebuild` 清空知识表与 Milvus 集合后重建（需二次确认参数 `--yes`，避免误删）。

## 7. 检索

`KnowledgeRetriever.search(query, limit=5)`：

1. 空串直接返回 `[]`；`embedder.encode([query])`；
2. Milvus `search(collection, data=[vec], limit=12, output_fields=["vector_id"], search_params={"metric_type":"COSINE","params":{"ef":64}})`；
3. 按 distance 降序取 `vector_id`，批量查 MySQL `id IN (...) AND vectorize_status='done'`，恢复排序；
4. 过滤 `distance < knowledge_min_score`（默认 0.35），截断 `limit`；
5. 返回 `[{"id","question","answer","category"}]`，字段名与 `search_faq` 完全一致。

`query_faq` 的 `FaqArgs`（`keyword: 1..40`）与返回结构不变；Milvus/模型异常 → `{"ok": false, "error": "knowledge_unavailable", "message": "知识库暂时不可用，请稍后重试。"}`。不做关键词兜底。

嵌入模型懒加载，首次检索可能 >10s；入口脚本负责预热。`BgeM3Embedder` 使用 `SentenceTransformer(model, device=...)`，`encode(..., normalize_embeddings=True, batch_size=16, convert_to_numpy=True)`；GPU 不可用时回退 CPU 并记录日志。

## 8. 会话挖掘

- 数据源：近 `KNOWLEDGE_MINING_LOOKBACK_DAYS`（默认 7）天内有 ≥2 条消息的会话；已有 staging 记录的会话跳过；
- 每个会话取 user/assistant 文本（跳过 tool 消息），截断到预算内，连同提示词交现有 ChatOpenAI 输出 JSON：`[{"question","answer"}]`，最多 5 对；
- 每会话一个 `batch_no`（`YYYYmmddHHMMSS-<8位随机>`），`source_ref=conversation:{id}`；写 staging 状态 `extracted`；
- 去重：
  - 精确：规范化问题 + 答案哈希，对 staging 内其它行与知识库全量比对；
  - 语义：问题向量 cosine ≥ `KNOWLEDGE_DEDUPE_THRESHOLD`（默认 0.95），对 kept staging 与知识库比对；
  - 保留者置 `kept`，其余置 `discarded`（原因只写日志，DDL 无原因列）；
- kept 行以 `content_type='mined_qa'`、`category='会话问答'`、`section_path=source_ref` 入 `knowledge_chunks`（pending），随后统一向量化；
- 解析失败/空结果：该 batch 记 0 条并跳过，不写半成品；LLM 异常记录日志，定时任务下轮重试。

`app/knowledge/scheduler.py`：lifespan 内 `AsyncIOScheduler`（APScheduler 3.x），仅当 `KNOWLEDGE_MINING_ENABLED=true` 且 `MINING_INTERVAL_MINUTES>0` 时启动，`max_instances=1`、`coalesce=True`；关闭时 `shutdown(wait=False)`。README 注明只在单 worker 下启用。CLI：`python -m scripts.mine_knowledge --once [--limit N]`。

## 9. 配置

`.env` / `.env.example` 新增：

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 模型名或本地路径 |
| `EMBEDDING_DEVICE` | `cuda` | cuda / cpu |
| `EMBEDDING_BATCH_SIZE` | `16` | 编码批量 |
| `MILVUS_URI` | `http://127.0.0.1:19530` | standalone 地址 |
| `MILVUS_COLLECTION` | `knowledge` | 集合名 |
| `KNOWLEDGE_DOC_DIR` | `examples/knowledge` | 文档目录 |
| `KNOWLEDGE_TOP_K` | `5` | 最终返回条数 |
| `KNOWLEDGE_CANDIDATES` | `12` | Milvus 候选数 |
| `KNOWLEDGE_MIN_SCORE` | `0.35` | cosine 下限 |
| `KNOWLEDGE_MINING_ENABLED` | `false` | 定时挖掘开关 |
| `KNOWLEDGE_MINING_INTERVAL_MINUTES` | `60` | 间隔 |
| `KNOWLEDGE_MINING_LOOKBACK_DAYS` | `7` | 回溯天数 |
| `KNOWLEDGE_DEDUPE_THRESHOLD` | `0.95` | 语义去重阈值 |

权重放项目 `.cache/models/bge-m3`（默认 `EMBEDDING_MODEL` 指向该目录，已 gitignore），避免占 C 盘；国内网络用 ModelScope 下载，不走 HuggingFace。

## 10. 环境与启动

`docker-compose.yml` 增加 Milvus 2.6.1 standalone 三件套（`etcd`、`minio`、`milvus`），均不暴露公网端口，只有 `127.0.0.1:19530`、`127.0.0.1:9091`；卷名 `xiaolin1_ch03-*`，不碰 ch02 数据卷。2.6.1 的单容器 embedded etcd 会 panic，故用官方三容器形态。

`start.bat`：依赖检查加入新包 → `docker compose up -d` → `scripts.init_db` → `scripts.ingest_knowledge`（幂等；缺权重时打印 ModelScope 下载命令，失败只警告不阻断启动）→ uvicorn。

## 11. 错误与降级

- Milvus 不可用：应用正常启动，`query_faq` 返回 `knowledge_unavailable`，模型如实告知；
- 嵌入模型加载失败：同上，日志写明原因；
- MySQL 不可用：沿用 ch02 的 `database_unavailable`；
- 向量化中断：pending 保留，脚本可重复执行；`--vectorize-only` 只补向量；
- 不让半条知识进入检索：只有 `done` 行可被返回。

## 12. 测试

单元（默认离线，SQLite + fake embedder/vector store）：

- 切分：标题路径、category、关键条款标记剥离、长文 400/80 与句号对齐、表格拆行重复表头、代码围栏；
- 去重：规范化、精确哈希、语义阈值；
- 入库：待向量化、回填、重复运行不新增、模拟「Milvus 成功但回填前崩溃」后续跑补齐、`pending` 清零；
- 检索：返回结构与 `search_faq` 一致、分数过滤、只取 done；
- 工具：`query_faq` Schema 不变、命中/未命中/知识库不可用三种返回；
- 挖掘：抽取解析、staging 状态、精确+语义去重、kept 入库（fake LLM）。

集成（环境不可用自动跳过）：MySQL 建表与 DDL 一致性；Milvus 真实 upsert/search；BGE-M3 编码维度 1024。

真实验收：

- `python -m scripts.ingest_knowledge` 后统计 `knowledge_chunks`、`pending=0`；
- 启动服务，问「邮费是多少」→ `query_faq` ok、答案含运费；
- 真实中断：`ingest_knowledge` 处理中途 kill，重跑后 `pending=0`、向量数等于 done 行数；
- `scripts/demo_knowledge.py` 输出验收结果；`pytest` 全绿。

## 13. 决策记录

| 决策 | 理由 |
|---|---|
| 自研 Markdown 切分 | 现成 splitter 无法同时满足父路径 category、句号对齐 overlap、表头重复 |
| MySQL 先写 pending，再写 Milvus | MySQL 为权威源；崩溃后 pending 可续跑，Milvus 同主键 upsert 幂等 |
| Milvus 只存 id + 向量 | 文本与状态只认 MySQL，避免双源不一致；过滤暂不需要 |
| `vector_id = str(chunk_id)` | 确定性主键，重跑不产生重复向量 |
| 语义去重比较问题文本 | 问答对的核心是问题意图；答案常被改写，直接比答案误杀 |
| 定时器进程内运行 | 用户选定；单 worker 部署够用，省一个调度服务 |
| 不做关键词兜底 | 用户明确本章只跑 dense 单路；兜底会掩盖向量链路故障 |
| 示例文档由我编写 | 用户确认；仓库原本没有知识文档，且必须覆盖「邮费」缺失场景 |
| Milvus 用三容器 standalone（2.6.1） | 单容器 embedded etcd 在 2.6.1 直接 panic；本机已有 etcd/MinIO 镜像，三容器更稳 |
| 检索用 Strong 一致性 | Bounded 下刚 upsert 完立刻检索返回空，验收与演示都会误判 |

## 14. 交付

- 演示：`start.bat`、`python -m scripts.ingest_knowledge`、`python scripts/demo_knowledge.py`、`python -m scripts.mine_knowledge --once`；
- 测试结果：`pytest` 全绿 + 集成项（Docker 可用时）；
- 开发过程：`dev-notes/ch03.md`，按阶段追加。
