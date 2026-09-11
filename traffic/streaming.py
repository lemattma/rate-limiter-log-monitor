"""Streaming line reader with a bounded reorder buffer.

The program never loads a whole file into memory. Lines are read one at a time
and pushed through a fixed-size min-heap keyed on timestamp; once the heap is
full, the earliest record is popped and handed downstream. That delivers records
to the sliding windows in time order without holding more than ``capacity``
records at once.

The buffer exists because the brief says logs come from "a variety of third
party upstream services owned by other teams". Independently-produced streams
interleave, so records arrive out of order, and a sliding window fed unordered
timestamps is silently wrong. A reorder buffer of N records corrects any record
displaced by fewer than N positions.

Anything displaced *further* than that is counted as a late record rather than
quietly mishandled. Late records still count toward request totals -- they are
real requests -- but are withheld from the window computation, because feeding a
backwards timestamp into a rolling deque corrupts its eviction invariant. The
report says how many were affected, per client and in total, so burst figures can
be read with the right amount of trust.

Lateness is judged **per client**, not against a global high-water mark. The
invariant that needs protecting belongs to one client's deque: ``ClientWindow``
evicts relative to the newest timestamp *it* has seen, and is indifferent to what
any other client did. Judging against a global watermark is strictly stricter
than the invariant requires, and the excess strictness is not conservative -- it
discards real traffic. Interleaved producers are usually each internally ordered
and merely offset from one another, which is exactly the shape a global watermark
handles worst: a client whose producer lags the global maximum would have every
record excluded and report a peak burst of zero.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Iterator, List, Optional, TextIO, Tuple

from .parsing import BLANK, Malformed, ParseResult, Record, parse_line

RECORD = "record"
MALFORMED = "malformed"
BLANK_LINE = "blank"

DEFAULT_BUFFER = 10_000


@dataclass
class Event:
    """One thing that happened while reading, tagged by kind."""

    kind: str
    source: str
    lineno: int
    record: Optional[Record] = None
    malformed: Optional[Malformed] = None
    late: bool = False


class ReorderBuffer:
    """Fixed-capacity min-heap that emits records in timestamp order."""

    __slots__ = ("_capacity", "_heap", "_seq")

    def __init__(self, capacity: int) -> None:
        # Capacity 0 disables reordering entirely (records pass straight
        # through), which is useful for testing and for input known to be
        # sorted.
        self._capacity = max(0, capacity)
        self._heap: List[Tuple] = []
        self._seq = 0

    def push(self, event: Event) -> Iterator[Event]:
        """Add an event, yielding any that are now safe to emit."""
        assert event.record is not None
        # The sequence number breaks ties between equal timestamps so that
        # equal-timestamped records keep their original file order, and so the
        # heap never has to compare Event objects.
        heapq.heappush(self._heap, (event.record.timestamp, self._seq, event))
        self._seq += 1
        while len(self._heap) > self._capacity:
            yield heapq.heappop(self._heap)[2]

    def drain(self) -> Iterator[Event]:
        """Flush everything still buffered, in timestamp order."""
        while self._heap:
            yield heapq.heappop(self._heap)[2]


def iter_lines(handle: TextIO, source: str) -> Iterator[Tuple[str, int, str]]:
    """Yield (source, line number, raw line) for a file handle."""
    for lineno, line in enumerate(handle, start=1):
        yield source, lineno, line


def stream_events(
    lines: Iterable[Tuple[str, int, str]], buffer_size: int = DEFAULT_BUFFER
) -> Iterator[Event]:
    """Parse raw lines and emit events, with records in timestamp order.

    Malformed and blank lines are emitted immediately: they carry no timestamp,
    so they have nothing to order by and nothing downstream depends on their
    position.
    """
    buffer = ReorderBuffer(buffer_size)
    # client_id -> newest timestamp already handed downstream for that client.
    # Per client, because that is the scope of the ordering invariant the
    # sliding windows actually require (see module docstring).
    last_emitted: Dict[str, datetime] = {}

    def finalise(event: Event) -> Event:
        assert event.record is not None
        record = event.record
        seen = last_emitted.get(record.client_id)
        if seen is not None and record.timestamp < seen:
            event.late = True
        else:
            last_emitted[record.client_id] = record.timestamp
        return event

    for source, lineno, raw in lines:
        result = parse_line(raw)
        if result is BLANK:
            yield Event(BLANK_LINE, source, lineno)
        elif isinstance(result, Malformed):
            yield Event(MALFORMED, source, lineno, malformed=result)
        else:
            pending = Event(RECORD, source, lineno, record=result)
            for ready in buffer.push(pending):
                yield finalise(ready)

    for ready in buffer.drain():
        yield finalise(ready)
