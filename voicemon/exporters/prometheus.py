"""Prometheus exporter — metrics for SLO dashboards and alerting.

Exposes voice-specific histograms, counters, and gauges that Prometheus
scrapes via /metrics endpoint. Works with Prometheus Alertmanager for
latency budget exhaustion alerts.
"""

from __future__ import annotations

import logging
from typing import Any

from voicemon.exporters.base import BaseExporter

logger = logging.getLogger("voicemon.exporters.prometheus")

# Bucket boundaries (ms) tuned for voice pipeline latencies
LATENCY_BUCKETS_MS = (50, 100, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000)
CONFIDENCE_BUCKETS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.99)


class PrometheusExporter(BaseExporter):
    """Export voice telemetry as Prometheus metrics."""

    def __init__(self, *, prefix: str = "voicemon", port: int = 9090) -> None:
        self.prefix = prefix
        self.port = port
        self._initialized = False
        # Metric handles
        self._histograms: dict[str, Any] = {}
        self._counters: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}

    def initialize(self) -> None:
        """Lazily create all metric instruments."""
        if self._initialized:
            return
        try:
            from prometheus_client import Counter, Gauge, Histogram
        except ImportError:
            logger.error("prometheus_client not installed, metrics disabled")
            return

        p = self.prefix

        # ── Histograms (latency distributions) ──────────────────────────
        self._histograms = {
            "stt_latency": Histogram(
                f"{p}_stt_latency_ms", "STT recognition latency (ms)",
                ["provider", "model"], buckets=LATENCY_BUCKETS_MS,
            ),
            "llm_ttft": Histogram(
                f"{p}_llm_ttft_ms", "LLM time-to-first-token (ms)",
                ["provider", "model"], buckets=LATENCY_BUCKETS_MS,
            ),
            "llm_total": Histogram(
                f"{p}_llm_total_ms", "LLM total inference time (ms)",
                ["provider", "model"], buckets=LATENCY_BUCKETS_MS,
            ),
            "tts_ttfb": Histogram(
                f"{p}_tts_ttfb_ms", "TTS time-to-first-byte (ms)",
                ["provider"], buckets=LATENCY_BUCKETS_MS,
            ),
            "tts_total": Histogram(
                f"{p}_tts_total_ms", "TTS total synthesis time (ms)",
                ["provider"], buckets=LATENCY_BUCKETS_MS,
            ),
            "e2e_latency": Histogram(
                f"{p}_e2e_latency_ms", "End-to-end user-perceived latency (ms)",
                ["agent_id"], buckets=LATENCY_BUCKETS_MS,
            ),
            "stt_confidence": Histogram(
                f"{p}_stt_confidence", "STT confidence score distribution",
                ["provider"], buckets=CONFIDENCE_BUCKETS,
            ),
        }

        # ── Counters ────────────────────────────────────────────────────
        self._counters = {
            "sessions_total": Counter(
                f"{p}_sessions_total", "Total voice sessions",
                ["agent_id", "framework"],
            ),
            "turns_total": Counter(
                f"{p}_turns_total", "Total conversation turns",
                ["agent_id", "speaker"],
            ),
            "interruptions_total": Counter(
                f"{p}_interruptions_total", "Total interruption events",
                ["agent_id"],
            ),
            "errors_total": Counter(
                f"{p}_errors_total", "Total pipeline errors",
                ["agent_id", "component"],
            ),
            "tool_calls_total": Counter(
                f"{p}_tool_calls_total", "Total tool/function calls",
                ["agent_id", "function_name"],
            ),
            "llm_tokens_total": Counter(
                f"{p}_llm_tokens_total", "Total LLM tokens consumed",
                ["provider", "direction"],
            ),
            "tasks_resolved": Counter(
                f"{p}_tasks_resolved_total", "Tasks resolved by outcome",
                ["agent_id", "resolution_type"],
            ),
        }

        # ── Gauges ──────────────────────────────────────────────────────
        self._gauges = {
            "active_sessions": Gauge(
                f"{p}_active_sessions", "Currently active voice sessions",
                ["agent_id"],
            ),
            "stt_confidence_current": Gauge(
                f"{p}_stt_confidence_current", "Most recent STT confidence",
                ["agent_id"],
            ),
            "mos_score": Gauge(
                f"{p}_mos_score", "Current Mean Opinion Score",
                ["agent_id"],
            ),
        }

        self._initialized = True
        logger.info("Prometheus metrics initialized with prefix '%s'", p)

    def start_server(self) -> None:
        """Start Prometheus HTTP metrics server."""
        self.initialize()
        try:
            from prometheus_client import start_http_server
            start_http_server(self.port)
            logger.info("Prometheus metrics server on :%d/metrics", self.port)
        except Exception:
            logger.exception("Failed to start Prometheus metrics server")

    def export(self, event_type: str, data: dict[str, Any]) -> None:
        """Route events to appropriate Prometheus metrics."""
        if not self._initialized:
            self.initialize()
        if not self._initialized:
            return

        try:
            handler = self._event_handlers.get(event_type)
            if handler:
                handler(self, data)
        except Exception:
            logger.exception("Error exporting %s to Prometheus", event_type)

    # ── Event handlers ──────────────────────────────────────────────────

    def _handle_session_start(self, d: dict) -> None:
        self._counters["sessions_total"].labels(
            agent_id=d.get("agent_id", ""), framework=d.get("framework", ""),
        ).inc()
        self._gauges["active_sessions"].labels(agent_id=d.get("agent_id", "")).inc()

    def _handle_session_end(self, d: dict) -> None:
        self._gauges["active_sessions"].labels(agent_id=d.get("agent_id", "")).dec()

    def _handle_turn(self, d: dict) -> None:
        self._counters["turns_total"].labels(
            agent_id=d.get("agent_id", ""), speaker=d.get("speaker", "user"),
        ).inc()

    def _handle_stt(self, d: dict) -> None:
        provider = d.get("provider", "unknown")
        model = d.get("model", "")
        latency = d.get("latency_ms", 0)
        confidence = d.get("confidence", 0)

        if latency > 0:
            self._histograms["stt_latency"].labels(provider=provider, model=model).observe(latency)
        if confidence > 0:
            self._histograms["stt_confidence"].labels(provider=provider).observe(confidence)
            self._gauges["stt_confidence_current"].labels(
                agent_id=d.get("agent_id", ""),
            ).set(confidence)

    def _handle_llm(self, d: dict) -> None:
        provider = d.get("provider", "unknown")
        model = d.get("model", "")

        if d.get("ttft_ms", 0) > 0:
            self._histograms["llm_ttft"].labels(provider=provider, model=model).observe(d["ttft_ms"])
        if d.get("total_ms", 0) > 0:
            self._histograms["llm_total"].labels(provider=provider, model=model).observe(d["total_ms"])

        if provider == "tool_call":
            self._counters["tool_calls_total"].labels(
                agent_id=d.get("agent_id", ""), function_name=model,
            ).inc()
        else:
            if d.get("input_tokens"):
                self._counters["llm_tokens_total"].labels(
                    provider=provider, direction="input",
                ).inc(d["input_tokens"])
            if d.get("output_tokens"):
                self._counters["llm_tokens_total"].labels(
                    provider=provider, direction="output",
                ).inc(d["output_tokens"])

    def _handle_tts(self, d: dict) -> None:
        provider = d.get("provider", "unknown")
        if d.get("ttfb_ms", 0) > 0:
            self._histograms["tts_ttfb"].labels(provider=provider).observe(d["ttfb_ms"])
        if d.get("total_ms", 0) > 0:
            self._histograms["tts_total"].labels(provider=provider).observe(d["total_ms"])

    def _handle_ux(self, d: dict) -> None:
        ux_type = d.get("event_type", "")
        if ux_type == "interruption":
            self._counters["interruptions_total"].labels(
                agent_id=d.get("agent_id", ""),
            ).inc()
        if d.get("e2e_latency_ms", 0) > 0:
            self._histograms["e2e_latency"].labels(
                agent_id=d.get("agent_id", ""),
            ).observe(d["e2e_latency_ms"])

    def _handle_infra(self, d: dict) -> None:
        if d.get("mos_score", 0) > 0:
            self._gauges["mos_score"].labels(agent_id=d.get("agent_id", "")).set(d["mos_score"])

    def _handle_outcome(self, d: dict) -> None:
        self._counters["tasks_resolved"].labels(
            agent_id=d.get("agent_id", ""),
            resolution_type=d.get("resolution_type", "unknown"),
        ).inc()

    _event_handlers = {
        "session_start": _handle_session_start,
        "session_end": _handle_session_end,
        "turn": _handle_turn,
        "stt": _handle_stt,
        "llm": _handle_llm,
        "tts": _handle_tts,
        "ux": _handle_ux,
        "infra": _handle_infra,
        "outcome": _handle_outcome,
    }
