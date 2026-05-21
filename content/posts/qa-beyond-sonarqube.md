---
title: "Static Analysis Is the Floor, Not the Ceiling: SIRA's Layered QA Pipeline"
date: 2026-05-22
draft: false
tags: ["quality-assurance", "sonarqube", "static-analysis", "profiling", "python", "fastapi"]
---

When people talk about quality assurance in a student software project, they usually mean "we ran SonarQube and the gate is green." That's fine — but it's a floor, not a ceiling. SonarQube can tell you a function is too complex. It can't tell you that function is also making 50 database queries when it should make one. Those are different problems, caught by different tools, and both matter.

On SIRA, a FastAPI-based invoice reminder system, we ended up with a QA pipeline that covers both layers: static analysis that catches structural problems, and behavioral analysis that measures what the code actually does at runtime. Here's what that pipeline looks like and what each layer is actually catching.

## The Static Layer

The static tools run in the `ci` and `quality` stages of our GitLab CI pipeline, in this order:

**Ruff** (v0.15.0) runs first — lint and format check. If `ruff check .` or `ruff format --check .` fails, the pipeline stops before tests even start. Ruff's ruleset covers pyflakes (F), pycodestyle (E/W), isort (I), pep8-naming (N), and pyupgrade (UP). It's fast enough that it's not a bottleneck, and it catches the easy stuff — unused imports, undefined variables, inconsistent formatting — before anything heavier runs.

**mypy** (v1.19.1, `strict = true`) runs next. Strict mode means no implicit `Any`, no untyped function bodies, no missing stubs. Every function in `apps/api/src/` is typed. This catches a different class of bugs than Ruff — wrong argument types, missing Optional checks, incorrect return types. It also forces a design discipline: if your function is hard to type, that's often a signal that its interface is unclear.

**SonarQube** runs in the `quality` stage after all tests complete, because it needs the coverage XML reports to compute coverage metrics. The quality gate configuration enforces:
- New code coverage ≥ 85% on MRs (actually achieving 96.8% on the current branch)
- New duplicated lines ≤ 3% (currently 0.14%)
- New hotspots reviewed = 100%
- New violations = 0 (this is where the gate currently fails — more on this below)

**Bandit** (v1.9.4) runs in the `security:sast` job alongside pnpm audit. Bandit scans Python source for security issues — hardcoded passwords, use of unsafe functions like `subprocess.call(shell=True)`, SQL injection patterns. The CI policy: HIGH severity findings hard-fail the pipeline (exit 1, blocks merge). MEDIUM findings soft-fail (exit 77, surfaces in the MR comment but allows the reviewer to accept the risk). Zero HIGH findings in the current codebase.

The whole static layer catches what you'd expect: code style violations, type errors, security anti-patterns, and structural metrics like cognitive complexity. What it can't catch is whether the code is *fast*.

## The Complexity Problem

SonarQube's `python:S3776` rule flags functions whose cognitive complexity exceeds 15. This is a useful signal — high cognitive complexity predicts maintainability issues. But it's a static metric computed from the structure of the code, not from its runtime behavior.

Commit `3641324b` shows this in practice. `ClientService.update` had a PII field encryption loop nested inside several conditionals, pushing cognitive complexity to 21 — well above the threshold of 15. SonarQube flagged it. I extracted the loop into a `_build_update_data` helper, dropping the complexity to 14.

```python
# Before: PII handling inline in update(), complexity 21
async def update(self, client_id: str, payload: ClientUpdate, ...) -> Client:
    data: dict[str, Any] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in PII_FIELDS:
            if value is not None:
                data[field] = self._encryptor.encrypt(str(value))
            else:
                data[field] = None
        else:
            data[field] = value
    # ... rest of method ...

# After: extracted helper, update() complexity drops to 14
def _build_update_data(self, payload: ClientUpdate) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in PII_FIELDS:
            data[field] = self._encryptor.encrypt(str(value)) if value is not None else None
        else:
            data[field] = value
    return data
```

Commit `822e8025` did the same thing on the frontend — `audit-log.tsx` had five functions defined inline inside the parent component (SonarQube rule S6478), a nested ternary (S3358), and cognitive complexity violations (S3776). Extracting `ExpandRowButton`, `FilterChipsBar`, `renderEventTypeBadge`, `formatDateRangeLabel`, and `computeFilterCount` as module-level functions resolved all three classes of issues. Function coverage improved from 63% to 83%, branch coverage from 85% to 88% as a side effect — smaller functions are easier to test.

These fixes address what SonarQube can see. The SIRA project currently shows 0 bugs, 0 vulnerabilities, 0 security hotspots, and 96.9% coverage on the main branch. Technical debt is 51 minutes. The open issue list has 7 items, all code smells — two of which are mine (cognitive complexity violations in `invoice_service.py` and `client_service.py` that need the same extract-helper treatment).

That's a clean static picture. Now here's what static analysis missed entirely.

## The N+1 Problem SonarQube Couldn't See

The `send_overdue_reminders` Celery task dispatches reminders for all overdue invoices. The original implementation fetched the client record inside the loop:

