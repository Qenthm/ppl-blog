---
title: 'Two Design Patterns I Applied in One MR'
date: 2026-05-21
draft: false
tags: ['refactoring', 'python', 'design-patterns', 'software-engineering']
aliases: ['/posts/three-refactors-one-mr/']
---

There's a specific kind of file you learn to dread. Not broken — tests pass, CI is green, customers are using it. But you avoid it anyway. You touch it only when you have to, and when you do, you spend the first few minutes re-orienting: which method calls what, what side effects lurk where, whether there's another spot you'll forget to update.

`ClientService` was that file. 354 lines, tested, shipping. But every time I touched anything PII-related, I'd Ctrl+F the file to find every spot that called `encrypt()`, because I didn't trust myself to remember all three. That reflex is the smell. I'd been ignoring it for weeks.

So I opened a branch and fixed two things in one MR. Each fix is a named design pattern applied to a named smell. Here's what the service looked like, what each pattern was, and what the numbers looked like before and after.

The service is part of an invoice-reminder SaaS. There's a class that manages clients and a class that sends reminders. The patterns I hit are generic enough that you've probably got the same ones somewhere.

## Case 1 — Hiding the crypto subsystem

`ClientService` was calling `encrypt(value, self._key)` directly — in three different methods. The encryption key, the cipher primitive, the fallback behavior on failure: `ClientService` knew all of it, scattered across 354 lines.

The concrete problem this creates: if the encryption algorithm ever needs to change (say, rotating from AES-GCM to ChaCha20, or switching to a new key), you have to hunt through all 354 lines to find every call site and change them together. Miss one, and data silently decrypts with the wrong key. There's also nothing stopping future developers from adding a fourth call site in a new method, spreading it further.

What you actually want from a caller's perspective is something like `self._pii.encrypt_field(value)` — a call that speaks the domain language and hides everything about keys, ciphers, and error cases behind it. The caller doesn't need to know whether the implementation uses AES-GCM or ChaCha20, or what happens when a legacy plaintext value comes in. It just wants an encrypted field back.

That's the distinction between Extract Class and the **Facade pattern**. Extract Class is the mechanical move — pull code into a new class. Facade is a stricter claim about what that class does: it provides a simplified interface to a subsystem, so that callers are permanently insulated from the subsystem's internals. You could extract crypto into a helper class and still leak key management or error handling into callers. A Facade's job is specifically to prevent that — the caller knows `encrypt_field` and `decrypt_row`, and nothing else.

```python
# BEFORE: crypto scattered across three ClientService methods, silent on failure
class ClientService:
    def create(self, ...):
        encrypted = encrypt(value, self._key)  # inline in three different methods

    def update(self, ...):
        encrypted = encrypt(value, self._key)  # again here

    def _decrypt_pii_field(self, value):
        try:
            return decrypt(value, self._key)
        except ValueError:
            return value      # legacy plaintext fallback — silent, no log
        except InvalidTag:
            return value      # key-rotation fallback — also silent

# AFTER: Extract Class — PIIEncryptor owns all crypto, ClientService delegates
class PIIEncryptor:
    """Single responsibility: encrypt and decrypt PII fields."""

    def __init__(self, key: str, fields: tuple[str, ...]) -> None:
        self._key = key
        self._fields = fields

    def encrypt_field(self, value: str | None) -> str | None:
        return encrypt(value, self._key) if value else None

    def decrypt_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decrypted = {**row}
        for field in self._fields:
            decrypted[field] = self._decrypt_field(row, field)
        return decrypted

    def _decrypt_field(self, row: dict[str, Any], field: str) -> Any:
        value = row.get(field)
        if value is None:
            return None
        try:
            return decrypt(value, self._key)
        except ValueError:
            _logger.warning(
                "Client %s field %s is stored as plaintext; returning legacy value",
                row.get("id"), field,
            )
            return value
        except InvalidTag:
            _logger.error(
                "Failed to decrypt field %s for client %s (key mismatch or corrupted data)",
                field, row.get("id"),
            )
            return "[encrypted]"

class ClientService:
    def __init__(self, db: Client, encryption_key: str) -> None:
        self.db = db
        self._pii = PIIEncryptor(encryption_key, PII_FIELDS)  # delegates all crypto

    def create(self, ...):
        encrypted = self._pii.encrypt_field(value)
```

