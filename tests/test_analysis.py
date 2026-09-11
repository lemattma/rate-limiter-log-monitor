"""Threshold derivation, report shape, and end-to-end behaviour."""

import json
import os
import statistics
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

from traffic.analysis import Analyzer, Config
from traffic.parsing import Record
from traffic.streaming import RECORD, Event

UTC = timezone.utc
BASE = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "sample_input", "requests.jsonl")


def build_report(cohort, config=None):
    """cohort: {client_id: requests_within_one_burst_window}."""
    analyzer = Analyzer(config or Config())
    lineno = 0
    for client_id, count in sorted(cohort.items()):
        for i in range(count):
            # Spread across 9s so every request lands in one 10s window.
            offset = (9.0 * i / (count - 1)) if count > 1 else 0.0
            lineno += 1
            analyzer.consume(
                Event(
                    RECORD,
                    "test",
                    lineno,
                    record=Record(
                        timestamp=BASE + timedelta(seconds=offset),
                        client_id=client_id,
                        endpoint="/v1/widgets",
                        status_code=200,
                        request_id="r%d" % lineno,
                    ),
                )
            )
    return analyzer.report()


def flagged(report):
    return sorted(v["client_id"] for v in report["rate_limits"]["violations"])


class ThresholdScenarioTests(unittest.TestCase):
    """The scenarios that decided the design.

    A threshold derived from an upper percentile sits *inside* the abusive
    tail, so abusers inflate the very limit meant to catch them. The median has
    a 50% breakdown point and does not move until abusers outnumber everyone
    else. These tests pin that reasoning down as executable assertions.
    """

    def test_sample_shape_falls_back_to_floor(self):
        # Two clients is below the population gate, so the static floor applies
        # and the busier client is still caught.
        report = build_report({"acct_1": 6, "acct_2": 2})
        burst = report["rate_limits"]["thresholds"]["burst"]
        self.assertEqual(burst["source"], "static_floor")
        self.assertEqual(burst["reason"], "insufficient_population")
        self.assertEqual(flagged(report), ["acct_1"])

    def test_dense_file_with_one_outlier(self):
        # Twenty clients at 30, one at 300. A static floor of 5 would flag all
        # twenty-one and bury the real offender.
        cohort = {"normal_%02d" % i: 30 for i in range(20)}
        cohort["abuser"] = 300
        report = build_report(cohort)
        self.assertEqual(report["rate_limits"]["thresholds"]["burst"]["value"], 90)
        self.assertEqual(flagged(report), ["abuser"])

    def test_heavily_contaminated_population(self):
        # Six normal at 3, four abusive at 50. This is the case an upper
        # percentile gets wrong: p95 of the peaks is 50, which flags nobody.
        cohort = {"normal_%d" % i: 3 for i in range(6)}
        cohort.update({"abuser_%d" % i: 50 for i in range(4)})
        report = build_report(cohort)

        peaks = [3] * 6 + [50] * 4
        p95 = statistics.quantiles(peaks, n=100, method="inclusive")[94]
        self.assertEqual(p95, 50)
        self.assertEqual(sum(1 for p in peaks if p > p95), 0, "p95 would flag nobody")

        self.assertEqual(report["rate_limits"]["thresholds"]["burst"]["value"], 9)
        self.assertEqual(flagged(report), ["abuser_0", "abuser_1", "abuser_2", "abuser_3"])

    def test_quiet_file_flags_nobody(self):
        report = build_report({"normal_%02d" % i: 2 for i in range(50)})
        self.assertEqual(flagged(report), [])

    def test_uniformly_high_but_no_outlier_flags_nobody(self):
        # Every client at 30. Nobody is out of line with anybody, so the correct
        # answer is an empty list -- which a static floor cannot produce.
        report = build_report({"normal_%02d" % i: 30 for i in range(21)})
        self.assertEqual(flagged(report), [])

        static = build_report(
            {"normal_%02d" % i: 30 for i in range(21)}, Config(adaptive=False)
        )
        self.assertEqual(len(flagged(static)), 21)

    def test_floor_wins_when_derived_value_is_lower(self):
        # Derived would be 3 x 1 = 3, below the floor of 5; the floor holds so a
        # quiet cohort cannot produce a hair-trigger threshold.
        report = build_report({"c%02d" % i: 1 for i in range(10)})
        burst = report["rate_limits"]["thresholds"]["burst"]
        self.assertEqual(burst["value"], 5)
        self.assertEqual(burst["source"], "static_floor")


