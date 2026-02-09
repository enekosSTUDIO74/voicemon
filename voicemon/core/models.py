"""Canonical data models for voice agent observability events.

These models map to the 4-Layer Voice Observability Framework:
  Layer 1 — Infrastructure: InfraEvent (audio quality, network)
  Layer 2 — Execution:      STTEvent, LLMEvent, TTSEvent, ToolCallEvent (agent behavior)
  Layer 3 — User Experience: UXEvent (conversation quality)
  Layer 4 — Outcome:         OutcomeEvent (business / compliance impact)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class Speaker(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# Layer 0 — Session & Turn (structural envelope)
# ---------------------------------------------------------------------------

class VoiceSession(BaseModel):
    session_id: str = Field(default_factory=_new_id)
    agent_id: str = ""
    agent_version: str = ""
    framework: str = ""
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    duration_ms: float | None = None
    total_turns: int = 0
    total_cost_usd: float | None = None


class Turn(BaseModel):
    turn_id: str = Field(default_factory=_new_id)
    session_id: str
    turn_number: int = 0
    speaker: Speaker = Speaker.USER
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None
    duration_ms: float | None = None
    was_interrupted: bool = False
    transcript: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


# Layer 1 — Infrastructure
class InfraEvent(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    session_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    jitter_ms: float | None = None
    packet_loss_pct: float | None = None
    mos_score: float | None = None
    rtt_ms: float | None = None
    audio_codec: str | None = None
    sample_rate_hz: int | None = None
    bitrate_kbps: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# Layer 2 — Execution
class ToolCallEvent(BaseModel):
    tool_call_id: str = Field(default_factory=_new_id)
    function_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    success: bool = True
    latency_ms: float | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class STTEvent(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)
    transcript: str = ""
    is_final: bool = True
    confidence: float | None = None
    language: str | None = None
    latency_ms: float | None = None
    audio_duration_ms: float | None = None
    streamed: bool = False
    word_error_rate: float | None = None
    word_confidences: list[float] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMEvent(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)
    model: str = ""
    provider: str = ""
    ttft_ms: float | None = None
    duration_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    tokens_per_second: float | None = None
    tool_calls: list[ToolCallEvent] = Field(default_factory=list)
    cancelled: bool = False
    prompt_compliant: bool | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TTSEvent(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)
    text: str = ""
    provider: str = ""
    voice_id: str = ""
    ttfb_ms: float | None = None
    duration_ms: float | None = None
    audio_duration_ms: float | None = None
    characters_count: int = 0
    streamed: bool = False
    cancelled: bool = False
    segment_id: str = ""
    speech_id: str = ""
    cost_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# Layer 3 — User Experience
class UXEvent(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    session_id: str
    turn_id: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)
    e2e_latency_ms: float | None = None
    ttfw_ms: float | None = None
    interruption_count: int = 0
    false_interruption_count: int = 0
    was_barge_in: bool = False
    silence_duration_ms: float | None = None
    talk_ratio: float | None = None
    frustration_score: float | None = None
    sentiment: str | None = None
    is_dialog_loop: bool = False
    repeated_prompt_count: int = 0
    fallback_triggered: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# Layer 4 — Outcome
class OutcomeEvent(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    session_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    task_completed: bool | None = None
    task_name: str = ""
    turns_to_complete: int | None = None
    escalated: bool = False
    transferred: bool = False
    transfer_target: str = ""
    compliance_pass: bool | None = None
    compliance_violations: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    revenue_impact_usd: float | None = None
    ended_reason: str = ""
    containment_success: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Aggregate types
# ---------------------------------------------------------------------------

class TurnReport(BaseModel):
    turn: Turn
    stt: STTEvent | None = None
    llm: LLMEvent | None = None
    tts: TTSEvent | None = None
    ux: UXEvent | None = None
    infra: InfraEvent | None = None


class SessionReport(BaseModel):
    session: VoiceSession
    turns: list[TurnReport] = Field(default_factory=list)
    outcome: OutcomeEvent | None = None

    @property
    def e2e_latency_p50(self) -> float | None:
        vals = sorted(
            t.ux.e2e_latency_ms for t in self.turns
            if t.ux and t.ux.e2e_latency_ms is not None
        )
        return vals[len(vals) // 2] if vals else None

    @property
    def total_interruptions(self) -> int:
        return sum(t.ux.interruption_count for t in self.turns if t.ux)

    @property
    def avg_stt_confidence(self) -> float | None:
        confs = [t.stt.confidence for t in self.turns if t.stt and t.stt.confidence is not None]
        return sum(confs) / len(confs) if confs else None
