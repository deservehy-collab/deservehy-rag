-- ch09 · 可观测性与数据飞轮 · 建表 DDL
-- Docker 首次初始化会自动执行（挂载在 docker-entrypoint-initdb.d）；已有库手工执行一次，或跑
-- python scripts/migrate_ch09.py（幂等）；用 python -m scripts.init_db 建的库不要再跑本文件
-- （列已由 ORM 建出，ADD COLUMN 会 Duplicate）。
SET NAMES utf8mb4;

CREATE TABLE review_queue (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  normalized_question VARCHAR(512) NOT NULL,
  ai_suggested_answer TEXT NULL,
  occurrence_count INT UNSIGNED NOT NULL DEFAULT 1,
  review_status ENUM('待审','通过','驳回') NOT NULL DEFAULT '待审',
  approved_answer TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_review_queue_status (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE eval_runs (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  triggered_by ENUM('定时','手动') NOT NULL DEFAULT '定时',
  dataset_size INT UNSIGNED NOT NULL,
  metrics JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_eval_runs_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE low_confidence_questions
  ADD COLUMN retrieved_chunks JSON NULL AFTER reason,
  ADD COLUMN matched_review_id BIGINT UNSIGNED NULL AFTER retrieved_chunks,
  ADD KEY idx_lcq_matched_review_id (matched_review_id),
  ADD CONSTRAINT fk_lcq_review FOREIGN KEY (matched_review_id)
    REFERENCES review_queue (id) ON DELETE SET NULL;

ALTER TABLE messages
  ADD COLUMN retrieval_snapshot JSON NULL AFTER tool_call_id;
