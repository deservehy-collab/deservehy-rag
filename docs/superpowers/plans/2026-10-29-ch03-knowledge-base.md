# ch03 实施计划：知识库与向量语义检索

日期：2026-10-29
规格：`docs/superpowers/specs/2026-10-29-ch03-knowledge-base-design.md`

## 任务 1：表结构、配置、示例文档

文件：

- `app/models.py`：`KnowledgeChunk`、`QaExtractionStaging`
- `scripts/init_sql/003_rag.sql`：严格照用户 DDL
- `app/config.py`：嵌入/Milvus/检索/挖掘配置
- `.env.example`、`.env`（仅补非敏感默认值）
- `examples/knowledge/01-配送与运费.md`、`02-退换货与退款.md`、`03-商品使用与保修.md`
- `.gitignore`：`.cache/`

验证：`Base.metadata.create_all` 在 SQLite 建表成功；`python -m scripts.init_db` 在 MySQL 建表；DDL 字段与 ORM 对照。

## 任务 2：切分器（TDD）

文件：`app/knowledge/__init__.py`、`splitter.py`、`tests/test_splitter.py`

先写失败测试：

1. 二级/三级标题生成 `category` 父路径与 `section_path`；
2. `【关键条款】` 置位并剥离；
3. 600 字小节切成多块，长度 ≤ 500，重叠 ≥ 60 且起点是句末；
4. 表格超长按行拆，每块首两行是表头；
5. 代码围栏内的 `#` 不当作标题。

再实现并跑通。

## 任务 3：嵌入与 Milvus（TDD 解析层 + 集成）

文件：`app/knowledge/embedding.py`、`vector_store.py`、`tests/test_vector_store.py`

- `BgeM3Embedder`：懒加载、设备回退、`normalize_embeddings=True`、1024 维校验；
- `MilvusVectorStore.ensure_collection/upsert/search/delete`；
- 集成测试无 Milvus 自动跳过；真实环境验证 upsert 后 search 命中。

## 任务 4：双写入库与续跑（TDD）

文件：`app/knowledge/ingest.py`、`dedupe.py`、`tests/test_ingest.py`、`scripts/ingest_knowledge.py`

- 文档/FAQ/挖掘三类入库，内容键查重；
- pending → encode → vector upsert → done 回填；
- 用 fake 组件模拟「Milvus 成功、回填前崩溃」与「MySQL pending 后崩溃」，续跑后 pending=0 且无重复；
- 回填 prev/next；
- CLI 参数 `--no-faq`、`--vectorize-only`、`--rebuild --yes`。

## 任务 5：检索与 query_faq 接线（TDD）

文件：`app/knowledge/retrieval.py`、`app/tools.py`、`app/service.py`、`app/main.py`、`tests/test_retrieval.py`、`tests/test_tools.py`（补充）

- `KnowledgeRetriever.search` 过滤 done、按分数截断、结构同 `search_faq`；
- `make_tools(..., knowledge=...)`，`FaqArgs` 与返回键不变；
- 知识库不可用返回同形状错误；
- `create_app` 可注入 retriever；lifespan 构建真实组件（懒连接，不阻塞启动）。

## 任务 6：会话挖掘与定时任务（TDD）

文件：`app/knowledge/mining.py`、`scheduler.py`、`tests/test_mining.py`、`scripts/mine_knowledge.py`

- 会话筛选、LLM JSON 抽取、staging 写入；
- 精确 + 语义去重、kept 入库；
- 定时器开关与 `max_instances=1`；
- fake LLM 覆盖正常、空结果、坏 JSON。

## 任务 7：环境、验收与交付

文件：`docker-compose.yml`（Milvus）、`requirements.txt`、`requirements.lock.txt`、`start.bat`、`README.md`、`scripts/demo_knowledge.py`、`dev-notes/ch03.md`

- 拉起 Milvus，初始化 DB，真实构建知识库；
- 真实中断 + 重跑验证补齐；
- 启动服务问「邮费是多少」，核对 SSE 工具事件与回答；
- `pytest` 全绿、集成项通过；
- 隐私检查后按「第三次提交（ch03）」提交。

## 验证命令

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m scripts.init_db
.venv/Scripts/python.exe -m scripts.ingest_knowledge
.venv/Scripts/python.exe -m scripts.mine_knowledge --once
.venv/Scripts/python.exe scripts/demo_knowledge.py
```
