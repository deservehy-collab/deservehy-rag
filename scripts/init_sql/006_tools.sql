SET NAMES utf8mb4;
-- ch08 工具层：调用审计表。已有库执行一次；新库由 `python scripts/init_db.py` 用 ORM 直接建表。
-- 不挂外键：写审计失败不许反过来拦住工具执行（规格 §5）。

CREATE TABLE tool_audit_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NULL COMMENT '所属会话；无会话上下文时为 NULL',
  tool_call_id VARCHAR(64) NULL COMMENT '模型给出的 tool_call_id',
  tool_name VARCHAR(128) NOT NULL COMMENT '工具名',
  tool_source ENUM('builtin','mcp') NOT NULL COMMENT '来源',
  mcp_server VARCHAR(64) NULL COMMENT 'MCP Server 名；内置为 NULL',
  arguments JSON NULL COMMENT '调用参数（含用户原话，与 messages 同级隐私）',
  result_summary TEXT NULL COMMENT '结果摘要：formatting.summarize() 压到 60 字，写入侧再按列上限截到 2000（TEXT 本身不限长）',
  status ENUM('成功','失败','超时','校验拦下','权限拒绝') NOT NULL COMMENT '状态',
  error_message VARCHAR(512) NULL COMMENT '错误说明',
  retry_count TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '实际重试次数 = 尝试次数 - 1',
  duration_ms INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '耗时（毫秒）',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_conversation_id (conversation_id),
  KEY idx_tool_name (tool_name),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
