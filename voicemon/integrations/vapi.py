"""Vapi integration — webhook handler + REST API client.

Usage with FastAPI:
    from fastapi import FastAPI
    from voicemon.integrations.vapi import create_vapi_router

    app = FastAPI()
    collector = VoiceMonCollector(...)
    app.include_router(create_vapi_router(collector))

Usage polling mode:
    from voicemon.integrations.vapi import VapiClient
    client = VapiClient(api_key="...", collector=collector)
    await client.poll_recent_calls(minutes=5)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from voicemon.core.collector import VoiceMonCollector

logger = logging.getLogger("voicemon.integrations.vapi")


class VapiWebhookHandler:
    """Processes Vapi server-message webhooks and maps them to VoiceMon events."""

    def __init__(self, collector: VoiceMonCollector, *, agent_id: str = "vapi-agent") -> None:
        self.collector = collector
        self.agent_id = agent_id
        self._active_calls: dict[str, str] = {}  # vapi_call_id → voicemon_session_id

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg_type = payload.get("message", {}).get("type", "")
        call = payload.get("message", {}).get("call", {})
        call_id = call.get("id", payload.get("message", {}).get("call_id", ""))

        handler = self._handlers.get(msg_type)
        if handler:
            try:
                return await handler(self, call_id, call, payload.get("message", {}))
            except Exception:
                logger.exception("Error handling Vapi webhook %s", msg_type)
                return {"status": "error"}
        return {"status": "ignored", "type": msg_type}

    async def _on_status_update(self, call_id: str, call: dict, msg: dict) -> dict:
        status = msg.get("status", "")

        if status == "in-progress" and call_id not in self._active_calls:
            sid = self.collector.start_session(
                agent_id=self.agent_id, framework="vapi",
                metadata={"call_id": call_id, "phone_number": call.get("phoneNumber", ""),
                           "assistant_id": call.get("assistantId", "")},
            )
            self._active_calls[call_id] = sid
            return {"status": "ok", "session_id": sid}

        elif status == "ended" and call_id in self._active_calls:
            sid = self._active_calls.pop(call_id)
            self.collector.end_session(session_id=sid)
            return {"status": "ok", "session_id": sid}

        return {"status": "ok"}

    async def _on_transcript(self, call_id: str, call: dict, msg: dict) -> dict:
        sid = self._active_calls.get(call_id) or self._ensure_session(call_id, call)
        role = msg.get("role", "user")
        transcript = msg.get("transcript", "")
        transcript_type = msg.get("transcriptType", "final")

        self.collector.record_stt(
            session_id=sid, provider="vapi", model="default",
            latency_ms=0.0, confidence=1.0,
            transcript=transcript,
            is_final=(transcript_type == "final"),
            metadata={"role": role},
        )
        return {"status": "ok"}

    async def _on_speech_update(self, call_id: str, call: dict, msg: dict) -> dict:
        sid = self._active_calls.get(call_id) or self._ensure_session(call_id, call)
        status = msg.get("status", "")

        if status == "started" and msg.get("role") == "user":
            self.collector.start_turn(session_id=sid, speaker="user")
        elif status == "stopped" and msg.get("role") == "assistant":
            self.collector.end_turn(session_id=sid)

        return {"status": "ok"}

    async def _on_end_of_call_report(self, call_id: str, call: dict, msg: dict) -> dict:
        sid = self._active_calls.pop(call_id, None)
        if not sid:
            sid = self._ensure_session(call_id, call)

        # Extract rich end-of-call metrics
        cost = msg.get("cost", 0.0)
        duration_s = msg.get("durationSeconds", 0)
        ended_reason = msg.get("endedReason", "")
        summary = msg.get("summary", "")
        analysis = msg.get("analysis", {})

        self.collector.record_outcome(
            session_id=sid,
            task_success=analysis.get("successEvaluation", "") == "true",
            resolution_type=ended_reason,
            metadata={
                "cost_usd": cost,
                "duration_s": duration_s,
                "summary": summary,
                "analysis": analysis,
                "ended_reason": ended_reason,
            },
        )
        self.collector.end_session(session_id=sid)
        return {"status": "ok"}

    async def _on_tool_calls(self, call_id: str, call: dict, msg: dict) -> dict:
        sid = self._active_calls.get(call_id) or self._ensure_session(call_id, call)
        tool_calls = msg.get("toolCalls", msg.get("toolCallList", []))
        for tc in (tool_calls if isinstance(tool_calls, list) else [tool_calls]):
            fn_name = tc.get("function", {}).get("name", "unknown")
            self.collector.record_llm(
                session_id=sid, provider="tool_call", model=fn_name,
                ttft_ms=0, total_ms=0,
                metadata={"tool_call": tc},
            )
        return {"status": "ok"}

    async def _on_hang(self, call_id: str, call: dict, msg: dict) -> dict:
        sid = self._active_calls.get(call_id)
        if sid:
            self.collector.record_ux(
                session_id=sid, event_type="hang_notification",
                metadata=msg,
            )
        return {"status": "ok"}

    def _ensure_session(self, call_id: str, call: dict) -> str:
        """Create session on-demand if webhook arrived out of order."""
        if call_id in self._active_calls:
            return self._active_calls[call_id]
        sid = self.collector.start_session(
            agent_id=self.agent_id, framework="vapi",
            metadata={"call_id": call_id, "late_start": True},
        )
        self._active_calls[call_id] = sid
        return sid

    _handlers: dict[str, Any] = {
        "status-update": _on_status_update,
        "transcript": _on_transcript,
        "speech-update": _on_speech_update,
        "end-of-call-report": _on_end_of_call_report,
        "tool-calls": _on_tool_calls,
        "hang": _on_hang,
    }


class VapiClient:
    """REST client for Vapi's /call and /analytics endpoints (polling mode)."""

    BASE_URL = "https://api.vapi.ai"

    def __init__(
        self, *, api_key: str, collector: VoiceMonCollector,
        agent_id: str = "vapi-agent",
    ) -> None:
        self.api_key = api_key
        self.collector = collector
        self.agent_id = agent_id

    async def poll_recent_calls(self, *, minutes: int = 5) -> list[str]:
        """Pull recent call data from Vapi REST API and ingest into VoiceMon."""
        import httpx

        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        session_ids: list[str] = []

        async with httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        ) as client:
            resp = await client.get("/call", params={"createdAtGt": since, "limit": "100"})
            resp.raise_for_status()
            calls = resp.json()

            for call_data in calls:
                sid = self._ingest_call(call_data)
                session_ids.append(sid)

        logger.info("Polled %d Vapi calls", len(session_ids))
        return session_ids

    def _ingest_call(self, call: dict[str, Any]) -> str:
        sid = self.collector.start_session(
            agent_id=self.agent_id, framework="vapi",
            metadata={"call_id": call.get("id"), "source": "poll"},
        )

        # Ingest each message as a turn
        for msg in call.get("messages", []):
            role = msg.get("role", "user")
            self.collector.start_turn(session_id=sid, speaker=role)
            if msg.get("message"):
                self.collector.record_stt(
                    session_id=sid, provider="vapi", model="default",
                    latency_ms=0, confidence=1.0,
                    transcript=msg["message"], is_final=True,
                )
            self.collector.end_turn(session_id=sid)

        # Cost + outcome
        analysis = call.get("analysis", {})
        self.collector.record_outcome(
            session_id=sid,
            task_success=analysis.get("successEvaluation", "") == "true",
            resolution_type=call.get("endedReason", ""),
            metadata={
                "cost_usd": call.get("cost", 0),
                "duration_s": call.get("durationSeconds", 0),
                "summary": analysis.get("summary", ""),
            },
        )
        self.collector.end_session(session_id=sid)
        return sid

    async def get_call(self, call_id: str) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        ) as client:
            resp = await client.get(f"/call/{call_id}")
            resp.raise_for_status()
            return resp.json()


def create_vapi_router(
    collector: VoiceMonCollector, *, agent_id: str = "vapi-agent",
    webhook_secret: str | None = None,
) -> Any:
    """Create a FastAPI router for Vapi webhooks.

    Returns:
        fastapi.APIRouter with POST /vapi/webhook endpoint.
    """
    from fastapi import APIRouter, Header, HTTPException, Request

    router = APIRouter(prefix="/vapi", tags=["vapi"])
    handler = VapiWebhookHandler(collector, agent_id=agent_id)

    @router.post("/webhook")
    async def vapi_webhook(
        request: Request,
        x_vapi_secret: str | None = Header(None),
    ) -> dict[str, Any]:
        if webhook_secret and x_vapi_secret != webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        payload = await request.json()
        return await handler.handle_webhook(payload)

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "integration": "vapi"}

    return router
