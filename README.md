# API Traffic Report

Reads JSONL API request logs and reports who is exceeding reasonable rate
limits, what the traffic looked like, and what was wrong with the input.

## Repository contents

| File | What it is |
|---|---|
| `README.md` | What was built, the rate-limit rule and why, the output specification, and the assumptions |
| **`RESEARCH.md`** | What was *considered and rejected*, with primary sources — the algorithm survey, why the median beat an upper percentile, the real-world limits the defaults are calibrated against, and what three rounds of adversarial review changed after the code was already working |
| **`TODO.md`** | What was deliberately deferred, including one known weakness in the shipped code |
| `report.py`, `traffic/` | The program |
| `tests/` | 79 tests, no dependencies |
| `tools/generate_traffic.py` | Synthetic traffic generator used for the performance figures below |
| `sample_input/` | The brief's sample log, plus five scenario logs — see *Cookbook* |

`RESEARCH.md` §8 and `TODO.md` are the two worth reading alongside the code: the
first records the defects review found in this implementation and the arguments
that survived it, the second states plainly what is still wrong.

## Running it

**Requires Python 3.9 or newer. No third-party dependencies — nothing to install.**

```bash
python3 report.py sample_input/requests.jsonl
```

Also accepts stdin and multiple files:

```bash
cat requests.jsonl | python3 report.py
python3 report.py logs/*.jsonl
./report.py sample_input/requests.jsonl      # executable, shebanged
```

### Output style

**stdout is JSON, unconditionally.** The brief states one hard output requirement
— *"prints a single JSON API traffic report to stdout"* — so nothing about the
environment changes it. A harness that allocates a pty (`docker -t`, pexpect,
some CI runners) receives exactly the bytes a pipe does.

```bash
python3 report.py logs.jsonl                 # JSON
python3 report.py logs.jsonl > report.json   # JSON
python3 report.py logs.jsonl | jq .summary   # JSON
python3 report.py --output text logs.jsonl   # readable summary
```

When stdout is a terminal, a one-line note about `--output text` is written to
**stderr** — the diagnostics channel, which no pipe or redirect captures, so it
cannot contaminate the report.

```
API TRAFFIC REPORT
sample_input/requests.jsonl  -  8 lines read

SUMMARY
  requests  clients  endpoints  violating  malformed  blank
  --------  -------  ---------  ---------  ---------  -----
         8        2          2          1          0      0
  window  2024-01-15T10:00:00Z  ->  2024-01-15T10:05:05Z   (5m 5s)

RATE LIMITS
  window     effective limit     rate  how
  ---------  ---------------  -------  ---------------------------------------------------
  burst          5 req / 10s    0.5/s  static floor - only 2 client(s), 5 needed to derive
  sustained     20 req / 60s  0.333/s  static floor - only 2 client(s), 5 needed to derive

  [!] insufficient_population
      Only 2 client(s) in this input -- too few to derive a threshold from the population, so the
      static floor of 5 requests / 10s (0.50 req/s) was applied instead.
      -> If you know the intended limit, pass --limit N --window SECONDS.

VIOLATIONS (1)
  client  rule    sev  peak   rate  busiest window        requests  429s
  ------  -----  ----  ----  -----  --------------------  --------  ----
  acct_1  burst  1.2x     6  0.6/s  10:00:00 -> 10:00:08         6     0
```

The text view is rendered from the finished JSON document rather than computed
separately, so the two can never disagree. Its one deliberate difference is that
tables are truncated to the busiest `--top` rows (default 20) with the remainder
stated — a 2,000-row table is not something anyone reads. **JSON output is never
truncated.**

### Tests

```bash
python3 -m unittest discover -s tests -t .
```

79 tests, no dependencies, well under a second.

### Options

Every option has a working default; the bare invocation above is the intended
usage.

| Flag | Default | Purpose |
|---|---|---|
| `--limit N` | `5` | Burst allowance: requests permitted per burst window |
| `--window SECONDS` | `10` | Burst window length |
| `--sustained-limit N` | `20` | Sustained allowance per sustained window |
| `--sustained-window SECONDS` | `60` | Sustained window length |
| `--multiplier K` | `3.0` | How many times the typical client's burst counts as a violation |
| `--min-population N` | `5` | Clients required before a threshold is derived from the data |
| `--no-adaptive` | off | Use the static floor only; never derive from the input |
| `--raw-paths` | off | Group endpoints by raw path instead of collapsing ids |
| `--reorder-buffer N` | `10000` | Records buffered to correct out-of-order timestamps |
| `--malformed-samples N` | `5` | Malformed lines quoted in the report for debugging |
| `--output STYLE` | `auto` | `text` for a readable summary; `auto` and `json` both print JSON |
| `--max-endpoints N` | `10000` | Distinct endpoints tracked before the tail is folded into `(other)` |
| `--top N` | `20` | Rows per table in text output; JSON is never truncated |
| `--indent N` | `2` | JSON indentation; `0` for a single compact line |
| `--version` | — | Print the version and exit |

