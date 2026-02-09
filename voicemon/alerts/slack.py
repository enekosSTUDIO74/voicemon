"""Slack notifier — ChatOps alerts with severity-based formatting.

Posts rich Block Kit messages to Slack via incoming webhooks.
Severity routing: info→ #voicemon-info, warning/critical→ #voicemon-alerts, p0→ @oncall
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("voicemon.alerts.slack")

SEVERITY_EMOJI = {"info": ":information_source:", "warning": ":warning:",
                  "critical": ":rotating_light:", "p0": ":fire:"}
SEVERITY_COLOR = {"info": "#36a64f", "warning": "#daa520", "critical": "#ff4444", "p0": "#cc0000"}


class SlackNotifier:
    """Send alert notifications to Slack via incoming webhook."""

    def __init__(
        self, *, webhook_url: str, channel: str = "#voicemon-alerts",
        mention_users: list[str] | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.channel = channel
        self.mention_users = mention_users or []

    async def send_alert(self, alert: dict[str, Any]) -> bool:
        """Post a rich alert message to Slack."""
        severity = alert.get("severity", "warning")
        emoji = SEVERITY_EMOJI.get(severity, ":bell:")
        color = SEVERITY_COLOR.get(severity, "#888888")

        # Build mention string for critical/p0
        mentions = ""
        if severity in ("critical", "p0") and self.mention_users:
            mentions = " ".join(f"<@{u}>" for u in self.mention_users) + " "

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} VoiceMon Alert: {alert.get('rule_name', 'Unknown')}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity:* `{severity.upper()}`"},
                    {"type": "mrkdwn", "text": f"*Agent:* `{alert.get('agent_id', 'N/A')}`"},
                    {"type": "mrkdwn", "text": f"*Value:* `{alert.get('value', 0):.2f}`"},
                    {"type": "mrkdwn", "text": f"*Threshold:* `{alert.get('threshold', 0):.2f}`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{mentions}{alert.get('message', 'No details')}",
                },
            },
        ]

        if alert.get("session_id"):
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Session: `{alert['session_id']}`"},
                ],
            })

        payload = {
            "channel": self.channel,
            "username": "VoiceMon",
            "icon_emoji": ":studio_microphone:",
            "attachments": [{"color": color, "blocks": blocks}],
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
            logger.info("Slack alert sent: %s [%s]", alert.get("rule_name"), severity)
            return True
        except Exception:
            logger.exception("Failed to send Slack alert")
            return False

    async def send_message(self, text: str, *, channel: str | None = None) -> bool:
        """Send a plain text message to Slack."""
        payload = {
            "channel": channel or self.channel,
            "text": text,
            "username": "VoiceMon",
            "icon_emoji": ":studio_microphone:",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to send Slack message")
            return False
