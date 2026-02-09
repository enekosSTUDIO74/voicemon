"""TimescaleDB storage client — async writes and queries."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from voicemon.core.config import TimescaleConfig

logger = logging.getLogger("voicemon.storage")


class TimescaleStore:
    """Async TimescaleDB client for persisting voice agent telemetry."""

    def __init__(self, config: TimescaleConfig | None = None) -> None:
        self._config = config or TimescaleConfig()
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._config.dsn,
            min_size=self._config.pool_min,
            max_size=self._config.pool_max,
        )
        logger.info("Connected to TimescaleDB")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("TimescaleDB connection closed")

    async def init_schema(self, schema_path: str | None = None) -> None:
        """Run the schema SQL to create tables and hypertables."""
        import importlib.resources as pkg_resources

        if schema_path is None:
            # Use bundled schema
            import voicemon.storage as storage_pkg
            schema_sql = (
                pkg_resources.files(storage_pkg).joinpath("schema.sql").read_text()
            )
        else:
            with open(schema_path) as f:
                schema_sql = f.read()

        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(schema_sql)
        logger.info("Schema initialized")

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    async def insert_session(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO sessions (session_id, agent_id, agent_version, framework,
                                  status, started_at, ended_at, duration_ms,
                                  total_turns, total_cost_usd, metadata, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (session_id) DO UPDATE SET
                status = EXCLUDED.status,
                ended_at = EXCLUDED.ended_at,
                duration_ms = EXCLUDED.duration_ms,
                total_turns = EXCLUDED.total_turns,
                total_cost_usd = EXCLUDED.total_cost_usd,
                metadata = EXCLUDED.metadata
            """,
            data.get("session_id", ""),
            data.get("agent_id", ""),
            data.get("agent_version", ""),
            data.get("framework", ""),
            data.get("status", "active"),
            _parse_ts(data.get("started_at")),
            _parse_ts(data.get("ended_at")),
            data.get("duration_ms"),
            data.get("total_turns", 0),
            data.get("total_cost_usd"),
            json.dumps(data.get("metadata", {})),
            data.get("tags", []),
        )

    async def insert_turn(self, data: dict[str, Any]) -> None:
        turn = data.get("turn", data)
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO turns (turn_id, session_id, turn_number, speaker,
                               started_at, ended_at, duration_ms,
                               was_interrupted, transcript, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (turn_id) DO NOTHING
            """,
            turn.get("turn_id", ""),
            turn.get("session_id", ""),
            turn.get("turn_number", 0),
            turn.get("speaker", "user"),
            _parse_ts(turn.get("started_at")),
            _parse_ts(turn.get("ended_at")),
            turn.get("duration_ms"),
            turn.get("was_interrupted", False),
            turn.get("transcript", ""),
            json.dumps(turn.get("metadata", {})),
        )

    async def insert_stt_event(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO stt_events (event_id, session_id, turn_id, timestamp,
                                    transcript, is_final, confidence, language,
                                    latency_ms, audio_duration_ms, streamed,
                                    word_error_rate, provider, model, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """,
            data.get("event_id", ""),
            data.get("session_id", ""),
            data.get("turn_id", ""),
            _parse_ts(data.get("timestamp")),
            data.get("transcript", ""),
            data.get("is_final", True),
            data.get("confidence"),
            data.get("language"),
            data.get("latency_ms"),
            data.get("audio_duration_ms"),
            data.get("streamed", False),
            data.get("word_error_rate"),
            data.get("provider", ""),
            data.get("model", ""),
            json.dumps(data.get("metadata", {})),
        )

    async def insert_llm_event(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO llm_events (event_id, session_id, turn_id, timestamp,
                                    model, provider, ttft_ms, duration_ms,
                                    prompt_tokens, completion_tokens, total_tokens,
                                    cached_tokens, tokens_per_second, cancelled,
                                    prompt_compliant, cost_usd, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            """,
            data.get("event_id", ""),
            data.get("session_id", ""),
            data.get("turn_id", ""),
            _parse_ts(data.get("timestamp")),
            data.get("model", ""),
            data.get("provider", ""),
            data.get("ttft_ms"),
            data.get("duration_ms"),
            data.get("prompt_tokens", 0),
            data.get("completion_tokens", 0),
            data.get("total_tokens", 0),
            data.get("cached_tokens", 0),
            data.get("tokens_per_second"),
            data.get("cancelled", False),
            data.get("prompt_compliant"),
            data.get("cost_usd"),
            json.dumps(data.get("metadata", {})),
        )

    async def insert_tts_event(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO tts_events (event_id, session_id, turn_id, timestamp,
                                    text, provider, voice_id, ttfb_ms,
                                    duration_ms, audio_duration_ms, characters_count,
                                    streamed, cancelled, cost_usd, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """,
            data.get("event_id", ""),
            data.get("session_id", ""),
            data.get("turn_id", ""),
            _parse_ts(data.get("timestamp")),
            data.get("text", ""),
            data.get("provider", ""),
            data.get("voice_id", ""),
            data.get("ttfb_ms"),
            data.get("duration_ms"),
            data.get("audio_duration_ms"),
            data.get("characters_count", 0),
            data.get("streamed", False),
            data.get("cancelled", False),
            data.get("cost_usd"),
            json.dumps(data.get("metadata", {})),
        )

    async def insert_ux_event(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO ux_events (event_id, session_id, turn_id, timestamp,
                                   e2e_latency_ms, ttfw_ms, interruption_count,
                                   false_interruption_count, was_barge_in,
                                   silence_duration_ms, talk_ratio,
                                   frustration_score, sentiment, is_dialog_loop,
                                   repeated_prompt_count, fallback_triggered, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            """,
            data.get("event_id", ""),
            data.get("session_id", ""),
            data.get("turn_id", ""),
            _parse_ts(data.get("timestamp")),
            data.get("e2e_latency_ms"),
            data.get("ttfw_ms"),
            data.get("interruption_count", 0),
            data.get("false_interruption_count", 0),
            data.get("was_barge_in", False),
            data.get("silence_duration_ms"),
            data.get("talk_ratio"),
            data.get("frustration_score"),
            data.get("sentiment"),
            data.get("is_dialog_loop", False),
            data.get("repeated_prompt_count", 0),
            data.get("fallback_triggered", False),
            json.dumps(data.get("metadata", {})),
        )

    async def insert_outcome_event(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO outcome_events (event_id, session_id, timestamp,
                                        task_completed, task_name, turns_to_complete,
                                        escalated, transferred, transfer_target,
                                        compliance_pass, compliance_violations,
                                        cost_usd, revenue_impact_usd,
                                        ended_reason, containment_success, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            """,
            data.get("event_id", ""),
            data.get("session_id", ""),
            _parse_ts(data.get("timestamp")),
            data.get("task_completed"),
            data.get("task_name", ""),
            data.get("turns_to_complete"),
            data.get("escalated", False),
            data.get("transferred", False),
            data.get("transfer_target", ""),
            data.get("compliance_pass"),
            data.get("compliance_violations", []),
            data.get("cost_usd"),
            data.get("revenue_impact_usd"),
            data.get("ended_reason", ""),
            data.get("containment_success"),
            json.dumps(data.get("metadata", {})),
        )

    async def insert_infra_event(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO infra_events (event_id, session_id, timestamp,
                                      jitter_ms, packet_loss_pct, mos_score,
                                      rtt_ms, audio_codec, sample_rate_hz,
                                      bitrate_kbps, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            data.get("event_id", ""),
            data.get("session_id", ""),
            _parse_ts(data.get("timestamp")),
            data.get("jitter_ms"),
            data.get("packet_loss_pct"),
            data.get("mos_score"),
            data.get("rtt_ms"),
            data.get("audio_codec"),
            data.get("sample_rate_hz"),
            data.get("bitrate_kbps"),
            json.dumps(data.get("metadata", {})),
        )

    async def insert_alert(self, data: dict[str, Any]) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO alerts (alert_id, rule_name, severity, status,
                                fired_at, metric_name, metric_value,
                                threshold, message, context, notified_via)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (alert_id) DO UPDATE SET
                status = EXCLUDED.status,
                resolved_at = EXCLUDED.resolved_at
            """,
            data.get("alert_id", ""),
            data.get("rule_name", ""),
            data.get("severity", "P2"),
            data.get("status", "firing"),
            _parse_ts(data.get("fired_at")),
            data.get("metric_name", ""),
            data.get("metric_value"),
            data.get("threshold"),
            data.get("message", ""),
            json.dumps(data.get("context", {})),
            data.get("notified_via", []),
        )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        assert self._pool is not None
        row = await self._pool.fetchrow(
            "SELECT * FROM sessions WHERE session_id = $1", session_id
        )
        return dict(row) if row else None

    async def get_recent_sessions(
        self, *, limit: int = 50, agent_id: str | None = None
    ) -> list[dict[str, Any]]:
        assert self._pool is not None
        if agent_id:
            rows = await self._pool.fetch(
                "SELECT * FROM sessions WHERE agent_id = $1 ORDER BY started_at DESC LIMIT $2",
                agent_id, limit,
            )
        else:
            rows = await self._pool.fetch(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT $1", limit
            )
        return [dict(r) for r in rows]

    async def get_session_turns(self, session_id: str) -> list[dict[str, Any]]:
        assert self._pool is not None
        rows = await self._pool.fetch(
            "SELECT * FROM turns WHERE session_id = $1 ORDER BY turn_number",
            session_id,
        )
        return [dict(r) for r in rows]

    async def get_latency_percentiles(
        self,
        *,
        hours: int = 1,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Get e2e latency percentiles for the last N hours."""
        assert self._pool is not None
        query = """
            SELECT
                COUNT(*) as count,
                AVG(e2e_latency_ms) as avg_ms,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY e2e_latency_ms) as p50_ms,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY e2e_latency_ms) as p90_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY e2e_latency_ms) as p95_ms,
                percentile_cont(0.99) WITHIN GROUP (ORDER BY e2e_latency_ms) as p99_ms
            FROM ux_events ux
            JOIN sessions s ON ux.session_id = s.session_id
            WHERE ux.timestamp > NOW() - $1 * INTERVAL '1 hour'
              AND ux.e2e_latency_ms IS NOT NULL
        """
        params: list[Any] = [hours]
        if agent_id:
            query += " AND s.agent_id = $2"
            params.append(agent_id)

        row = await self._pool.fetchrow(query, *params)
        return dict(row) if row else {}

    async def get_stt_drift(
        self, *, hours: int = 24, provider: str | None = None
    ) -> list[dict[str, Any]]:
        """Get hourly ASR confidence trend for drift detection."""
        assert self._pool is not None
        query = """
            SELECT
                time_bucket('1 hour', timestamp) as bucket,
                AVG(confidence) as avg_confidence,
                COUNT(*) as event_count,
                AVG(latency_ms) as avg_latency_ms
            FROM stt_events
            WHERE timestamp > NOW() - $1 * INTERVAL '1 hour'
              AND is_final = TRUE
              AND confidence IS NOT NULL
        """
        params: list[Any] = [hours]
        if provider:
            query += " AND provider = $2"
            params.append(provider)
        query += " GROUP BY bucket ORDER BY bucket"

        rows = await self._pool.fetch(query, *params)
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return datetime.fromisoformat(val)
    return None
