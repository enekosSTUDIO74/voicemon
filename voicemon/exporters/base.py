"""Exporter interface — all exporters implement this protocol."""

from __future__ import annotations

import abc
from typing import Any


class BaseExporter(abc.ABC):
    @abc.abstractmethod
    async def export(self, stream: str, event: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    async def flush(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class ConsoleExporter(BaseExporter):
    def __init__(self, *, verbose: bool = False) -> None:
        self._verbose = verbose

    async def export(self, stream: str, event: dict[str, Any]) -> None:
        if self._verbose:
            import json
            print(f"[voicemon:{stream}] {json.dumps(event, default=str, indent=2)}")
        else:
            eid = event.get("event_id", event.get("turn_id", "?"))[:8]
            print(f"[voicemon:{stream}] {eid}")

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        pass