```python
for invoice in overdue_invoices:
    client = await get_client_by_id(db, invoice["client_id"])
    # determine tone from client.current_risk_category
    # dispatch reminder
```

SonarQube had no issue with this. mypy had no issue with this. Ruff had no issue with this. The cognitive complexity was fine. The unit tests passed. Coverage was good.

But this code makes one `get_client_by_id` DB call per invoice. For 50 overdue invoices, that's 50 separate round-trips. At 2ms per trip — a realistic estimate for a cloud Postgres — you're burning 100ms of DB wait per Celery task cycle, scaling linearly with invoice count.

**pyinstrument** found it first. Running the profiler against the task under load identified `get_client_by_id` as the dominant hotspot — not because the function itself was slow, but because it was called 50 times in a loop. Profiling gives you call-tree data that no static tool can derive from source alone.

Then **pytest-benchmark** (v5.2.3) quantified it. I wrote a benchmark that simulates 2ms DB latency with `time.sleep`, runs both the N+1 approach and the batch approach against 50 invoices with 10 unique clients, and measures wall time:

```python
N_INVOICES = 50
DB_LATENCY_S = 0.002  # 2ms per simulated DB call

def _fake_get_client_by_id(client_id: str) -> dict[str, Any] | None:
    time.sleep(DB_LATENCY_S)  # one DB round-trip
    return CLIENTS.get(client_id)

def _fake_get_clients_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    time.sleep(DB_LATENCY_S)  # one batched round-trip
    return [CLIENTS[cid] for cid in ids if cid in CLIENTS]

def test_send_overdue_n1_approach(benchmark: Any) -> None:
    result = benchmark(approach_n1, INVOICES)
    assert result == N_INVOICES

def test_send_overdue_batch_approach(benchmark: Any) -> None:
    result = benchmark(approach_batch, INVOICES)
    assert result == N_INVOICES
```

Measured result (AMD Ryzen 5 7600X, Python 3.12.10, stored in `.benchmarks/Windows-CPython-3.12-64bit/0001_before_n1.json`):

```
test_send_overdue_n1_approach    mean=121.03ms  stddev=1.02ms  rounds=9
```

121ms for 50 invoices. The batch implementation — one `get_clients_by_ids` call before the loop — brings this to approximately 4ms regardless of invoice count (2 queries × 2ms). That's a **97% reduction in DB latency** for the same operation.

The DB call count is also verifiable without timing:

```python
def test_n1_makes_n_plus_1_db_calls() -> None:
    # patches time.sleep to count invocations
    assert call_count == N_INVOICES  # 50 calls for 50 invoices

def test_batch_makes_exactly_1_db_call() -> None:
    assert call_count == 1  # 1 call regardless of invoice count
```

These tests are now regression guards. If anyone refactors the batch back into a loop, the test fails immediately — no timing dependency, no environment sensitivity.

## What Each Layer Catches

The full pipeline has three distinct layers, each catching a different failure mode:

**Ruff + mypy** catch syntactic and type-level mistakes before tests run. They're fast and mechanical — no configuration needed beyond enabling strict mode. The value is zero-overhead rejection of the easy bugs.

**SonarQube + Bandit** catch structural and security problems. Cognitive complexity violations, nested ternaries, security anti-patterns, duplicate function bodies. These require more context than a linter can provide, which is why SonarQube builds a full AST model. The quality gate enforcing 0 new violations on the main branch means structural debt can't accumulate silently — every PR is checked, and the CI job blocks merge if the gate fails.

**pyinstrument + pytest-benchmark** catch performance problems that don't show up in any static metric. The N+1 pattern is entirely invisible to SonarQube — the code structure is fine. Only a profiler or a benchmark can show that "fine code" is making 50 DB calls when it should make 1.

The interaction between layers matters too. Static analysis can flag high cognitive complexity as a risk signal — it's often *correlated* with slow code because complex code tends to do too much. But correlation isn't causation. The actual performance verification requires behavioral measurement. You need both.

## Current State and What's Still Open

As of the latest main branch analysis:
- Coverage: **96.9%**, 0 bugs, 0 vulnerabilities, 0 security hotspots
- Technical debt: **51 minutes** across the entire codebase
- Open code smells: **7** — two are mine (cognitive complexity in `invoice_service.py:238` and `client_service.py:235`, both at 16-17 vs threshold 15), five belong to other contributors

The quality gate is currently ERROR on the current branch because of the 7 open violations — the same ones from main. This is expected behavior: the gate requires 0 *new* violations, and these existed before the branch. Fixing them requires the same extract-helper approach used in `3641324b` and `822e8025`.

The benchmark file stays in `.benchmarks/` and can be replayed with `uv run pytest tests/test_perf_send_overdue.py --benchmark-compare --benchmark-verbose`. When the N+1 vs batch comparison is re-run, the stored baseline shows the before state. That's the point: QA tools produce artifacts — coverage XML, benchmark JSON, bandit reports, SonarQube analysis — that make quality visible over time, not just at the moment of the last CI run.

A green quality gate is necessary but not sufficient. The behavioral analysis layer is what catches the things that are structurally fine but operationally wrong.
