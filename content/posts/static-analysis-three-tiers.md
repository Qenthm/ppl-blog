---
title: "Static Analysis Isn't Optional: How We Built a Three-Tier Quality Gate for SIRA"
date: 2026-05-25
draft: false
tags: ['static-analysis', 'python', 'typescript', 'ci-cd', 'code-quality']
---

There's a version of static analysis that doesn't work: you add a linter to CI, it flags style issues nobody fixes, the team learns to ignore the yellow warnings, and the tool becomes decoration. The failures it was supposed to catch go to production anyway.

The version that works is where the tool is in the critical path. It blocks the commit, or blocks the merge, or blocks the deploy. When it fires, something stops until someone fixes it. The tool has teeth.

This is how we built it for SIRA, a FastAPI + React invoice-reminder service. Five tools, three tiers, one consistent answer: if analysis flags it, it doesn't ship.

## The Three Tiers

The pipeline isn't one analysis pass — it's three, at different points in the loop with different latency budgets and different failure modes they're designed to catch.

| Tier | When it runs | Latency budget | Fails → | What it catches |
|---|---|---|---|---|
| Local hooks (pre-push) | On `git push` | ~60s | Push rejected | Format, type errors, import issues, dead code, test regressions |
| CI gate (ci stage) | Every commit to remote | ~3 min | Pipeline red | Same as local + full build validation |
| Quality gate (quality stage) | MRs + main branch | ~5 min | Merge blocked | Coverage threshold, security severity, React health score, schema drift |

The tiers are not redundant. Each one catches what the tier above it misses — usually because the tier above has a tighter latency budget and can't afford the full scan.

## Tier 1 — Local Hooks

The pre-push hook runs on every `git push`, before the remote ever sees the commit. It fails fast: if it takes more than ~60 seconds, engineers stop running it, so the hook has to be worth its time.

What runs:

```bash
# Backend (apps/api/)
uv run ruff check .          # lint: style, unused imports, naming conventions
uv run ruff format --check . # format: enforce double quotes, 100-char line width
uv run mypy src/             # type checking (strict mode — see below)

# Frontend (apps/web/)
pnpm biome check .           # lint + format in one pass
pnpm tsc --noEmit            # TypeScript type checking
pnpm knip                    # dead code: unused exports, dependencies, files
```

The pre-push hook (not pre-commit) is the right place for mypy and tsc. Pre-commit is for fast, cheap checks (format, lint). Type checking touches the whole dependency graph and can take 10–20 seconds for a medium codebase — acceptable before push, annoying before every commit.

## Tier 2 — CI Gate

Every remote commit triggers the CI stage. These are the same checks as the local hook, but:

1. They run in a clean Docker container (no "works on my machine" variance)
2. They run in parallel across multiple jobs (api:lint, api:typecheck, web:lint, web:typecheck — 4 parallel jobs)
3. They block subsequent pipeline stages if they fail

The CI gate catches a specific class of failure: code that passes locally because of a machine-specific install, a stale `.venv`, or a `# type: ignore` that was valid last week but now hides a real error. Clean CI environment, same failure surface.

## Tier 3 — Quality Gate

The quality stage runs only on MR branches and `main`. It's the expensive scan — it needs coverage reports from the test run, it calls external APIs (SonarQube), and it has higher latency. This is also where merge blocking happens.

Five jobs:

**sonar-scan** — SonarQube analysis. The hard rule: **85% coverage on new code, enforced**. Anything below fails the quality gate and blocks merge. SonarQube also tracks code smells, cognitive complexity, and duplication — not as merge blockers but as metrics that accumulate and surface during code review.

**security:sast** — Bandit (Python) + npm audit (JavaScript). Severity tiers are explicit:

| Severity | Tool | Pipeline result | Effect |
|---|---|---|---|
| critical / high | npm audit | exit 1 | Merge blocked |
| high | Bandit | exit 1 | Merge blocked |
| moderate | npm audit | exit 77 | Yellow — reviewable |
| medium | Bandit | exit 77 | Yellow — reviewable |
| low | either | exit 0 | Green |

Exit 77 is GitLab's "allow failure" code — the job is yellow, the pipeline continues, but the finding is visible on the MR. A reviewer has to consciously decide to merge over a medium finding, rather than having it silently pass.

**web:react-doctor** — React-specific analysis: correctness, effect dependencies, performance patterns, accessibility, dead code, security. The hard floor is **HCE (Healthy Components Entropy) score ≥ 95**. Below 95 is a merge blocker. Between 95 and 100 with warnings is yellow — reviewable.

**api:schema-test** — Schemathesis fuzzing against the OpenAPI spec (manual trigger on MRs). 20 example variations per endpoint, checked for non-5xx responses. Non-blocking but visible.

**mutation:python / mutation:typescript** — mutmut and Stryker (manual trigger). Non-blocking, but the results inform whether the test suite is actually mutation-resistant or just coverage-padded.