The `except` clauses are worth examining closely, because the refactor improved them, not just relocated them. The old code returned the raw value silently on both failure cases — no log, no signal. A client's email could be silently returned as plaintext and the calling code would have no idea. The new `PIIEncryptor._decrypt_field` logs a `WARNING` for `ValueError` (legacy plaintext — expected but worth tracking) and an `ERROR` for `InvalidTag` (key mismatch or corrupted data — unexpected). Now when Sentry fires an alert, there's something to read.

The old handlers also sat inside a class that nominally knows nothing about encryption. Anyone debugging a decryption failure had to already know to look in `ClientService`. Now they're in `PIIEncryptor` — a class whose name tells you exactly what it does.

**Result.** The SRP violation meant that a future encryption algorithm change (say, AES-GCM → ChaCha20) would require hunting through 354 lines of `ClientService` across three methods. After the refactor, you touch `PIIEncryptor` only — 38 lines, one class, one reason to be there. `ClientService` doesn't change at all. The 42 existing tests stayed green; `PIIEncryptor` is now independently testable without standing up the whole client service.

| Metric | Before | After |
|---|---|---|
| Crypto code locations | 3 (scattered across create, update, _decrypt_pii_field) | 1 (PIIEncryptor) |
| Lines to read for any crypto change | 354 (all of ClientService) | 38 (PIIEncryptor only) |
| Lines per call site | 2–4 (inline per method) | 1 (self._pii.encrypt_field / decrypt_row) |
| Decryption failures logged | 0 — silent fallback in both error cases | 1 WARNING (plaintext) or ERROR (key mismatch) |
| Classes that know the encryption key | 2 (ClientService + any class that passed it) | 1 (PIIEncryptor only) |

The observability row is the one that matters most in production. Silent fallbacks mean a key-rotation failure looks identical to a successful decrypt until a customer complains about garbled data. Now Sentry catches it before anyone notices.

## Case 2 — Declaring the hidden dependency

A class's `__init__` is its contract: read it, and you should know everything the class needs to work. `ReminderService.__init__` takes `db` and `encryption_key`. That looks like the complete list.

It wasn't. Inside `_send_telegram`, the method reached into a deferred import and built a `ClientService` on every single call — a third dependency that `__init__` never declared.

The concrete problems this creates: a static analyzer reading `__init__` sees two dependencies and misses the third entirely. A developer writing a test for `ReminderService` has to discover the hidden dependency by running the code, not by reading the signature. And on every Telegram send, a fresh `ClientService` is constructed and discarded — which after Case 1 also means a fresh `PIIEncryptor` loading the encryption key.

What you actually want is `__init__` to be the honest, complete dependency list. If `ReminderService` needs a `ClientService`, that should appear in `__init__`, type-annotated, like every other dependency. Then static analyzers can see it, tests can inspect it, and any engineer reading the class for the first time knows the full picture without having to run the code.

That's what **Constructor Injection** means in Dependency Injection terms. The pattern isn't just "move the import to the top" — it's a deliberate choice to own the dependency at the class level so the class's lifecycle controls when it's built, and every tool that reads `__init__` gets the truth. The alternative (building a dependency inside a method body) is the **Service Locator** anti-pattern: the class locates what it needs on demand, hiding the dependency from everyone who hasn't read the method.

```python
# BEFORE: Service Locator — hidden dependency, ClientService rebuilt on every call
class ReminderService:
    def __init__(self, db, encryption_key):
        self._db = db
        self._key = encryption_key
        # __init__ declares two dependencies. That's the lie.

    def _send_telegram(self, invoice):
        from app.services.client_service import ClientService   # hidden dependency
        client_svc = ClientService(self._db, self._key)         # built on every call
        client = asyncio.run(client_svc.get_client(invoice["client_id"]))

# AFTER: Constructor Injection — all dependencies declared in __init__, type-annotated
from app.services.client_service import ClientService  # declared at module level

class ReminderService:
    def __init__(self, db, encryption_key):
        # all dependencies visible here
        self._client_service: ClientService | None = (
            ClientService(db, encryption_key) if encryption_key else None
        )

    def _send_telegram(self, invoice):
        if not self._client_service:
            logger.error("Cannot send Telegram: ClientService not initialized")
            return "FAILED"
        client = asyncio.run(self._client_service.get_client(invoice["client_id"]))
```

