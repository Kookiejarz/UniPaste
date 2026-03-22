from collections import deque
from dataclasses import dataclass
import time


@dataclass
class AppliedClipboardEvent:
    event_id: str | None
    origin_device_id: str | None
    kind: str
    fingerprint: str
    expected_change_count: int | None
    created_at: float
    consumed: bool = False


class ClipboardLoopGuard:
    """Tracks clipboard writes we performed so their immediate echo can be ignored once."""

    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 64):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._events = deque()

    def _prune(self):
        cutoff = time.time() - self.ttl_seconds
        while self._events and (
            self._events[0].created_at < cutoff or self._events[0].consumed
        ):
            self._events.popleft()

    def register(
        self,
        *,
        kind: str,
        fingerprint: str | None,
        expected_change_count: int | None,
        event_id: str | None = None,
        origin_device_id: str | None = None,
    ):
        if not fingerprint:
            return

        self._prune()
        self._events.append(
            AppliedClipboardEvent(
                event_id=event_id,
                origin_device_id=origin_device_id,
                kind=kind,
                fingerprint=fingerprint,
                expected_change_count=expected_change_count,
                created_at=time.time(),
            )
        )
        while len(self._events) > self.max_entries:
            self._events.popleft()

    def consume_if_expected(
        self,
        *,
        kind: str,
        fingerprint: str | None,
        change_count: int | None = None,
    ) -> AppliedClipboardEvent | None:
        if not fingerprint:
            return None

        self._prune()
        for event in reversed(self._events):
            if event.consumed:
                continue
            if event.kind != kind or event.fingerprint != fingerprint:
                continue
            if (
                event.expected_change_count is not None
                and change_count is not None
                and event.expected_change_count != change_count
            ):
                continue
            event.consumed = True
            self._prune()
            return event
        return None
