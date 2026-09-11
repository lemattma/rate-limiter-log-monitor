# TODO — deferred work

Considered and deliberately left out. Reasoning for each rejection is in `RESEARCH.md`; this is the
actionable list.

## Known weakness in shipped code

- **The reorder buffer is denominated in the wrong unit.** `--reorder-buffer` counts *records in the
  merged stream*, but what it has to correct is *within-client displacement in time*. With C clients
  interleaved, consecutive records from one client sit roughly C positions apart, so the default
  10,000 corrects about **five** records of within-client disorder at 2,000 clients — far weaker than
  the flag's name implies, and its correct value depends on a variable the operator neither controls
  nor knows in advance.

  The fix is a time watermark: hold a record until a timestamp ≥ `record.ts + T` has been seen. `T`
  is client-count-independent and expressible as something an operator actually knows ("upstream
  clocks skew by up to 30s"), where "10,000 records" is a statement about nothing. Two things go with
  it: a **record-count backstop** that emits its own condition code when hit, so the memory trade
  fails loudly rather than silently; and `T ≥ max(burst_window, sustained_window)`, which guarantees
  anything the buffer fails to place is already older than the eviction cutoff and so cannot corrupt
  a deque's ordering invariant.

## Detection

- **Per-`(client, endpoint)` violation keys.** Violations are keyed on the client, matching the
  brief's wording. Keying on the pair as a *secondary* section would catch a client hammering one
  expensive endpoint while staying unremarkable overall — the same sliding-window machinery with a
  different key. The trade-off is fragmentation: a client spreading 60 requests across six endpoints
  looks quiet on every individual pair.
- **Per-endpoint derived baselines.** `/v1/status` and `/v1/reports` have incomparable natural rates,
  so one global median describes neither. Cloudflare keys its volumetric detection on the endpoint
  for exactly this reason. Deferred because keying per endpoint shrinks the population behind each
  baseline, which is precisely when derived thresholds degrade — so it needs a fallback design, not
  just a different group-by.
- **Configurable per-endpoint limit map.** Real providers publish different limits per route
  (Stripe's Files API at 20/s against a 100/s global). Needs configuration this program cannot infer
  from a log alone.
- **Cost-weighted metering.** Charging the limiter in units of work — CPU time, rows scanned, bytes —
  rather than requests, so a flood of cheap calls and a trickle of expensive queries stop looking
  identical. GitHub charges 1 point for a GET and 5 for a POST. Not implementable against this
  schema: there is no HTTP verb and no cost field.
- **MAD / robust z-score outlier layer.** `RESEARCH.md` §3c. Rejected because it divides by zero when
  more than half the values are identical, needs roughly ten clients to say anything, and is silent
  on the two-client sample. `K × median` delivers the same intent with a fraction of the edge cases.
- **A working scanning signal.** `distinct_endpoints` was removed rather than kept: under default
  normalisation it counted templates, so it was near-constant across clients and carried no
  information about endpoint-walking. Raw path cardinality is the obvious replacement but conflates
  scanning with volume (a high-volume steady client reached 6,612 distinct paths against a scanner's
  847), and normalising it as `distinct_paths / requests` failed to separate them either — 0.398
  against 0.374. A working version likely needs entropy over the path distribution or a new-path
  arrival rate, plus bounded memory, plus real traffic to calibrate against. Note the generator's own
  scanner archetype turned out not to model scanning convincingly, which is part of why this could
  not be validated.
- **Multi-scale windows beyond burst and sustained.** A third daily window would catch slow grinders
  that stay under both current limits.

## Ingestion

- **Duplicate `request_id` handling.** At-least-once delivery from upstream producers is plausible,
  and double-counting inflates burst figures into false accusations. Deferred because deduplication
  needs either an unbounded seen-set or a bounded probabilistic one, and the choice depends on
  delivery semantics that would need confirming with the producing teams.
- **Unbounded client cardinality.** Endpoint cardinality is now capped (tail folded into `(other)`),
  but the per-client term is still unbounded in principle. A top-K sketch (Space-Saving, which
  guarantees finding everything above `1/m` of the stream) or HyperLogLog for the distinct counts
  would bound it, at the cost of exact figures for the long tail.
- **Better endpoint folding.** The cap currently keeps the first N endpoints seen and folds the rest.
  Keeping the *busiest* N — again Space-Saving — would be more useful, since arrival order is
  arbitrary.
- **Compressed input.** Transparently reading `.gz` / `.zst` logs.
- **Configurable path normalisation.** The heuristic is fixed (integers, UUIDs, long hex).
  User-supplied route templates would remove the `/v1/2024/reports` false positive.

## Output and operations

- **NDJSON output mode.** One JSON object per client per line, for direct ingestion into a metrics
  pipeline rather than a single document.
- **Cross-file merge.** Multiple input files are currently concatenated into one report. Per-file
  sections with a merged summary would suit batch processing better.
- **Prometheus / OpenMetrics exposition** for the counters, so the report can feed a dashboard.
- **Exit code on threshold breach**, so the program can gate a CI job rather than always exiting 0.
- **`--since` / `--until` filters** for scoping a report to an incident window.
- **Rename `peak_burst` / `peak_sustained`.** Looser than the formal CIR/PIR vocabulary
  (`RESEARCH.md` §10) — `peak_sustained` is close to oxymoronic. Deferred rather than churn a
  published schema.

## Testing

- **Property-based tests** (Hypothesis) over the parser, asserting that no input string can raise an
  unhandled exception — the strongest guarantee available for a program whose whole premise is
  hostile third-party input, and the one thing table-driven tests cannot provide.
- **Large-file benchmark in CI.** `tools/generate_traffic.py` and the README's *Performance* figures
  establish the baseline; asserting against them would catch a regression that reintroduces
  whole-file buffering. The endpoint-cardinality case belongs in that assertion specifically — it is
  the variable whose absence hid the original memory defect.
