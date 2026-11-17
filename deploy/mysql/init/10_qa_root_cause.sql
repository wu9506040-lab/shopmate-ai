-- 知识运营闭环：给质检结果打知识侧根因
--
-- 为什么单独加列而不复用 failure_layer：
-- failure_layer 说"哪一层坏了"（retrieval/generation/...），但 retrieval 一档里
-- 混了「知识库里根本没有」「有但没搜到」「搜到了但内容过期」三种完全不同的病，
-- 修法分别是补卡 / 调检索 / 改卡。不拆开，运营拿到的清单没法直接执行。
--
-- root_cause 取值：
--   no_kb          知识库里没这条       → 补一张新卡
--   retrieval_miss 有这条但 top-k 没命中 → 调检索（同义词/切分/权重）
--   kb_stale       命中但内容与答案冲突   → 改这张卡
--   ranking_weak   命中但排位靠后没用上   → 调排序权重
--   generation     资料对且用上了，答案仍歪 → 改 prompt/模型
--   unknown        缺 expected_source 标注或检索不可用，不进闭环

USE customer_service;

-- 建表（原先只在开发库手工建过，init 目录缺 DDL → 干净库/CI 到 ALTER 就报
-- 1146 Table doesn't exist）。这里不含 root_cause / root_cause_evidence，
-- 交给下面的 ALTER 加，避免重复列。
CREATE TABLE IF NOT EXISTS qa_results (
  id BIGINT NOT NULL AUTO_INCREMENT,
  batch_id VARCHAR(40) NOT NULL,
  session_id VARCHAR(64) NOT NULL,
  message_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  query TEXT NOT NULL,
  answer TEXT NOT NULL,
  intent VARCHAR(32) DEFAULT NULL,
  failure_layer VARCHAR(24) DEFAULT NULL,
  rule_hits JSON DEFAULT NULL,
  dims JSON DEFAULT NULL,
  score FLOAT DEFAULT NULL,
  verdict VARCHAR(12) NOT NULL,
  source VARCHAR(8) NOT NULL,
  calibrated TINYINT(1) NOT NULL,
  review_status VARCHAR(12) NOT NULL,
  note VARCHAR(500) DEFAULT NULL,
  latency_ms INT DEFAULT NULL,
  create_time DATETIME NOT NULL DEFAULT (now()),
  update_time DATETIME NOT NULL DEFAULT (now()),
  deleted SMALLINT NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_qa_session_message (session_id, message_id),
  KEY ix_qa_results_batch_id (batch_id),
  KEY ix_qa_results_failure_layer (failure_layer),
  KEY ix_qa_results_session_id (session_id),
  KEY ix_qa_results_intent (intent),
  KEY ix_qa_results_verdict (verdict),
  KEY ix_qa_results_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='七维评测结果';

ALTER TABLE qa_results
  ADD COLUMN root_cause VARCHAR(24) NULL
    COMMENT '知识侧根因：no_kb/retrieval_miss/kb_stale/ranking_weak/generation/unknown'
    AFTER failure_layer,
  ADD COLUMN root_cause_evidence JSON NULL
    COMMENT '判定依据快照：命中位次/库里是否存在/dims 取值'
    AFTER root_cause,
  ADD INDEX idx_qa_root_cause (root_cause);
