"""In-process publish/subscribe hub feeding dashboard WebSocket clients.

Publishing never blocks the ingest path.  A client that cannot keep up does not slow
anyone down: its backlog is discarded and it is told to resynchronise.
"""

from __future__ import annotations

import asyncio
from typing import Any

RESYNC_MESSAGE: dict[str, Any] = {"type": "resync"}


class LiveHub:
    def __init__(self, max_queue: int = 256) -> None:
        self._max_queue = max_queue
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.published = 0
        self.overflows = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        self.published += 1
        for queue in self._subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.overflows += 1
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(RESYNC_MESSAGE)