### Cookbook — the options in action

`sample_input/` carries small scenario logs, each built to make one group of
options visible. All run instantly.

| File | Shape | Makes visible |
|---|---|---|
| `requests.jsonl` | the brief's sample, 2 clients | the population gate and the static floor |
| `cohort.jsonl` | 10 clients: 8 at 6 req/10s, 2 at 30 | `--multiplier`, `--no-adaptive` |
| `saturated.jsonl` | 6 clients all at 30 req/10s, no outlier | `--min-population`, why adaptive exists |
| `dirty.jsonl` | every defect class the parser survives | `--malformed-samples`, repairs |
| `paths.jsonl` | ids, UUIDs and query strings in paths | `--raw-paths`, `--max-endpoints` |
| `out_of_order.jsonl` | two interleaved producers, one backwards record | `--reorder-buffer` |

**Deriving a threshold vs. imposing one.** The cohort's typical client bursts at
6, so the derived limit lands at `3 × 6 = 18` and catches only the two genuine
abusers. The static floor of 5 flags all ten:

```bash
python3 report.py --output text sample_input/cohort.jsonl
#   threshold 18 (derived_median, median 6)   ->  2 of 10 flagged

python3 report.py --no-adaptive sample_input/cohort.jsonl
#   threshold 5  (static_floor)               -> 10 of 10 flagged, high_flagged_ratio

python3 report.py --multiplier 10 sample_input/cohort.jsonl
#   threshold 60 (derived_median, median 6)   ->  0 of 10 flagged
```

`--multiplier` reads as a false-positive budget: raising it from 3 to 10 raises
the bar from 3× the typical client to 10×.

**Why the derived threshold is the default.** In `saturated.jsonl` every client
is equally busy and none is out of line with any other, so the correct answer is
an empty violations list — which a static floor cannot produce:

```bash
python3 report.py sample_input/saturated.jsonl
#   threshold 90 -> 0 of 6 flagged, population_saturated

python3 report.py --no-adaptive sample_input/saturated.jsonl
#   threshold 5  -> 6 of 6 flagged, high_flagged_ratio

python3 report.py --min-population 10 sample_input/saturated.jsonl
#   6 clients < 10 required -> falls back to the floor, insufficient_population
```

**Hostile input.** `dirty.jsonl` is 14 lines containing a stringified status
code, a float status, a naive timestamp, an epoch timestamp, an offset timestamp
with two fractional digits, truncated JSON, a JSON array, a missing timestamp, an
unparseable timestamp, a blank line, a record with no `client_id`, an empty
`client_id`, an uncoercible status and unknown extra fields:

```bash
python3 report.py --output text sample_input/dirty.jsonl
#   accepted 8, discarded 5, blank 1, repaired 8
#   discarded: invalid_json 2, not_an_object 1, missing_timestamp 1, bad_timestamp 1
#   repaired:  status_code_from_string, status_code_from_float, timestamp_assumed_utc,
#              timestamp_from_epoch, timestamp_space_separator, missing_client_id, ...

python3 report.py --malformed-samples 0 sample_input/dirty.jsonl   # counts only, no quoted lines
```

Nothing is silently dropped: every discard has a reason and every coercion is
named.

**Endpoint cardinality.** `paths.jsonl` has 12 widget ids, 6 paginated URLs and
4 UUID order paths — 22 distinct raw strings describing 3 real routes:

```bash
python3 report.py sample_input/paths.jsonl
#   3 endpoints: /v1/widgets/{id}, /v1/reports, /v2/orders/{uuid}/items

python3 report.py --raw-paths sample_input/paths.jsonl
#   22 endpoints: /v1/reports?page=1, ?page=2, ... -- the per-endpoint view is now noise

python3 report.py --max-endpoints 2 sample_input/paths.jsonl
#   2 tracked + "(other)", 4 requests folded -- memory bounded, and the report says so
```

