"""Event bus: every event goes to WebSocket subscribers and a per-review JSONL log."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

from backend.config import settings
from backend.models import Event

_TOKEN = re.compile(
    r"(ghp_|gho_|ghs_|ghu_|github_pat_|sk-ant-|sk-|AIza|xox[abp]-)[A-Za-z0-9_\-]{8,}"
)
_MAX_STR = 200


def _mask(match: re.Match[str]) -> str:
    s = match.group(0)
    return f"{s[:3]}…{s[-3:]}"


def redact(value: Any) -> Any:
    """Mask anything token-shaped and truncate long strings (e.g. file contents)."""
    if isinstance(value, str):
        value = _TOKEN.sub(_mask, value)
        if len(value) > _MAX_STR:
            value = value[:_MAX_STR] + f"… (+{len(value) - _MAX_STR} chars)"
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self.history: deque[Event] = deque(maxlen=3000)

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=5000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def emit(self, event: Event) -> Event:
        self.history.append(event)
        if event.review_id:
            settings.runs_dir.mkdir(exist_ok=True)
            with open(settings.runs_dir / f"{event.review_id}.jsonl", "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(q)
        return event

    def clear(self) -> None:
        self.history.clear()


bus = EventBus()
