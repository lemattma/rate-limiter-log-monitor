"""Sliding-window burst tracking, per client.

This is the detection primitive. The precedent is fail2ban, which solves the
same shape of problem -- find abusive clients by reading a log file -- with the
same mechanism: count occurrences for one identity inside a rolling window and
compare against a limit.

Gateway-grade algorithms (token bucket, GCRA) were considered and rejected. They
*simulate* an enforcement decision, whereas we need to *measure* what happened:
their internal state ("bucket reached 0.3 tokens") is not something a human can
verify against the log, while a sliding window's state is the evidence itself --
"6 requests between 10:00:00Z and 10:00:08Z". Token buckets also derive refill
from ``now - last_seen``, which makes them order-dependent on the out-of-order
input this program expects.

Memory is bounded: each client keeps only the timestamps still inside the
window, which is bounded by that client's own burst rate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional


@dataclass
class Burst:
    """The busiest window observed for one client."""

    count: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None


class ClientWindow:
    """Rolling window of recent request times for a single client.

    Timestamps must arrive in non-decreasing order; the reorder buffer upstream
    is what guarantees that.
    """

    __slots__ = ("_span", "_times", "peak")

    def __init__(self, window_seconds: float) -> None:
        self._span = timedelta(seconds=window_seconds)
        self._times: "deque[datetime]" = deque()
        self.peak = Burst()

    def add(self, when: datetime) -> int:
        """Record a request, returning the current count inside the window."""
        self._times.append(when)

        # Evict anything that has fallen out of the trailing window. A request
        # exactly one span old is outside it.
        cutoff = when - self._span
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()

        count = len(self._times)
        if count > self.peak.count:
            self.peak = Burst(count=count, start=self._times[0], end=when)
        return count


class WindowTracker:
    """One sliding window size, tracked independently for every client."""

    def __init__(self, name: str, window_seconds: float, limit: int) -> None:
        self.name = name
        self.window_seconds = window_seconds
        self.limit = limit
        self._clients: Dict[str, ClientWindow] = {}

    def add(self, client_id: str, when: datetime) -> None:
        window = self._clients.get(client_id)
        if window is None:
            window = ClientWindow(self.window_seconds)
            self._clients[client_id] = window
        window.add(when)

    def peak_for(self, client_id: str) -> Burst:
        window = self._clients.get(client_id)
        return window.peak if window is not None else Burst()

    def peaks(self) -> List[int]:
        """Peak burst counts across all clients, for threshold derivation."""
        return [w.peak.count for w in self._clients.values()]

    def clients(self) -> Iterable[str]:
        return self._clients.keys()