**Out-of-order producers.** `out_of_order.jsonl` interleaves two producers, each
internally ordered but offset by ten minutes, plus one record genuinely backwards
within its own client:

```bash
python3 report.py --reorder-buffer 0 sample_input/out_of_order.jsonl
#   late 1 -- only the genuinely backwards record
#   acct_A peak_burst 8, records_excluded 1
#   acct_B peak_burst 8, records_excluded 0
```

With reordering disabled entirely, the lagging producer is still measured
correctly, because lateness is judged per client rather than against a global
high-water mark. Only the one record that actually goes backwards inside its own
client is excluded, and the client row says so.


---

## What the program does, end to end

1. **Read** one line at a time from the given files, or stdin if none. The whole
   file is never held in memory.
2. **Parse** each line into a record, leniently — see *Parsing* below.
3. **Reorder** records through a bounded min-heap so the sliding windows receive
   them in timestamp order despite interleaved upstream producers, judging
   lateness per client.
4. **Measure** each client's traffic with two sliding windows (burst and
   sustained), recording the busiest window ever observed for every client.
5. **Derive** the effective limit from the observed traffic, floored by a static
   default.
6. **Report** counts, peak bursts for every client, violations with evidence,
   ingestion problems, and any calibration warnings — as one JSON document on
   stdout, or as a readable summary with `--output text`.

Diagnostics go to stderr; stdout carries only the report, so it is safe to pipe.
Exit code is `0` for any data condition (including an all-malformed file) and
`2` only for a usage or file-access error.

---

## The rate-limit rule, and why

### The detection primitive: a sliding window log

For each client, keep the timestamps still inside a rolling window; the largest
count ever observed is that client's peak burst.

The precedent is **fail2ban**, which solves the same shape of problem — find
abusive clients by reading a log file — with the same mechanism: count
occurrences for one identity inside `findtime` seconds and fire at `maxretry`.
That matters, because the more famous rate-limiting algorithms solve a
*different* problem.

Token bucket, leaky bucket and GCRA are built for cheap inline enforcement. They
were considered and rejected here for three reasons:

- **They simulate rather than measure.** A token bucket answers "would a gateway
  have rejected this request?" A sliding window answers "how much traffic did
  this client actually send in any 10-second span?" This is an observability
  tool; the second question is the one we were asked.
- **Their state is not reportable.** "Bucket reached 0.3 tokens" cannot be
  verified by eye against the log. "6 requests between 10:00:00Z and 10:00:08Z"
  can, and the sliding window's own state *is* that evidence.
- **They are order-dependent.** Refill is computed from `now - last_seen`, so a
  clock-skewed upstream producing a negative delta needs clamping and the result
  depends on arrival order. The brief warns explicitly about multiple
  third-party producers.

### Two windows, not one

Defaults are **5 requests / 10s (burst)** and **20 requests / 60s (sustained)**.
No real provider enforces a single number: Shopify runs 2 req/s sustained with a
40-request burst allowance; GitHub layers an hourly limit, a concurrency limit, a
points-per-minute limit and a CPU-time limit. The two windows catch different
shapes of abuse — a tight flood versus a steady grind.

On the sample log, `acct_1` sends 6 requests across 9 seconds. That trips the
burst rule and not the sustained rule, which is the correct reading: it is a
short spike, not a sustained campaign.

The two-window shape is the traffic-policing pattern from networking, expressed
as sliding windows rather than as chained token buckets. In RFC 2698 terms the
sustained window is the committed rate (CIR) and the burst window is the peak
rate ceiling (PIR); conforming to both is green, exceeding the burst window only
is the yellow band, and exceeding both is red. Naming that lineage matters
because it is *why* one number cannot do the job: a single rate cannot express
both long-run throughput and how much clumping is tolerated.

### The threshold: `max(floor, 3 × median peak burst)`

This is the part the brief leaves open with the word *reasonable*, and it is
where most of the design effort went.

**Why a static number alone is not enough.** Published limits cluster between 2
and 100 req/s (Shopify 2/s, Stripe 100/s, GitHub 5000/hour). The busiest client
in the sample log runs at **0.7 req/s**. So a threshold defensible as a
real-world API limit flags *nobody* on the data this program is meant to
analyse — while a threshold tuned to the sample is indefensible as a real limit
and may be wildly wrong on a denser file. The program will be run against files
whose traffic density cannot be known in advance.

