"""Pipecat integration — BaseObserver implementation for frame interception.

Usage:
    from pipecat.pipeline.pipeline import Pipeline
    from voicemon.integrations.pipecat import VoiceMonPipecatObserver

    collector = VoiceMonCollector(...)
    observer = VoiceMonPipecatObserver(collector, agent_id="my-agent")
    pipeline = Pipeline([...], observers=[observer])
"""

from __future__ import annotations

import logging
import time
from typing import Any

from voicemon.core.collector import VoiceMonCollector

logger = logging.getLogger("voicemon.integrations.pipecat")


class VoiceMonPipecatObserver:
    """Pipecat BaseObserver-compatible observer for voice pipeline telemetry.

    Intercepts frames flowing through the Pipecat pipeline to record
    STT, LLM, TTS latencies, interruptions, and user experience metrics.
    """

    def __init__(
        self,
        collector: VoiceMonCollector,
        *,
        agent_id: str = "pipecat-agent",
        agent_version: str = "0.1.0",
    ) -> None:
        self.collector = collector
        self.agent_id = agent_id
        self.agent_version = agent_version
        self._session_id: str | None = None
        self._turn_active = False
        self._stt_start: float = 0.0
        self._llm_start: float = 0.0
        self._tts_start: float = 0.0

    def on_pipeline_started(self, pipeline: Any) -> None:
        """Called when the pipeline starts processing."""
        self._session_id = self.collector.start_session(
            agent_id=self.agent_id,
            agent_version=self.agent_version,
            framework="pipecat",
            metadata={"pipeline": type(pipeline).__name__},
        )
        logger.info("VoiceMon Pipecat session started: %s", self._session_id)

    def on_pipeline_stopped(self, pipeline: Any) -> None:
        """Called when pipeline shuts down."""
        if self._session_id:
            if self._turn_active:
                self.collector.end_turn(session_id=self._session_id)
            self.collector.end_session(session_id=self._session_id)
            logger.info("VoiceMon Pipecat session ended: %s", self._session_id)

    def on_push_frame(self, src: Any, dst: Any, frame: Any) -> None:
        """Called on every frame push in the pipeline."""
        if not self._session_id:
            return
        frame_name = type(frame).__name__
        try:
            self._handle_frame(frame, frame_name, direction="push")
        except Exception:
            logger.exception("Error handling push frame %s", frame_name)

    def on_pull_frame(self, src: Any, dst: Any, frame: Any) -> None:
        """Called on every frame pull in the pipeline."""
        if not self._session_id:
            return
        frame_name = type(frame).__name__
        try:
            self._handle_frame(frame, frame_name, direction="pull")
        except Exception:
            logger.exception("Error handling pull frame %s", frame_name)

    # ── Frame dispatch ──────────────────────────────────────────────────

    def _handle_frame(self, frame: Any, name: str, direction: str) -> None:
        assert self._session_id is not None

        # ── User speech detection ───────────────────────────────────────
        if name in ("UserStartedSpeakingFrame", "StartFrame"):
            if not self._turn_active:
                self.collector.start_turn(session_id=self._session_id, speaker="user")
                self._turn_active = True
            self._stt_start = time.monotonic()

        elif name == "UserStoppedSpeakingFrame":
            pass  # wait for transcription

        # ── STT / Transcription ─────────────────────────────────────────
        elif name in ("TranscriptionFrame", "InterimTranscriptionFrame"):
            is_final = name == "TranscriptionFrame"
            latency = _elapsed_ms(self._stt_start) if self._stt_start else 0.0
            self.collector.record_stt(
                session_id=self._session_id,
                provider=_get(frame, "provider", "deepgram"),
                model=_get(frame, "model", ""),
                latency_ms=latency,
                confidence=_get(frame, "confidence", 0.0),
                transcript=_get(frame, "text", ""),
                is_final=is_final,
            )
            if is_final:
                self._stt_start = 0.0

        # ── LLM ────────────────────────────────────────────────────────
        elif name == "LLMFullResponseStartFrame":
            self._llm_start = time.monotonic()

        elif name in ("LLMFullResponseEndFrame", "TextFrame") and self._llm_start:
            latency = _elapsed_ms(self._llm_start)
            self.collector.record_llm(
                session_id=self._session_id,
                provider=_get(frame, "provider", "openai"),
                model=_get(frame, "model", "gpt-4o"),
                ttft_ms=latency,
                total_ms=latency,
                input_tokens=_get(frame, "input_tokens", 0),
                output_tokens=_get(frame, "output_tokens", 0),
            )
            self._llm_start = 0.0

        # ── TTS ─────────────────────────────────────────────────────────
        elif name == "TTSStartedFrame":
            self._tts_start = time.monotonic()

        elif name in ("TTSStoppedFrame", "AudioRawFrame") and self._tts_start:
            latency = _elapsed_ms(self._tts_start)
            self.collector.record_tts(
                session_id=self._session_id,
                provider=_get(frame, "provider", "elevenlabs"),
                ttfb_ms=latency,
                total_ms=latency,
                voice_id=_get(frame, "voice_id", ""),
            )
            self._tts_start = 0.0

        # ── Interruption ────────────────────────────────────────────────
        elif name in ("BotInterruptionFrame", "StopInterruptionFrame",
                      "UserInterruptionFrame"):
            self.collector.record_ux(
                session_id=self._session_id,
                event_type="interruption",
                metadata={"frame": name},
            )

        # ── Bot done speaking → end turn ────────────────────────────────
        elif name == "BotStoppedSpeakingFrame":
            if self._turn_active:
                self.collector.end_turn(session_id=self._session_id)
                self._turn_active = False

        # ── Metrics frame (Pipecat built-in) ────────────────────────────
        elif name == "MetricsFrame":
            self._ingest_metrics_frame(frame)

    def _ingest_metrics_frame(self, frame: Any) -> None:
        """Handle Pipecat's native MetricsFrame with aggregated data."""
        assert self._session_id
        metrics = _get(frame, "metrics", {})
        if not isinstance(metrics, dict):
            return

        if "processing_time" in metrics:
            self.collector.record_ux(
                session_id=self._session_id,
                event_type="latency_snapshot",
                e2e_latency_ms=metrics.get("processing_time", 0) * 1000,
            )


# ── Helpers ──────────────────────────────────────────────────────────────

def _get(obj: Any, attr: str, default: Any = None) -> Any:
    """Safely get attribute from frame or dict."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _elapsed_ms(start: float) -> float:
    if start <= 0:
        return 0.0
    return round((time.monotonic() - start) * 1000, 2)
