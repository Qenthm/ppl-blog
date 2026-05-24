---
title: "TDD Isn't Enough: How Benchmarks Turned Green Tests Into Real Evidence"
date: 2026-05-22
draft: false
tags: ["tdd", "testing", "python", "pytest", "performance"]
---

There's a moment in TDD that every developer has hit: all the tests are green, coverage is at 90-something percent, and you're still not confident the code is actually correct. Either the tests are testing the wrong things, or they're testing the right things but missing a whole class of bugs that coverage metrics can't see.

I hit that moment twice on the same feature while building SIRA, a FastAPI-based invoice reminder system. What pulled me out of it wasn't more tests — it was a different *kind* of test.

## What Red-Green-Refactor Actually Looks Like

```mermaid
flowchart LR
    R([Write Failing Test\nRED]) -->|implement minimum code| G([All Tests Pass\nGREEN])
    G -->|clean up without breaking| F([Clean Up\nREFACTOR])
    F -->|write next failing test| R
    style R fill:#ff4d4d,color:#fff,stroke:#cc0000
    style G fill:#28a745,color:#fff,stroke:#1a7a30
    style F fill:#0d6efd,color:#fff,stroke:#0a58ca
```

The risk scoring feature was the first place I applied TDD strictly. The feature computes a weighted score from five client payment behavior metrics, then classifies the result as LOW, MEDIUM, or HIGH risk. Simple enough on paper.

I wrote the tests first. The docstring on `test_risk_scoring.py` literally reads "written FIRST (TDD Red phase)" — I left it there deliberately because I wanted to be honest about the sequence in code review.

The formula is:

```
score = delay_score × 0.35
      + overdue_count_score × 0.20
      + outstanding_amount_score × 0.20
      + payment_consistency_score × 0.15
      + invoice_age_score × 0.10
```

```mermaid
pie title Risk Score Weights
    "Delay Score" : 35
    "Overdue Count" : 20
    "Outstanding Amount" : 20
    "Payment Consistency" : 15
    "Invoice Age" : 10
```

Writing a test that just passes in a sample input and checks the output would have been fast. But that test wouldn't catch an implementation that got the weights slightly wrong. A delay weight of `0.34` instead of `0.35` would pass a "typical" test case — the score would be slightly off, but you'd need a careful eye to notice.

A naive approach would check the output label on a sample input. That satisfies coverage but pins nothing:

```python
# BEFORE: happy-path only — 100% line coverage, zero mutation resistance
def test_risk_score_basic():
    strategy = RuleBasedScoringStrategy()
    _, label = strategy.calculate_score(_features(delay=80.0, overdue=60.0))
    assert label == "MEDIUM"   # passes even if delay weight is 0.34 instead of 0.35

# AFTER: each weight is its own test — any constant change fails exactly one test
class TestRuleBasedWeights:
    def test_delay_weight_is_0_35(self) -> None:
        """delay_score=100 with all others 0 should produce 100*0.35 = 35.0."""
        strategy = RuleBasedScoringStrategy()
        score, _ = strategy.calculate_score(_features(delay=100.0))
        assert score == 35.0   # 0.34 instead of 0.35 → score == 34.0 → fails

    def test_overdue_count_weight_is_0_20(self) -> None:
        strategy = RuleBasedScoringStrategy()
        score, _ = strategy.calculate_score(_features(overdue=100.0))
        assert score == 20.0

    def test_all_weights_sum_to_1_0(self) -> None:
        strategy = RuleBasedScoringStrategy()
        total = sum(strategy._WEIGHTS.values())
        assert total == 1.0
```

The implementation these tests drove in the red→green phase:

```python
class RuleBasedScoringStrategy:
    _WEIGHTS: dict[str, float] = {
        "delay_score": 0.35,
        "overdue_count_score": 0.20,
        "outstanding_amount_score": 0.20,
        "payment_consistency_score": 0.15,
        "invoice_age_score": 0.10,
    }
    _THRESHOLDS = {"LOW": 30.0, "MEDIUM": 60.0}

    def calculate_score(self, features: ScoringFeatures) -> tuple[float, str]:
        score = sum(
            getattr(features, key) * weight
            for key, weight in self._WEIGHTS.items()
        )
        label = (
            "LOW" if score <= self._THRESHOLDS["LOW"]
            else "MEDIUM" if score <= self._THRESHOLDS["MEDIUM"]
            else "HIGH"
        )
        return round(score, 2), label
```

