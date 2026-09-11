"""Human-readable rendering of the report.

The JSON document is the program's contract; this is a view over it for reading
in a terminal. It is deliberately derived from the finished report dictionary
rather than from the analyzer, so the two output styles can never disagree about
what was measured.

One difference from JSON is intentional: the client and endpoint tables are
truncated to the busiest few, because a 2,000-row table is not something a human
reads. The truncation is always stated, and JSON output is never truncated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TOP = 20


def _num(value: Any) -> str:
    return format(value, ",") if isinstance(value, int) else str(value)


def _clock(stamp: Optional[str]) -> str:
    """Time-of-day from an ISO timestamp, for short spans where the date is noise."""
    if not stamp:
        return "-"
    if "T" in stamp:
        return stamp.split("T", 1)[1].rstrip("Z")[:12]
    return stamp


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %ds" % (seconds // 60, seconds % 60)
    return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)


def _span(first: Optional[str], last: Optional[str]) -> str:
    if not first or not last:
        return "-"
    from datetime import datetime

    def parse(text: str) -> datetime:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))

    try:
        length = (parse(last) - parse(first)).total_seconds()
        return "%s  ->  %s   (%s)" % (first, last, _duration(length))
    except ValueError:
        return "%s  ->  %s" % (first, last)


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _truncate_path(text: str, width: int) -> str:
    """Elide the middle of a path: the filename is the part worth keeping."""
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1):]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str]) -> List[str]:
    """Render an aligned table. `aligns` is one of 'l'/'r' per column."""
    if not rows:
        return []
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: Sequence[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.rjust(widths[i]) if aligns[i] == "r" else cell.ljust(widths[i]))
        return "  " + "  ".join(parts).rstrip()

    out = [line(headers), "  " + "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in rows)
    return out


def _status_summary(classes: Dict[str, int]) -> str:
    if not classes:
        return "-"
    return " ".join("%s:%s" % (name, _num(count)) for name, count in sorted(classes.items()))


def _threshold_note(block: Dict[str, Any]) -> str:
    source = block.get("source", "")
    if source == "derived_median":
        return "derived: %g x median burst of %g" % (
            block.get("multiplier", 0),
            block.get("median_peak_burst", 0),
        )
    reason = block.get("reason")
    if reason == "insufficient_population":
        return "static floor - only %d client(s), %d needed to derive" % (
            block.get("population", 0),
            block.get("required_population", 0),
        )
    if reason == "adaptive_disabled":
        return "static floor - adaptive disabled"
    return "static floor - derived value was lower"


def render_text(report: Dict[str, Any], top: int = DEFAULT_TOP) -> str:
    summary = report["summary"]
    limits = report["rate_limits"]
    ingestion = report["ingestion"]
    out: List[str] = []

    sources = report["input"]["sources"]
    shown = ", ".join(sources[:3]) + (" (+%d more)" % (len(sources) - 3) if len(sources) > 3 else "")
    out.append("API TRAFFIC REPORT")
    out.append("%s  -  %s lines read" % (shown or "-", _num(report["input"]["lines_read"])))
    out.append("")

    # -- summary -----------------------------------------------------------
    out.append("SUMMARY")
    out.extend(
        _table(
            ["requests", "clients", "endpoints", "violating", "malformed", "blank"],
            [
                [
                    _num(summary["requests"]),
                    _num(summary["clients"]),
                    _num(summary["endpoints"]),
                    _num(summary["violating_clients"]),
                    _num(summary["malformed"]),
                    _num(summary["blank_lines"]),
                ]
            ],
            ["r"] * 6,
        )
    )
    out.append("  window  %s" % _span(summary["first_seen"], summary["last_seen"]))
    out.append("")

    # -- thresholds --------------------------------------------------------
    out.append("RATE LIMITS")
    rows = []
    for name in ("burst", "sustained"):
        block = limits["thresholds"][name]
        rows.append(
            [
                name,
                "%s req / %gs" % (_num(block["value"]), block["window_seconds"]),
                "%.3g/s" % (block["value"] / block["window_seconds"]),
                _threshold_note(block),
            ]
        )
    out.extend(_table(["window", "effective limit", "rate", "how"], rows, ["l", "r", "r", "l"]))
    out.append("")

    for warning in limits["warnings"]:
        out.append("  [!] %s" % warning["code"])
        for chunk in _wrap(warning["message"], 92):
            out.append("      " + chunk)
        out.append("      -> %s" % warning["suggestion"])
        out.append("")

    # -- violations --------------------------------------------------------
    violations = limits["violations"]
    out.append("VIOLATIONS (%s)" % _num(len(violations)))
    if not violations:
        out.append("  none")
    else:
        rows = []
        for row in violations[:top]:
            burst = row["peak_burst"]
            rows.append(
                [
                    _truncate(row["client_id"] or "(no client_id)", 28),
                    "+".join(row["violated"]),
                    # The ranking key. Without it the reader cannot see why rows
                    # are in the order they are, and raw counts across two
                    # different windows are not comparable.
                    "%.2gx" % row["severity"],
                    _num(burst["count"]),
                    "%.3g/s" % burst["rate_per_second"],
                    "%s -> %s" % (_clock(burst["start"]), _clock(burst["end"])),
                    _num(row["requests"]),
                    _num(row["rate_limited_responses"]),
                ]
            )
        out.extend(
            _table(
                ["client", "rule", "sev", "peak", "rate", "busiest window", "requests", "429s"],
                rows,
                ["l", "l", "r", "r", "r", "l", "r", "r"],
            )
        )
        if len(violations) > top:
            out.append("  ... and %s more (use --output json for all)" % _num(len(violations) - top))
    out.append("")

    # -- clients -----------------------------------------------------------
    clients = report["clients"]
    out.append("CLIENTS (%s)" % _num(len(clients)))
    if not clients:
        out.append("  none")
    else:
        rows = []
        for row in clients[:top]:
            rows.append(
                [
                    _truncate(row["client_id"] or "(no client_id)", 28),
                    _num(row["requests"]),
                    _num(row["peak_burst"]["count"]),
                    _num(row["peak_sustained"]["count"]),
                    _num(row["rate_limited_responses"]),
                    _num(row["records_excluded"]),
                    "+".join(row["violations"]) or "-",
                ]
            )
        out.extend(
            _table(
                ["client", "requests", "peak", "sust", "429s", "excl", "violations"],
                rows,
                ["l", "r", "r", "r", "r", "r", "l"],
            )
        )
        if len(clients) > top:
            out.append("  ... and %s more (use --output json for all)" % _num(len(clients) - top))
    out.append("")

    # -- endpoints ---------------------------------------------------------
    endpoints = report["endpoints"]
    out.append("ENDPOINTS (%s)" % _num(len(endpoints)))
    if not endpoints:
        out.append("  none")
    else:
        rows = []
        for row in endpoints[:top]:
            rows.append(
                [
                    _truncate(row["endpoint"] or "(no endpoint)", 44),
                    _num(row["requests"]),
                    _num(row["distinct_clients"]),
                    _status_summary(row["status_classes"]),
                ]
            )
        out.extend(_table(["endpoint", "requests", "clients", "status"], rows, ["l", "r", "r", "l"]))
        if len(endpoints) > top:
            out.append("  ... and %s more (use --output json for all)" % _num(len(endpoints) - top))
    out.append("")

    # -- ingestion ---------------------------------------------------------
    out.append("INGESTION")
    out.extend(
        _table(
            ["accepted", "late", "blank", "malformed", "repaired"],
            [
                [
                    _num(ingestion["records_accepted"]),
                    _num(ingestion["records_late"]),
                    _num(ingestion["blank_lines"]),
                    _num(ingestion["malformed"]["total"]),
                    _num(ingestion["repairs"]["total"]),
                ]
            ],
            ["r"] * 5,
        )
    )

    malformed = ingestion["malformed"]
    if malformed["total"]:
        out.append("")
        out.append("  discarded by reason:")
        for reason, count in sorted(malformed["by_reason"].items()):
            out.append("    %-22s %s" % (reason, _num(count)))
        if malformed["samples"]:
            out.append("  first offending lines:")
            for sample in malformed["samples"]:
                out.append(
                    "    %s:%-8s %s"
                    % (_truncate_path(sample["source"], 32), sample["line"], sample["reason"])
                )

    repairs = ingestion["repairs"]
    if repairs["total"]:
        out.append("")
        out.append("  repaired by kind:")
        for kind, count in sorted(repairs["by_kind"].items()):
            out.append("    %-28s %s" % (kind, _num(count)))

    return "\n".join(out).rstrip() + "\n"


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = current + " " + word if current else word
    if current:
        lines.append(current)
    return lines
