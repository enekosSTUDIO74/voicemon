"""Tests for Prometheus exporter."""

from voicemon.exporters.prometheus import PrometheusExporter


class TestPrometheusExporter:
    def test_init(self):
        exp = PrometheusExporter(prefix="test_vm", port=9999)
        assert exp.prefix == "test_vm"
        assert not exp._initialized

    def test_export_stt(self):
        exp = PrometheusExporter(prefix="test_stt")
        # initialize() will fail gracefully if prometheus_client isn't installed
        try:
            exp.initialize()
            exp.export("stt", {
                "provider": "deepgram", "model": "nova-2",
                "latency_ms": 200, "confidence": 0.95,
                "agent_id": "test",
            })
        except ImportError:
            pass  # prometheus_client not installed in test env

    def test_export_llm(self):
        exp = PrometheusExporter(prefix="test_llm")
        try:
            exp.initialize()
            exp.export("llm", {
                "provider": "openai", "model": "gpt-4o",
                "ttft_ms": 300, "total_ms": 800,
                "input_tokens": 100, "output_tokens": 50,
                "agent_id": "test",
            })
        except ImportError:
            pass

    def test_export_unknown_type(self):
        exp = PrometheusExporter(prefix="test_unk")
        try:
            exp.initialize()
            exp.export("unknown_type", {"data": "test"})
        except ImportError:
            pass