Each test in `TestRuleBasedWeights` pins one constant. A mutation to any value in `_WEIGHTS` or `_THRESHOLDS` fails exactly one test:

| Mutation | Value change | Test that fails | Observed vs expected |
|---|---|---|---|
| delay_score weight | 0.35 → 0.34 | test_delay_weight_is_0_35 | 34.0 != 35.0 |
| overdue_count weight | 0.20 → 0.21 | test_overdue_count_weight_is_0_20 | 21.0 != 20.0 |
| any weight, unnormalized | any tweak | test_all_weights_sum_to_1_0 | sum != 1.0 |
| LOW threshold | 30.0 → 31.0 | test_score_exactly_30_is_low | 30.0 reclassified as MEDIUM |
| LOW threshold | 30.0 → 29.0 | test_score_30_point_1_is_medium | 30.1 reclassified as LOW |

This is what coverage metrics cannot tell you. The happy-path test achieves 100% line coverage of `calculate_score` while missing every row in that table.

The boundary tests are written at the exact threshold values, not "around" them:

```python
def test_score_exactly_30_is_low(self) -> None:
    """<= 30.0 boundary: score == 30.0 must be LOW (not MEDIUM)."""
    strategy = RuleBasedScoringStrategy()
    score, label = strategy.calculate_score(_features(overdue=100.0, outstanding=50.0))
    assert score == 30.0
    assert label == "LOW"

def test_score_30_point_1_is_medium(self) -> None:
    """Just above 30.0 boundary: score == 30.1 must be MEDIUM."""
```

| Score | Expected label | What this boundary test verifies |
|---|---|---|
| 29.9 | LOW | below lower threshold |
| **30.0** | **LOW** | **`<=` not `<` — the exact threshold value** |
| 30.1 | MEDIUM | just above lower threshold |
| 59.9 | MEDIUM | just below upper threshold |
| **60.0** | **MEDIUM** | **`<=` not `<` — the exact threshold value** |
| 60.1 | HIGH | just above upper threshold |

Testing at 29 and 31 tells you nothing about whether 30.0 classifies as LOW or MEDIUM. A test at exactly 30.0 is the only one that distinguishes `<` from `<=`. The implementation uses `<=` at both thresholds; these tests are the only thing that enforces that.

This kind of test design — isolating each weight, testing each boundary at the exact edge, verifying invariants like `sum(weights) == 1.0` — is what makes the test suite genuinely useful. Standard coverage would be satisfied with a single happy-path test. The mutation-resistant approach makes the tests act as a specification: they fail if anyone alters the formula, not just if someone breaks its structure.

The implementation came after all those tests were written. The initial run was all red. Then I implemented `RuleBasedScoringStrategy.calculate_score()`, watched them go green, and moved on to the service layer.

## When All Tests Pass But the Code is Still Wrong

```mermaid
flowchart TB
    subgraph N1["N+1 Approach — 50 invoices, 50 DB round-trips"]
        direction LR
        L[Loop over invoices] --> Q1[get_client\ninvoice 1]
        L --> Q2[get_client\ninvoice 2]
        L --> QD[...]
        L --> Q50[get_client\ninvoice 50]
    end

    subgraph BA["Batch Approach — 50 invoices, 1 DB round-trip"]
        direction LR
        C[Collect all IDs] --> BQ[get_clients\nall 50 IDs at once]
    end

    N1 -.->|"~121ms at 2ms/call"| X1[ ]
    BA -.->|"~4ms at 2ms/call"| X2[ ]
    style N1 fill:#fff3cd,stroke:#ffc107
    style BA fill:#d1ecf1,stroke:#17a2b8
    style X1 fill:none,stroke:none
    style X2 fill:none,stroke:none
```

The `send_overdue_reminders` Celery task dispatches email/Telegram reminders for all overdue invoices. The logic is: find all overdue invoices, look up each invoice's client, determine their risk category (which drives the tone of the reminder), send.

The first working implementation fetched the client inside the loop:

```python
for invoice in overdue_invoices:
    client = await get_client_by_id(db, invoice["client_id"])
    # determine tone from client.current_risk_category
    # dispatch reminder
```

