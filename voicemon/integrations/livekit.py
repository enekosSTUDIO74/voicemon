"""LiveKit Agents SDK integration — hooks into AgentSession events.

Usage:
    from livekit.agents import AgentSession
    from voicemon.integrations.livekit import instrument_livekit

    collector = VoiceMonCollector(...)
    session = AgentSession(...)
    instrument_livekit(session, collector, agent_id="my-agent")
"""

from __future__ import annotations

import logging
import time
from typing import Any

from voicemon.core.collector import VoiceMonCollector

logger = logging.getLogger("voicemon.integrations.livekit")


def instrument_livekit(
    agent_session: Any,  # livekit.agents.AgentSession
    collector: VoiceMonCollector,
    *,
    agent_id: str = "livekit-agent",
    agent_version: str = "0.1.0",
) -> str:
    """Attach VoiceMon hooks to a LiveKit AgentSession.

    Returns the VoiceMon session_id for reference.
    """
    session_id = collector.start_session(
        agent_id=agent_id,
        agent_version=agent_version,
        framework="livekit",
        metadata={"room_name": getattr(agent_session, "room_name", "")},
    )
    _state: dict[str, Any] = {
        "turn_active": False,
        "turn_start": 0.0,
        "user_speaking_start": 0.0,
    }

    # ── metrics_collected: primary pipeline latency source ──────────────
    @agent_session.on("metrics_collected")
    async def _on_metrics(metrics: Any) -> None:
        try:
            if hasattr(metrics, "stt_duration"):
                collector.record_stt(
                    session_id=session_id,
                    provider=getattr(metrics, "stt_provider", "deepgram"),
                    model=getattr(metrics, "stt_model", "nova-2"),
                    latency_ms=_ms(getattr(metrics, "stt_duration", 0)),
                    confidence=getattr(metrics, "stt_confidence", 0.0),
                    transcript=getattr(metrics, "transcript", ""),
                    is_final=getattr(metrics, "is_final", True),
                )

            if hasattr(metrics, "llm_ttft"):
                collector.record_llm(
                    session_id=session_id,
                    provider=getattr(metrics, "llm_provider", "openai"),
                    model=getattr(metrics, "llm_model", "gpt-4o"),
                    ttft_ms=_ms(getattr(metrics, "llm_ttft", 0)),
                    total_ms=_ms(getattr(metrics, "llm_duration", 0)),
                    input_tokens=getattr(metrics, "input_tokens", 0),
                    output_tokens=getattr(metrics, "output_tokens", 0),
                )

            if hasattr(metrics, "tts_ttfb"):
                collector.record_tts(
                    session_id=session_id,
                    provider=getattr(metrics, "tts_provider", "elevenlabs"),
                    ttfb_ms=_ms(getattr(metrics, "tts_ttfb", 0)),
                    total_ms=_ms(getattr(metrics, "tts_duration", 0)),
                    voice_id=getattr(metrics, "tts_voice_id", ""),
                )
        except Exception:
            logger.exception("Error processing LiveKit metrics")

    # ── User / Agent state transitions ──────────────────────────────────
    @agent_session.on("user_state_changed")
    async def _on_user_state(state: str) -> None:
        if state == "speaking":
            _state["user_speaking_start"] = time.monotonic()
            if not _state["turn_active"]:
                collector.start_turn(session_id=session_id, speaker="user")
                _state["turn_active"] = True
                _state["turn_start"] = time.monotonic()
        elif state == "listening" and _state["turn_active"]:
            pass  # turn continues into agent phase

    @agent_session.on("agent_state_changed")
    async def _on_agent_state(state: str) -> None:
        if state == "speaking" and not _state["turn_active"]:
            collector.start_turn(session_id=session_id, speaker="agent")
            _state["turn_active"] = True
            _state["turn_start"] = time.monotonic()
        elif state == "listening" and _state["turn_active"]:
            collector.end_turn(session_id=session_id)
            _state["turn_active"] = False

    # ── User transcript events ──────────────────────────────────────────
    @agent_session.on("user_input_transcribed")
    async def _on_user_transcript(transcript: Any) -> None:
        text = transcript if isinstance(transcript, str) else getattr(transcript, "text", str(transcript))
        if text:
            logger.debug("User said: %s", text[:80])

    # ── Interruptions ───────────────────────────────────────────────────
    @agent_session.on("agent_speech_interrupted")
    async def _on_interruption(*_args: Any) -> None:
        collector.record_ux(
            session_id=session_id,
            event_type="interruption",
            metadata={"source": "user_barge_in"},
        )

    # ── Tool / function calls ───────────────────────────────────────────
    @agent_session.on("function_tools_executed")
    async def _on_tool(results: Any) -> None:
        if isinstance(results, (list, tuple)):
            for r in results:
                _record_tool(r)
        else:
            _record_tool(results)

    def _record_tool(r: Any) -> None:
        collector.record_llm(
            session_id=session_id,
            provider="tool_call",
            model=getattr(r, "function_name", "unknown"),
            ttft_ms=0.0,
            total_ms=_ms(getattr(r, "duration", 0)),
            metadata={
                "function_name": getattr(r, "function_name", ""),
                "success": getattr(r, "success", True),
            },
        )

    # ── Session close ───────────────────────────────────────────────────
    @agent_session.on("close")
    async def _on_close(*_args: Any) -> None:
        if _state["turn_active"]:
            collector.end_turn(session_id=session_id)
        collector.end_session(session_id=session_id)
        logger.info("VoiceMon session %s closed", session_id)

    logger.info("VoiceMon instrumented LiveKit session %s", session_id)
    return session_id


def _ms(seconds: float) -> float:
    """Convert seconds to milliseconds."""
    return round(seconds * 1000, 2) if seconds else 0.0
