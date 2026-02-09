"""Tests for core data models."""

from voicemon.core.models import (
    InfraEvent, LLMEvent, OutcomeEvent, SessionReport, STTEvent,
    TTSEvent, Turn, TurnReport, UXEvent, VoiceSession,
)


class TestVoiceSession:
    def test_create_session(self):
        s = VoiceSession(agent_id="test-agent", framework="livekit")
        assert s.agent_id == "test-agent"
        assert s.framework == "livekit"
        assert s.status == "active"
        assert s.session_id

    def test_session_defaults(self):
        s = VoiceSession(agent_id="a")
        assert s.metadata == {}
        assert s.agent_version == ""


class TestTurn:
    def test_create_turn(self):
        t = Turn(session_id="s1", turn_number=1, speaker="user")
        assert t.speaker == "user"
        assert t.turn_number == 1
        assert not t.interrupted


class TestTurnReport:
    def test_e2e_latency(self):
        tr = TurnReport(
            turn=Turn(session_id="s1", turn_number=1, speaker="user"),
            stt_events=[STTEvent(session_id="s1", provider="deepgram", latency_ms=150, confidence=0.95)],
            llm_events=[LLMEvent(session_id="s1", provider="openai", model="gpt-4o", ttft_ms=300)],
            tts_events=[TTSEvent(session_id="s1", provider="elevenlabs", ttfb_ms=180)],
        )
        assert tr.e2e_latency_ms == 630  # 150 + 300 + 180

    def test_e2e_latency_empty(self):
        tr = TurnReport(
            turn=Turn(session_id="s1", turn_number=1, speaker="user"),
        )
        assert tr.e2e_latency_ms == 0


class TestSessionReport:
    def test_aggregated_metrics(self):
        turns = [
            TurnReport(
                turn=Turn(session_id="s1", turn_number=i, speaker="user"),
                stt_events=[
                    STTEvent(session_id="s1", provider="deepgram",
                             latency_ms=100 + i * 50, confidence=0.9 - i * 0.05)
                ],
                ux_events=[UXEvent(session_id="s1", event_type="interruption")]
                if i == 1 else [],
            )
            for i in range(3)
        ]
        report = SessionReport(
            session=VoiceSession(agent_id="a"),
            turns=turns,
        )
        assert report.total_turns == 3
        assert report.total_interruptions == 1
        assert 0.7 < report.avg_stt_confidence < 0.95

    def test_empty_report(self):
        report = SessionReport(
            session=VoiceSession(agent_id="a"),
            turns=[],
        )
        assert report.total_turns == 0
        assert report.total_interruptions == 0
        assert report.avg_stt_confidence == 0


class TestEvents:
    def test_stt_event(self):
        e = STTEvent(session_id="s", provider="deepgram", latency_ms=200, confidence=0.92)
        assert e.provider == "deepgram"

    def test_llm_event_tokens(self):
        e = LLMEvent(session_id="s", provider="openai", model="gpt-4o",
                     ttft_ms=500, input_tokens=100, output_tokens=50)
        assert e.input_tokens == 100

    def test_tts_event(self):
        e = TTSEvent(session_id="s", provider="elevenlabs", ttfb_ms=150)
        assert e.ttfb_ms == 150

    def test_infra_event(self):
        e = InfraEvent(session_id="s", jitter_ms=15, mos_score=4.2)
        assert e.mos_score == 4.2

    def test_ux_event(self):
        e = UXEvent(session_id="s", event_type="interruption")
        assert e.event_type == "interruption"

    def test_outcome_event(self):
        e = OutcomeEvent(session_id="s", task_success=True, resolution_type="completed")
        assert e.task_success
