"""Text rendering and output-style selection."""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report import resolve_style  # noqa: E402
from traffic.analysis import Analyzer, Config  # noqa: E402
from traffic.render import render_text  # noqa: E402

from tests.test_analysis import build_report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "sample_input", "requests.jsonl")


class FakeStream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class ResolveStyleTests(unittest.TestCase):
    def test_auto_picks_text_for_a_terminal(self):
        self.assertEqual(resolve_style("auto", FakeStream(True)), "text")

    def test_auto_picks_json_for_a_pipe(self):
        # The brief specifies a JSON report on stdout; anything capturing the
        # output must receive JSON, so this is the case that protects it.
        self.assertEqual(resolve_style("auto", FakeStream(False)), "json")

    def test_explicit_choice_overrides_the_terminal(self):
        self.assertEqual(resolve_style("json", FakeStream(True)), "json")
        self.assertEqual(resolve_style("text", FakeStream(False)), "text")

    def test_stream_that_cannot_answer_is_treated_as_not_a_terminal(self):
        class Awkward:
            def isatty(self):
                raise ValueError("closed")

        self.assertEqual(resolve_style("auto", Awkward()), "json")
        self.assertEqual(resolve_style("auto", object()), "json")


class RenderTextTests(unittest.TestCase):
    def test_sample_report_sections(self):
        report = build_report({"acct_1": 6, "acct_2": 2})
        text = render_text(report)
        for heading in [
            "API TRAFFIC REPORT",
            "SUMMARY",
            "RATE LIMITS",
            "VIOLATIONS (1)",
            "CLIENTS (2)",
            "ENDPOINTS (1)",
            "INGESTION",
        ]:
            self.assertIn(heading, text)
        self.assertIn("acct_1", text)

    def test_warnings_are_rendered_with_their_suggestion(self):
        text = render_text(build_report({"acct_1": 6, "acct_2": 2}))
        self.assertIn("[!] insufficient_population", text)
        self.assertIn("--limit", text)

    def test_empty_report_renders_without_error(self):
        text = render_text(Analyzer(Config()).report())
        self.assertIn("VIOLATIONS (0)", text)
        self.assertIn("none", text)

    def test_no_violations_says_none(self):
        text = render_text(build_report({"c%02d" % i: 2 for i in range(50)}))
        self.assertIn("VIOLATIONS (0)", text)

    def test_tables_are_truncated_and_say_so(self):
        report = build_report({"c%03d" % i: 2 for i in range(50)})
        text = render_text(report, top=5)
        self.assertIn("CLIENTS (50)", text)
        self.assertIn("and 45 more", text)
        self.assertIn("--output json", text)

    def test_missing_client_id_is_labelled(self):
        text = render_text(build_report({"": 3, "acct_1": 1}))
        self.assertIn("(no client_id)", text)

    def test_output_ends_with_a_single_newline(self):
        text = render_text(build_report({"acct_1": 6, "acct_2": 2}))
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))


class OutputStyleEndToEndTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "report.py")] + list(args),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    def test_piped_output_is_json(self):
        result = self._run(SAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        json.loads(result.stdout)  # must parse

    def test_forced_text(self):
        result = self._run("--output", "text", SAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("API TRAFFIC REPORT"))

    def test_forced_json_stays_json(self):
        result = self._run("--output", "json", SAMPLE)
        self.assertEqual(json.loads(result.stdout)["summary"]["requests"], 8)

    def test_text_and_json_agree(self):
        document = json.loads(self._run("--output", "json", SAMPLE).stdout)
        text = self._run("--output", "text", SAMPLE).stdout
        # The text view is a projection of the same document, so the headline
        # figures must match rather than being computed twice.
        self.assertIn("VIOLATIONS (%d)" % len(document["rate_limits"]["violations"]), text)
        self.assertIn("CLIENTS (%d)" % len(document["clients"]), text)

    def test_terminal_gets_text(self):
        # Run attached to a real pty so isatty() is genuinely true -- the only
        # way to exercise the default path a human actually hits.
        try:
            import pty
        except ImportError:  # pragma: no cover - Windows
            self.skipTest("pty unavailable on this platform")

        try:
            master, slave = pty.openpty()
        except OSError:  # pragma: no cover - no pty in this sandbox
            self.skipTest("cannot allocate a pty here")

        try:
            proc = subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "report.py"), SAMPLE],
                stdout=slave,
                stderr=subprocess.DEVNULL,
                cwd=ROOT,
            )
            os.close(slave)
            chunks = []
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            proc.wait()
        finally:
            os.close(master)

        output = b"".join(chunks).decode("utf-8", errors="replace")
        self.assertIn("API TRAFFIC REPORT", output)
        self.assertNotIn('"schema_version"', output)


if __name__ == "__main__":
    unittest.main()
