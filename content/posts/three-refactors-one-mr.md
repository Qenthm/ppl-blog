---
title: 'Three Refactors I Shipped in One MR'
date: 2026-05-21
draft: false
tags: ['refactoring', 'python', 'software-engineering']
---

`ClientService` had 354 lines. Nothing was broken — it passed CI, customers used it daily. But every time I touched anything PII-related, I'd Ctrl+F the file to find every spot that called `encrypt()`, because I didn't trust myself to remember all three. That reflex is the smell. I'd been ignoring it for weeks.

So I opened a branch and decided to fix three things in one MR. Three named refactors, each verified by tests, none of them changing behavior. Here's what the service looked like, what each move was, and what the numbers looked like before and after.

The service is part of an invoice-reminder SaaS. There's a class that manages clients, a class that sends reminders, and a background job that does the daily chasing. The smells are generic enough that you've probably got the same ones somewhere.

## Case 1: the class was doing two jobs

`ClientService` was holding two unrelated responsibilities: the business of clients (create, validate, bulk import) and the cryptography of clients (encrypting and decrypting email, phone, and Telegram ID). The practical version of the Single Responsibility Principle is that a class should have one reason to change. This class had at least two — swapping encryption algorithms and changing client validation logic are completely different kinds of work, and both meant editing the same 354-line file.

The fix has a name: Extract Class. Take the responsibility that doesn't belong and lift it into its own class.

Before, the crypto was sprinkled across three methods:

```python
class ClientService:
    def create(self, ...):
        encrypted = encrypt(value, self._key)  # inline crypto

    def _decrypt_pii_field(self, value):
        try:
            return decrypt(value, self._key)
        except ValueError:
            return value      # legacy plaintext fallback
        except InvalidTag:
            return value      # key-rotation fallback
```

After:

```python
class PIIEncryptor:
    def __init__(self, key: str, fields: tuple[str, ...]) -> None: ...
    def encrypt_field(self, value): ...
    def decrypt_row(self, row): ...
    def _decrypt_field(self, row, field): ...   # ValueError + InvalidTag edge cases

class ClientService:
    def __init__(self, ...):
        self._pii = PIIEncryptor(encryption_key, PII_FIELDS)

    def create(self, ...):
        encrypted = self._pii.encrypt_field(value)
```

The part I almost glossed over was those two `except` clauses. They're not defensive padding. `ValueError` handles legacy data written before encryption was turned on — plaintext rows that decryption will choke on. `InvalidTag` handles data encrypted under a key we've since rotated away from. Both are real production states with real rows behind them.

In the old code, both handlers sat quietly inside a class that nominally knows nothing about encryption. Anyone debugging a decryption failure had to already know to look in `ClientService`. Now they live in `PIIEncryptor`, 38 lines whose entire job is making sense of bytes — exactly where you'd look.

**Before vs. after.** The change scope for a future crypto migration: before, you'd touch `ClientService` across three methods and hunt through 354 lines for every `encrypt()` call. After, you touch `PIIEncryptor` only — 38 lines, one class, one reason to be there. `ClientService` doesn't change at all. The 42 existing tests all stayed green; `PIIEncryptor` is now independently testable without standing up the whole client service.

## Case 2: the class was lying about what it needed

`ReminderService` had a method `_send_telegram()` that looked clean on the surface. Then you read the constructor next to it:

```python
class ReminderService:
    def __init__(self, db, encryption_key):
        self._db = db
        self._key = encryption_key
        # no mention of ClientService anywhere

    def _send_telegram(self, invoice):
        from app.services.client_service import ClientService   # deferred import
        client_svc = ClientService(self._db, self._key)         # new instance every call
        client = asyncio.run(client_svc.get_client(invoice["client_id"]))
```

Two things wrong here, and they compound. The import is deferred — hidden in the method body instead of at the top of the file. And every Telegram reminder constructs a brand-new `ClientService`, which after Case 1 also constructs its own `PIIEncryptor` each time. So a batch of reminders builds and discards that whole object graph once per message.

The real problem isn't the wasted allocations. It's that the dependency is invisible. The `__init__` signature says this class needs a database handle and an encryption key, nothing else. A static analyzer reading constructors will miss the `ClientService` dependency entirely. A reviewer skimming `__init__` will miss it. The dependency only materializes at the moment the method runs, which is the worst possible time to discover it.

The fix is constructor injection. Declare what you need where you can see it:

