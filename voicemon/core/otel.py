"""OpenTelemetry bridge — voice-aware tracing with span hierarchies.

Span hierarchy:
  voice_session → voice_turn → stt_recognition / llm_inference → tool_call / tts_synthesis
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor, SpanExporter,
)
from opentelemetry.trace import Span, StatusCode

logger = logging.getLogger("voicemon.otel")


class VoiceAttributes:
    SESSION_ID = "voice.session.id"
    AGENT_ID = "voice.agent.id"
    AGENT_VERSION = "voice.agent.version"
    FRAMEWORK = "voice.framework"
    TURN_NUMBER = "voice.turn.number"
    TURN_SPEAKER = "voice.turn.speaker"
    TURN_INTERRUPTED = "voice.turn.interrupted"
    STT_PROVIDER = "voice.stt.provider"
    STT_MODEL = "voice.stt.model"
    STT_CONFIDENCE = "voice.stt.confidence"
    STT_LANGUAGE = "voice.stt.language"
    STT_LATENCY_MS = "voice.stt.latency_ms"
    LLM_PROVIDER = "gen_ai.provider.name"
    LLM_MODEL = "gen_ai.request.model"
    LLM_TTFT_MS = "voice.llm.ttft_ms"
    LLM_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    LLM_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    TTS_PROVIDER = "voice.tts.provider"
    TTS_VOICE_ID = "voice.tts.voice_id"
    TTS_TTFB_MS = "voice.tts.ttfb_ms"
    E2E_LATENCY_MS = "voice.ux.e2e_latency_ms"
    TTFW_MS = "voice.ux.ttfw_ms"
    JITTER_MS = "voice.infra.jitter_ms"
    PACKET_LOSS_PCT = "voice.infra.packet_loss_pct"
    MOS_SCORE = "voice.infra.mos_score"


class VoiceTracer:
    def __init__(
        self, *, service_name: str = "voicemon",
        exporter: SpanExporter | None = None,
        provider: TracerProvider | None = None, batch: bool = True,
    ) -> None:
        if provider:
            self._provider = provider
        else:
            self._provider = TracerProvider()
            exp = exporter or ConsoleSpanExporter()
            proc = BatchSpanProcessor(exp) if batch else SimpleSpanProcessor(exp)
            self._provider.add_span_processor(proc)
        trace.set_tracer_provider(self._provider)
        self._tracer = self._provider.get_tracer(service_name, "0.1.0")

    @property
    def tracer(self) -> trace.Tracer:
        return self._tracer

    @property
    def provider(self) -> TracerProvider:
        return self._provider

    @contextmanager
    def session_span(self, session_id: str, *, agent_id: str = "",
                     framework: str = "", **kw: Any) -> Generator[Span, None, None]:
        attrs = {VoiceAttributes.SESSION_ID: session_id,
                 VoiceAttributes.AGENT_ID: agent_id,
                 VoiceAttributes.FRAMEWORK: framework, **kw}
        with self._tracer.start_as_current_span("voice_session", attributes=attrs) as s:
            try:
                yield s
            except Exception as e:
                s.set_status(StatusCode.ERROR, str(e)); s.record_exception(e); raise

    @contextmanager
    def turn_span(self, *, turn_number: int = 0, speaker: str = "user",
                  **kw: Any) -> Generator[Span, None, None]:
        attrs = {VoiceAttributes.TURN_NUMBER: turn_number,
                 VoiceAttributes.TURN_SPEAKER: speaker, **kw}
        with self._tracer.start_as_current_span("voice_turn", attributes=attrs) as s:
            try:
                yield s
            except Exception as e:
                s.set_status(StatusCode.ERROR, str(e)); s.record_exception(e); raise

    @contextmanager
    def stt_span(self, *, provider: str = "", **kw: Any) -> Generator[Span, None, None]:
        attrs = {VoiceAttributes.STT_PROVIDER: provider, **kw}
        with self._tracer.start_as_current_span("stt_recognition", attributes=attrs) as s:
            try:
                yield s
            except Exception as e:
                s.set_status(StatusCode.ERROR, str(e)); s.record_exception(e); raise

    @contextmanager
    def llm_span(self, *, model: str = "", provider: str = "",
                 **kw: Any) -> Generator[Span, None, None]:
        attrs = {VoiceAttributes.LLM_MODEL: model,
                 VoiceAttributes.LLM_PROVIDER: provider, **kw}
        with self._tracer.start_as_current_span("llm_inference", attributes=attrs) as s:
            try:
                yield s
            except Exception as e:
                s.set_status(StatusCode.ERROR, str(e)); s.record_exception(e); raise

    @contextmanager
    def tts_span(self, *, provider: str = "", **kw: Any) -> Generator[Span, None, None]:
        attrs = {VoiceAttributes.TTS_PROVIDER: provider, **kw}
        with self._tracer.start_as_current_span("tts_synthesis", attributes=attrs) as s:
            try:
                yield s
            except Exception as e:
                s.set_status(StatusCode.ERROR, str(e)); s.record_exception(e); raise

    @contextmanager
    def tool_call_span(self, *, function_name: str = "",
                       **kw: Any) -> Generator[Span, None, None]:
        attrs = {"voice.tool.function_name": function_name, **kw}
        with self._tracer.start_as_current_span("tool_call", attributes=attrs) as s:
            try:
                yield s
            except Exception as e:
                s.set_status(StatusCode.ERROR, str(e)); s.record_exception(e); raise

    def shutdown(self) -> None:
        self._provider.shutdown()
