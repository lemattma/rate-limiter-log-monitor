# Research Notes — Rate-Limit Violation Detection

Working notes for the Circumvent API Traffic Report exercise. `README.md` covers *what was built and
why*; this covers *what was considered and rejected*, with sources, plus the review process that
changed the implementation after it was first working.

---

## 1. What the brief constrains

Several phrases are load-bearing:

| Phrase | What it implies |
|---|---|
| "Identify clients who violate **reasonable** HTTP rate limits" | No threshold is given. The rule *and its justification* are the graded artifact. |
| "Log files are produced by a **variety of third party upstream services owned by other teams**" | Input is hostile: mixed offsets, out-of-order, duplicates, type drift. Malformed handling is a first-class feature. |
| "runs in production as a **platform-owned service for API request observability**" | Observability, not enforcement. Deterministic output, stdout purity, bounded memory. |
| "prints a single **JSON** API traffic report to **stdout**" | The one hard output requirement. Not conditional on anything. |
| "run your program against **a set of input files** with the same command-line contract" | Bare invocation must work with defaults; output stable across unknown files. |
| "Use your best judgement to fill in any product requirement gaps" | The gaps are the test. |

### The scale problem — the actual design question

The sample has `acct_1` sending 6 requests across 9 seconds: **0.7 req/s**. Every published real-world
limit (§5) sits at 2–100 req/s. So a threshold defensible as a real API limit **flags nobody on the
sample**, while one tuned to the sample is indefensible as a real limit and may be wildly wrong on
the graders' other files.

Note that "works at any size" is two different problems. **File size** is trivial. **Traffic
density** is not: 1,000 rows could span a day (nobody is abusing) or one second (everybody is). This
is why the rule is expressed as a **rate**, not a raw count.

---

## 2. Algorithm survey

| Family | Mechanism | Why not / why yes |
|---|---|---|
| **Fixed window** | Bucket into wall-clock windows, count per client | **Rejected.** Boundary problem: `limit` at `10:00:59` plus `limit` at `10:01:00` admits 2× the rate across two seconds. |
| **Sliding window counter** | Weighted blend of current and previous fixed window | **Rejected.** ~O(1) but approximate, and cannot produce the exact "which 6 requests, and when" evidence. |
| **Sliding window log** | Deque of timestamps per client, evict older than `W` | **Adopted.** See below. |
| **Token bucket** | Tokens accrue to a cap; a request spends one | Rejected as primitive — see below. State is `(tokens, last_seen)`, O(1). |
| **Leaky bucket** | A *shaper*, not a policer: queues and smooths | nginx `limit_req` uses this (`rate=1r/s burst=5`; `nodelay` serves the burst immediately). |
| **GCRA** | Token bucket compressed to one "theoretical arrival time" | Elegant, distribution-friendly, no refill process — but "less debuggable than token bucket". |

### Why sliding window log

**`fail2ban` is the direct precedent, and it is the same shape of program as ours** — a log-parsing
tool, not an inline gateway:

> A host is banned if it has generated `maxretry` during the last `findtime` seconds.

Read lines → extract identity → count occurrences inside the window → fire at the limit. That is our
task with `client_id` in place of IP. The industry-standard tool for *detecting abuse from a log
file* uses a sliding window log, not a token bucket.

Three reasons the gateway family loses here despite being the more realistic model:

1. **It simulates rather than measures.** Token bucket answers "would a gateway have rejected this?"
   Sliding window answers "how much did this client actually send in any W-second span?" We are
   writing an observability report; the second question is ours.
2. **Its state is not reportable.** "Minimum token level reached 0.3" cannot be verified by eye
   against the log. The sliding window's own state *is* the evidence: *"6 requests between
   10:00:00Z and 10:00:08Z, limit 5."*
3. **Out-of-order input breaks it.** Refill is computed from `now - last_seen`; a negative delta from
   a clock-skewed upstream needs clamping, making the result order-dependent. The brief explicitly
   warns about multiple third-party producers and mixed `Z`/`+HH:MM` offsets.

The cost is O(N) memory per client — "prohibitively expensive for a high-traffic gateway", true
inline, largely irrelevant offline where the deque is bounded by the client's own burst rate.

### Two windows, not one

