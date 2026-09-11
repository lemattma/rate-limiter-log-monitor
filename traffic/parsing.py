"""Parsing of raw JSONL log lines into records.

Everything here is pure: no file handles, no line numbers, no global state. The
caller owns I/O and attaches positional context to whatever comes back. That
keeps the whole module testable as a table of (input string, expected result).

Parsing is deliberately *lenient*. This program diagnoses rate-limit problems;
it is not a schema validator. Upstream services are owned by other teams and
emit near-miss records -- a stringified status code, a naive timestamp, a
missing field. Discarding those would throw away exactly the traffic we are
trying to measure, and would bias the measurement toward whichever producers
happen to serialise correctly.

Lenient is not silent, though: every coercion is recorded as a named "repair" so
the report can show what was fixed and how often, which is the feedback channel
back to the teams producing the bad records.

Only ``timestamp`` is structurally required. Without a usable time a record
cannot take part in any sliding-window computation at all, so it is unusable
rather than merely incomplete. Every other field degrades to a labelled unknown
bucket and still counts toward that client's traffic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Optional, Tuple, Union

# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """A usable log record, normalised to UTC."""

    timestamp: datetime
    client_id: str
    endpoint: str
    status_code: Optional[int]
    request_id: str
    repairs: FrozenSet[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Malformed:
    """A line that could not yield a usable record."""

    reason: str
    detail: str = ""


class _Blank:
    """Sentinel for empty/whitespace-only lines.

    Kept distinct from Malformed so that a trailing newline -- which every
    well-formed file has -- does not inflate the malformed count.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "BLANK"


BLANK = _Blank()

ParseResult = Union[Record, Malformed, _Blank]

REQUIRED_FIELDS = ("timestamp",)
OPTIONAL_FIELDS = ("client_id", "endpoint", "status_code", "request_id")

UNKNOWN_STATUS = None

# --------------------------------------------------------------------------
# Timestamp handling
# --------------------------------------------------------------------------

# Python 3.9's datetime.fromisoformat only accepts what datetime.isoformat()
# emits. Two consequences matter here, and both are invisible on 3.11+ where
# fromisoformat became permissive -- which is precisely why they are handled by
# hand rather than delegated:
#
#   1. A trailing "Z" is rejected, even though RFC 3339 requires supporting it
#      and the sample data uses it exclusively.
#   2. Fractional seconds must be exactly 3 or 6 digits. "10:00:00.12Z" fails.
#
# Normalising both before delegating keeps behaviour identical from 3.9 to 3.14.

_FRACTION_RE = re.compile(r"\.(\d+)")
_BARE_OFFSET_RE = re.compile(r"([+-])(\d{2})(\d{2})$")


def _normalise_offset(text: str) -> str:
    """Turn a trailing Z or a colon-less offset into an explicit +HH:MM."""
    if text.endswith(("Z", "z")):
        return text[:-1] + "+00:00"
    return _BARE_OFFSET_RE.sub(r"\1\2:\3", text)


def _normalise_fraction(text: str) -> str:
    """Pad or truncate fractional seconds to the 6 digits 3.9 demands."""

    def repl(match: "re.Match[str]") -> str:
        digits = match.group(1)[:6]
        return "." + digits.ljust(6, "0")

    return _FRACTION_RE.sub(repl, text, count=1)


def parse_timestamp(raw: Any) -> Tuple[datetime, FrozenSet[str]]:
    """Parse a timestamp into an aware UTC datetime.

    Returns the datetime plus the set of repairs applied. Raises ValueError if
    the value cannot be interpreted as a time at all.
    """
    repairs = set()

    # Some producers emit epoch seconds rather than a string. Accepting them
    # costs one branch and recovers records that are otherwise perfectly usable.
    if isinstance(raw, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(raw, (int, float)):
        try:
            parsed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(raw))
        except (OverflowError, OSError, ValueError):
            raise ValueError("epoch value out of range")
        return parsed, frozenset({"timestamp_from_epoch"})

    if not isinstance(raw, str):
        raise ValueError("timestamp is not a string")

    text = raw.strip()
    if not text:
        raise ValueError("timestamp is empty")

    # RFC 3339 allows a space in place of "T" by mutual agreement; plenty of
    # loggers emit it unilaterally. Accept it, but record the deviation.
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
        repairs.add("timestamp_space_separator")

    text = _normalise_offset(text)
    text = _normalise_fraction(text)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("unparseable timestamp")

    if parsed.tzinfo is None:
        # No offset at all. Assuming UTC is the only defensible default, but it
        # is a guess, so it is recorded as such.
        parsed = parsed.replace(tzinfo=timezone.utc)
        repairs.add("timestamp_assumed_utc")
    else:
        parsed = parsed.astimezone(timezone.utc)

    return parsed, frozenset(repairs)