```python
from app.services.client_service import ClientService

class ReminderService:
    def __init__(self, db, encryption_key):
        self._client_service: ClientService | None = (
            ClientService(db, encryption_key) if encryption_key else None
        )

    def _send_telegram(self, invoice):
        client = asyncio.run(self._client_service.get_client(invoice["client_id"]))
```

**Before vs. after.** The diff was 6 insertions, 5 deletions — 11 lines total. All 54 existing `ReminderService` unit tests stayed green with zero changes to their patch targets. The reason is worth understanding: the tests were patching at the class-method level (`patch('app.services.client_service.ClientService.get_client')`), not at the instance level. A class-method patch replaces the method on the class itself, so it applies regardless of whether the instance was built in `__init__` or inside the method body. The structure moved; the behavior didn't; the tests couldn't tell the difference.

The structural win: `ReminderService` now builds one `ClientService` for its lifetime instead of one per Telegram message. And any future engineer reading `__init__` will see the dependency immediately — no surprises at runtime.

## Case 3: fifty-one database calls to send fifty reminders

This one wasn't a design-pattern fix. It was a performance bug wearing a design pattern as a disguise. The core of the daily reminder job:

```python
for invoice in invoices:
    client = asyncio.run(get_client_by_id(db, invoice["client_id"]))
    # do stuff
```

This is the N+1 query problem. One query fetches the list of overdue invoices. Then one more query per invoice fetches that invoice's client. Fifty invoices means fifty-one round-trips.

The cost isn't the database work — fetching one row by primary key is microseconds. It's the round-trip: each call sends a request and waits, and that waiting doesn't overlap with anything. The latency compounds serially. On a developer laptop with three rows it's invisible; with a customer who has hundreds of overdue invoices it dominates. And `get_clients_by_ids()`, the batch version, already existed in the query layer. Nobody was using it.

I found the hotspot by attaching pyinstrument to the job — `get_client_by_id` lit up as the dominant cost. The fix was four lines:

```python
# 2 DB calls total, regardless of invoice count
client_ids = list({str(inv["client_id"]) for inv in invoices})
clients_by_id = {
    str(c["id"]): c
    for c in asyncio.run(get_clients_by_ids(db, client_ids))
}
for invoice in invoices:
    client = clients_by_id.get(str(invoice["client_id"]))
```

The `set` comprehension does real work. If fifty invoices belong to only ten unique clients — realistic, since a few big customers usually generate most of the overdue ones — the deduplication fetches ten rows instead of fifty. The dict gives O(1) lookup inside the loop. Two queries total, and exactly one `asyncio.run` instead of N.

**Before vs. after.** I measured both approaches with pytest-benchmark, simulating 2ms of DB round-trip latency per call (conservative for a remote Postgres over a LAN), 50 invoices across 10 unique clients:

| Approach | Mean | Min | Max |
|---|---|---|---|
| N+1 (before) | 120.98 ms | 119.57 ms | 122.92 ms |
| Batch (after) | 2.48 ms | 2.05 ms | 3.44 ms |

That's a **49× speedup** for 50 invoices. The ratio gets worse as invoice count grows — at 500 invoices the N+1 approach would take ~1.2 seconds for the client-fetch step alone, while the batch stays flat at ~2ms regardless of N.

I added a regression test that asserts the batch call is made exactly once for any number of invoices — not timing-based, because wall time is noisy, but query-count-based, because that's exact. One existing BDD test needed a one-line update: its mock target changed from `get_client_by_id` to `get_clients_by_ids`. That's the full test-suite cost.

## What all three have in common

Each one followed the same three steps. Name the smell precisely — not "this feels off" but "this is an SRP violation," "this is a hidden dependency," "this is N+1." The naming matters because a named smell has a known fix. You stop staring at the code and go look up the move.

Then apply the named move. Extract Class. Constructor injection. Batch query. None of these are clever; they're in any refactoring book from the last twenty years. The vocabulary exists so that recognizing the smell is fast.

Then verify with tests. All three left existing tests green; two of them added new tests. If the only evidence a refactor works is that you reread the diff and it looked right, you don't actually know it works.

The one thing I keep underestimating is how cheap each move is once you've decided to do it. Extract Class was about an hour. Moving the import was eleven lines of diff. The N+1 fix was four lines and a 49× speedup. The files I'd stopped trusting are files I trust again — and I didn't schedule anything to make it happen.
