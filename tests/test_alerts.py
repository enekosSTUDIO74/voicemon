"""Tests for the alerts engine."""

import pytest

from voicemon.alerts.engine import AlertEngine, AlertRule
from voicemon.core.config import VoiceMonConfig


class TestAlertRule:
    def test_gt_operator(self):
        rule = AlertRule({"name": "test", "metric": "stt_latency_ms", "threshold": 500})
        assert rule.evaluate(600)
        assert not rule.evaluate(400)

    def test_lt_operator(self):
        rule = AlertRule({"name": "test", "metric": "confidence",
                          "operator": "<", "threshold": 0.7})
        assert rule.evaluate(0.5)
        assert not rule.evaluate(0.8)

    def test_defaults(self):
        rule = AlertRule({"name": "test", "metric": "m", "threshold": 1})
        assert rule.severity == "warning"
        assert rule.cooldown_minutes == 5
        assert rule.enabled
        assert "slack" in rule.notify


class TestAlertEngine:
    def test_default_rules(self):
        config = VoiceMonConfig()
        engine = AlertEngine(config)
        engine._load_rules()
        assert len(engine.rules) >= 4

    @pytest.mark.asyncio
    async def test_evaluate_metric(self):
        config = VoiceMonConfig()
        engine = AlertEngine(config)
        engine.rules = [
            AlertRule({"name": "test_high", "metric": "stt_latency_ms",
                       "threshold": 500, "severity": "warning",
                       "cooldown_minutes": 0, "notify": []}),
        ]
        fired = await engine.evaluate_metric("stt_latency_ms", 600)
        assert len(fired) == 1
        assert fired[0]["rule_name"] == "test_high"

    @pytest.mark.asyncio
    async def test_no_alert_below_threshold(self):
        config = VoiceMonConfig()
        engine = AlertEngine(config)
        engine.rules = [
            AlertRule({"name": "test", "metric": "stt_latency_ms",
                       "threshold": 500, "cooldown_minutes": 0, "notify": []}),
        ]
        fired = await engine.evaluate_metric("stt_latency_ms", 300)
        assert len(fired) == 0
