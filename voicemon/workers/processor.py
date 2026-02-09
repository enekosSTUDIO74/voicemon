"""Metrics processor worker — async Redis consumer that aggregates telemetry.

Reads events from Redis Streams, computes rolling aggregates,
detects anomalies, stores to TimescaleDB, and fires alerts.

Usage:
    python -m voicemon.workers.processor
"""

from __future__ import annotations

import asyncio
import logging
import signal
import statistics
import time
from collections import defaultdict, deque
from typing import Any

from voicemon.core.config import VoiceMonConfig

logger = logging.getLogger("voicemon.workers.processor")

# Rolling window sizes
WINDOW_SIZE = 100  # last N values for stats
ANOMALY_WINDOW = 50  # for z-score anomaly detection
Z_SCORE_THRESHOLD = 3.0


class MetricsProcessor:
    """Consumes Redis Streams, computes aggregates, persists to TimescaleDB."""

    EVENT_TYPES = ("session", "turn", "infra", "stt", "llm", "tts", "ux", "outcome")

    def __init__(
        self, config: VoiceMonConfig, *,
        consumer_name: str = "worker-1",
        batch_size: int = 100,
        poll_interval_ms: int = 2000,
    ) -> None:
        self.config = config
        self.consumer_name = consumer_name
        self.batch_size = batch_size
        self.poll_interval_ms = poll_interval_ms

        # Rolling metric windows for real-time aggregation
        self._windows: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=WINDOW_SIZE))
        # Alert state: cooldown tracking
        self._alert_cooldowns: dict[str, float] = {}

        self._redis: Any = None
        self._timescale: Any = None
        self._alert_engine: Any = None
        self._running = False
        self._stats = {"processed": 0, "errors": 0, "alerts_fired": 0}

    async def start(self) -> None:
        """Initialize connections and start processing loop."""
        logger.info("Starting MetricsProcessor [%s]", self.consumer_name)

        # Connect Redis
        from voicemon.exporters.redis import RedisStreamsExporter
        self._redis = RedisStreamsExporter(
            redis_url=str(self.config.redis.url),
            stream_prefix=self.config.redis.stream_prefix,
            consumer_group=self.config.redis.consumer_group,
        )
        await self._redis.connect()

        # Connect TimescaleDB
        from voicemon.storage.timescale import TimescaleStore
        self._timescale = TimescaleStore(dsn=str(self.config.timescale.dsn))
        await self._timescale.connect()

        # Initialize alert engine
        try:
            from voicemon.alerts.engine import AlertEngine
            self._alert_engine = AlertEngine(config=self.config)
            await self._alert_engine.initialize()
        except ImportError:
            logger.warning("AlertEngine not available, alerting disabled")

        self._running = True
        logger.info("MetricsProcessor ready, polling every %dms", self.poll_interval_ms)

        # Process all event types concurrently
        tasks = [
            asyncio.create_task(self._consume_loop(event_type))
            for event_type in self.EVENT_TYPES
        ]
        tasks.append(asyncio.create_task(self._stats_reporter()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("MetricsProcessor shutting down")

    async def stop(self) -> None:
        self._running = False
        if self._redis:
            await self._redis.close()
        if self._timescale:
            await self._timescale.close()
        logger.info("MetricsProcessor stopped. Stats: %s", self._stats)

    async def _consume_loop(self, event_type: str) -> None:
        """Main consumer loop for a single event type."""
        while self._running:
            try:
                messages = await self._redis.read_stream(
                    event_type,
                    consumer_name=self.consumer_name,
                    count=self.batch_size,
                    block_ms=self.poll_interval_ms,
                )
                if not messages:
                    continue

                ack_ids: list[str] = []
                for msg_id, data in messages:
                    try:
                        await self._process_event(event_type, data)
                        ack_ids.append(msg_id)
                        self._stats["processed"] += 1
                    except Exception:
                        logger.exception("Error processing %s event %s", event_type, msg_id)
                        self._stats["errors"] += 1

                if ack_ids:
                    await self._redis.ack(event_type, *ack_ids)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Consumer loop error for %s", event_type)
                await asyncio.sleep(1)

    async def _process_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Process a single event: persist, aggregate, check thresholds."""
        # 1. Persist to TimescaleDB
        await self._persist(event_type, data)

        # 2. Update rolling windows
        self._update_windows(event_type, data)

        # 3. Check thresholds and anomalies
        await self._check_thresholds(event_type, data)

    async def _persist(self, event_type: str, data: dict[str, Any]) -> None:
        """Write event to TimescaleDB."""
        if not self._timescale:
            return
        try:
            method = getattr(self._timescale, f"insert_{event_type}", None)
            if method:
                await method(**data)
            else:
                logger.debug("No TimescaleDB insert method for %s", event_type)
        except Exception:
            logger.exception("TimescaleDB insert failed for %s", event_type)

    def _update_windows(self, event_type: str, data: dict[str, Any]) -> None:
        """Track rolling metric windows for real-time aggregation."""
        metric_keys = {
            "stt": [("latency_ms", "stt_latency"), ("confidence", "stt_confidence")],
            "llm": [("ttft_ms", "llm_ttft"), ("total_ms", "llm_total")],
            "tts": [("ttfb_ms", "tts_ttfb"), ("total_ms", "tts_total")],
            "infra": [("jitter_ms", "infra_jitter"), ("mos_score", "infra_mos")],
            "ux": [("e2e_latency_ms", "e2e_latency")],
        }
        for data_key, window_key in metric_keys.get(event_type, []):
            val = data.get(data_key)
            if val is not None and val > 0:
                self._windows[window_key].append(float(val))

    async def _check_thresholds(self, event_type: str, data: dict[str, Any]) -> None:
        """Check if metrics breach configured thresholds."""
        thresholds = self.config.thresholds
        alerts: list[dict[str, Any]] = []

        if event_type == "stt":
            latency = data.get("latency_ms", 0)
            if latency > thresholds.stt_latency_critical_ms:
                alerts.append(self._build_alert(
                    "stt_latency_critical", "critical", data,
                    f"STT latency {latency:.0f}ms > {thresholds.stt_latency_critical_ms}ms",
                    latency, thresholds.stt_latency_critical_ms,
                ))
            elif latency > thresholds.stt_latency_warn_ms:
                alerts.append(self._build_alert(
                    "stt_latency_warning", "warning", data,
                    f"STT latency {latency:.0f}ms > {thresholds.stt_latency_warn_ms}ms",
                    latency, thresholds.stt_latency_warn_ms,
                ))
            confidence = data.get("confidence", 1.0)
            if 0 < confidence < thresholds.stt_confidence_min:
                alerts.append(self._build_alert(
                    "stt_confidence_low", "warning", data,
                    f"STT confidence {confidence:.2f} < {thresholds.stt_confidence_min}",
                    confidence, thresholds.stt_confidence_min,
                ))

        elif event_type == "llm":
            ttft = data.get("ttft_ms", 0)
            if ttft > thresholds.llm_ttft_critical_ms:
                alerts.append(self._build_alert(
                    "llm_ttft_critical", "critical", data,
                    f"LLM TTFT {ttft:.0f}ms > {thresholds.llm_ttft_critical_ms}ms",
                    ttft, thresholds.llm_ttft_critical_ms,
                ))
            elif ttft > thresholds.llm_ttft_warn_ms:
                alerts.append(self._build_alert(
                    "llm_ttft_warning", "warning", data,
                    f"LLM TTFT {ttft:.0f}ms > {thresholds.llm_ttft_warn_ms}ms",
                    ttft, thresholds.llm_ttft_warn_ms,
                ))

        elif event_type == "tts":
            ttfb = data.get("ttfb_ms", 0)
            if ttfb > thresholds.tts_ttfb_critical_ms:
                alerts.append(self._build_alert(
                    "tts_ttfb_critical", "critical", data,
                    f"TTS TTFB {ttfb:.0f}ms > {thresholds.tts_ttfb_critical_ms}ms",
                    ttfb, thresholds.tts_ttfb_critical_ms,
                ))

        elif event_type == "ux":
            e2e = data.get("e2e_latency_ms", 0)
            if e2e > thresholds.e2e_latency_critical_ms:
                alerts.append(self._build_alert(
                    "e2e_latency_critical", "p0", data,
                    f"E2E latency {e2e:.0f}ms > {thresholds.e2e_latency_critical_ms}ms — users will notice!",
                    e2e, thresholds.e2e_latency_critical_ms,
                ))

        # Anomaly detection on rolling windows
        anomaly_alert = self._detect_anomaly(event_type, data)
        if anomaly_alert:
            alerts.append(anomaly_alert)

        # Fire alerts
        for alert in alerts:
            await self._fire_alert(alert)

    def _detect_anomaly(self, event_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Z-score anomaly detection on rolling windows."""
        window_map = {"stt": "stt_latency", "llm": "llm_ttft", "tts": "tts_ttfb"}
        window_key = window_map.get(event_type)
        if not window_key:
            return None

        window = self._windows.get(window_key)
        if not window or len(window) < ANOMALY_WINDOW:
            return None

        values = list(window)
        current = values[-1]
        mean = statistics.mean(values[:-1])
        stdev = statistics.stdev(values[:-1])
        if stdev == 0:
            return None

        z_score = (current - mean) / stdev
        if abs(z_score) > Z_SCORE_THRESHOLD:
            return self._build_alert(
                f"{event_type}_anomaly", "warning", data,
                f"Anomaly detected: {window_key}={current:.0f}ms "
                f"(z-score={z_score:.1f}, mean={mean:.0f}ms, stdev={stdev:.0f}ms)",
                current, mean,
            )
        return None

    def _build_alert(
        self, rule_name: str, severity: str, data: dict, message: str,
        value: float, threshold: float,
    ) -> dict[str, Any]:
        return {
            "rule_name": rule_name,
            "severity": severity,
            "session_id": data.get("session_id", ""),
            "agent_id": data.get("agent_id", ""),
            "message": message,
            "value": value,
            "threshold": threshold,
        }

    async def _fire_alert(self, alert: dict[str, Any]) -> None:
        """Fire an alert with cooldown to prevent spam."""
        key = f"{alert['rule_name']}:{alert.get('agent_id', '')}"
        now = time.monotonic()
        cooldown_s = self.config.alert.cooldown_minutes * 60

        if key in self._alert_cooldowns:
            if now - self._alert_cooldowns[key] < cooldown_s:
                return  # still in cooldown

        self._alert_cooldowns[key] = now
        self._stats["alerts_fired"] += 1

        # Persist alert to TimescaleDB
        if self._timescale:
            try:
                await self._timescale._pool.execute(
                    """INSERT INTO alerts (rule_name, severity, session_id, agent_id,
                       message, value, threshold) VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    alert["rule_name"], alert["severity"],
                    alert.get("session_id") or None, alert.get("agent_id", ""),
                    alert["message"], alert["value"], alert["threshold"],
                )
            except Exception:
                logger.exception("Failed to persist alert")

        # Notify via alert engine
        if self._alert_engine:
            try:
                await self._alert_engine.notify(alert)
            except Exception:
                logger.exception("Failed to send alert notification")

        logger.warning("[ALERT:%s] %s — %s", alert["severity"].upper(), alert["rule_name"], alert["message"])

    async def _stats_reporter(self) -> None:
        """Log processing stats every 60 seconds."""
        while self._running:
            await asyncio.sleep(60)
            pending = {}
            for et in self.EVENT_TYPES:
                try:
                    pending[et] = await self._redis.get_stream_length(et)
                except Exception:
                    pass
            logger.info(
                "Processor stats: processed=%d errors=%d alerts=%d queues=%s",
                self._stats["processed"], self._stats["errors"],
                self._stats["alerts_fired"], pending,
            )

    def get_rolling_stats(self) -> dict[str, dict[str, float]]:
        """Get current rolling statistics for all tracked metrics."""
        result = {}
        for key, window in self._windows.items():
            values = list(window)
            if not values:
                continue
            result[key] = {
                "count": len(values),
                "mean": statistics.mean(values),
                "p50": statistics.median(values),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
                "min": min(values),
                "max": max(values),
            }
        return result


def _percentile(data: list[float], pct: float) -> float:
    """Simple percentile calc."""
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct)
    return sorted_data[min(idx, len(sorted_data) - 1)]


async def main() -> None:
    """CLI entrypoint for running the metrics processor."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    config = VoiceMonConfig()
    processor = MetricsProcessor(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(processor.stop()))
        except NotImplementedError:
            pass  # Windows

    await processor.start()


if __name__ == "__main__":
    asyncio.run(main())
