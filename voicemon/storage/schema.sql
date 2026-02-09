-- VoiceMon: TimescaleDB Schema
-- 4-Layer Voice Observability Framework
-- Run: psql -U voicemon -d voicemon -f schema.sql

-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- Core: Sessions
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    agent_version   TEXT DEFAULT '',
    framework       TEXT DEFAULT '',  -- livekit | pipecat | vapi
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    duration_ms     DOUBLE PRECISION,
    status          TEXT DEFAULT 'active',  -- active | completed | error
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sessions_agent ON sessions(agent_id);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_sessions_status ON sessions(status);

-- ============================================================
-- Core: Turns
-- ============================================================
CREATE TABLE IF NOT EXISTS turns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_number     INTEGER NOT NULL,
    speaker         TEXT NOT NULL,  -- user | agent
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    duration_ms     DOUBLE PRECISION,
    interrupted     BOOLEAN DEFAULT FALSE,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX idx_turns_session ON turns(session_id);
CREATE INDEX idx_turns_started ON turns(started_at DESC);

-- ============================================================
-- Layer 1: Infrastructure Events (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS infra_events (
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      UUID NOT NULL,
    jitter_ms       DOUBLE PRECISION DEFAULT 0,
    packet_loss_pct DOUBLE PRECISION DEFAULT 0,
    round_trip_ms   DOUBLE PRECISION DEFAULT 0,
    mos_score       DOUBLE PRECISION DEFAULT 0,
    codec           TEXT DEFAULT '',
    bitrate_kbps    INTEGER DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('infra_events', 'time', if_not_exists => TRUE);
CREATE INDEX idx_infra_session ON infra_events(session_id, time DESC);

-- ============================================================
-- Layer 2a: STT Events (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS stt_events (
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      UUID NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT DEFAULT '',
    latency_ms      DOUBLE PRECISION NOT NULL,
    confidence      DOUBLE PRECISION DEFAULT 0,
    transcript      TEXT DEFAULT '',
    is_final        BOOLEAN DEFAULT TRUE,
    language        TEXT DEFAULT 'en',
    word_count      INTEGER DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('stt_events', 'time', if_not_exists => TRUE);
CREATE INDEX idx_stt_session ON stt_events(session_id, time DESC);
CREATE INDEX idx_stt_provider ON stt_events(provider, time DESC);

-- ============================================================
-- Layer 2b: LLM Events (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_events (
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      UUID NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT DEFAULT '',
    ttft_ms         DOUBLE PRECISION NOT NULL,
    total_ms        DOUBLE PRECISION DEFAULT 0,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    temperature     DOUBLE PRECISION DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('llm_events', 'time', if_not_exists => TRUE);
CREATE INDEX idx_llm_session ON llm_events(session_id, time DESC);
CREATE INDEX idx_llm_model ON llm_events(provider, model, time DESC);

-- ============================================================
-- Layer 2c: TTS Events (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS tts_events (
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      UUID NOT NULL,
    provider        TEXT NOT NULL,
    voice_id        TEXT DEFAULT '',
    ttfb_ms         DOUBLE PRECISION NOT NULL,
    total_ms        DOUBLE PRECISION DEFAULT 0,
    audio_duration_ms DOUBLE PRECISION DEFAULT 0,
    char_count      INTEGER DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('tts_events', 'time', if_not_exists => TRUE);
CREATE INDEX idx_tts_session ON tts_events(session_id, time DESC);

-- ============================================================
-- Layer 3: UX Events (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS ux_events (
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      UUID NOT NULL,
    event_type      TEXT NOT NULL,  -- interruption | silence | latency_snapshot
    e2e_latency_ms  DOUBLE PRECISION DEFAULT 0,
    ttfw_ms         DOUBLE PRECISION DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('ux_events', 'time', if_not_exists => TRUE);
CREATE INDEX idx_ux_session ON ux_events(session_id, time DESC);
CREATE INDEX idx_ux_type ON ux_events(event_type, time DESC);

-- ============================================================
-- Layer 4: Outcome Events (hypertable)
-- ============================================================
CREATE TABLE IF NOT EXISTS outcome_events (
    time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_id      UUID NOT NULL,
    task_success    BOOLEAN DEFAULT FALSE,
    resolution_type TEXT DEFAULT '',
    csat_score      DOUBLE PRECISION DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

SELECT create_hypertable('outcome_events', 'time', if_not_exists => TRUE);
CREATE INDEX idx_outcome_session ON outcome_events(session_id, time DESC);

-- ============================================================
-- Alerts
-- ============================================================
CREATE TABLE IF NOT EXISTS alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,  -- info | warning | critical | p0
    session_id      UUID,
    agent_id        TEXT DEFAULT '',
    message         TEXT NOT NULL,
    value           DOUBLE PRECISION DEFAULT 0,
    threshold       DOUBLE PRECISION DEFAULT 0,
    fired_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged    BOOLEAN DEFAULT FALSE,
    ack_at          TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX idx_alerts_fired ON alerts(fired_at DESC);
CREATE INDEX idx_alerts_severity ON alerts(severity, fired_at DESC);
CREATE INDEX idx_alerts_rule ON alerts(rule_name, fired_at DESC);

-- ============================================================
-- Continuous Aggregates — hourly rollups
-- ============================================================

-- Hourly latency stats across all pipeline stages
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_latency_stats
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', s.time) AS bucket,
    s.provider AS stt_provider,
    AVG(s.latency_ms) AS avg_stt_ms,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY s.latency_ms) AS p50_stt_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY s.latency_ms) AS p95_stt_ms,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY s.latency_ms) AS p99_stt_ms,
    COUNT(*) AS stt_count
FROM stt_events s
GROUP BY bucket, s.provider
WITH NO DATA;

SELECT add_continuous_aggregate_policy('hourly_latency_stats',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Hourly STT quality stats
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_stt_stats
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    provider,
    model,
    AVG(confidence) AS avg_confidence,
    MIN(confidence) AS min_confidence,
    COUNT(*) FILTER (WHERE confidence < 0.7) AS low_confidence_count,
    COUNT(*) AS total_count
FROM stt_events
WHERE is_final = TRUE
GROUP BY bucket, provider, model
WITH NO DATA;

SELECT add_continuous_aggregate_policy('hourly_stt_stats',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Hourly LLM stats (cost tracking)
CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_llm_stats
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    provider,
    model,
    AVG(ttft_ms) AS avg_ttft_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms) AS p95_ttft_ms,
    SUM(input_tokens) AS total_input_tokens,
    SUM(output_tokens) AS total_output_tokens,
    COUNT(*) AS request_count
FROM llm_events
WHERE provider != 'tool_call'
GROUP BY bucket, provider, model
WITH NO DATA;

SELECT add_continuous_aggregate_policy('hourly_llm_stats',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- ============================================================
-- Retention Policies
-- ============================================================
-- Raw events: 30 days
SELECT add_retention_policy('infra_events',   INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('stt_events',     INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('llm_events',     INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('tts_events',     INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('ux_events',      INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('outcome_events', INTERVAL '30 days', if_not_exists => TRUE);
-- Aggregates: 1 year
-- (continuous aggregates retain independently)

-- ============================================================
-- Useful Views
-- ============================================================

-- Session summary with pipeline latency breakdown
CREATE OR REPLACE VIEW session_summary AS
SELECT
    s.id,
    s.agent_id,
    s.framework,
    s.started_at,
    s.duration_ms,
    s.status,
    (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turn_count,
    (SELECT AVG(latency_ms) FROM stt_events e WHERE e.session_id = s.id) AS avg_stt_ms,
    (SELECT AVG(ttft_ms) FROM llm_events e WHERE e.session_id = s.id AND e.provider != 'tool_call') AS avg_llm_ttft_ms,
    (SELECT AVG(ttfb_ms) FROM tts_events e WHERE e.session_id = s.id) AS avg_tts_ttfb_ms,
    (SELECT COUNT(*) FROM ux_events e WHERE e.session_id = s.id AND e.event_type = 'interruption') AS interruption_count,
    (SELECT task_success FROM outcome_events e WHERE e.session_id = s.id ORDER BY e.time DESC LIMIT 1) AS task_success
FROM sessions s;
