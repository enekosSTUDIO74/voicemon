"""Alert engine — rule-based alerting with severity routing and cooldowns.

Loads rules from YAML config, evaluates them against incoming events,
routes notifications to Slack (ChatOps) and PagerDuty (P0/P1 escalation).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from voicemon.core.config import VoiceMonConfig

logger = logging.getLogger("voicemon.alerts.engine")

DEFAULT_RULES_PATH = Path(__file__).parent.parent / "alert_rules.yaml"


class AlertRule:
    """A single alert rule parsed from YAML config."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.name: str = data["name"]
        self.description: str = data.get("description", "")
        self.metric: str = data["metric"]  # e.g. "stt_latency_ms"
        self.operator: str = data.get("operator", ">")  # >, <, >=, <=, ==
        self.threshold: float = data["threshold"]
        self.severity: str = data.get("severity", "warning")  # info|warning|critical|p0
        self.cooldown_minutes: int = data.get("cooldown_minutes", 5)
        self.notify: list[str] = data.get("notify", ["slack"])  # slack, pagerduty
        self.enabled: bool = data.get("enabled", True)
        self.tags: list[str] = data.get("tags", [])

    def evaluate(self, value: float) -> bool:
        ops = {">": lambda a, b: a > b, "<": lambda a, b: a < b,
               ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
               "==": lambda a, b: a == b}
        return ops.get(self.operator, ops[">"])(value, self.threshold)


class AlertEngine:
    """Evaluates alert rules and dispatches notifications."""

    def __init__(self, config: VoiceMonConfig, *, rules_path: Path | None = None) -> None:
        self.config = config
        self.rules_path = rules_path or DEFAULT_RULES_PATH
        self.rules: list[AlertRule] = []
        self._cooldowns: dict[str, float] = {}
        self._slack: Any = None
        self._pagerduty: Any = None

    async def initialize(self) -> None:
        """Load rules and init notification channels."""
        self._load_rules()

        if self.config.slack.webhook_url:
            try:
                from voicemon.alerts.slack import SlackNotifier
                self._slack = SlackNotifier(
                    webhook_url=str(self.config.slack.webhook_url),
                    channel=self.config.slack.channel,
                    mention_users=self.config.slack.mention_users,
                )
                logger.info("Slack notifier initialized")
            except ImportError:
                logger.warning("Slack notifier not available")

        if self.config.pagerduty.routing_key:
            try:
                from voicemon.alerts.pagerduty import PagerDutyNotifier
                self._pagerduty = PagerDutyNotifier(
                    routing_key=self.config.pagerduty.routing_key,
                    service_name=self.config.pagerduty.service_name,
                )
                logger.info("PagerDuty notifier initialized")
            except ImportError:
                logger.warning("PagerDuty notifier not available")

    def _load_rules(self) -> None:
        """Load alert rules from YAML file."""
        if not self.rules_path.exists():
            logger.warning("No alert rules file at %s, using defaults", self.rules_path)
            self.rules = self._default_rules()
            return
        try:
            with open(self.rules_path) as f:
                data = yaml.safe_load(f)
            self.rules = [AlertRule(r) for r in data.get("rules", []) if r.get("enabled", True)]
            logger.info("Loaded %d alert rules from %s", len(self.rules), self.rules_path)
        except Exception:
            logger.exception("Failed to load rules, using defaults")
            self.rules = self._default_rules()

    def _default_rules(self) -> list[AlertRule]:
        """Hardcoded fallback rules."""
        return [
            AlertRule({"name": "stt_latency_critical", "metric": "stt_latency_ms",
                       "threshold": 1000, "severity": "critical", "notify": ["slack", "pagerduty"]}),
            AlertRule({"name": "llm_ttft_critical", "metric": "llm_ttft_ms",
                       "threshold": 2000, "severity": "critical", "notify": ["slack", "pagerduty"]}),
            AlertRule({"name": "e2e_latency_p0", "metric": "e2e_latency_ms",
                       "threshold": 1800, "severity": "p0", "notify": ["slack", "pagerduty"]}),
            AlertRule({"name": "stt_confidence_low", "metric": "stt_confidence",
                       "operator": "<", "threshold": 0.7, "severity": "warning", "notify": ["slack"]}),
        ]

    async def notify(self, alert: dict[str, Any]) -> None:
        """Route alert notification to configured channels."""
        severity = alert.get("severity", "warning")

        # Always log
        logger.warning(
            "[ALERT] %s [%s]: %s (value=%.2f, threshold=%.2f)",
            alert.get("rule_name", "unknown"), severity,
            alert.get("message", ""), alert.get("value", 0), alert.get("threshold", 0),
        )

        # Slack — all severities
        if self._slack:
            try:
                await self._slack.send_alert(alert)
            except Exception:
                logger.exception("Slack notification failed")

        # PagerDuty — critical and p0 only
        if self._pagerduty and severity in ("critical", "p0"):
            try:
                await self._pagerduty.trigger_incident(alert)
            except Exception:
                logger.exception("PagerDuty notification failed")

    async def evaluate_metric(
        self, metric_name: str, value: float, context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate a metric value against all matching rules. Returns fired alerts."""
        fired: list[dict[str, Any]] = []
        now = time.monotonic()

        for rule in self.rules:
            if rule.metric != metric_name:
                continue
            if not rule.evaluate(value):
                continue

            # Cooldown check
            cooldown_key = f"{rule.name}:{(context or {}).get('agent_id', '')}"
            if cooldown_key in self._cooldowns:
                if now - self._cooldowns[cooldown_key] < rule.cooldown_minutes * 60:
                    continue

            self._cooldowns[cooldown_key] = now
            alert = {
                "rule_name": rule.name,
                "severity": rule.severity,
                "message": f"{rule.description or rule.name}: {metric_name}={value:.2f} "
                           f"{rule.operator} {rule.threshold}",
                "value": value,
                "threshold": rule.threshold,
                **(context or {}),
            }
            await self.notify(alert)
            fired.append(alert)

        return fired
