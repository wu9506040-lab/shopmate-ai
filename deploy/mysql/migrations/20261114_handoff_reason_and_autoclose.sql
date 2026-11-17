-- 人工工单：转人工原因可读化 + 多轮回复 / 超时自动结单
-- 幂等：information_schema 预检 + 动态 SQL（可直接重复执行）
USE `customer_service`;

-- 1) 转人工原因（中文可读，坐席一眼看清为什么要人工介入）
SET @sql = (
  SELECT IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'handoff_tickets'
       AND column_name = 'reason_detail') = 0,
    'ALTER TABLE handoff_tickets ADD COLUMN reason_detail VARCHAR(500) NULL COMMENT ''转人工原因说明（中文可读 + 触发依据）'' AFTER reason',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2) 坐席最后一次回复时间：回复与结单拆开后，用它算 2 分钟超时窗口
SET @sql = (
  SELECT IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'handoff_tickets'
       AND column_name = 'last_agent_reply_at') = 0,
    'ALTER TABLE handoff_tickets ADD COLUMN last_agent_reply_at DATETIME NULL COMMENT ''坐席最后一次回复时间'' AFTER reply',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3) 是否为系统自动结单（区分坐席主动结单与用户超时未回复）
SET @sql = (
  SELECT IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'handoff_tickets'
       AND column_name = 'auto_closed') = 0,
    'ALTER TABLE handoff_tickets ADD COLUMN auto_closed TINYINT NOT NULL DEFAULT 0 COMMENT ''1=用户超时未回复，系统自动结单'' AFTER last_agent_reply_at',
    'SELECT 1'
  )
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