class WarningTests(unittest.TestCase):
    def _codes(self, report):
        return {w["code"] for w in report["rate_limits"]["warnings"]}

    def test_insufficient_population_warns_and_suggests_a_limit(self):
        report = build_report({"acct_1": 6, "acct_2": 2})
        warnings = report["rate_limits"]["warnings"]
        self.assertIn("insufficient_population", self._codes(report))
        warning = next(w for w in warnings if w["code"] == "insufficient_population")
        self.assertIn("--limit", warning["suggestion"])

    def test_saturated_population_is_reported(self):
        report = build_report({"c%02d" % i: 30 for i in range(21)})
        self.assertIn("population_saturated", self._codes(report))

    def test_high_flagged_ratio_is_reported(self):
        # Three clients all bursting hard: below the population gate, so the
        # floor applies and flags all of them. That ratio is itself the signal
        # that the floor is wrong for this input.
        report = build_report({"c%d" % i: 60 for i in range(3)})
        self.assertIn("high_flagged_ratio", self._codes(report))

    def test_clean_input_produces_no_warnings(self):
        report = build_report({"c%02d" % i: 2 for i in range(50)})
        self.assertEqual(self._codes(report), set())


class ReportShapeTests(unittest.TestCase):
    def test_empty_input_is_a_valid_report(self):
        report = Analyzer(Config()).report()
        self.assertEqual(report["summary"]["requests"], 0)
        self.assertEqual(report["rate_limits"]["violations"], [])
        self.assertEqual(report["clients"], [])
        json.dumps(report)  # must stay serialisable

    def test_missing_client_id_bucket_is_annotated(self):
        report = build_report({"": 3, "acct_1": 1})
        row = next(r for r in report["clients"] if r["client_id"] == "")
        self.assertIn("note", row)
        self.assertEqual(row["requests"], 3)

    def test_client_ordering_is_deterministic(self):
        report = build_report({"b": 5, "a": 5, "c": 9})
        self.assertEqual([r["client_id"] for r in report["clients"]], ["c", "a", "b"])

    def test_peak_burst_reported_for_every_client_not_just_violators(self):
        # The threshold-free layer: even if the limit is wrong for this input,
        # the report still shows who was fastest and how fast.
        report = build_report({"c%02d" % i: 2 for i in range(50)})
        self.assertEqual(flagged(report), [])
        for row in report["clients"]:
            self.assertEqual(row["peak_burst"]["count"], 2)
            self.assertIn("rate_per_second", row["peak_burst"])