No real provider enforces a single number. **GitHub** layers 5,000 req/hour (60 unauthenticated),
≤100 concurrent, ≤900 points/minute (GET=1pt, POST/PATCH/DELETE=5pts), and ≤90s CPU per 60s wall
clock. **Shopify** runs 2 req/s sustained with a 40-request burst bucket. The pattern is universal —
a burst allowance plus a sustained allowance — because they catch different abuse shapes: a tight
flood versus a steady grind. Checking two windows costs a few lines.

---

## 3. Deriving a threshold from the data

This is where most of the design effort went, because it is what "reasonable" leaves open.

### 3a. Percentile-derived — the published answer, and its trap

**Cloudflare API Shield — Volumetric Abuse Detection** derives limits from traffic itself:

- Per-endpoint, **per-session** (not per-IP — fewer false positives behind NAT/CDN).
- Per session it computes the **maximum request rate in a window** (their unit: requests per 10
  minutes), then takes **percentiles across sessions** — p50, p90, p99.
- *"If your p90 value is 83, then 90% of your sessions had maximum request rates less than 83
  requests per 10 minutes."*
- Recalculated over a **rolling 24 hours**; endpoints need 7 days of traffic before any
  recommendation appears.
- They explicitly warn against a raw percentile: *"choosing a single percentile value may cause false
  positives due to a high number of outliers"* — so they blend, and a human confirms before
  enforcement.

Their per-session-max-rate metric is **identical to our per-client peak burst**, which independently
validates that metric.

**The trap:** a percentile threshold flags roughly (1−p) of clients *by construction*. On a file
where everyone is well behaved the correct answer is an empty list, and a pure percentile rule can
never produce one. Cloudflare survives this with millions of sessions, blended percentiles, and human
review. We have none of those. Checked on the sample (`[2, 6]`): interpolated p90 = 5.6, so `acct_1`
is flagged — but by luck of interpolation, since the top client is almost always above p90 at small
n.

### 3b. Why the median, not an upper percentile — the correction that mattered

The first draft used **p95** of the peak-burst distribution. That is wrong for the same reason
mean/stddev is wrong for outlier detection: **p95 sits inside the abusive tail**, so abusers inflate
the very threshold meant to catch them.

Worked counter-example — 10 clients, six normal at 3 req/10s and four abusive at 50:

- p95 of `[3,3,3,3,3,3,50,50,50,50]` = 50 → threshold 50 → **zero flagged**. A silent, total miss.
- median = 3 → threshold `3 × 3 = 9` → **all four abusers flagged**, no false positives.

The median has a 50% breakdown point; percentiles above p50 do not. Verified across five shapes:

| Shape | Peak bursts | Median | Threshold | Result |
|---|---|---|---|---|
| Sample (n=2) | `[2, 6]` | gated off | 5 (floor) | `acct_1` flagged ✅ |
| Dense, one outlier | twenty at 30, one at 300 | 30 | 90 | only the 300 flagged ✅ |
| Heavily contaminated | six at 3, four at 50 | 3 | 9 | all four flagged ✅ |
| Quiet | fifty at 1–2 | 2 | 5 (floor wins) | nobody flagged ✅ |
| Uniformly high, none abusive | twenty-one at 30 | 30 | 90 | nobody flagged ✅ (static flags all 21 ❌) |

That last row is what a static threshold handles *worst*, and is the strongest argument for deriving
from the data at all. All five are pinned as tests in `tests/test_analysis.py`.

**Irreducible limit:** if more than half the clients are abusive the median is contaminated and the
threshold rises above everyone. No relative method survives that — which is what the saturation
warning exists to surface.

### 3c. MAD and the rest of the robust-statistics family — considered, not shipped

```
median   = median(values)
MAD      = median(|value − median|)
robust_z = 0.6745 × (value − median) / MAD        flag when |robust_z| > 3.5
```

**Why MAD over mean/stddev — "masking".** Given `[40, 42, 45, 47, 50, 52, 55, 60, 5000]`: mean = 599
and stddev ≈ 1556 are both dragged up by the abuser, which then scores `(5000−599)/1556 = 2.83`,
*under* the usual 3.0 cutoff. It hides inside the variance it created. Robustly: median = 50, MAD = 5,
so `robust_z(5000) = 667` and `robust_z(60) = 1.35`.

