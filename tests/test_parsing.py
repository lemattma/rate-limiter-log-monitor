"""Parser tests, written as tables of (input, expectation)."""

import unittest
from datetime import datetime, timezone

from traffic.parsing import (
    BLANK,
    Malformed,
    Record,
    normalise_endpoint,
    parse_line,
    parse_timestamp,
)

UTC = timezone.utc


class TimestampTests(unittest.TestCase):
    def test_accepted_forms_all_normalise_to_utc(self):
        expected = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        cases = [
            "2024-01-15T10:00:00Z",
            "2024-01-15T10:00:00z",
            "2024-01-15T10:00:00+00:00",
            "2024-01-15T10:00:00+0000",
            "2024-01-15T15:30:00+05:30",
            "2024-01-15T05:00:00-05:00",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                when, _ = parse_timestamp(raw)
                self.assertEqual(when, expected)

    def test_fractional_seconds_of_any_precision(self):
        # Python 3.9's fromisoformat only accepts 3 or 6 fractional digits,
        # while 3.11+ is permissive. Normalising by hand keeps the two
        # identical, so this test is the guard against a 3.11-only regression.
        for raw, micros in [
            ("2024-01-15T10:00:00.1Z", 100000),
            ("2024-01-15T10:00:00.12Z", 120000),
            ("2024-01-15T10:00:00.123Z", 123000),
            ("2024-01-15T10:00:00.123456Z", 123456),
            ("2024-01-15T10:00:00.1234567Z", 123456),
        ]:
            with self.subTest(raw=raw):
                when, _ = parse_timestamp(raw)
                self.assertEqual(when.microsecond, micros)

    def test_naive_timestamp_assumes_utc_and_says_so(self):
        when, repairs = parse_timestamp("2024-01-15T10:00:00")
        self.assertEqual(when, datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
        self.assertIn("timestamp_assumed_utc", repairs)

    def test_space_separator_is_repaired(self):
        when, repairs = parse_timestamp("2024-01-15 10:00:00")
        self.assertEqual(when, datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
        self.assertIn("timestamp_space_separator", repairs)

    def test_epoch_seconds_accepted(self):
        when, repairs = parse_timestamp(1705312800)
        self.assertEqual(when, datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
        self.assertIn("timestamp_from_epoch", repairs)

    def test_rejected_values(self):
        for raw in ["", "   ", "not-a-time", "2024-13-45T99:99:99Z", None, True, [], {}]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_timestamp(raw)


class ParseLineTests(unittest.TestCase):
    def test_well_formed_line(self):
        line = (
            '{"request_id":"a1_1","timestamp":"2024-01-15T10:00:00Z",'
            '"client_id":"acct_1","endpoint":"/v1/widgets","status_code":200}'
        )
        record = parse_line(line)
        self.assertIsInstance(record, Record)
        self.assertEqual(record.client_id, "acct_1")
        self.assertEqual(record.endpoint, "/v1/widgets")
        self.assertEqual(record.status_code, 200)
        self.assertEqual(record.repairs, frozenset())

    def test_blank_lines_are_not_malformed(self):
        # A trailing newline is present in every well-formed file; counting it
        # as malformed would be misleading.
        for raw in ["", "\n", "   \n", "\t"]:
            with self.subTest(raw=repr(raw)):
                self.assertIs(parse_line(raw), BLANK)

    def test_malformed_reasons(self):
        cases = [
            ("not json at all", "invalid_json"),
            ("[1,2,3]", "not_an_object"),
            ('"a string"', "not_an_object"),
            ('{"client_id":"acct_1"}', "missing_timestamp"),
            ('{"timestamp":null,"client_id":"a"}', "missing_timestamp"),
            ('{"timestamp":"nope","client_id":"a"}', "bad_timestamp"),
        ]
        for line, reason in cases:
            with self.subTest(line=line):
                result = parse_line(line)
                self.assertIsInstance(result, Malformed)
                self.assertEqual(result.reason, reason)

    def test_status_code_coercion(self):
        cases = [
            ('{"timestamp":"2024-01-15T10:00:00Z","status_code":200}', 200, None),
            ('{"timestamp":"2024-01-15T10:00:00Z","status_code":"200"}', 200, "status_code_from_string"),
            ('{"timestamp":"2024-01-15T10:00:00Z","status_code":200.0}', 200, "status_code_from_float"),
            ('{"timestamp":"2024-01-15T10:00:00Z","status_code":"abc"}', None, "bad_status_code"),
            ('{"timestamp":"2024-01-15T10:00:00Z","status_code":200.5}', None, "bad_status_code"),
            ('{"timestamp":"2024-01-15T10:00:00Z","status_code":true}', None, "bad_status_code"),
            ('{"timestamp":"2024-01-15T10:00:00Z"}', None, "missing_status_code"),
        ]
        for line, expected, repair in cases:
            with self.subTest(line=line):
                record = parse_line(line)
                self.assertIsInstance(record, Record)
                self.assertEqual(record.status_code, expected)
                if repair:
                    self.assertIn(repair, record.repairs)

    def test_only_timestamp_is_required(self):
        # Missing identity fields must not discard the record: it still counts
        # toward that bucket's traffic, and dropping it would bias the
        # measurement toward whichever producers serialise correctly.
        record = parse_line('{"timestamp":"2024-01-15T10:00:00Z"}')
        self.assertIsInstance(record, Record)
        self.assertEqual(record.client_id, "")
        self.assertEqual(record.endpoint, "")
        self.assertIn("missing_client_id", record.repairs)
        self.assertIn("missing_endpoint", record.repairs)

    def test_empty_and_non_string_identities(self):
        record = parse_line('{"timestamp":"2024-01-15T10:00:00Z","client_id":"","endpoint":42}')
        self.assertEqual(record.client_id, "")
        self.assertEqual(record.endpoint, "42")
        self.assertIn("empty_client_id", record.repairs)
        self.assertIn("non_string_endpoint", record.repairs)

    def test_unknown_fields_are_ignored(self):
        record = parse_line(
            '{"timestamp":"2024-01-15T10:00:00Z","client_id":"a","region":"eu","latency_ms":12}'
        )
        self.assertIsInstance(record, Record)
        self.assertEqual(record.client_id, "a")


class NormaliseEndpointTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("/v1/widgets", "/v1/widgets"),
            ("/v1/widgets/123", "/v1/widgets/{id}"),
            ("/v1/widgets/123/parts/456", "/v1/widgets/{id}/parts/{id}"),
            (
                "/v1/w/7a1f2b3c-1111-2222-3333-444455556666",
                "/v1/w/{uuid}",
            ),
            ("/v1/w/7A1F2B3C-1111-2222-3333-444455556666", "/v1/w/{uuid}"),
            ("/v1/w/deadbeefdeadbeef", "/v1/w/{hex}"),
            ("/v1/widgets/", "/v1/widgets/"),
            ("", ""),
            ("/v1/2024/reports", "/v1/{id}/reports"),  # known false positive
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalise_endpoint(raw), expected)


if __name__ == "__main__":
    unittest.main()