**Why the median, and not a percentile.** The first design used p95 of the
per-client peak-burst distribution. That is wrong for the same reason mean and
standard deviation are wrong for outlier detection: **p95 sits inside the
abusive tail**, so abusers inflate the very threshold meant to catch them. Given
ten clients where six are normal at 3 requests/window and four abuse at 50:

| Statistic | Value | Threshold | Clients flagged |
|---|---|---|---|
| p95 | 50 | 50 | **0** — silent, total miss |
| median | 3 | 9 | **4** — all abusers, no false positives |

The median has a 50% breakdown point; percentiles above p50 do not. This is
pinned down as an executable assertion in
`tests/test_analysis.py::test_heavily_contaminated_population`, which asserts
both that our rule flags all four *and* that p95 would have flagged none.

**Why a floor underneath it.** A purely relative rule flags roughly a fixed
share of clients no matter how well behaved they are. The floor means a quiet
file correctly produces an empty violations list.

**What the multiplier actually buys you.** Per-client traffic is typically
power-law distributed, and under a pure Zipf distribution a `k × median` rule
flags exactly `1/(2k)` of the population *regardless of how many clients there
are* — 16.7% at the default `k=3`, 5% at `k=10`. So `--multiplier` is best read
as a false-positive budget rather than as a severity setting.

In practice this bites far less than the arithmetic suggests, because peak burst
is not distributed like volume: burstiness is a behavioural property largely
independent of tenant size, and small clients quantise to a peak burst of 1 or 2
which pins the median low. Measured on a generated Zipf-shaped day of 1,000
clients, the default caught **100% of the deliberately bursty clients (batch and
abuser archetypes) while flagging only 1.5–2.7% of steady and diurnal ones**. The
caveat is still worth knowing: if a population's *peak bursts* genuinely follow a
power law, raise `k`.

**Why a population gate.** Below five distinct clients there is no meaningful
cohort: one client is at least a quarter of the population, so any "typical"
figure just reads off the extremes. Below the gate the program falls back to the
floor and says so in the output. On the two-client sample this is exactly what
happens — and `acct_1` is still correctly caught.

Behaviour across the shapes an unknown input file might take:

| Input shape | Median | Threshold | Result | Static floor alone |
|---|---|---|---|---|
| Sample (2 clients: 6, 2) | gated off | 5 | `acct_1` flagged ✅ | `acct_1` flagged ✅ |
| 20 clients at 30, one at 300 | 30 | 90 | only the outlier ✅ | all 21 flagged ❌ |
| 6 normal at 3, 4 abusive at 50 | 3 | 9 | all four abusers ✅ | all four ✅ |
| 50 clients at 1–2 | 2 | 5 (floor) | nobody ✅ | nobody ✅ |
| 21 clients all at 30, none abusive | 30 | 90 | nobody ✅ | all 21 flagged ❌ |

That last row is the case a static threshold handles worst, and it is the
strongest argument for deriving from the data at all.

**The irreducible limit.** If more than half the clients are abusive, the median
is contaminated and the threshold rises above everyone. No relative method
survives that. Rather than pretend otherwise, the program detects the condition
and warns.

### Corroboration: 429s already in the log

If an upstream gateway already returned 429 to a client, that is evidence rather
than inference — the same insight behind Google SRE's adaptive throttling, which
derives its limit from the backend's own rejections.

It is used rather than merely displayed: each violation carries `corroborated`,
and corroborated violations break ties in the ranking. A client flagged by the
window rule *and* carrying upstream 429s has been independently judged by the
gateway; one flagged with zero 429s rests on this program's threshold alone. That
distinction is worth surfacing precisely because the threshold is the part of
this design least certain to suit an unknown input.

### This is a detector, not an enforcer

Worth stating explicitly, because it licenses the whole approach. Detection can
afford to be slow and statistical; enforcement has to be cheap and immediate.
A production stack separates them — a detector computes limits, and a token
bucket or queue applies them inline. This program is purely the first half, which
is why a median over the whole file is an acceptable thing to compute here and
would be an unacceptable thing to compute in a request path.

### The layer that makes a wrong threshold survivable

**Peak burst is reported for every client, flagged or not.** It falls out of the
sliding window's state for free, and it means the threshold can be wrong without
the report becoming worthless: whatever the density of the input, the reader can
still see who was fastest, how fast, and exactly when. Cloudflare's volumetric
abuse detection uses the same metric — per-session maximum request rate — as the
basis for its own recommendations.

