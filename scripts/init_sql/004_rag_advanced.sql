SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS low_confidence_questions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NULL,
  raw_question TEXT NOT NULL,
  source ENUM('retrieval_low_conf','self_check','user_feedback') NOT NULL,
  reason TEXT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_source (source),
  KEY idx_created_at (created_at),
  CONSTRAINT fk_lcq_conversation FOREIGN KEY (conversation_id) REFERENCES conversations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='低置信度问题池';

CREATE TABLE IF NOT EXISTS faith_cases (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  eval_id VARCHAR(16) NOT NULL,
  bucket VARCHAR(24) NOT NULL,
  query VARCHAR(512) NOT NULL,
  strategy VARCHAR(24) NOT NULL DEFAULT 'hybrid_rerank',
  answer TEXT NOT NULL,
  reason TEXT NOT NULL,
  citations JSON NULL COMMENT '喂给模型的 Top-K 证据快照：[{n,chunk_id,section_path,question,answer}]',
  judge_model VARCHAR(64) NULL,
  status ENUM('未解决','已解决','无需解决') NOT NULL DEFAULT '未解决',
  seen_count INT UNSIGNED NOT NULL DEFAULT 1,
  first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  resolution VARCHAR(300) NULL,
  resolved_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_eval_id (eval_id),
  KEY idx_status (status),
  KEY idx_last_seen_at (last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='ch04 忠实度编造个案台账';
