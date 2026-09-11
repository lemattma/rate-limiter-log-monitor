#!/usr/bin/env python3
"""Generate a synthetic day of API traffic for performance testing.

Not part of the shipped program -- a development tool for producing large,
realistically-shaped inputs so the streaming path can be measured rather than
assumed.

The output deliberately includes everything the parser is supposed to cope with:
a diurnal traffic curve, clients of several different behavioural archetypes,
identifier-bearing paths, mixed timezone offsets, records interleaved out of
order the way independent upstream producers would deliver them, and a small
proportion of malformed lines.

    python3 tools/generate_traffic.py --rows 5000000 --output /tmp/day.jsonl
"""

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# Endpoint pool. Several carry identifiers so that path normalisation has
# something to collapse -- without them the per-endpoint view of a realistic log
# is meaningless.
ENDPOINTS = [
    ("/v1/widgets", 0.24, None),
    ("/v1/widgets/{}", 0.20, "int"),
    ("/v1/reports", 0.12, None),
    ("/v1/reports/{}", 0.08, "uuid"),
    ("/v1/status", 0.16, None),
    ("/v1/search", 0.10, None),
    ("/v2/orders/{}/items", 0.10, "int"),
]

# (status, weight) for ordinary traffic.
NORMAL_STATUSES = [(200, 0.82), (201, 0.05), (204, 0.04), (400, 0.03), (404, 0.03), (500, 0.02), (503, 0.01)]

ARCHETYPES = (
    # name, share of clients, relative weight, burstiness
    ("steady", 0.55, 1.0),
    ("diurnal", 0.30, 1.6),
    ("batch", 0.10, 2.5),
    ("scanner", 0.03, 2.0),
    ("abuser", 0.02, 4.0),
)


def diurnal_factor(hour):
    """Traffic multiplier by hour, peaking mid-afternoon."""
    return 0.35 + 0.65 * (0.5 * (1 - math.cos(2 * math.pi * (hour - 3) / 24.0)))


def pick(rng, weighted):
    roll = rng.random()
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if roll <= cumulative:
            return value
    return weighted[-1][0]


class Client:
    __slots__ = ("cid", "kind", "weight", "endpoints", "error_rate", "burst_at")

    def __init__(self, cid, kind, weight, rng):
        self.cid = cid
        self.kind = kind
        self.weight = weight
        self.error_rate = rng.choice([0.0, 0.0, 0.02, 0.08, 0.25])
        # Scanners walk a wide id space; everyone else reuses a handful.
        self.endpoints = ENDPOINTS
        # Abusers and batch jobs concentrate their traffic into a few windows
        # rather than spreading it evenly across the day.
        if kind == "abuser":
            self.burst_at = sorted(rng.sample(range(0, 86400, 60), rng.randint(3, 6)))
        elif kind == "batch":
            self.burst_at = sorted(rng.sample(range(0, 86400, 3600), rng.randint(2, 4)))
        else:
            self.burst_at = []


def build_clients(count, rng, zipf=0.0):
    """Build the client population.

    With zipf=0 every client of an archetype carries that archetype's weight,
    which produces a flat-ish population. Real API traffic is power-law
    distributed instead -- a handful of very large tenants and a long tail of
    small ones -- and that shape materially changes how a population-relative
    threshold behaves, so it is worth being able to generate it.
    """
    clients = []
    cid = 0
    for kind, share, weight in ARCHETYPES:
        n = max(1, int(round(count * share)))
        for _ in range(n):
            cid += 1
            clients.append(Client("acct_%05d" % cid, kind, weight, rng))
    rng.shuffle(clients)
    if zipf > 0:
        # Assign Zipf ranks across the shuffled population so archetype and
        # size are independent: a big tenant can be any behavioural kind.
        for rank, client in enumerate(clients, start=1):
            client.weight *= 1.0 / (rank ** zipf)
    return clients