---

## Output specification

One JSON object on stdout (see *Output style* above for when the readable view is
used instead; it is a projection of this same document). Key order is stable and rows are sorted
deterministically (violations by severity, everything else busiest first, ties
broken alphabetically), so two runs over
the same input are byte-identical and two reports can be diffed meaningfully.

| Section | Contents |
|---|---|
| `schema_version` | `"1.0"` |
| `input` | `sources`, `lines_read` |
| `summary` | `requests`, `clients`, `endpoints`, `endpoints_folded`, `malformed`, `blank_lines`, `first_seen`, `last_seen`, `violating_clients` |
| `ingestion` | `records_accepted`, `records_late`, `blank_lines`, `malformed` (total, `by_reason`, `samples`), `repairs` (total, `by_kind`) |
| `rate_limits` | `path_normalisation`, `adaptive`, `thresholds`, `violations`, `warnings` |
| `clients` | one row per client — `requests`, `records_excluded`, `first_seen`, `last_seen`, `status_classes`, `rate_limited_responses`, `peak_burst`, `peak_sustained`, `violations` |
| `endpoints` | one row per endpoint — `requests`, `distinct_clients`, `status_classes` |

`thresholds` carries the effective limit *and how it was reached*, so no number
in the report is unexplained:

```json
"burst": {
  "window_seconds": 10.0,
  "floor": 5,
  "floor_rate_per_second": 0.5,
  "population": 2,
  "value": 5,
  "source": "static_floor",
  "reason": "insufficient_population",
  "required_population": 5
}
```

Each violation carries its evidence, and a `severity` — the exceedance ratio,
which is the only figure comparable across the two windows and is what the list
is sorted by:

```json
{
  "client_id": "acct_1",
  "violated": [
    "burst"
  ],
  "severity": 1.2,
  "corroborated": false,
  "requests": 6,
  "peak_burst": {
    "count": 6,
    "window_seconds": 10.0,
    "rate_per_second": 0.6,
    "start": "2024-01-15T10:00:00Z",
    "end": "2024-01-15T10:00:08Z"
  },
  "peak_sustained": {
    "count": 6,
    "window_seconds": 60.0,
    "rate_per_second": 0.1,
    "start": "2024-01-15T10:00:00Z",
    "end": "2024-01-15T10:00:08Z"
  },
  "rate_limited_responses": 0,
  "evidence": "6 requests between 2024-01-15T10:00:00Z and 2024-01-15T10:00:08Z exceeds the burst limit of 5 per 10s."
}
```

`corroborated` is true when the upstream gateway already answered that client
with 429s. Such a violation rests on an independent observer rather than on this
program's threshold alone, so it breaks ties in the ordering.

`warnings` is where the program admits to uncertainty rather than hiding it.
Each carries a machine-readable `code`, a human `message`, and a concrete
`suggestion`:

- `insufficient_population` — too few clients to derive a threshold; floor applied
- `population_saturated` — the typical client already exceeds the floor, so
  either this traffic is uniformly high-rate or the majority are abusive, and a
  derived threshold cannot tell those apart
- `high_flagged_ratio` — most of the population was flagged, which more likely
  means a mis-calibrated threshold than mass abuse
- `unattributed_traffic` — records arrived with no usable `client_id`; the
  aggregate is reported here rather than as a violation, since it names nobody
- `late_records` — records arrived too far out of order to be placed; burst
  figures may be understated

---

## Implementation decisions and assumptions

### Parsing is lenient, but never silent

This program diagnoses rate-limit problems; it is not a schema validator. The
brief says logs come from "a variety of third party upstream services owned by
other teams", which is a warning that near-miss records are normal: a stringified
status code, a naive timestamp, a missing field. Discarding those would throw
away exactly the traffic being measured — and would bias the measurement toward
whichever producers happen to serialise correctly.

So coercion is best-effort, but **every repair is counted and reported by kind**,
because the malformed count is the feedback channel back to the teams producing
the bad records.

**Only `timestamp` is structurally required.** Without a usable time a record
cannot enter a sliding window at all, so it is unusable rather than merely
incomplete. Every other field degrades to a labelled unknown bucket and still
counts toward that client's traffic.

