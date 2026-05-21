---
title: 'Three Refactors I Shipped in One MR'
date: 2026-05-21
draft: false
tags: ['refactoring', 'python', 'design-patterns', 'software-engineering', 'solid']
---

There's a specific kind of file you learn to dread. Not broken — tests pass, CI is green, customers are using it. But you avoid it anyway. You touch it only when you have to, and when you do, you spend the first few minutes re-orienting: which method calls what, what side effects lurk where, whether there's another spot you'll forget to update.

`ClientService` was that file. 354 lines, tested, shipping. But every time I touched anything PII-related, I'd Ctrl+F the file to find every spot that called `encrypt()`, because I didn't trust myself to remember all three. That reflex is the smell. I'd been ignoring it for weeks.

So I opened a branch and decided to fix three things in one MR. Each fix is a named design pattern applied to a named smell. Here's what the service looked like, what each pattern was, and what the numbers looked like before and after.

The service is part of an invoice-reminder SaaS. There's a class that manages clients, a class that sends reminders, and a background job that does the daily chasing. The patterns I hit are generic enough that you've probably got the same ones somewhere.

## Case 1 — Single Responsibility Principle + Extract Class

**The pattern: Single Responsibility Principle (SRP).** From the SOLID principles: a class should have one, and only one, reason to change. "Reason to change" means a source of requirements that could force a rewrite. If two different stakeholders can independently demand changes to the same class, that class has more than one responsibility.

`ClientService` violated this. It was holding two unrelated responsibilities: the *business* of clients (create, validate, bulk import) and the *cryptography* of clients (encrypting and decrypting email, phone, and Telegram ID). Changing client validation logic and swapping encryption algorithms are completely different kinds of work — one comes from product requirements, the other from the security team — and both meant editing the same 354-line file.

**The refactoring move: Extract Class.** Take the responsibility that doesn't belong and lift it into its own class. This is one of Fowler's core refactoring catalog entries: when a class is doing the work of two, find the cluster of fields and methods that belong together and move them to a new class.

Before, the crypto was sprinkled across three methods:

```python
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
```

After applying Extract Class, the crypto logic lives in `PIIEncryptor`:

```python
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

## Case 2 — Dependency Injection (Constructor Injection)

**The pattern: Dependency Injection.** A class should declare its dependencies explicitly, where callers can see them, rather than creating or fetching them internally. The most common form is constructor injection: pass the dependency in through `__init__`, where it's visible in the signature, instead of letting the class reach out and build it on demand.

The inverse — a class that creates or locates its own dependencies internally — is sometimes called the Service Locator anti-pattern. It works, but it hides what the class actually needs.

`ReminderService` had this problem:

```python
class ReminderService:
    def __init__(self, db, encryption_key):
        self._db = db
        self._key = encryption_key
        # __init__ declares two dependencies. That's the lie.

    def _send_telegram(self, invoice):
        from app.services.client_service import ClientService   # hidden dependency
        client_svc = ClientService(self._db, self._key)         # built on every call
        client = asyncio.run(client_svc.get_client(invoice["client_id"]))
```

The `__init__` signature is a contract: "to use this class, give me a `db` and an `encryption_key`." But the class secretly also needs `ClientService`. That dependency only materializes at the moment `_send_telegram` runs, which means:

- A static analyzer (mypy, pyright) tracing `ReminderService`'s dependencies by reading `__init__` will miss it entirely.
- A reviewer writing a new test for `ReminderService` has to discover the hidden dep by running the code or reading every method.
- Deferred imports suppress circular import errors until runtime — if `client_service.py` ever imported from `reminder_service.py`, Python would catch the cycle at module load time with a top-level import. With a deferred import, you find out in production.
- Every Telegram message constructs a brand-new `ClientService`, which after Case 1 also builds its own `PIIEncryptor`. A batch of fifty reminders builds and discards that object graph fifty times.

Applying Constructor Injection:

```python
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

## Case 3 — Repository Pattern + Eager Loading (eliminating N+1)

**The pattern: Repository Pattern.** The Repository pattern separates data access logic from business logic. The application layer asks for objects by ID or by query; the repository layer handles how to fetch them. One of the things the repository pattern enables — and that's easy to misuse — is batch fetching: asking for multiple objects in a single query instead of one per object.

The daily reminder job wasn't using it:

```python
for invoice in invoices:
    client = asyncio.run(get_client_by_id(db, invoice["client_id"]))
    # map risk level → message tone, then send
```

**The anti-pattern: N+1 queries.** One query fetches the list of overdue invoices. Then, inside the loop, one more query per invoice fetches its client. Fifty invoices: fifty-one queries. This is the N+1 problem — the "1" is the initial query, the "N" is the per-item queries inside the loop.