class SeverityAndEvidenceTests(unittest.TestCase):
    """Ranking and justification must work for both rules, not just burst."""

    def _report(self, cohort_spec):
        analyzer = Analyzer(Config())
        n = 0
        for client_id, offsets in cohort_spec.items():
            for offset in offsets:
                n += 1
                analyzer.consume(Event(RECORD, "t", n, record=Record(
                    timestamp=BASE + timedelta(seconds=offset), client_id=client_id,
                    endpoint="/v1/x", status_code=200, request_id="r%d" % n)))
        return analyzer.report()

    def test_breaching_both_rules_cites_both(self):
        report = self._report({"both": [i * 0.3 for i in range(30)]})
        violation = report["rate_limits"]["violations"][0]
        self.assertEqual(violation["violated"], ["burst", "sustained"])
        self.assertIn("burst limit", violation["evidence"])
        self.assertIn("sustained limit", violation["evidence"])

    def test_evidence_names_which_limit(self):
        report = self._report({"spike": list(range(8))})
        self.assertIn("burst limit", report["rate_limits"]["violations"][0]["evidence"])

    def test_severity_ranks_across_rules(self):
        # A 1.20x burst breach must not outrank a 1.50x sustained breach. The
        # raw counts are not commensurable -- one is over 10s, the other 60s.
        report = self._report({
            "mild_burst": [i * 1.5 for i in range(6)],          # 6/10s  = 1.20x
            "heavy_sustained": [300 + i * 2.0 for i in range(30)],  # 30/60s = 1.50x
        })
        order = [v["client_id"] for v in report["rate_limits"]["violations"]]
        self.assertEqual(order, ["heavy_sustained", "mild_burst"])
        self.assertAlmostEqual(report["rate_limits"]["violations"][0]["severity"], 1.5, places=2)

    def test_corroboration_breaks_ties(self):
        analyzer = Analyzer(Config())
        n = 0
        for client_id, status in [("quiet", 200), ("throttled", 429)]:
            for i in range(8):
                n += 1
                analyzer.consume(Event(RECORD, "t", n, record=Record(
                    timestamp=BASE + timedelta(seconds=i), client_id=client_id,
                    endpoint="/v1/x", status_code=status, request_id="r%d" % n)))
        violations = analyzer.report()["rate_limits"]["violations"]
        self.assertEqual([v["client_id"] for v in violations], ["throttled", "quiet"])
        self.assertTrue(violations[0]["corroborated"])
        self.assertFalse(violations[1]["corroborated"])


class UnattributedTrafficTests(unittest.TestCase):
    """Records whose producer dropped client_id are an ingestion finding."""

    def _report(self):
        analyzer = Analyzer(Config())
        for i in range(12):
            analyzer.consume(Event(RECORD, "t", i, record=Record(
                timestamp=BASE + timedelta(seconds=i * 0.5), client_id="",
                endpoint="/v1/x", status_code=200, request_id="m%d" % i)))
        for i in range(3):
            analyzer.consume(Event(RECORD, "t", 100 + i, record=Record(
                timestamp=BASE + timedelta(seconds=100 + i), client_id="real",
                endpoint="/v1/x", status_code=200, request_id="r%d" % i)))
        return analyzer.report()

    def test_not_reported_as_a_violation(self):
        report = self._report()
        self.assertEqual([v["client_id"] for v in report["rate_limits"]["violations"]], [])
        self.assertEqual(report["summary"]["violating_clients"], 0)

    def test_surfaced_as_a_warning_instead(self):
        codes = {w["code"] for w in self._report()["rate_limits"]["warnings"]}
        self.assertIn("unattributed_traffic", codes)

    def test_still_visible_in_the_clients_table_with_a_note(self):
        row = next(r for r in self._report()["clients"] if r["client_id"] == "")
        self.assertEqual(row["requests"], 12)
        self.assertIn("note", row)

    def test_excluded_from_threshold_derivation(self):
        # The bucket aggregates arbitrarily many producers, so its peak is
        # systematically high; left in, it pulls the median up and makes the
        # detector less sensitive.
        analyzer = Analyzer(Config())
        n = 0
        for i in range(60):
            n += 1
            analyzer.consume(Event(RECORD, "t", n, record=Record(
                timestamp=BASE + timedelta(seconds=i * 0.1), client_id="",
                endpoint="/v1/x", status_code=200, request_id="m%d" % n)))
        for c in range(6):
            for i in range(2):
                n += 1
                analyzer.consume(Event(RECORD, "t", n, record=Record(
                    timestamp=BASE + timedelta(seconds=200 + c * 50 + i),
                    client_id="c%d" % c, endpoint="/v1/x", status_code=200,
                    request_id="r%d" % n)))
        burst = analyzer.report()["rate_limits"]["thresholds"]["burst"]
        self.assertEqual(burst["population"], 6)
        self.assertEqual(burst["median_peak_burst"], 2)