| Input | Outcome |
|---|---|
| Blank / whitespace line | counted as `blank_lines`, **not** malformed — every well-formed file ends with a newline |
| Not valid JSON | discarded, `invalid_json` |
| Valid JSON, not an object | discarded, `not_an_object` |
| `timestamp` absent or null | discarded, `missing_timestamp` |
| `timestamp` unparseable | discarded, `bad_timestamp` |
| `status_code` as `"200"` or `200.0` | repaired to `200` |
| `status_code` as `"abc"`, `true`, `200.5` | `unknown` bucket, repair recorded |
| `timestamp` with no offset | assumed UTC, repair recorded |
| `timestamp` as epoch seconds | accepted, repair recorded |
| `timestamp` with a space instead of `T` | accepted, repair recorded |
| `client_id` / `endpoint` missing or empty | `""` bucket, repair recorded |
| Unknown extra fields | ignored |

The `""` client bucket is annotated in the output so a reader does not mistake it
for one real client, and it is **excluded from threshold derivation and from
violations**. It aggregates every producer that dropped the field, so its peak is
the combined traffic of arbitrarily many unrelated clients: left in the
population it is a systematically high sample that pulls the median up and makes
the detector *less* sensitive, and a violation naming `""` names nobody to
investigate.

A producer dropping client IDs at volume is still worth knowing about — but it is
an **ingestion** finding, and the report already has the right home for it. It
surfaces as the `unattributed_traffic` warning, pointing at the
`missing_client_id` / `empty_client_id` repair counters.

### Timestamps are normalised by hand, deliberately

Python 3.9's `datetime.fromisoformat` rejects a trailing `Z` — which the sample
data uses exclusively — and rejects fractional seconds of any precision other
than 3 or 6 digits. Python 3.11+ accepts both.

This is a trap rather than an inconvenience: developing on 3.14 and delegating to
`fromisoformat` produces code that works perfectly on the development machine and
fails on a stock Mac. Normalising `Z` and fractional-second precision by hand
before delegating keeps behaviour identical from 3.9 to 3.14, and
`tests/test_parsing.py` pins it.

### Out-of-order input is corrected, within a bound

Independently-produced upstream streams interleave, so records arrive out of
order, and a sliding window fed unordered timestamps is silently wrong. Records
pass through a fixed-size min-heap keyed on timestamp, correcting anything
displaced by fewer than `--reorder-buffer` positions.

Anything displaced further is counted as a **late record**: it still counts
toward request totals — it is a real request — but is withheld from the window
computation, because a backwards timestamp corrupts the deque's eviction
invariant. The report says how many were affected, in total and **per client**,
so `peak_burst: 0` can be read correctly rather than mistaken for "never
bursted". Silently producing a slightly wrong number would have been the worse
option.

**Lateness is judged per client, not against a global high-water mark.** The
invariant that needs protecting belongs to one client's deque, which is
indifferent to what any other client did. An earlier version used a global
watermark, which is strictly stricter than the invariant requires — and the
excess strictness is not conservative, it discards real traffic. Interleaved
producers are typically each internally ordered and merely offset from one
another, which is the shape a global watermark handles worst: a client whose
producer lagged had *every* record excluded and reported a peak burst of zero.

### Memory is bounded

`O(buffer + clients + min(endpoints, --max-endpoints))`. Lines are read one at a
time; each client's deque holds only timestamps still inside the window.

The endpoint term is the one that had to be *bounded* rather than merely counted.
Endpoint cardinality is attacker-controlled — paths carry identifiers, and with
`--raw-paths` a single client can mint a new endpoint per request. An earlier
version of this README claimed `O(buffer + clients)` and called it measured; the
benchmark varied file size and client count, the two variables that do *not*
drive that term, while path normalisation held endpoint cardinality at 7. Holding
rows and clients fixed and moving only endpoint cardinality produced **26.0 MB
against 279.3 MB**. The cap (default 10,000, tail folded into `(other)`) brings
that to 34.9 MB with request totals still exact. Client cardinality remains
unbounded in principle — see `TODO.md`.

### Path normalisation is on by default

`/v1/widgets/123` and `/v1/widgets/456` are the same endpoint hit twice, not two
endpoints hit once. Without normalisation a client walking a thousand distinct
ids registers as a thousand endpoints of one request each, and the per-endpoint
view sees nothing at all.

The heuristic is deliberately naive — integers, UUIDs, long hex strings — and it
can be wrong: a genuinely meaningful `/v1/2024/reports` becomes
`/v1/{id}/reports`. That is why the substitution is **visible in the output key**
rather than silent, and why `--raw-paths` turns it off. The payoff is
asymmetric: a no-op on logs without ids, and it rescues the endpoint view on logs
with them.

