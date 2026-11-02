# ch10 主题分类语料

| 文件 | 入库 | 说明 |
|---|---|---|
| `synthetic_v1.jsonl` | 是 | 1200 条 LLM 合成问题，17 类配额固定，仅合成无隐私 |
| `paraphrase_v1.jsonl` | 是 | 句式改写池，只挂训练集，`seed_id` 指回合成样本 |
| `real_train.jsonl` | 否 | 飞轮真实问题清洗后 + 人工标签，git-ignored |
| `real_audit.csv` | 否 | 真实问题人工确认表，含 DB 原文，人工就地编辑；`--apply-real-audit` 直接读它，git-ignored |

字段：`id,text,labels,origin,target_labels,prelabels`。标签只允许 `app/topics/taxonomy.py`
的 17 类，写入前按术语表顺序排序。

重建命令见 `docs/superpowers/plans/2026-11-02-ch10-topic-classifier.md` 的
「数据构建与生成」任务；真实池原文不提交，仓库里只保留合成语料。