## Ruff: What the Rules Actually Catch

Ruff is configured in `apps/api/pyproject.toml` with six rule families:

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP"]
```

| Rule family | What it catches | Example |
|---|---|---|
| E (pycodestyle errors) | PEP 8 violations: spacing, blank lines, indentation | E711: `x == None` → `x is None` |
| F (Pyflakes) | Logic errors: unused imports (F401), undefined names (F821), redefined variables | F401: `from typing import Optional` imported but unused |
| I (isort) | Import ordering: stdlib before third-party before local | Mixed import order in any file |
| N (pep8-naming) | Naming conventions: CamelCase classes, snake_case functions | N803: argument `camelCased` in a function |
| W (pycodestyle warnings) | Whitespace issues, line endings | W291: trailing whitespace |
| UP (pyupgrade) | Modern Python syntax: `Optional[X]` → `X \| None`, `Union[X, Y]` → `X \| Y` | UP007: use `X \| Y` for union types |

The UP rules are the ones that require a real decision. In Python 3.10+ (3.12 for this project), `X | Y` syntax is preferred over `Optional[X]` and `Union[X, Y]`. Ruff enforces that automatically — once it's in the config, the old forms stop appearing in new code.

The MR bot service adds two more rule families:

```toml
select = ["E", "F", "I", "B", "UP", "ASYNC", "RUF"]
```

**B (flake8-bugbear)** catches logic errors beyond style: mutable default arguments (`def f(x=[]):`), using `assert` for control flow, comparisons to `True`/`False` with `==`.

**ASYNC (flake8-async)** catches `asyncio` misuse: calling a sync sleep inside an async function, using blocking I/O in async context, misusing `asyncio.run` inside a coroutine.

## mypy: Strict Mode Means All of It

```toml
[tool.mypy]
python_version = "3.12"
strict = true

[[tool.mypy.overrides]]
module = "user_agents"
ignore_missing_imports = true
```

`strict = true` is a single flag that enables every mypy strictness check:

| Check | What it enforces |
|---|---|
| disallow_untyped_defs | Every function must have annotated arguments and return type |
| disallow_incomplete_defs | Partial annotations (some args typed, some not) are rejected |
| disallow_untyped_calls | Calling an untyped function from typed code is an error |
| warn_return_any | Returning `Any` from a typed function is flagged |
| no_implicit_optional | `def f(x: str = None)` must be `def f(x: str \| None = None)` |
| strict_equality | `if x == None:` is flagged; must use `is None` |

The only override is `user_agents`, a third-party library without type stubs. Everything else is fully typed.

What this looks like in practice. A function without annotations:

```python
# BEFORE: mypy strict rejects this — "Function is missing a type annotation"
async def score_client(self, client_id):
    features = self._compute_features(client_id)
    return self._strategy.calculate_score(features)

# AFTER: explicit types, return type enforced by mypy
async def score_client(self, client_id: str) -> ScoreRiskResponse:
    features = await self._compute_features(client_id)
    return self._strategy.calculate_score(features)
```

The return type isn't just documentation. mypy verifies that every code path in `score_client` returns a `ScoreRiskResponse`. If a future change adds a branch that returns `None` or `dict`, mypy fails before the code reaches CI.

The `ClientService | None` annotation in `ReminderService.__init__` is a direct consequence of strict mode:

```python
self._client_service: ClientService | None = (
    ClientService(db, encryption_key) if encryption_key else None
)
```

Because `_client_service` is `ClientService | None`, mypy forces every call site to check for `None` before using it. An unchecked access like `self._client_service.get_client(...)` fails with "Item `None` of `ClientService | None` has no attribute `get_client`". The null check isn't a convention — it's enforced by the type checker.

## Bandit: Security Analysis with Explicit Severity Tiers

Bandit scans the entire `src/` tree for known vulnerability patterns:

```toml
[tool.bandit]
targets = ["src"]
skips = []
```

No skips. Every rule runs. The severity tiers are enforced in CI:

```bash
# Fail hard on HIGH severity
if [ "$HIGH_COUNT" -gt 0 ]; then exit 1; fi