### Violations are keyed on the client

The brief splits this: *"identify clients who violate…"* keys on the client,
while *"provide visibility into request counts"* is where the endpoint breakdown
belongs.

`distinct_endpoints` was removed rather than shipped. Under default
normalisation it counted endpoint *templates*, so a client walking a thousand
widget ids and one touching a single id reported the same handful — on a
generated 1,000-client log, every client reported exactly 7. A column identical
for every row conveys nothing, and the name invites a reading the data does not
support. Detecting endpoint-walking needs raw path cardinality relative to
request volume, which is a different measurement; it was tried, could not be
shown to separate scanners from merely-busy clients, and was not shipped
unvalidated. See `TODO.md`.

### Assumptions made

- Timestamps without an offset are UTC.
- A file may contain records from any number of clients and endpoints; nothing
  assumes the sample's two-and-two shape.
- Multiple input files are concatenated into a single report rather than
  reported separately.
- Duplicate `request_id`s are not deduplicated (see `TODO.md`).
- Each invocation sees one input in isolation — no state is carried between
  runs, so "adaptive" means *relative to the clients inside this input*, not
  learned baselining. Cloudflare needs 24 hours of rolling history for that and
  AWS Shield Advanced needs 30 days; one file supports neither.

---

## Performance

Measured on a 1.42 GB, 10.2-million-row synthetic log covering a full day
(2,000 clients, diurnal traffic curve, mixed timezone offsets, 0.5% malformed
lines). `tools/generate_traffic.py` reproduces it:

```bash
python3 tools/generate_traffic.py --rows 10000000 --clients 2000 --output /tmp/day.jsonl
python3 report.py /tmp/day.jsonl > report.json
```

| Input | Rows | Clients | Size | Wall time | Peak RSS |
|---|---|---|---|---|---|
| smoke | 25,827 | 50 | 4 MB | 0.3 s | 26.0 MB |
| small | 538,678 | 100 | 71 MB | 5.9 s | 26.2 MB |
| small | 538,678 | 500 | 75 MB | 6.5 s | 28.3 MB |
| small | 538,678 | **5,000** | 132 MB | 12.7 s | **50.9 MB** |
| **full day** | **10,175,081** | 2,000 | **1,423 MB** | 143 s | **35.4 MB** |

Roughly **71,000 rows/second**, and the figures are arranged to separate the
three variables that could drive memory rather than to flatter the result:

- **File size does not.** Going from a 71 MB input to a 1,423 MB input — twenty
  times the rows — moves peak memory from 26.2 MB to 35.4 MB, and even that rise
  is explained by client count rather than row count.
- **Client count does.** Holding rows fixed at ~538k and going from 100 to 5,000
  clients moves peak memory from 26.2 MB to 50.9 MB.
- **Endpoint cardinality does too** — which is the term the first version of this
  table missed entirely, because every run in it had endpoint cardinality pinned
  at 7 by path normalisation. Holding rows *and* clients fixed and varying only
  endpoint cardinality: **26.0 MB against 279.3 MB**, now capped to 34.9 MB.

Which gives roughly **25 MB of interpreter baseline plus ~5 KB per client**, flat
in file size, with the endpoint term bounded by `--max-endpoints`.

### Two design decisions, measured

**Path normalisation is not cosmetic.** On the same 538k-row log containing
scanner-style clients that walk a wide id space:

| Mode | Endpoints tracked | Folded into `(other)` |
|---|---|---|
| default (normalised) | **7** | 0 |
| `--raw-paths` | 10,001 (the cap) | 71,730 |
| `--raw-paths --max-endpoints 1000000` | **81,718** | 0 |

Without normalisation the per-endpoint view is 81,718 rows of near-noise and
tells you nothing. With it, `/v1/widgets/{id}` is a single line carrying 2 million
requests. The middle row is the cap doing its job: memory stays bounded and the
report states how much detail was folded away, rather than either growing without
limit or silently losing rows.

**The reorder buffer degrades honestly — and matters less than it looked.**
Running the same 2M-row slice at different buffer sizes:

| `--reorder-buffer` | Late records | Violators found | Warning |
|---|---|---|---|
| 10,000 (default) | 0 | 173 | — |
| 100 | 0 | 173 | — |
| 10 | 3,443 | 173 | `late_records` |

Starving the buffer to 10 records still finds every violator, and the handful of
records it cannot place are counted and warned about rather than silently
dropped.