Prior art: Twitter/X's **Seasonal Hybrid ESD** decomposes a series (STL) then runs the Extreme
Studentized Deviate test on residuals, substituting median/MAD *"for data sets that have a high
percentage of anomalies"* — the same masking motivation.

**Rejected because:** MAD collapses to zero when >50% of values are identical (`[5,5,5,5,5,5,100]` →
divide-by-zero, every non-median client scores infinity); it needs ~10+ clients; and it is **silent
on the sample** (two clients → median 4, deviations `[2,2]`, `robust_z(6) = 0.67` — with two points
both are always equidistant, so neither can *ever* be an outlier). The sophisticated layer is silent
exactly where the simple one works.

Same family, also rejected: **IQR/Tukey fence** (`Q3 + 1.5×IQR` lands at 12 on the sample — nothing
flagged); **largest gap in the sorted list** (zero parameters, but always finds a split even when
every client is fine).

**`K × median` delivers the same intent with a fraction of the edge cases**, and explains without
statistics vocabulary: *a client that burst more than 3× harder than the typical one.*

### 3d. What the multiplier costs under a power-law population

Per-client traffic is Zipf distributed. For rank-ordered rates `r_i = C/i` the median sits at rank
`N/2`, so `r_i > k × median` resolves to `i < N/(2k)`:

**a `k × median` rule flags `1/(2k)` of a Zipf population, independent of `N`.**

Verified numerically at N = 200, 2,000 and 20,000 — 16.7% at `k=3`, 5.0% at `k=10`, exact every time.
That makes `--multiplier` readable as a **false-positive budget**, not a severity dial.

It bites far less in practice, and the data says why: peak burst is not distributed like volume.
Burstiness is behavioural and largely independent of tenant size, and small clients quantise to a
peak burst of 1–2, pinning the median low (754 of 1,000 clients sat at ≤2 in a generated Zipf day).
Measured end to end on that log:

| Archetype | Flagged | Share of population |
|---|---|---|
| batch | **100 / 100** | 10% |
| abuser | **20 / 20** | 2% |
| steady | 8 / 550 (1.5%) | 55% |
| diurnal | 8 / 300 (2.7%) | 30% |
| scanner | 0 / 30 | 3% |

Every deliberately bursty client caught, ~2% false positives on the rest. The analytic caveat holds
where peak bursts *themselves* follow a power law.

---

## 4. Adaptive control — the frontier, and why none of it applies

- **Google SRE adaptive throttling** (*Handling Overload*): each client tracks `requests`/`accepts`
  over 2 minutes and self-rejects once `requests/accepts > K`. Notable because the control signal is
  **the backend's own rejections** — the system infers the limit rather than being told it. *Our
  cheap analogue: 429s already in the log.*
- **AWS WAF rate-based rules:** per-IP counting over a 5-minute default window (1/2/10 configurable)
  — AWS's baseline offering is a fixed-threshold sliding window.
- **AWS Shield Advanced:** adaptive baselining, *"most accurate when it has observed 30 days of
  normal traffic"*.
- **CoDel / DAGOR:** overload defined by queueing delay rather than request count.
- **Reinforcement learning:** vendor claims of ~30% fewer false positives. Unverifiable; needs a
  reward signal and training loop.

**Transferable insight:** every mature adaptive system needs **history** (24h, 7d, 30d) and a
**feedback signal**. A single log file with no prior state has neither. That is a principled reason
to stay simple here, not a concession.

---

## 5. Real-world reference limits

| Service | Limit | Algorithm |
|---|---|---|
| Shopify REST Admin | 2 req/s sustained, burst 40 (Plus: 20 req/s) | Leaky bucket |
| Stripe | 100 req/s live, 25 sandbox; 25 req/s default per endpoint | Token-bucket style |
| GitHub REST | 5,000 req/hour authenticated (60 unauthenticated) + secondary limits | Multi-dimensional |
| AWS WAF | Configurable per IP over 1/2/5/10 min | Fixed window |
| nginx `limit_req` | e.g. `rate=1r/s burst=5` | Leaky bucket |

Published limits cluster at **2–100 req/s**; the sample's violator runs at 0.7 req/s. Any shipped
default must be calibrated to the observed scale and say so openly, or the report is vacuous.