# Yellow (reviewable) on MEDIUM severity
if [ "$MEDIUM_COUNT" -gt 0 ]; then exit 77; fi
```

What Bandit flags as HIGH in Python code:

| Check | Rule | Example |
|---|---|---|
| Hardcoded password | B105/B106/B107 | `password = "secret123"` in source |
| SQL injection via string | B608 | `f"SELECT * FROM {table}"` passed to execute |
| Shell injection | B602/B603 | `subprocess.call(cmd, shell=True)` |
| Weak cryptography | B303/B304 | `md5()`, `sha1()` used for passwords |
| Assert in non-test code | B101 | `assert condition, "msg"` — stripped by `-O` |

The `--exclude src/app/tests,src/app/seed` flag keeps false positives out of test fixtures (which deliberately use weak values for seeding). Everything in the service layer runs clean.

## SonarQube: The 85% Threshold Is a Hard Gate

```properties
sonar.qualitygate.wait=true
```

`sonar.qualitygate.wait=true` means the CI job waits for SonarQube to finish analysis and blocks on the result. The quality gate condition: **new code must have ≥ 85% line coverage**. If a merge request introduces 100 lines of new service code and the tests cover 84 of them, merge is blocked.

The coverage is pulled from two separate reports:

```properties
sonar.python.coverage.reportPaths=apps/api/coverage.xml,apps/api/coverage-integration.xml
sonar.javascript.lcov.reportPaths=apps/web/coverage/lcov.info
```

Two reports for Python: unit tests and integration tests are run separately and their coverage is merged. A function that's only called in integration tests (e.g., `log_activity`) counts toward coverage even if no unit test touches it directly.

Coverage exclusions are explicit — routes (auto-generated from TanStack Router), generated files, and seeders don't count toward the threshold:

```properties
sonar.coverage.exclusions=\
  apps/web/src/routes/**,\
  apps/web/src/routeTree.gen.ts,\
  apps/api/src/app/seed/**
```

The exclusions are narrow and intentional. Including them in coverage would inflate the number without measuring anything meaningful. Excluding them means the 85% applies to code that actually has logic to test.

## React Doctor: Component Health Score

React Doctor is the frontend-specific layer that tsc misses. It checks:

- **Effect dependencies**: missing `useEffect` deps that cause stale closures
- **State design**: unnecessary state, state that should be derived
- **Performance**: missing `useMemo`/`useCallback` on expensive operations passed as props
- **Accessibility**: missing ARIA labels, keyboard nav issues
- **Dead code**: components imported but never rendered
- **Security**: `dangerouslySetInnerHTML` without sanitization

The HCE (Healthy Components Entropy) score is an aggregate health signal — 0 to 100. The hard floor is 95.

```bash
pnpm dlx react-doctor@latest . --offline --yes --fail-on none --json
# Fails pipeline if HCE < 95
```

`--fail-on none` means the tool itself doesn't exit nonzero on individual findings. The CI script reads the JSON output, extracts the HCE score, and applies the threshold. This separation is intentional: we control the threshold, not the tool's default opinion.

## What Each Layer Catches That the Others Miss

The tiers are complementary, not redundant:

| Failure class | Local hook | CI gate | Quality gate |
|---|---|---|---|
| Format / style violations | ✓ Ruff, Biome | ✓ | — |
| Type errors (Python) | ✓ mypy | ✓ | — |
| Type errors (TypeScript) | ✓ tsc | ✓ | — |
| Unused exports / dead imports | ✓ Knip | ✓ | — |
| Coverage regression on new code | — | — | ✓ SonarQube |
| Security vulnerabilities (HIGH) | — | — | ✓ Bandit / npm audit |
| React component health | — | — | ✓ React Doctor |
| API schema drift / 500s | — | — | ✓ Schemathesis (manual) |
| Mutation-resistant test suite | — | — | ✓ mutmut / Stryker (manual) |
| Machine-specific env artifacts | — | ✓ Clean Docker env | — |

The local hook catches things fast (seconds after commit, no remote round-trip). The CI gate verifies in a clean environment. The quality gate enforces thresholds that require full test runs and external API calls — costs that would make the local hook unusable.

## The Stack at a Glance

| Tool | Version | Scope | Hard gate | Configured in |
|---|---|---|---|---|
| Ruff | 0.15.0 | Python linting + formatting | Yes (CI stage) | apps/api/pyproject.toml |
| mypy | 1.19.1 | Python type checking (strict) | Yes (CI stage) | apps/api/pyproject.toml |
| Biome | latest | TypeScript linting + formatting | Yes (CI stage) | apps/web/ |
| tsc | — | TypeScript type checking | Yes (CI stage) | apps/web/tsconfig.app.json |
| Knip | latest | Dead code detection (TS) | Yes (pre-push) | apps/web/ |
| SonarQube | hosted | Coverage threshold (≥85%) + code smells | Yes (quality stage) | sonar-project.properties |
| Bandit | 1.9.4 | Python security (HIGH = blocker) | Yes (quality stage) | apps/api/pyproject.toml |
| npm audit | — | JS dependency vulnerabilities | Yes (quality stage) | — |
| React Doctor | latest | React component health (≥95 HCE) | Yes (quality stage) | — |
| Schemathesis | 4.12.1 | API schema fuzzing | No (yellow) | — |
| mutmut | 3.5.0 | Python mutation testing | No (yellow) | — |
| Stryker | latest | TypeScript mutation testing | No (yellow) | — |

Static analysis with teeth means it fails the build, not the meeting. Every tool in that table either stops a commit, stops a merge, or produces a finding that blocks merge unless a reviewer consciously overrides it. The "soft" tools (Schemathesis, mutation testing) produce findings that accumulate in the MR thread — they inform the review, even when they don't block it.
