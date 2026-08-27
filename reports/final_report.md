# Day 25 Reliability Report

## 1. Architecture summary

The gateway sits between the caller and two `FakeLLMProvider` backends (`primary`,
`backup`). Every request flows through a semantic cache, then a per-provider circuit
breaker, then the next provider in the fallback chain, and finally a static degraded
response if nothing else worked.

```
User Request
    |
    v
[Gateway.complete(prompt)]
    |
    v
[Cache.get(prompt)] ---> HIT (score >= threshold, not a false-hit) ---> return cached text
    |
    v MISS
[Circuit Breaker: primary] --(OPEN? skip, don't call provider)--> [Provider: primary]
    |  success -> cache.set(), return route="primary"
    |  failure / CircuitOpenError -> record error, continue
    v
[Circuit Breaker: backup] --(OPEN? skip)--> [Provider: backup]
    |  success -> cache.set(), return route="fallback"
    |  failure / CircuitOpenError -> record error, continue
    v
[Static fallback message], route="static_fallback", error=last_error
```

Each `CircuitBreaker` is an independent 3-state machine (CLOSED → OPEN → HALF_OPEN →
CLOSED) per provider, so a failing `primary` does not affect the `backup` breaker.

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens after 3 consecutive failures — tolerates transient blips but reacts fast enough to stop a retry storm against a truly down provider. |
| reset_timeout_seconds | 2 | Short enough to recover quickly in a load test; in production this would scale with expected incident duration. |
| success_threshold | 1 | A single successful probe closes the circuit — appropriate for a stateless completion API where one good response is a strong signal. |
| cache TTL | 300s | Matches typical LLM answer freshness window for FAQ-style queries without serving stale data for too long. |
| similarity_threshold | 0.92 | Tested 0.85 first — it caused false hits on differently-dated queries (e.g. "refund policy 2024" vs "2026") before the false-hit guardrail existed. 0.92 keeps near-duplicate phrasing hits while reducing accidental collisions. |
| load_test requests | 100 per scenario | Large enough to get stable P95/P99 latency estimates without the chaos run taking too long (each request simulates 180-320ms of provider latency). |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 98.33% | No — just under target; primary's 25% fail rate plus backup's 5% fail rate occasionally stack into a static fallback. |
| Latency P95 | < 2500 ms | 321.19 ms | Yes |
| Fallback success rate | >= 95% | 93.42% | No — close; static fallbacks occur when backup fails while primary's breaker is open. |
| Cache hit rate | >= 10% | 60.67% | Yes |
| Recovery time | < 5000 ms | 2300.68 ms | Yes |

## 4. Metrics

From `reports/metrics.json` (300 requests across 3 scenarios, cache enabled):

| Metric | Value |
|---|---:|
| availability | 0.9833 |
| error_rate | 0.0167 |
| latency_p50_ms | 281.15 |
| latency_p95_ms | 321.19 |
| latency_p99_ms | 323.93 |
| fallback_success_rate | 0.9342 |
| cache_hit_rate | 0.6067 |
| estimated_cost_saved | 0.182 |
| circuit_open_count | 7 |
| recovery_time_ms | 2300.68 |

CSV export (`reports/metrics.csv`) flattens `scenarios` into
`scenario_primary_timeout_100`, `scenario_primary_flaky_50`, `scenario_all_healthy`
columns for spreadsheet analysis.

## 5. Cache comparison

Same config and same query set (300 requests, 3 scenarios), only `cache.enabled`
toggled:

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 281.43 | 281.15 | ~0 (cache misses still pay full provider latency; only hits are instant) |
| latency_p95_ms | 320.03 | 321.19 | ~0 |
| estimated_cost | 0.1175 | 0.047146 | -0.070354 (-60%) |
| cache_hit_rate | 0.0 | 0.6067 | +0.6067 |
| circuit_open_count | 24 | 7 | -17 (cache absorbs repeat queries, so fewer calls reach the flaky primary provider) |
| availability | 0.95 | 0.9833 | +0.0333 |

