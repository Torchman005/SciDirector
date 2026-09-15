-- =============================================================================
-- SciDirector Postgres 初始化
-- 职责：LangGraph checkpointer（图状态持久化）的库内扩展准备
-- 说明：表结构由 LangGraph 的 PostgresSaver.setup() 在运行时自动创建，
--       这里只负责创建扩展与只读观测视图，保持幂等。
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
-- 阶段五 RAG 用：向量检索（pgvector 需镜像支持，未装则跳过）
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector 未安装，跳过（RAG 检索将降级为关键词检索）';
END
$$;

-- 任务元数据表：Go 侧可选的持久化审计（Redis 为热状态，此表为冷归档）
CREATE TABLE IF NOT EXISTS job_audit (
    id           BIGSERIAL PRIMARY KEY,
    job_id       TEXT        NOT NULL,
    shot_id      TEXT,
    event_type   TEXT        NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    attempt      INTEGER     NOT NULL DEFAULT 0,
    reason       TEXT,
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 事件流按 job 查询是最高频的访问模式
CREATE INDEX IF NOT EXISTS idx_job_audit_job_created
    ON job_audit (job_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_job_audit_shot
    ON job_audit (job_id, shot_id, attempt);

COMMENT ON TABLE job_audit IS 'SciDirector 状态迁移审计日志：每次 PENDING->GENERATING 之类的迁移写一行，用于追溯与指标计算';