# --------------------------------------------------------------------------
# Field coercion
# --------------------------------------------------------------------------


def _coerce_status(raw: Any, repairs: set) -> Optional[int]:
    """Best-effort integer status code, or None with a repair recorded."""
    if raw is None:
        repairs.add("missing_status_code")
        return UNKNOWN_STATUS
    if isinstance(raw, bool):
        repairs.add("bad_status_code")
        return UNKNOWN_STATUS
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            repairs.add("status_code_from_float")
            return int(raw)
        repairs.add("bad_status_code")
        return UNKNOWN_STATUS
    if isinstance(raw, str):
        text = raw.strip()
        try:
            value = int(text)
        except ValueError:
            repairs.add("bad_status_code")
            return UNKNOWN_STATUS
        repairs.add("status_code_from_string")
        return value
    repairs.add("bad_status_code")
    return UNKNOWN_STATUS


def _coerce_text(raw: Any, name: str, repairs: set) -> str:
    """Best-effort string, defaulting to '' with a repair recorded."""
    if raw is None:
        repairs.add("missing_" + name)
        return ""
    if isinstance(raw, str):
        if not raw:
            repairs.add("empty_" + name)
        return raw
    repairs.add("non_string_" + name)
    return str(raw)


# --------------------------------------------------------------------------
# Line parsing
# --------------------------------------------------------------------------


def parse_line(line: str) -> ParseResult:
    """Parse one raw JSONL line.

    Returns a Record, a Malformed with a machine-readable reason, or BLANK for
    empty lines.
    """
    if not line or not line.strip():
        return BLANK

    try:
        payload = json.loads(line)
    except ValueError as exc:
        return Malformed("invalid_json", str(exc))

    if not isinstance(payload, dict):
        return Malformed("not_an_object", "top-level value is %s" % type(payload).__name__)

    if "timestamp" not in payload or payload["timestamp"] is None:
        return Malformed("missing_timestamp", "record has no usable timestamp")

    repairs = set()
    try:
        when, ts_repairs = parse_timestamp(payload["timestamp"])
    except ValueError as exc:
        return Malformed("bad_timestamp", str(exc))
    repairs.update(ts_repairs)

    return Record(
        timestamp=when,
        client_id=_coerce_text(payload.get("client_id"), "client_id", repairs),
        endpoint=_coerce_text(payload.get("endpoint"), "endpoint", repairs),
        status_code=_coerce_status(payload.get("status_code"), repairs),
        request_id=_coerce_text(payload.get("request_id"), "request_id", repairs),
        repairs=frozenset(repairs),
    )


# --------------------------------------------------------------------------
# Endpoint normalisation
# --------------------------------------------------------------------------

_INT_SEGMENT_RE = re.compile(r"^\d+$")
_UUID_SEGMENT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_SEGMENT_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


def normalise_endpoint(path: str) -> str:
    """Collapse identifier-looking path segments into placeholders.

    ``/v1/widgets/123`` and ``/v1/widgets/456`` are the same endpoint hit twice,
    not two endpoints hit once. Without this, a client walking a thousand
    distinct ids registers as a thousand endpoints of one request each and the
    per-endpoint view sees nothing at all -- the classic high-cardinality
    problem that APM tools solve the same way.

    The heuristic is deliberately naive (integers, UUIDs, long hex strings). It
    can be wrong: a genuinely meaningful ``/v1/2024/reports`` becomes
    ``/v1/{id}/reports``. That is why the substitution is visible in the output
    key rather than silent, and why --raw-paths turns it off.
    """
    if not path:
        return path
    parts = path.split("/")
    out = []
    for part in parts:
        if _INT_SEGMENT_RE.match(part):
            out.append("{id}")
        elif _UUID_SEGMENT_RE.match(part):
            out.append("{uuid}")
        elif _HEX_SEGMENT_RE.match(part):
            out.append("{hex}")
        else:
            out.append(part)
    return "/".join(out)