---

## 6. The endpoint dimension

Endpoints plainly differ — a polling status endpoint and an expensive report generator should not
share a limit. The question is *where* that belongs, and "just group by path" has two failure modes.

**The brief already splits it.** *"Identify clients who violate…"* keys on the **client**; *"provide
visibility into request counts"* is where the **endpoint** breakdown lives.

**Trap 1 — fragmentation.** Keying violations on `(client, endpoint)` splits a client's traffic and
can hide the thing being looked for: 60 requests in 10s across six endpoints is flagged per-client
but ~10 each per-pair and **missed**. Conversely 6 requests to one expensive endpoint that normally
sees 1 req/min is unremarkable per-client but clearly anomalous with endpoint context. Neither key
dominates — which is why real providers run both (Stripe: global *and* per-endpoint; Cloudflare:
per-endpoint *and* per-session).

**Trap 2 — path cardinality.** `/v1/widgets/123`, `/456`, `/789` grouped raw are three endpoints of
one request each; a client walking 1,000 ids registers as 1,000 endpoints and per-endpoint analysis
sees nothing. This is the classic high-cardinality-label problem, and APM tools all solve it by
templating the path. Cheap insurance: replace numeric and UUID-shaped segments with `{id}`. It can be
wrong (`/v1/2024/reports` → `/v1/{id}/reports`), so it is visible in the output key and overridable
with `--raw-paths` rather than silent.

**Where per-endpoint would genuinely pay:** deriving the threshold per endpoint is more correct than
globally — `/v1/status` and `/v1/reports` have incomparable natural rates and one global median
describes neither. The tension is that it shrinks the population behind each baseline, which is
exactly when derived thresholds degrade. Deferred (`TODO.md`).

---

## 7. Decisions

| Decision | Value | Rationale |
|---|---|---|
| Detection primitive | Sliding window log, keyed on **client** | fail2ban precedent (§2); its state doubles as the report |
| Threshold | `max(floor, K × median_peak_burst)`, `K = 3` | §3b |
| Population gate | 5 distinct clients; below that, floor only | a median over <5 points is dominated by which points you have |
| Floor defaults | 5 req / 10s (burst), 20 req / 60s (sustained) | calibrated to the sample's scale, stated openly; `--limit`/`--window` override |
| Always reported | Peak burst for **every** client, flagged or not | free from the window's state; means a wrong threshold does not make the report worthless |
| Required field | **`timestamp` only** | without a time a record cannot enter a window; every other field degrades to a labelled unknown bucket and still counts |
| Ordering | Bounded reorder min-heap (10,000 records); lateness judged **per client** | §8 |
| Endpoint cardinality | Capped at 10,000 distinct, tail folded into `(other)` | §8 |
| Ranking | `severity` = exceedance ratio, comparable across both windows | §8 |
| Path grouping | `{id}`/`{uuid}` normalisation, query and fragment stripped, `--raw-paths` to disable | asymmetric payoff: a no-op without ids, saves the endpoint view with them |
| Corroboration | 429 counts per client, surfaced as `corroborated` | Google SRE's insight (§4) at zero cost |
| Unattributed traffic | `""` client bucket excluded from thresholds and violations; raised as a warning | §8 |
| Output | JSON to stdout unconditionally; `--output text` for a readable view | the brief's one hard requirement |

**Why this is defensible:** a rate limit is a *contract*, not a *distribution* — which is why the
rule is absolute and deterministic — while the correct absolute number is unknowable from one file,
which is why a derived component and a threshold-free reporting layer sit alongside it.

**Scope note.** Neither the derived threshold nor the calibration warnings read more than one file or
carry state between runs. "Adaptive" here means *relative to the clients inside this single file* —
cohort comparison, not learned baselining. It keys on **density, not row count**.

---

## 8. What structured review changed

After the implementation was working and tested, it was put through three rounds of adversarial
review. That process changed more than the original design work did, and is recorded here because
the corrections are more instructive than the initial decisions.

### Defects found and fixed

