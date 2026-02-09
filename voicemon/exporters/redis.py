"""Redis Streams exporter — real-time event ingestion pipeline.

Events are published to Redis Streams for downstream consumers
(metrics worker, alert engine, dashboard). Consumer groups allow
horizontally-scaled processing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from voicemon.exporters.base import BaseExporter

logger = logging.getLogger("voicemon.exporters.redis")


class RedisStreamsExporter(BaseExporter):
    """Export voice telemetry events to Redis Streams."""

    def __init__(
        self, *, redis_url: str = "redis://localhost:6379/0",
        stream_prefix: str = "voicemon",
        consumer_group: str = "voicemon-workers",
        max_stream_len: int = 100_000,
    ) -> None:
        self.redis_url = redis_url
        self.stream_prefix = stream_prefix
        self.consumer_group = consumer_group
        self.max_stream_len = max_stream_len
        self._redis: Any = None

    async def connect(self) -> None:
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(
            self.redis_url, decode_responses=True, max_connections=20,
        )
        # Ensure consumer groups exist
        for stream in self._stream_names():
            try:
                await self._redis.xgroup_create(
                    stream, self.consumer_group, id="0", mkstream=True,
                )
            except Exception:
                pass  # group already exists
        logger.info("Redis Streams exporter connected: %s", self.redis_url)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis Streams exporter closed")

    def export(self, event_type: str, data: dict[str, Any]) -> None:
        """Synchronous export — wraps async _publish for non-async callers."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._publish(event_type, data))
        except RuntimeError:
            asyncio.run(self._publish(event_type, data))

    async def export_async(self, event_type: str, data: dict[str, Any]) -> str | None:
        """Async export to Redis Stream. Returns message ID."""
        return await self._publish(event_type, data)

    async def _publish(self, event_type: str, data: dict[str, Any]) -> str | None:
        if not self._redis:
            logger.warning("Redis not connected, dropping event %s", event_type)
            return None
        stream = f"{self.stream_prefix}:{event_type}"
        payload = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": json.dumps(data, default=str),
        }
        try:
            msg_id = await self._redis.xadd(
                stream, payload, maxlen=self.max_stream_len, approximate=True,
            )
            return msg_id
        except Exception:
            logger.exception("Failed to publish to Redis stream %s", stream)
            return None

    async def export_batch(self, events: list[tuple[str, dict[str, Any]]]) -> int:
        """Publish multiple events in a pipeline. Returns count of successes."""
        if not self._redis:
            return 0
        pipe = self._redis.pipeline(transaction=False)
        for event_type, data in events:
            stream = f"{self.stream_prefix}:{event_type}"
            payload = {
                "event_type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": json.dumps(data, default=str),
            }
            pipe.xadd(stream, payload, maxlen=self.max_stream_len, approximate=True)
        results = await pipe.execute()
        return sum(1 for r in results if r)

    # ── Consumer helpers ────────────────────────────────────────────────

    async def read_stream(
        self, event_type: str, *, consumer_name: str = "worker-1",
        count: int = 100, block_ms: int = 5000,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Read pending messages from a stream using consumer group."""
        if not self._redis:
            return []
        stream = f"{self.stream_prefix}:{event_type}"
        try:
            results = await self._redis.xreadgroup(
                self.consumer_group, consumer_name,
                {stream: ">"}, count=count, block=block_ms,
            )
            messages = []
            for _stream_name, stream_messages in results:
                for msg_id, fields in stream_messages:
                    data = json.loads(fields.get("data", "{}"))
                    messages.append((msg_id, data))
            return messages
        except Exception:
            logger.exception("Failed to read from stream %s", stream)
            return []

    async def ack(self, event_type: str, *msg_ids: str) -> int:
        """Acknowledge processed messages."""
        if not self._redis or not msg_ids:
            return 0
        stream = f"{self.stream_prefix}:{event_type}"
        return await self._redis.xack(stream, self.consumer_group, *msg_ids)

    async def get_pending_count(self, event_type: str) -> int:
        """Get number of pending (unacknowledged) messages in stream."""
        if not self._redis:
            return 0
        stream = f"{self.stream_prefix}:{event_type}"
        try:
            info = await self._redis.xpending(stream, self.consumer_group)
            return info.get("pending", 0) if isinstance(info, dict) else 0
        except Exception:
            return 0

    async def get_stream_length(self, event_type: str) -> int:
        if not self._redis:
            return 0
        stream = f"{self.stream_prefix}:{event_type}"
        return await self._redis.xlen(stream)

    def _stream_names(self) -> list[str]:
        return [
            f"{self.stream_prefix}:{t}"
            for t in ("session", "turn", "infra", "stt", "llm", "tts", "ux", "outcome")
        ]
