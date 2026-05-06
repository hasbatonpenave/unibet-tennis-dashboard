"""SSE Broadcaster — fan-out to subscriber queues."""

import asyncio
import logging

logger = logging.getLogger("unibet_server")


class Broadcaster:
    """Manages SSE subscriber queues with dead-client eviction."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def client_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event_json: str) -> None:
        dead: list[asyncio.Queue] = []
        for sub in list(self._subscribers):
            try:
                sub.put_nowait(event_json)
            except asyncio.QueueFull:
                dead.append(sub)
        for sub in dead:
            self._subscribers.discard(sub)
