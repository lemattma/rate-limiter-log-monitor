"""Sliding-window and reorder-buffer tests."""

import unittest
from datetime import datetime, timedelta, timezone

from traffic.parsing import Record
from traffic.streaming import RECORD, ReorderBuffer, Event, stream_events
from traffic.windows import ClientWindow, WindowTracker

UTC = timezone.utc
BASE = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


class ClientWindowTests(unittest.TestCase):
    def test_counts_within_window(self):
        window = ClientWindow(10)
        for offset in [0, 2, 4, 5, 6, 8]:
            window.add(at(offset))
        # The sample log's acct_1: all six land inside one 10s window.
        self.assertEqual(window.peak.count, 6)
        self.assertEqual(window.peak.start, at(0))
        self.assertEqual(window.peak.end, at(8))

    def test_evicts_outside_window(self):
        window = ClientWindow(10)
        for offset in [0, 20, 40, 60]:
            window.add(at(offset))
        self.assertEqual(window.peak.count, 1)

    def test_request_exactly_one_span_old_is_outside(self):
        window = ClientWindow(10)
        window.add(at(0))
        self.assertEqual(window.add(at(10)), 1)

    def test_peak_is_the_maximum_not_the_last(self):
        window = ClientWindow(10)
        for offset in [0, 1, 2, 3]:  # burst of 4
            window.add(at(offset))
        for offset in [100, 200]:  # then quiet
            window.add(at(offset))
        self.assertEqual(window.peak.count, 4)

    def test_simultaneous_timestamps(self):
        window = ClientWindow(10)
        for _ in range(5):
            window.add(at(3))
        self.assertEqual(window.peak.count, 5)

    def test_empty_window_has_zero_peak(self):
        self.assertEqual(ClientWindow(10).peak.count, 0)


class WindowTrackerTests(unittest.TestCase):
    def test_clients_tracked_independently(self):
        tracker = WindowTracker("burst", 10, 5)
        for offset in [0, 1, 2, 3, 4, 5]:
            tracker.add("noisy", at(offset))
        tracker.add("quiet", at(0))
        self.assertEqual(tracker.peak_for("noisy").count, 6)
        self.assertEqual(tracker.peak_for("quiet").count, 1)
        self.assertEqual(sorted(tracker.peaks()), [1, 6])

    def test_unknown_client_has_zero_peak(self):
        tracker = WindowTracker("burst", 10, 5)
        self.assertEqual(tracker.peak_for("nobody").count, 0)


def record_at(seconds, client="acct_1"):
    return Record(
        timestamp=at(seconds),
        client_id=client,
        endpoint="/v1/widgets",
        status_code=200,
        request_id="r%d" % seconds,
    )


class ReorderBufferTests(unittest.TestCase):
    def _emit(self, offsets, capacity):
        buffer = ReorderBuffer(capacity)
        out = []
        for offset in offsets:
            out.extend(buffer.push(Event(RECORD, "t", 0, record=record_at(offset))))
        out.extend(buffer.drain())
        return [event.record.timestamp for event in out]

    def test_sorted_input_passes_through_in_order(self):
        offsets = [0, 1, 2, 3, 4]
        self.assertEqual(self._emit(offsets, 3), [at(o) for o in offsets])

    def test_shuffled_input_is_reordered(self):
        self.assertEqual(
            self._emit([4, 0, 3, 1, 2], 10),
            [at(o) for o in [0, 1, 2, 3, 4]],
        )

    def test_capacity_zero_disables_reordering(self):
        self.assertEqual(self._emit([4, 0, 2], 0), [at(o) for o in [4, 0, 2]])

    def test_equal_timestamps_keep_file_order(self):
        buffer = ReorderBuffer(5)
        out = []
        for i in range(4):
            event = Event(RECORD, "t", i, record=record_at(0))
            out.extend(buffer.push(event))
        out.extend(buffer.drain())
        self.assertEqual([event.lineno for event in out], [0, 1, 2, 3])


class LateRecordTests(unittest.TestCase):
    def _stream(self, offsets, buffer_size):
        lines = [
            (
                "t",
                i,
                '{"request_id":"r%d","timestamp":"%s","client_id":"acct_1",'
                '"endpoint":"/v1/widgets","status_code":200}'
                % (i, at(o).isoformat().replace("+00:00", "Z")),
            )
            for i, o in enumerate(offsets)
        ]
        return list(stream_events(lines, buffer_size=buffer_size))

    def test_within_buffer_nothing_is_late(self):
        events = self._stream([5, 0, 3, 1], buffer_size=10)
        self.assertEqual(sum(1 for e in events if e.late), 0)

    def test_beyond_buffer_is_flagged_late(self):
        # A record displaced further than the buffer can absorb cannot be
        # placed correctly; it is reported rather than silently mishandled.
        events = self._stream([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0], buffer_size=2)
        self.assertEqual(sum(1 for e in events if e.late), 1)


if __name__ == "__main__":
    unittest.main()