def format_timestamp(when, rng):
    """RFC 3339, mostly Z but sometimes a real offset."""
    roll = rng.random()
    if roll < 0.80:
        text = when.strftime("%Y-%m-%dT%H:%M:%S")
        if rng.random() < 0.3:
            text += ".%03d" % (when.microsecond // 1000)
        return text + "Z"
    offset_hours = rng.choice([-8, -5, -3, 1, 2, 5.5, 9])
    shifted = when + timedelta(hours=offset_hours)
    sign = "+" if offset_hours >= 0 else "-"
    total = abs(offset_hours)
    return "%s%s%02d:%02d" % (
        shifted.strftime("%Y-%m-%dT%H:%M:%S"),
        sign,
        int(total),
        int(round((total - int(total)) * 60)),
    )


def endpoint_for(client, rng):
    path, _, kind = pick(rng, [(e, w) for e, w, _ in ENDPOINTS]), None, None
    for candidate, _weight, id_kind in ENDPOINTS:
        if candidate == path:
            kind = id_kind
            break
    if kind == "int":
        # Scanners walk a wide id space; everyone else revisits a few.
        span = 500000 if client.kind == "scanner" else 200
        return path.format(rng.randint(1, span))
    if kind == "uuid":
        return path.format(
            "%08x-%04x-%04x-%04x-%012x"
            % (
                rng.getrandbits(32),
                rng.getrandbits(16),
                rng.getrandbits(16),
                rng.getrandbits(16),
                rng.getrandbits(48),
            )
        )
    return path


def make_malformed(rng, when, seq):
    """One of the input defects the parser is expected to survive."""
    kind = rng.randint(0, 6)
    if kind == 0:
        return "this is not json at all"
    if kind == 1:
        return '{"request_id":"broken_%d","timestamp":' % seq
    if kind == 2:
        return "[1, 2, 3]"
    if kind == 3:  # missing timestamp
        return json.dumps({"request_id": "m_%d" % seq, "client_id": "acct_00001", "endpoint": "/v1/widgets", "status_code": 200})
    if kind == 4:  # unparseable timestamp
        return json.dumps({"request_id": "m_%d" % seq, "timestamp": "yesterday", "client_id": "acct_00001", "endpoint": "/v1/widgets", "status_code": 200})
    if kind == 5:
        return ""  # blank line
    # Coercible, not malformed: string status code and a naive timestamp.
    return json.dumps(
        {
            "request_id": "m_%d" % seq,
            "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
            "client_id": "acct_00002",
            "endpoint": "/v1/widgets",
            "status_code": "200",
        }
    )


def generate(args):
    rng = random.Random(args.seed)
    clients = build_clients(args.clients, rng, args.zipf)
    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=UTC)

    bucket_seconds = 300
    buckets = 86400 // bucket_seconds

    # Weight each bucket by the diurnal curve so the day has a realistic shape.
    bucket_weights = []
    for index in range(buckets):
        hour = (index * bucket_seconds) / 3600.0
        bucket_weights.append(diurnal_factor(hour))
    weight_total = sum(bucket_weights)

    client_weight_total = sum(c.weight for c in clients)
    seq = 0
    written = 0

    out = open(args.output, "w", buffering=1024 * 1024)
    try:
        for index in range(buckets):
            bucket_start = day + timedelta(seconds=index * bucket_seconds)
            bucket_rows = int(round(args.rows * bucket_weights[index] / weight_total))
            if bucket_rows <= 0:
                continue

            events = []
            for client in clients:
                share = client.weight / client_weight_total
                count = int(bucket_rows * share)
                # Distribute the remainder probabilistically so small clients
                # are not systematically rounded out of existence.
                if rng.random() < (bucket_rows * share - count):
                    count += 1

                # Burst archetypes dump a concentrated volume when one of their
                # burst windows falls inside this bucket.
                bucket_from = index * bucket_seconds
                bucket_to = bucket_from + bucket_seconds
                for burst in client.burst_at:
                    if bucket_from <= burst < bucket_to:
                        count += rng.randint(200, 900) if client.kind == "abuser" else rng.randint(60, 200)

                for _ in range(count):
                    offset = rng.random() * bucket_seconds
                    events.append((offset, client))

            events.sort(key=lambda item: item[0])

            for offset, client in events:
                when = bucket_start + timedelta(seconds=offset)
                seq += 1

                if rng.random() < args.malformed_rate:
                    out.write(make_malformed(rng, when, seq) + "\n")
                    written += 1
                    continue

                if client.kind == "abuser" and rng.random() < 0.15:
                    status = 429  # the upstream gateway already throttled them
                elif rng.random() < client.error_rate:
                    status = rng.choice([400, 401, 403, 404, 429, 500, 503])
                else:
                    status = pick(rng, NORMAL_STATUSES)

                out.write(
                    json.dumps(
                        {
                            "request_id": "req_%09d" % seq,
                            "timestamp": format_timestamp(when, rng),
                            "client_id": client.cid,
                            "endpoint": endpoint_for(client, rng),
                            "status_code": status,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                written += 1

            if args.progress and index % 24 == 0:
                sys.stderr.write(
                    "\r  %5.1f%%  %d rows" % (100.0 * index / buckets, written)
                )
                sys.stderr.flush()
    finally:
        out.close()

    if args.progress:
        sys.stderr.write("\r  100.0%%  %d rows\n" % written)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000, help="approximate number of lines")
    parser.add_argument("--clients", type=int, default=500, help="distinct client ids")
    parser.add_argument("--date", default="2024-01-15", help="UTC day to simulate")
    parser.add_argument("--seed", type=int, default=20240115, help="RNG seed for reproducibility")
    parser.add_argument("--malformed-rate", type=float, default=0.005, help="fraction of defective lines")
    parser.add_argument(
        "--zipf",
        type=float,
        default=0.0,
        help="Zipf exponent for client sizes (0 = flat, 1.0 = classic power law).",
    )
    parser.add_argument("--output", required=True, help="destination path")
    parser.add_argument("--progress", action="store_true", help="report progress on stderr")
    args = parser.parse_args()

    written = generate(args)
    sys.stderr.write("wrote %d rows to %s\n" % (written, args.output))


if __name__ == "__main__":
    main()