| Finding | Defect | Fix |
|---|---|---|
| **Global watermark** | Lateness was judged against a single high-water mark across *all* clients, but the invariant needing protection is per-client. Interleaved producers are each internally ordered and merely offset — the shape a global watermark handles worst. A client whose producer lagged had **every** record excluded and reported `peak_burst: 0`, indistinguishable from "never bursted". | Per-client watermark. Verified: a client sending 10 requests in 10s went from unflagged `peak_burst: 0` to correctly flagged at 10. |
| **Memory claim false** | `O(buffer + clients)` was asserted and "measured" — but the benchmark varied file size and client count, the two variables that *don't* drive the unbounded term, while path normalisation held endpoint cardinality at 7. Two cross-product sets and an unbounded endpoint dict. | Measured 26.0 MB → **279.3 MB** with rows and clients held constant and only endpoint cardinality moving. Deleted the per-client endpoint set; capped endpoint cardinality. Back to **34.9 MB**. |
| **Burst-biased ranking** | Violations sorted on raw burst count, and a sustained-only violator's burst count is by definition at or below the burst threshold — so it sorted beneath *every* burst violator regardless of magnitude. The two counts are not commensurable (one over 10s, one over 60s). | `severity` = exceedance ratio. A 1.50× sustained breach now outranks a 1.20× burst one. |
| **Half the evidence** | A client breaching *both* rules produced prose identical to one that only spiked, because the evidence string was `if burst … else sustained`. | One clause per breached rule, each naming which limit. |
| **Unattributed traffic as a violator** | The `""` bucket (records whose producer dropped `client_id`) flowed into threshold derivation and `violations`. It aggregates arbitrarily many producers, so its peak is systematically high — pulling the median up and making the detector *less* sensitive — and a violation naming `""` names nobody. | Excluded from `peaks()` and `violations`; raised as an `unattributed_traffic` warning pointing at the repair counters, where it is an ingestion finding rather than a rate-limit one. |
| **Two populations** | `_derive` used the tracker population, `_warnings` recomputed `len(self.clients)`. The report could print `"population": 1` beside prose reading "Only 2 client(s) in this input". | Single source; the warning keys on the reason the gate recorded. |
| **Query strings** | `normalise_endpoint` split on `/` only, so `?page=1` and `?page=2` were two endpoints — the same cardinality failure normalisation exists to prevent, sitting on pagination, the commonest way a client generates volume. | Query and fragment stripped; trailing slash folded. |
| **`distinct_endpoints` shipped dead** | Documented in this repo as carrying no signal, yet still emitted. Under default normalisation it was **exactly 7 for every client** on a 1,000-client log. | Deleted. Two replacements were tried and neither separated scanners from busy clients (raw cardinality conflates with volume: a steady client reached 6,612 distinct paths against a scanner's 847; the ratio `distinct/requests` gave 0.398 vs 0.374). Recorded in `TODO.md` rather than replaced with an unvalidated metric. |
| **429s as decoration** | Documented as a design pillar citing Google SRE — a mechanism that *computes a limit from* rejections — while the code incremented a counter and printed a column. | `corroborated` on each violation, used as a ranking tiebreak. A violation corroborated by the upstream gateway's own decisions rests on an independent observer, not on this program's threshold alone. |

### Arguments that survived review

Not everything raised was accepted:

- **Sorted insert instead of a reorder buffer.** Proposed as simpler. But `bisect.insort` followed by
  the eviction line silently swallows any record displaced past the window span — trading a loud
  failure for a quiet one, inverting the principle the rest of the design rests on. Demonstrated: a
  record displaced 497s produced peak 5 where the truth was 6, with nothing counting it.
- **"A `k × median` rule can flag >100% of the population."** False in both the shipped and the
  proposed code: a client is flagged only if `peak > threshold ≥ 0`, so it has a window, so it is in
  the tracker. Flagged is a subset of the population unconditionally.
- **"The ordering inversion is bounded at 1.5×."** This was *my* argument and it was wrong — 1.5 is
  `6 × DEFAULT_BURST_LIMIT / DEFAULT_SUSTAINED_LIMIT`, a property of the static floor. Under derived
  thresholds both medians can coincide; measured **6.00×** on bursty low-volume traffic, which is the
  shape this program exists to find.

### Still open

The reorder buffer is denominated in **records**, but the disorder it corrects is **within-client
displacement in time**. With C clients interleaved, consecutive records from one client sit ~C
positions apart, so the default 10,000 corrects roughly *five* records of within-client disorder at
2,000 clients — a far weaker guarantee than the flag's name implies. The fix is to denominate it in
time (`hold until watermark ≥ ts + T`), which is client-count-independent, with a record-count
backstop that reports when hit. See `TODO.md`.

---

## 9. Note on AI usage

The brief permits AI tools and asks how they were used; this document is part of that answer.

Research was AI-assisted and then **checked against primary sources** — vendor documentation
(Cloudflare, AWS, nginx, GitHub, Shopify, Stripe) and the Google SRE book, rather than blog
summaries. Every quantitative claim used in a decision traces to a source in §10.

Statistical claims were **verified by computation, not accepted**: the p95-versus-median comparison,
the MAD masking example, the Zipf `1/(2k)` relationship, and the behaviour of each rule across five
input shapes were all computed against real data, and the load-bearing ones became tests.

The most useful pattern was **adversarial review** (§8). An implementation that was working, tested
and documented still carried a critical correctness bug, a false headline claim, and a field the
documentation itself described as useless. Every one of those was found by attacking the artifact
rather than by writing more of it.

---

## 10. Sources

**Algorithms**
- nginx — [`ngx_http_limit_req_module`](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)
- NGINX Community — [Rate Limiting with NGINX](https://blog.nginx.org/blog/rate-limiting-nginx)
- Arcjet — [Rate-limiting algorithms compared](https://blog.arcjet.com/rate-limiting-algorithms-token-bucket-vs-sliding-window-vs-fixed-window/)
- Crawlex — [Token bucket, sliding window, and GCRA](https://blog.crawlex.net/blog/rate-limiting-algorithms-defense/)

**Log-based detection**
- [Fail2ban — Gentoo Wiki](https://wiki.gentoo.org/wiki/Fail2ban)
- [Fine-tuning findtime and maxretry](https://dohost.us/index.php/2026/03/05/fine-tuning-findtime-and-maxretry-balancing-user-experience-and-security/)

**Adaptive / statistical**
- Cloudflare — [Volumetric Abuse Detection](https://developers.cloudflare.com/api-shield/security/volumetric-abuse-detection/)
- Google SRE Book — [Handling Overload](https://sre.google/sre-book/handling-overload/)
- AWS — [Shield Advanced layer-7 detection logic](https://docs.aws.amazon.com/waf/latest/developerguide/ddos-event-detection-application.html)
- AWS — [WAF rate limiting options](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rate-limiting-options.html)
- X/Twitter Engineering — [Practical and robust anomaly detection in a time series](https://blog.x.com/engineering/en_us/a/2015/introducing-practical-and-robust-anomaly-detection-in-a-time-series)
- [twitter/AnomalyDetection — S-H-ESD reference](https://rdrr.io/github/twitter/AnomalyDetection/man/AnomalyDetectionTs.html)

**Traffic policing / fairness vocabulary**
- RFC 2697 — [A Single Rate Three Color Marker](https://www.rfc-editor.org/rfc/rfc2697)
- RFC 2698 — [A Two Rate Three Color Marker](https://www.rfc-editor.org/rfc/rfc2698)
- Metwally, Agrawal & El Abbadi — *Efficient Computation of Frequent and Top-k Elements in Data Streams* (Space-Saving)
- Nichols & Jacobson — *Controlling Queue Delay* (CoDel)
- Zhou et al. — *Overload Control for Scaling WeChat Microservices* (DAGOR)
- Netflix `concurrency-limits`; Envoy adaptive concurrency filter

**Published limits**
- [Stripe — Rate limits](https://docs.stripe.com/rate-limits)
- [GitHub — REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [Shopify — REST Admin API rate limits](https://shopify.dev/docs/api/admin-rest/usage/rate-limits)

**Vocabulary note.** In the networking lineage these terms are not interchangeable: **sustained** is
a long-run rate (a token bucket's refill), **burst** is a *quantity* (the bucket's capacity), and
**peak** is a ceiling on instantaneous rate — formalised as CIR/CBS/PIR/PBS in RFC 2697/2698. The
two-window design maps onto that shape: the sustained window is the committed rate, the burst window
the peak-rate ceiling. The output field names `peak_burst` and `peak_sustained` are looser than the
formal vocabulary; renaming was considered and deferred rather than churn a published schema.