Three things break when a dependency is hidden like this. Static analyzers (mypy, pyright) read `__init__` to build a dependency graph — they see `db` and `encryption_key`, miss `ClientService` entirely. Circular imports are suppressed until runtime: with a top-level import, Python catches the cycle at module load; with a deferred import, you find out in production when `_send_telegram` first runs. And every Telegram send constructs a brand-new `ClientService` — which after Case 1 also builds a `PIIEncryptor` and loads the encryption key. Fifty reminders in a batch: fifty object graphs built and discarded.

The `ClientService | None` type annotation matters. The `if encryption_key` guard means `_client_service` can be `None` in test environments. Mypy and pyright will both flag any code path that uses `_client_service` without checking for `None` first — catching exactly the kind of mistake that a deferred-import pattern lets slip through silently.

**Result.** The diff was 6 insertions, 5 deletions — 11 lines total. All 54 existing `ReminderService` unit tests stayed green with zero changes to their patch targets.

That last point is the proof that constructor injection is behavior-preserving. The tests were patching at the class-method level:

```python
@patch('app.services.client_service.ClientService.get_client')
def test_send_telegram_success(self, mock_get_client):
    mock_get_client.return_value = fake_client
```

A class-method patch replaces the method on the class itself, not on a specific instance. It applies whether the instance was built in `__init__` or inside the method body. The structure moved; the behavior didn't; the tests couldn't tell the difference.

Structural win: `ReminderService` now builds one `ClientService` for its lifetime. Any engineer reading `__init__` sees the full dependency list immediately.

| Invoices in batch | ClientService constructions (before) | ClientService constructions (after) |
|---|---|---|
| 1 | 1 | 1 |
| 50 | 50 | 1 |
| 200 | 200 | 1 |
| 1,000 | 1,000 | 1 |

After Case 1, each `ClientService` construction also builds a `PIIEncryptor` and loads the encryption key. The Service Locator pattern was constructing and discarding that full object graph once per Telegram message. Constructor injection reduces it to once per job run, regardless of batch size. Not the bottleneck — the N+1 in Case 3 was — but it's wasted allocation that compounds with scale.

## Why bundle them into one MR

Worth addressing, because "two unrelated changes in one MR" sounds like something a reviewer would push back on.

These aren't unrelated. Both touch the same service cluster. Case 1 created `PIIEncryptor` and moved the encryption key inside it. Case 2 depended on Case 1 being done first — the argument that `ClientService` is being wastefully reconstructed on every Telegram send gets stronger once `ClientService.__init__` is doing real work with the key. The two changes tell one coherent story about a service cluster you stopped trusting — and why you trust it now.

## The pattern underneath both

| Case | Smell | Pattern | What it achieves |
|---|---|---|---|
| `ClientService` | Crypto knowledge scattered across 354 lines — any algorithm change requires hunting through all of them | **Facade** — `PIIEncryptor` exposes `encrypt_field`/`decrypt_row` and hides all key management and error handling | Algorithm changes touch 38 lines; `ClientService` doesn't change at all |
| `ReminderService` | Hidden dependency built fresh on every call, invisible to static analysis | **Dependency Injection** — move `ClientService` into `__init__` | Full dependency list visible at a glance; static analyzers can trace it; one construction per job run |

A principle and a pattern are different things. A principle (like SRP) tells you *something is wrong* — it diagnoses the smell. A pattern tells you *what to build* — it's the named solution. SRP diagnosed Case 1; Facade is what the fix looks like. The Service Locator anti-pattern named what was wrong in Case 2; Constructor Injection named the solution.

None of this is clever. Both patterns are in Fowler's *Refactoring* and Martin's *Clean Code*. The vocabulary exists so that recognizing the smell is fast — once it has a name, you stop staring at the code and go look up the move.

Extract Class was about an hour. Constructor injection was 11 lines. The files I'd stopped trusting are files I trust again.
