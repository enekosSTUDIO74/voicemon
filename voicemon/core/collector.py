"""VoiceMonCollector — central hub for recording voice agent telemetry."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from voicemon.core.config import VoiceMonConfig
from voicemon.core.models import (
    InfraEvent, LLMEvent, OutcomeEvent, SessionStatus, Speaker,
    STTEvent, TTSEvent, ToolCallEvent, Turn, TurnReport, UXEvent, VoiceSession,
)
from voicemon.exporters.base import BaseExporter, ConsoleExporter

logger = logging.getLogger("voicemon")


class VoiceMonCollector:
    """Thread-safe, async-first telemetry collector."""

    def __init__(self, config: VoiceMonConfig | None = None) -> None:
        self._config = config or VoiceMonConfig()
        self._exporters: list[BaseExporter] = []
        self._sessions: dict[str, VoiceSession] = {}
        self._turns: dict[str, Turn] = {}
        self._turn_reports: dict[str, TurnReport] = {}
        self._session_turns: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if not self._exporters:
            self._exporters.append(ConsoleExporter(verbose=self._config.debug))
        self._started = True
        logger.info("VoiceMonCollector started with %d exporter(s)", len(self._exporters))

    async def shutdown(self) -> None:
        for exp in self._exporters:
            await exp.flush()
            await exp.close()
        self._started = False

    def add_exporter(self, exporter: BaseExporter) -> None:
        self._exporters.append(exporter)

    # -- Session management ------------------------------------------------

    def start_session(
        self, *, agent_id: str = "", agent_version: str = "", framework: str = "",
        metadata: dict[str, Any] | None = None, tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> VoiceSession:
        session = VoiceSession(
            agent_id=agent_id, agent_version=agent_version, framework=framework,
            metadata=metadata or {}, tags=tags or [],
            **({"session_id": session_id} if session_id else {}),
        )
        self._sessions[session.session_id] = session
        self._session_turns[session.session_id] = []
        return session

    async def end_session(
        self, session_id: str, *, status: SessionStatus = SessionStatus.COMPLETED,
        task_completed: bool | None = None, ended_reason: str = "",
        cost_usd: float | None = None, metadata: dict[str, Any] | None = None,
    ) -> VoiceSession | None:
        session = self._sessions.get(session_id)
        if not session:
            return None
        now = datetime.now(timezone.utc)
        session.ended_at = now
        session.status = status
        session.duration_ms = (now - session.started_at).total_seconds() * 1000
        session.total_turns = len(self._session_turns.get(session_id, []))
        session.total_cost_usd = cost_usd
        if metadata:
            session.metadata.update(metadata)

        outcome = OutcomeEvent(session_id=session_id, task_completed=task_completed,
                               ended_reason=ended_reason, cost_usd=cost_usd)
        await self._emit("outcomes", outcome.model_dump())
        await self._emit("sessions", session.model_dump())
        return session

    # -- Turn management ---------------------------------------------------

    def start_turn(
        self, session_id: str, *, speaker: Speaker = Speaker.USER,
        turn_number: int | None = None, metadata: dict[str, Any] | None = None,
    ) -> Turn:
        existing = self._session_turns.get(session_id, [])
        num = turn_number if turn_number is not None else len(existing) + 1
        turn = Turn(session_id=session_id, turn_number=num,
                    speaker=speaker, metadata=metadata or {})
        self._turns[turn.turn_id] = turn
        self._turn_reports[turn.turn_id] = TurnReport(turn=turn)
        self._session_turns.setdefault(session_id, []).append(turn.turn_id)
        return turn

    async def end_turn(
        self, turn_id: str, *, was_interrupted: bool = False, transcript: str = "",
    ) -> Turn | None:
        turn = self._turns.get(turn_id)
        if not turn:
            return None
        now = datetime.now(timezone.utc)
        turn.ended_at = now
        turn.duration_ms = (now - turn.started_at).total_seconds() * 1000
        turn.was_interrupted = was_interrupted
        if transcript:
            turn.transcript = transcript
        report = self._turn_reports.get(turn_id)
        if report:
            await self._emit("turns", report.model_dump())
        return turn

    # -- Layer 1: Infrastructure -------------------------------------------

    async def record_infra(
        self, session_id: str, *, jitter_ms: float | None = None,
        packet_loss_pct: float | None = None, mos_score: float | None = None,
        rtt_ms: float | None = None, audio_codec: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> InfraEvent:
        event = InfraEvent(session_id=session_id, jitter_ms=jitter_ms,
                           packet_loss_pct=packet_loss_pct, mos_score=mos_score,
                           rtt_ms=rtt_ms, audio_codec=audio_codec,
                           metadata=metadata or {})
        await self._emit("infra", event.model_dump())
        return event

    # -- Layer 2: Execution ------------------------------------------------

    async def record_stt(
        self, session_id: str, turn_id: str = "", *, transcript: str = "",
        is_final: bool = True, confidence: float | None = None,
        language: str | None = None, latency_ms: float | None = None,
        audio_duration_ms: float | None = None, provider: str = "",
        model: str = "", streamed: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> STTEvent:
        event = STTEvent(
            session_id=session_id, turn_id=turn_id, transcript=transcript,
            is_final=is_final, confidence=confidence, language=language,
            latency_ms=latency_ms, audio_duration_ms=audio_duration_ms,
            provider=provider, model=model, streamed=streamed,
            metadata=metadata or {},
        )
        if turn_id and turn_id in self._turn_reports:
            self._turn_reports[turn_id].stt = event
        await self._emit("stt", event.model_dump())
        return event

    async def record_llm(
        self, session_id: str, turn_id: str = "", *, model: str = "",
        provider: str = "", ttft_ms: float | None = None,
        duration_ms: float | None = None, prompt_tokens: int = 0,
        completion_tokens: int = 0, total_tokens: int = 0,
        cached_tokens: int = 0, tokens_per_second: float | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        cancelled: bool = False, cost_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMEvent:
        tc = [ToolCallEvent(**t) for t in tool_calls] if tool_calls else []
        event = LLMEvent(
            session_id=session_id, turn_id=turn_id, model=model, provider=provider,
            ttft_ms=ttft_ms, duration_ms=duration_ms, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens or (prompt_tokens + completion_tokens),
            cached_tokens=cached_tokens, tokens_per_second=tokens_per_second,
            tool_calls=tc, cancelled=cancelled, cost_usd=cost_usd,
            metadata=metadata or {},
        )
        if turn_id and turn_id in self._turn_reports:
            self._turn_reports[turn_id].llm = event
        await self._emit("llm", event.model_dump())
        return event

    async def record_tts(
        self, session_id: str, turn_id: str = "", *, text: str = "",
        provider: str = "", voice_id: str = "", ttfb_ms: float | None = None,
        duration_ms: float | None = None, audio_duration_ms: float | None = None,
        characters_count: int = 0, streamed: bool = False,
        cancelled: bool = False, cost_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TTSEvent:
        event = TTSEvent(
            session_id=session_id, turn_id=turn_id, text=text, provider=provider,
            voice_id=voice_id, ttfb_ms=ttfb_ms, duration_ms=duration_ms,
            audio_duration_ms=audio_duration_ms,
            characters_count=characters_count or len(text),
            streamed=streamed, cancelled=cancelled, cost_usd=cost_usd,
            metadata=metadata or {},
        )
        if turn_id and turn_id in self._turn_reports:
            self._turn_reports[turn_id].tts = event
        await self._emit("tts", event.model_dump())
        return event

    # -- Layer 3: UX -------------------------------------------------------

    async def record_ux(
        self, session_id: str, turn_id: str = "", *,
        e2e_latency_ms: float | None = None, ttfw_ms: float | None = None,
        interruption_count: int = 0, false_interruption_count: int = 0,
        was_barge_in: bool = False, silence_duration_ms: float | None = None,
        talk_ratio: float | None = None, frustration_score: float | None = None,
        sentiment: str | None = None, is_dialog_loop: bool = False,
        fallback_triggered: bool = False, metadata: dict[str, Any] | None = None,
    ) -> UXEvent:
        event = UXEvent(
            session_id=session_id, turn_id=turn_id, e2e_latency_ms=e2e_latency_ms,
            ttfw_ms=ttfw_ms, interruption_count=interruption_count,
            false_interruption_count=false_interruption_count,
            was_barge_in=was_barge_in, silence_duration_ms=silence_duration_ms,
            talk_ratio=talk_ratio, frustration_score=frustration_score,
            sentiment=sentiment, is_dialog_loop=is_dialog_loop,
            fallback_triggered=fallback_triggered, metadata=metadata or {},
        )
        if turn_id and turn_id in self._turn_reports:
            self._turn_reports[turn_id].ux = event
        await self._emit("ux", event.model_dump())
        return event

    # -- Layer 4: Outcome --------------------------------------------------

    async def record_outcome(
        self, session_id: str, *, task_completed: bool | None = None,
        task_name: str = "", turns_to_complete: int | None = None,
        escalated: bool = False, transferred: bool = False,
        compliance_pass: bool | None = None,
        compliance_violations: list[str] | None = None,
        cost_usd: float | None = None, ended_reason: str = "",
        containment_success: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutcomeEvent:
        event = OutcomeEvent(
            session_id=session_id, task_completed=task_completed,
            task_name=task_name, turns_to_complete=turns_to_complete,
            escalated=escalated, transferred=transferred,
            compliance_pass=compliance_pass,
            compliance_violations=compliance_violations or [],
            cost_usd=cost_usd, ended_reason=ended_reason,
            containment_success=containment_success, metadata=metadata or {},
        )
        await self._emit("outcomes", event.model_dump())
        return event

    # -- Internal ----------------------------------------------------------

    async def _emit(self, stream: str, event: dict[str, Any]) -> None:
        if not self._started:
            return
        for exporter in self._exporters:
            try:
                await exporter.export(stream, event)
            except Exception:
                logger.exception("Exporter %s failed", type(exporter).__name__)
