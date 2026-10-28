-- Docker 首次启动时 mysql 客户端可能按 latin1 解析脚本，中文会变成乱码，所以先声明连接字符集。
SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS xiaolin_ch02 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE xiaolin_ch02;

CREATE TABLE IF NOT EXISTS conversations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '会话 ID',
  user_id VARCHAR(64) NOT NULL COMMENT '用户标识，页面端匿名 ID',
  status ENUM('进行中','已转人工','已结束') NOT NULL DEFAULT '进行中' COMMENT '处理状态',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客服会话';

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '消息 ID',
  conversation_id BIGINT UNSIGNED NOT NULL COMMENT '所属会话',
  role ENUM('user','assistant','tool') NOT NULL COMMENT '消息角色',
  content TEXT NULL COMMENT '消息正文；工具消息为工具返回 JSON',
  tool_calls JSON NULL COMMENT 'assistant 发起的工具调用',
  tool_call_id VARCHAR(64) NULL COMMENT '工具结果对应的调用 ID',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_conversation_id (conversation_id),
  CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话消息';

CREATE TABLE IF NOT EXISTS faq (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'FAQ ID',
  question VARCHAR(512) NOT NULL COMMENT '标准问题',
  answer TEXT NOT NULL COMMENT '标准答案',
  category VARCHAR(64) NOT NULL COMMENT '分类',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='常见问题';

CREATE TABLE IF NOT EXISTS tickets (
  ticket_no VARCHAR(32) NOT NULL COMMENT '工单号，如 T20260701008',
  conversation_id BIGINT UNSIGNED NOT NULL COMMENT '来源会话',
  description TEXT NOT NULL COMMENT '问题描述',
  ticket_type ENUM('售后','投诉','咨询') NOT NULL COMMENT '工单类型',
  status ENUM('待处理','已处理') NOT NULL DEFAULT '待处理' COMMENT '处理状态',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (ticket_no),
  KEY idx_conversation_id (conversation_id),
  CONSTRAINT fk_tickets_conversation FOREIGN KEY (conversation_id) REFERENCES conversations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人工工单';
