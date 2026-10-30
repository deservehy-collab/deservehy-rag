SET NAMES utf8mb4;
-- ch07 上下文管理：锚点列与会话摘要表。
-- 已有库执行一次；新库由脚本 `python scripts/init_db.py` 通过 ORM 直接建列建表。

ALTER TABLE conversations
  ADD COLUMN summary_upto_msg_id BIGINT UNSIGNED NULL COMMENT '摘要已覆盖到的消息 id（含）',
  ADD COLUMN layer1_from_msg_id BIGINT UNSIGNED NULL COMMENT '层 1 起始消息 id（含）';

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