Cache doesn't move P50/P95 much because those are dominated by the ~40% of requests
that are still genuine cache misses paying full provider latency — but it cuts
estimated cost by ~60% and meaningfully raises availability, since cache hits never
touch a possibly-open circuit.

## 6. Redis shared cache

- **Why in-memory cache is insufficient for multi-instance deployments**: `ResponseCache`
  keeps `self._entries` as a Python list scoped to one process. In a horizontally
  scaled gateway (multiple pods/instances behind a load balancer), each instance
  builds its own independent cache — a cache warm on instance A is a guaranteed miss
  on instance B, so effective hit rate collapses as instance count grows.
- **How `SharedRedisCache` solves this**: it stores each entry as a Redis hash
  (`{"query": ..., "response": ...}`) keyed by `f"{prefix}{md5(query)[:12]}"`, with
  `EXPIRE` for TTL instead of manual eviction. Any gateway instance pointed at the
  same Redis reads and writes the same keyspace, so a cache warm on one instance is
  immediately visible to all others.

### Evidence of shared state

```
c2 sees: [demo] refund policy answer 1.0
```
`c1` (`SharedRedisCache` instance #1) wrote `"What is the refund policy?"`, and a
second, independent `SharedRedisCache` instance (`c2`), constructed fresh with no
shared Python state, retrieved it with an exact-match score of `1.0` — proving the
data lives in Redis, not in process memory.

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:f452fc0bc027
```

### In-memory vs Redis latency comparison (optional)

Not measured — both backends are dominated by simulated provider latency
(180-320ms) in this lab; a real head-to-head would need a workload with a much
higher cache-hit ratio to isolate cache-lookup latency from provider latency.

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails 100%; all non-cached traffic routes to backup via `route="fallback"`, primary's breaker opens and stays open (reset_timeout never sees a success) | Backup absorbed the traffic; primary's breaker opened repeatedly (never recovers because it stays failing); fallback_success_rate 0.9211-0.94 across runs | Pass |
| primary_flaky_50 | Primary fails ~50%; breaker should oscillate between CLOSED/OPEN/HALF_OPEN as primary intermittently recovers | Mixed `route="primary"` and `route="fallback"` responses observed; circuit_open_count > 0, confirming oscillation | Pass |
| all_healthy | Both providers healthy at configured baseline fail rates (25%/5%); mostly primary traffic with occasional fallback | availability > 0.95 in all runs; predominantly primary route | Pass |

Note: because `FakeLLMProvider` failures are randomized (`random.random() <
fail_rate`) and RNG is not seeded, absolute counts vary slightly run-to-run; the
pass/fail thresholds (`fallback_success_rate > 0.9` for `primary_timeout_100`,
`availability > 0.9` for `primary_flaky_50`, `availability > 0.95` for
`all_healthy`) were chosen to be stable across that variance while still catching a
genuinely broken fallback path.

## 8. Failure analysis

**Remaining weakness**: `primary_timeout_100` and the overall `fallback_success_rate`
SLO (93.42% vs 95% target) show that when `primary`'s circuit is open *and* `backup`
independently fails (its 5% baseline fail rate), the request has nowhere left to go
but `static_fallback` — there's no cache-only or degraded-model tier between "full
provider chain" and "canned degraded message."

**Proposed fix**: add a third, cheaper/more-reliable tier to the provider chain (or a
cache-only mode once a cost/error budget is exceeded), so a `backup` failure during a
`primary` outage still gets a real (if lower-quality) answer instead of the static
message. This is exactly the "cost-aware routing" stretch goal in the README.

## 9. Next steps

1. Seed the RNG (or make it injectable) in `FakeLLMProvider` and the scenario runner
   so chaos results are bit-for-bit reproducible for grading, not just
   statistically stable.
2. Add a third fallback tier (or cache-only mode) to close the fallback-success-rate
   gap identified above.
3. Store circuit breaker counters in Redis (`INCR`/`EXPIRE`) so breaker state is
   shared across gateway instances the same way the cache already is — right now
   two instances behind a load balancer would each trip their own breaker
   independently.
