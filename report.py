#!/usr/bin/env python3
"""Command-line entry point for the API traffic report.

Usage:
    python3 report.py sample_input/requests.jsonl
    cat requests.jsonl | python3 report.py

Writes a single JSON report to stdout. Diagnostics go to stderr so stdout stays
machine-readable and safe to pipe.
"""

import sys

import argparse
import json
import os

from traffic import __version__
from traffic.analysis import (
    DEFAULT_BURST_LIMIT,
    DEFAULT_BURST_WINDOW,
    DEFAULT_MALFORMED_SAMPLES,
    DEFAULT_MIN_POPULATION,
    DEFAULT_MULTIPLIER,
    DEFAULT_SUSTAINED_LIMIT,
    DEFAULT_SUSTAINED_WINDOW,
    Analyzer,
    Config,
)
from traffic.render import DEFAULT_TOP, render_text
from traffic.streaming import DEFAULT_BUFFER, iter_lines, stream_events

EXIT_OK = 0
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report.py",
        description=(
            "Read JSONL API request logs and print a single JSON traffic report to stdout."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="JSONL log files. Reads standard input when none are given.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BURST_LIMIT,
        help="Burst allowance: requests permitted within one burst window.",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_BURST_WINDOW,
        help="Burst window length in seconds.",
    )
    parser.add_argument(
        "--sustained-limit",
        type=int,
        default=DEFAULT_SUSTAINED_LIMIT,
        help="Sustained allowance: requests permitted within one sustained window.",
    )
    parser.add_argument(
        "--sustained-window",
        type=float,
        default=DEFAULT_SUSTAINED_WINDOW,
        help="Sustained window length in seconds.",
    )
    parser.add_argument(
        "--multiplier",
        type=float,
        default=DEFAULT_MULTIPLIER,
        help="How many times the typical client's burst counts as a violation.",
    )
    parser.add_argument(
        "--min-population",
        type=int,
        default=DEFAULT_MIN_POPULATION,
        help="Distinct clients required before a threshold is derived from the data.",
    )
    parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Use the static floor only; never derive a threshold from the input.",
    )
    parser.add_argument(
        "--raw-paths",
        action="store_true",
        help="Group endpoints by raw path instead of collapsing ids to placeholders.",
    )
    parser.add_argument(
        "--reorder-buffer",
        type=int,
        default=DEFAULT_BUFFER,
        help="Records buffered to correct out-of-order timestamps. 0 disables reordering.",
    )
    parser.add_argument(
        "--malformed-samples",
        type=int,
        default=DEFAULT_MALFORMED_SAMPLES,
        help="Malformed lines quoted in the report for debugging.",
    )
    parser.add_argument(
        "--output",
        choices=("auto", "text", "json"),
        default="auto",
        help=(
            "Output style. 'auto' prints a readable summary when stdout is a terminal "
            "and JSON when it is piped or redirected."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help="Rows per table in text output. JSON is never truncated.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation. Use 0 for a single compact line.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        burst_limit=args.limit,
        burst_window=args.window,
        sustained_limit=args.sustained_limit,
        sustained_window=args.sustained_window,
        multiplier=args.multiplier,
        min_population=args.min_population,
        adaptive=not args.no_adaptive,
        normalise_paths=not args.raw_paths,
        malformed_samples=max(0, args.malformed_samples),
    )


def resolve_style(choice: str, stream) -> str:
    """Pick the output style.

    'auto' means readable in a terminal, JSON everywhere else. The brief
    specifies a JSON report on stdout, and that is what anything capturing the
    output receives -- a redirect, a pipe, or a subprocess. The readable view is
    strictly for a human looking at a terminal, where raw JSON would be the
    less useful answer.
    """
    if choice != "auto":
        return choice
    try:
        return "text" if stream.isatty() else "json"
    except (AttributeError, ValueError):
        # A stream that cannot answer the question is not a terminal.
        return "json"


def validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.window <= 0 or args.sustained_window <= 0:
        parser.error("window lengths must be positive")
    if args.limit < 0 or args.sustained_limit < 0:
        parser.error("limits must not be negative")
    if args.multiplier <= 0:
        parser.error("--multiplier must be positive")


def line_sources(paths, parser):
    """Yield (source, lineno, line) across every input, one line at a time."""
    if not paths:
        for item in iter_lines(sys.stdin, "<stdin>"):
            yield item
        return

    for path in paths:
        if not os.path.exists(path):
            parser.error("no such file: %s" % path)
        try:
            # errors="replace" keeps a stray non-UTF-8 byte from aborting the
            # whole run: the affected line is very likely to fail JSON parsing
            # and be counted as malformed, which is the correct outcome.
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            parser.error("cannot read %s: %s" % (path, exc))
        with handle:
            for item in iter_lines(handle, path):
                yield item


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(args, parser)

    analyzer = Analyzer(config_from_args(args))
    for source in args.files or ["<stdin>"]:
        analyzer.add_source(source)

    for event in stream_events(
        line_sources(args.files, parser), buffer_size=args.reorder_buffer
    ):
        analyzer.consume(event)

    document = analyzer.report()

    if resolve_style(args.output, sys.stdout) == "text":
        sys.stdout.write(render_text(document, top=max(1, args.top)))
    else:
        indent = args.indent if args.indent > 0 else None
        separators = (",", ": ") if indent else (",", ":")
        json.dump(document, sys.stdout, indent=indent, separators=separators)
        sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `report.py big.jsonl | head`).
        # Silence the interpreter's shutdown warning about it.
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(EXIT_OK)
    except KeyboardInterrupt:
        raise SystemExit(130)