class PopulationConsistencyTests(unittest.TestCase):
    def test_warning_and_threshold_report_the_same_population(self):
        report = build_report({"acct_1": 6, "acct_2": 2})
        burst = report["rate_limits"]["thresholds"]["burst"]
        warning = next(w for w in report["rate_limits"]["warnings"]
                       if w["code"] == "insufficient_population")
        self.assertIn("Only %d client(s)" % burst["population"], warning["message"])


class EndpointCapTests(unittest.TestCase):
    """Endpoint cardinality is attacker-controlled and must stay bounded."""

    def _report(self, distinct, cap):
        analyzer = Analyzer(Config(max_endpoints=cap, normalise_paths=False))
        for i in range(distinct):
            analyzer.consume(Event(RECORD, "t", i, record=Record(
                timestamp=BASE + timedelta(seconds=i), client_id="c",
                endpoint="/v1/w/%d" % i, status_code=200, request_id="r%d" % i)))
        return analyzer.report()

    def test_tail_is_folded_once_the_cap_is_reached(self):
        report = self._report(distinct=500, cap=50)
        self.assertLessEqual(report["summary"]["endpoints"], 51)
        self.assertEqual(report["summary"]["endpoints_folded"], 450)

    def test_request_totals_stay_exact(self):
        report = self._report(distinct=500, cap=50)
        self.assertEqual(report["summary"]["requests"], 500)
        self.assertEqual(sum(e["requests"] for e in report["endpoints"]), 500)

    def test_nothing_folded_below_the_cap(self):
        report = self._report(distinct=20, cap=50)
        self.assertEqual(report["summary"]["endpoints_folded"], 0)
        self.assertEqual(report["summary"]["endpoints"], 20)


class EndToEndTests(unittest.TestCase):
    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "report.py")] + list(args),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_sample_input(self):
        report = self._run(SAMPLE)
        self.assertEqual(report["summary"]["requests"], 8)
        self.assertEqual(report["summary"]["clients"], 2)
        self.assertEqual(report["summary"]["malformed"], 0)
        self.assertEqual(flagged(report), ["acct_1"])

        violation = report["rate_limits"]["violations"][0]
        self.assertEqual(violation["violated"], ["burst"])
        self.assertEqual(violation["peak_burst"]["count"], 6)
        self.assertEqual(violation["peak_burst"]["start"], "2024-01-15T10:00:00Z")
        self.assertEqual(violation["peak_burst"]["end"], "2024-01-15T10:00:08Z")

    def test_stdin_matches_file_argument(self):
        with open(SAMPLE) as handle:
            piped = subprocess.run(
                [sys.executable, os.path.join(ROOT, "report.py")],
                stdin=handle,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
        self.assertEqual(piped.returncode, 0, piped.stderr)
        from_stdin = json.loads(piped.stdout)
        from_file = self._run(SAMPLE)
        # Only the recorded source differs.
        from_stdin["input"] = from_file["input"]
        self.assertEqual(from_stdin, from_file)

    def test_repeated_runs_are_byte_identical(self):
        first = self._run(SAMPLE)
        second = self._run(SAMPLE)
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_missing_file_exits_with_usage_error(self):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "report.py"), "does-not-exist.jsonl"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("no such file", result.stderr)

    def test_explicit_limit_overrides_derivation(self):
        report = self._run(SAMPLE, "--limit", "100")
        self.assertEqual(flagged(report), [])

    def test_raw_paths_disables_normalisation(self):
        report = self._run(SAMPLE, "--raw-paths")
        self.assertFalse(report["rate_limits"]["path_normalisation"])


if __name__ == "__main__":
    unittest.main()
