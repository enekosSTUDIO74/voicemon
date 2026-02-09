"""PagerDuty notifier — Events API v2 for P0/P1 incident escalation.

Triggers PagerDuty incidents for critical and P0 voice pipeline alerts.
Uses the Events API v2 (https://developer.pagerduty.com/docs/events-api-v2/).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("voicemon.alerts.pagerduty")

PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

SEVERITY_MAP = {"info": "info", "warning": "warning", "critical": "critical", "p0": "critical"}


class PagerDutyNotifier:
    """Trigger and manage PagerDuty incidents via Events API v2."""

    def __init__(self, *, routing_key: str, service_name: str = "VoiceMon") -> None:
        self.routing_key = routing_key
        self.service_name = service_name

    async def trigger_incident(self, alert: dict[str, Any]) -> str | None:
        """Create a PagerDuty incident. Returns dedup_key on success."""
        severity = alert.get("severity", "critical")
        dedup_key = f"voicemon-{alert.get('rule_name', 'unknown')}-{alert.get('agent_id', '')}"

        payload = {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": f"[VoiceMon] {alert.get('rule_name', 'Alert')}: {alert.get('message', '')}",
                "source": self.service_name,
                "severity": SEVERITY_MAP.get(severity, "critical"),
                "component": "voice-pipeline",
                "group": alert.get("agent_id", "default"),
                "class": alert.get("rule_name", "voice_alert"),
                "custom_details": {
                    "value": alert.get("value", 0),
                    "threshold": alert.get("threshold", 0),
                    "session_id": alert.get("session_id", ""),
                    "agent_id": alert.get("agent_id", ""),
                    "severity": severity,
                },
            },
            "links": [],
            "images": [],
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(PD_EVENTS_URL, json=payload)
                resp.raise_for_status()
                result = resp.json()
            logger.info("PagerDuty incident triggered: %s (dedup=%s)", result.get("message"), dedup_key)
            return dedup_key
        except Exception:
            logger.exception("Failed to trigger PagerDuty incident")
            return None

    async def acknowledge_incident(self, dedup_key: str) -> bool:
        """Acknowledge a previously triggered incident."""
        return await self._send_event("acknowledge", dedup_key)

    async def resolve_incident(self, dedup_key: str) -> bool:
        """Resolve a previously triggered incident."""
        return await self._send_event("resolve", dedup_key)

    async def _send_event(self, action: str, dedup_key: str) -> bool:
        payload = {
            "routing_key": self.routing_key,
            "event_action": action,
            "dedup_key": dedup_key,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(PD_EVENTS_URL, json=payload)
                resp.raise_for_status()
            logger.info("PagerDuty event %s sent for %s", action, dedup_key)
            return True
        except Exception:
            logger.exception("Failed to %s PagerDuty incident %s", action, dedup_key)
            return False