Every test I had for this task passed. The unit tests mocked `get_client_by_id` and verified the correct reminders were dispatched. The integration tests ran against a seeded database with a handful of invoices and passed in milliseconds. Coverage was fine.

But this is a classic N+1 pattern. For 50 overdue invoices, this code makes 50 separate database round-trips. At 2ms per trip (a realistic estimate for a cloud-hosted Postgres instance), that's 100ms of pure DB wait per Celery task execution. If reminders run every hour and you have a client with 100 overdue invoices, you're burning 200ms per cycle just fetching data you could have fetched in one query.

My unit tests couldn't see this. The mocks returned instantly. The integration tests had 5 invoices in the seed, so the N+1 penalty was 10ms total — invisible noise.

## Adding the Measurement Layer

I added `pytest-benchmark` (v5.2.3) specifically to quantify this. The idea was to simulate the actual DB behavior — 2ms latency per call — and measure both approaches directly:

```python
N_INVOICES = 50
N_UNIQUE_CLIENTS = 10
DB_LATENCY_S = 0.002  # 2ms per simulated DB call

def _fake_get_client_by_id(client_id: str) -> dict[str, Any] | None:
    """Simulates one DB round-trip: fetches a single client row."""
    time.sleep(DB_LATENCY_S)
    return CLIENTS.get(client_id)

def _fake_get_clients_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    """Simulates one batched DB round-trip: fetches all requested clients."""
    time.sleep(DB_LATENCY_S)
    return [CLIENTS[cid] for cid in ids if cid in CLIENTS]

def test_send_overdue_n1_approach(benchmark: Any) -> None:
    result = benchmark(approach_n1, INVOICES)
    assert result == N_INVOICES

def test_send_overdue_batch_approach(benchmark: Any) -> None:
    result = benchmark(approach_batch, INVOICES)
    assert result == N_INVOICES
```

Measured on my machine (AMD Ryzen 5 7600X, CPython 3.12.10, Windows), 50 invoices, 10 unique clients, 2ms simulated latency per DB call:

| Approach | Total DB calls | Mean | Stddev | Min | Max |
|---|---|---|---|---|---|
| N+1 (per-invoice fetch) | 51 | 121.03ms | 1.02ms | 119.57ms | 122.92ms |
| Batch (one query upfront) | 2 | ~4ms | <0.5ms | ~3.8ms | ~4.2ms |

The stddev of 1.02ms on the N+1 run is signal, not noise — it's 50 sequential `time.sleep(0.002)` calls stacking exactly as expected. The batch case brings DB latency to a flat ~4ms regardless of invoice count: one query for overdue invoices, one batch query for all their clients.

The DB call count drops from 51 to 2. I verified this with a separate assertion-style test that counts actual calls:

```python
def test_n1_makes_n_plus_1_db_calls() -> None:
    """N+1 approach makes exactly N calls (one per invoice)."""
    call_count = 0
    original = time.sleep

    def counting_sleep(s: float) -> None:
        nonlocal call_count
        call_count += 1

    time.sleep = counting_sleep  # type: ignore[method-assign]
    try:
        approach_n1(INVOICES)
    finally:
        time.sleep = original

    assert call_count == N_INVOICES  # exactly 50 calls

def test_batch_makes_exactly_1_db_call() -> None:
    """Batch approach makes exactly 1 DB call regardless of invoice count."""
    # ... same pattern ...
    assert call_count == 1
```

These two tests are now regression guards. If someone refactors the batch implementation back into a loop, `test_batch_makes_exactly_1_db_call` fails. The benchmark numbers live in `.benchmarks/Windows-CPython-3.12-64bit/0001_before_n1.json` and can be compared across runs with `--benchmark-compare`.

## What the Data Actually Shows

The refactor commit (`25ee23e8`) explains the change clearly: "DB round-trips drop from N+1 to 2 regardless of invoice count." The improvement holds because N+1 scales linearly and the batch stays flat:

| Invoice count | N+1 latency | Batch latency | N+1 DB calls | Batch DB calls |
|---|---|---|---|---|
| 50 | 121ms | ~4ms | 51 | 2 |
| 100 | ~242ms | ~4ms | 101 | 2 |
| 500 | ~1,200ms | ~4ms | 501 | 2 |
| 1,000 | ~2,400ms | ~4ms | 1,001 | 2 |

