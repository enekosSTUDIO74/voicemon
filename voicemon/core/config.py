"""VoiceMon configuration — YAML-driven, environment-aware."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379"
    stream_prefix: str = "voicemon"
    max_stream_length: int = 100_000
    consumer_group: str = "voicemon-workers"


class TimescaleConfig(BaseModel):
    dsn: str = "postgresql://voicemon:voicemon@localhost:5432/voicemon"
    pool_min: int = 2
    pool_max: int = 10
    raw_retention_days: int = 30
    aggregate_retention_days: int = 365


class PrometheusConfig(BaseModel):
    enabled: bool = True
    port: int = 9464


class SlackConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""
    channels: dict[str, str] = Field(default_factory=lambda: {
        "P0": "#voice-oncall",
        "P1": "#voice-oncall",
        "P2": "#voice-alerts",
        "P3": "#voice-metrics",
    })
    cooldown_seconds: int = 900


class PagerDutyConfig(BaseModel):
    enabled: bool = False
    routing_key: str = ""
    severity_map: dict[str, str] = Field(default_factory=lambda: {
        "P0": "critical",
        "P1": "error",
        "P2": "warning",
        "P3": "info",
    })


class AlertConfig(BaseModel):
    rules_file: str = "alert_rules.yaml"
    slack: SlackConfig = Field(default_factory=SlackConfig)
    pagerduty: PagerDutyConfig = Field(default_factory=PagerDutyConfig)


class OTelConfig(BaseModel):
    enabled: bool = True
    service_name: str = "voicemon"
    exporter: str = "console"
    endpoint: str = "http://localhost:4317"


class VoiceMonConfig(BaseModel):
    redis: RedisConfig = Field(default_factory=RedisConfig)
    timescale: TimescaleConfig = Field(default_factory=TimescaleConfig)
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    otel: OTelConfig = Field(default_factory=OTelConfig)
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    thresholds: dict[str, Any] = Field(default_factory=lambda: {
        "e2e_latency_ok_ms": 800,
        "e2e_latency_warn_ms": 1200,
        "e2e_latency_critical_ms": 1800,
        "stt_latency_ok_ms": 200,
        "stt_latency_warn_ms": 350,
        "llm_ttft_ok_ms": 600,
        "llm_ttft_warn_ms": 1000,
        "tts_ttfb_ok_ms": 150,
        "tts_ttfb_warn_ms": 250,
        "jitter_ok_ms": 30,
        "jitter_warn_ms": 50,
        "packet_loss_ok_pct": 0.5,
        "packet_loss_warn_pct": 1.0,
        "mos_ok": 4.0,
        "mos_warn": 3.5,
        "asr_confidence_ok": 0.90,
        "asr_confidence_warn": 0.85,
    })

    @classmethod
    def from_yaml(cls, path: str) -> VoiceMonConfig:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
