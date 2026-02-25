"""
Structured run logger.

Writes to:
  1. SQLite run_logs table (persistent)
  2. JSONL file per run (persistent, flat)
  3. In-memory broadcast queue consumed by WebSocket clients
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from config import settings
import storage.db as db


# Global registry: run_id -> list of subscriber queues
_subscribers: dict[str, list[asyncio.Queue]] = {}


def subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.setdefault(run_id, []).append(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue):
    if run_id in _subscribers:
        try:
            _subscribers[run_id].remove(q)
        except ValueError:
            pass
        if not _subscribers[run_id]:
            del _subscribers[run_id]


def _broadcast(run_id: str, message: dict):
    for q in _subscribers.get(run_id, []):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass  # drop if client is too slow


class RunLogger:
    """
    Logger scoped to a single agent run.
    All methods are async to allow non-blocking DB writes.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._log_file = os.path.join(settings.log_path, f"{run_id}.jsonl")

    def _write_file(self, record: dict):
        """Append a JSONL record to the run log file (sync - cheap enough)."""
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def _emit(self, level: str, event: str, data: Any = None):
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "run_id": self.run_id,
            "level": level,
            "event": event,
            "data": data,
            "timestamp": now,
        }
        # 1. Persist to SQLite
        await db.append_log(self.run_id, level, event, data)
        # 2. Write to JSONL file
        self._write_file(record)
        # 3. Broadcast to live WebSocket subscribers
        _broadcast(self.run_id, record)

    async def info(self, event: str, data: Any = None):
        await self._emit("INFO", event, data)

    async def warn(self, event: str, data: Any = None):
        await self._emit("WARN", event, data)

    async def error(self, event: str, data: Any = None):
        await self._emit("ERROR", event, data)

    async def debug(self, event: str, data: Any = None):
        await self._emit("DEBUG", event, data)