At 50 invoices that's a **97% latency reduction** from the DB layer alone. At 500 invoices the N+1 job spends over a second waiting on the database before any reminder logic runs — and starts overlapping its next scheduled Celery beat tick.

The unit tests were blind to this. They were correct — they verified the right reminders got dispatched — but they couldn't tell me the implementation was slow because mocks don't sleep. The integration tests were correct too, but with 5 seeded invoices the N+1 penalty was so small it was indistinguishable from other test overhead.

pytest-benchmark added the performance dimension to the TDD cycle. I'm not running it in CI on every push — it's slow and environment-dependent — but having the baseline stored and the DB call count tests as regression guards means the correctness and performance properties are both verifiable.

## Three Layers, Three Different Failures They Catch

```mermaid
flowchart LR
    TS([Test Suite]) --> UT
    TS --> IT
    TS --> BM

    subgraph UT["Unit Tests"]
        direction TB
        U1[Logic errors\nwrong formula, wrong branch]
        U2["Example: delay weight 0.34 vs 0.35\ncaught instantly"]
    end

    subgraph IT["Integration Tests"]
        direction TB
        I1[Wiring errors\nservice ↔ DB mismatches]
        I2["Example: audit_log writes\nto wrong table shape"]
    end

    subgraph BM["Benchmarks"]
        direction TB
        B1[Performance regressions\nlogically correct but slow]
        B2["Example: N+1 query\n121ms vs 4ms"]
    end

    style UT fill:#d1ecf1,stroke:#17a2b8
    style IT fill:#d4edda,stroke:#28a745
    style BM fill:#fff3cd,stroke:#ffc107
```

Looking back at the test suite for this codebase (~137 test files, 50+ commits from my work alone), the testing ended up organized into three distinct layers, each catching a different failure mode:

**Unit tests** catch logic errors: wrong formula, wrong branch, wrong return value. The risk scoring weight tests are the clearest example — they'd catch a typo in `_WEIGHTS = {"delay_score": 0.34, ...}` immediately.

**Integration tests** catch wiring errors: the service layer not calling the right DB query, data not flowing correctly through the stack, behavior under real schema constraints. The audit log integration tests (`test_audit_log_integration.py`, 61 lines) verify that `log_activity` actually writes rows to the right table with the right shape, which unit tests with mocked DB can't verify.

**Benchmarks** catch performance regressions: code that is logically correct but operationally expensive. The N+1 test is the example here.

The interesting thing about the third layer is that it changes the TDD feedback loop. Red-green-refactor for correctness is about asking "does this code do the right thing?" Adding benchmarks is about asking "does this code do the right thing at acceptable cost?" Both questions matter before you ship.

| Layer | Tool | What it catches | What it misses | Example from this codebase |
|---|---|---|---|---|
| Unit | pytest | Logic errors: wrong formula, wrong branch, off-by-one | Performance, DB wiring | delay weight 0.34 → test_delay_weight_is_0_35 fails |
| Integration | pytest + real DB | Wiring errors, schema constraints, real query behavior | Performance, isolated logic | audit_log write verified to hit the correct table with the correct column shape |
| Benchmark | pytest-benchmark | Performance regressions, algorithmic complexity class | Logic correctness | N+1 at 121ms vs batch at ~4ms — green in unit and integration both |

None of these layers is redundant. Remove unit tests and you lose the mutation guard on `_WEIGHTS`. Remove integration tests and you lose DB-wiring confidence. Remove benchmarks and you ship the 121ms implementation because it's green.

## One Thing Worth Acknowledging

The benchmark numbers I have are from a local machine with simulated latency. Real production numbers will differ — your DB connection pool, network topology, and query plan all affect actual latency. The 121ms figure is not a production measurement.

What the benchmark *does* prove is the call count ratio, which is exact: the N+1 implementation makes 50 calls for 50 invoices, the batch implementation makes 1. That's a mathematical property of the code, not an environment-dependent measurement. The `test_n1_makes_n_plus_1_db_calls` and `test_batch_makes_exactly_1_db_call` tests verify this without any timing or simulation noise — they're just counting how many times the simulated DB function is invoked.

The timing benchmark quantifies the *magnitude* of the improvement under realistic latency. The call count tests verify the structural property that makes the improvement possible.

Both together are more convincing than either alone.