This table used to read 557,683 late records and 155 violators at `--reorder-buffer 10`.
Almost all of that was an artifact of judging lateness against a global watermark
rather than per client: interleaved producers each ordered internally were being
marked late for being offset from one another. Fixing that removed 99.4% of the
"late" records and the entire violator discrepancy — which is also the honest
measure of how much the buffer was really contributing.

### What it found in the day-long log

237 of 2,000 clients exceeded the derived threshold of 18 requests / 10s
(median peak burst 6 × 3), at severities between 6.36× and 6.85× over. All six of
the busiest carried **over 2,500 upstream 429 responses** and are marked
`corroborated` — the sliding-window detector and the upstream gateway's own
throttling decisions agree, having been computed completely independently of each
other. The `population_saturated` warning fired correctly, because the typical
client in this generated log already bursts above the static floor.

---

## What I'd do differently with more time

`TODO.md` has the full list. The items I would take first:

1. **Duplicate `request_id` handling.** At-least-once delivery is plausible from
   third-party producers, and double-counting inflates burst figures into false
   accusations. It is deferred only because the right deduplication strategy
   depends on delivery semantics I would want to confirm with the producing
   teams rather than guess.
2. **Per-endpoint derived baselines.** `/v1/status` and `/v1/reports` have
   incomparable natural rates, so one global median describes neither.
   Cloudflare keys its detection on the endpoint for exactly this reason. The
   blocker is that it shrinks the population behind each baseline, which is
   precisely when derived thresholds degrade — so it needs a fallback design,
   not just a different group-by.
3. **Property-based tests over the parser.** For a program whose entire premise
   is hostile third-party input, "no input string can raise an unhandled
   exception" is the guarantee worth having, and table-driven tests cannot
   provide it.
4. **Bounded client cardinality.** A top-K sketch would make memory genuinely
   constant rather than constant-plus-clients.
5. **Validation against real traffic.** Every threshold decision here is
   reasoned from published limits and the shape of the sample. A day with a real
   log would beat all of that reasoning.

I would also want to **challenge the schema itself**: without an HTTP verb there
is no way to weight requests by cost, which is how GitHub and Stripe actually
model this. That is a limitation of the input, not of the approach.

---

## Use of AI tools

Used throughout, and the brief invites it, so here is the honest accounting.

**Design was the collaborative part, and it is where the value was.** The
work happened as an extended back-and-forth: I would propose an approach, and it
would be pushed on until it broke. Several conclusions in this README are the
result of that process reversing an earlier decision:

- The threshold statistic changed from **p95 to the median** after working
  through a population where abusers are a large minority and finding p95 flags
  nobody. That correction is the single most important decision in the program.
- The detection primitive was reconsidered when **fail2ban** surfaced as a closer
  precedent than API gateways — same problem shape (abuse detection from a log
  file), same mechanism, and a much better justification than "sliding windows
  are easier to explain".
- An early plan to **sort records per client** was scrapped once it was pointed
  out that it quietly assumed the whole file in memory; the bounded reorder
  buffer replaced it.
- The **zero-install argument was overstated** at first and corrected: the brief
  explicitly permits any language and asks for the runtime version, so low setup
  friction is a tiebreaker, not a requirement.

**Research was AI-assisted and then verified against primary sources.** Vendor
documentation (Cloudflare API Shield, AWS WAF/Shield, nginx, GitHub, Shopify,
Stripe) and the Google SRE book, rather than blog summaries. Every quantitative
claim used in a decision traces to a primary source; the full trail is in
`RESEARCH.md`.

**Statistical claims were verified by computation, not accepted.** The p95-versus-median
comparison, the MAD masking example, and the behaviour of each threshold rule
across five input shapes were all computed against real data rather than asserted
— and the important ones became tests, so the reasoning is checkable rather than
merely stated.

**Implementation was largely AI-written**, against a design that was settled
first. It was reviewed line by line, and run against both Python 3.9 and 3.14
because the version gap between a development machine and a stock Mac is exactly
where AI-written code tends to assume a modern runtime — the
`fromisoformat` trap above is a real instance that testing on 3.9 caught.

**Review was the most productive part.** After the implementation was working,
tested and documented, three rounds of adversarial review still found a critical
correctness bug, a false headline claim about memory, and a field this README
itself described as useless. `RESEARCH.md` §8 records what each round changed —
and the arguments that did not survive it, including one where I was wrong.