The cost isn't the database computation — a primary-key lookup is microseconds. It's the round-trip: each call sends a request over the wire, waits for the reply, then proceeds. That waiting is serial and doesn't overlap with anything else. On a developer laptop with three rows it's invisible. With hundreds of overdue invoices it dominates the job's runtime.

I found the hotspot by running pyinstrument against the job:

```
2.832 send_overdue_reminders  task.py:47
   2.831 <module>  [47 frames hidden]
      2.809 get_client_by_id  queries/clients.py:31
         2.803 execute  [supabase internals]
```

`get_client_by_id` is 99% of the runtime. All the actual business logic — mapping risk categories to message tones, formatting reminders — barely registers. The repository layer already had `get_clients_by_ids()`, the batch version. Nobody was using it.

**The fix: Eager Loading.** Fetch all the data you'll need upfront, in one query, before the loop starts. This is a well-known companion pattern to the Repository — sometimes called eager loading or the batch-fetch pattern.

```python
# BEFORE: N+1 — one repository call per invoice
for invoice in invoices:
    client = asyncio.run(get_client_by_id(db, invoice["client_id"]))

# AFTER: eager load all clients in one batch, then look up from a dict
client_ids = list({str(inv["client_id"]) for inv in invoices})
clients_by_id = {
    str(c["id"]): c
    for c in asyncio.run(get_clients_by_ids(db, client_ids))
}
for invoice in invoices:
    client = clients_by_id.get(str(invoice["client_id"]))
```

The `set` comprehension deduplicates client IDs before fetching. If fifty invoices belong to ten unique clients — realistic, since a handful of large customers generate most overdue invoices — the batch fetches ten rows, not fifty. The `dict` provides O(1) lookup inside the loop. Total queries: two, regardless of N.

**Result.** Measured with pytest-benchmark, 50 invoices, 10 unique clients, 2ms simulated round-trip per DB call:

| Approach | Mean | Min | Max |
|---|---|---|---|
| N+1 (before) | 120.98 ms | 119.57 ms | 122.92 ms |
| Batch (after) | 2.48 ms | 2.05 ms | 3.44 ms |

**49× faster** at 50 invoices. The ratio gets worse as N grows, because N+1 scales linearly and the batch stays flat:

| Invoice count | N+1 (estimated) | Batch (estimated) |
|---|---|---|
| 50 | ~120 ms | ~2.5 ms |
| 200 | ~480 ms | ~2.5 ms |
| 500 | ~1,200 ms | ~2.5 ms |
| 1,000 | ~2,400 ms | ~2.5 ms |

At 500 invoices the N+1 approach spends over a second just fetching clients, before any reminder logic runs. A cron job scheduled to finish in seconds starts overlapping with its next scheduled run.

The regression test asserts on query count, not wall time — count is exact, milliseconds are noisy:

```python
def test_clients_fetched_in_single_batch_not_per_invoice():
    call_count = 0
    # ... intercepts time.sleep (which simulates DB latency) ...
    assert call_count == 1  # one batch call, not 50
```

If anyone drops a per-invoice `get_client_by_id` back inside the loop, this fails immediately.

## Why bundle them into one MR

Worth addressing, because "one MR for three changes" sounds like something a reviewer would push back on.

These aren't unrelated. All three touch the same service cluster. Case 1 created `PIIEncryptor`. Case 2 depended on Case 1 being done first — the "wasted object construction" argument gets stronger once `ClientService.__init__` is doing more work. Case 3 is in the job that drives `ReminderService`. The changes are causally linked.

Bundling also made the MR easier to review. A reviewer could see the whole picture: the SRP violation fixed, the hidden dependency exposed, the N+1 eliminated. Three diffs telling one coherent story about a service cluster you stopped trusting — and why you trust it now.

## The move underneath all three

| Case | Smell | Pattern applied | Principle |
|---|---|---|---|
| `ClientService` | Two responsibilities in one class | Extract Class | Single Responsibility (SRP) |
| `ReminderService` | Hidden dependency, deferred import | Constructor Injection | Dependency Injection (DI) |
| Reminder job | N+1 queries in a loop | Eager Loading / Batch Fetch | Repository Pattern |

Each one followed the same workflow: name the smell, apply the named pattern, verify with tests. The naming isn't pedantry — once a smell has a name, it has a known fix. You stop staring at the code and go look up the move.

None of these patterns are clever. They're all in Fowler's *Refactoring*, Martin's *Clean Code*, or any similar book from the last twenty years. The vocabulary exists so that recognizing the smell is fast.

Extract Class was about an hour. Constructor injection was 11 lines. The N+1 fix was four lines and a 49× speedup. The files I'd stopped trusting are files I trust again — and I didn't schedule anything to make it happen.
