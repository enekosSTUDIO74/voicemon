"""Tests for the VoiceMonCollector."""

from voicemon.core.collector import VoiceMonCollector
from voicemon.exporters.base import ConsoleExporter


class TestCollector:
    def setup_method(self):
        self.exporter = ConsoleExporter()
        self.collector = VoiceMonCollector(exporters=[self.exporter])

    def test_start_end_session(self):
        sid = self.collector.start_session(agent_id="test", framework="livekit")
        assert sid in self.collector._sessions
        self.collector.end_session(session_id=sid)
        assert sid not in self.collector._sessions

    def test_start_end_turn(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.start_turn(session_id=sid, speaker="user")
        session = self.collector._sessions[sid]
        assert session["turn_count"] == 1
        self.collector.end_turn(session_id=sid)
        self.collector.end_session(session_id=sid)

    def test_record_stt(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.record_stt(
            session_id=sid, provider="deepgram", model="nova-2",
            latency_ms=200, confidence=0.95, transcript="hello",
        )
        self.collector.end_session(session_id=sid)

    def test_record_llm(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.record_llm(
            session_id=sid, provider="openai", model="gpt-4o",
            ttft_ms=300, total_ms=800,
            input_tokens=100, output_tokens=50,
        )
        self.collector.end_session(session_id=sid)

    def test_record_tts(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.record_tts(
            session_id=sid, provider="elevenlabs",
            ttfb_ms=180, total_ms=500,
        )
        self.collector.end_session(session_id=sid)

    def test_record_ux(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.record_ux(
            session_id=sid, event_type="interruption",
        )
        self.collector.end_session(session_id=sid)

    def test_record_outcome(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.record_outcome(
            session_id=sid, task_success=True,
            resolution_type="completed",
        )
        self.collector.end_session(session_id=sid)

    def test_record_infra(self):
        sid = self.collector.start_session(agent_id="test")
        self.collector.record_infra(
            session_id=sid, jitter_ms=10,
            packet_loss_pct=0.1, mos_score=4.3,
        )
        self.collector.end_session(session_id=sid)

    def test_multiple_exporters(self):
        ex2 = ConsoleExporter()
        collector = VoiceMonCollector(exporters=[self.exporter, ex2])
        sid = collector.start_session(agent_id="test")
        collector.record_stt(
            session_id=sid, provider="deepgram", model="nova",
            latency_ms=100, confidence=0.9,
        )
        collector.end_session(session_id=sid)

    def test_invalid_session(self):
        # Should not raise, just log warning
        self.collector.record_stt(
            session_id="nonexistent", provider="x", model="x",
            latency_ms=100, confidence=0.5,
        )
        self.collector.end_session(session_id="nonexistent")
