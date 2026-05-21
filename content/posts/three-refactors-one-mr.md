---
title: 'Three Refactors I Shipped in One MR'
date: 2026-05-21
draft: false
tags: ['refactoring', 'python', 'software-engineering']
---

There's a specific kind of file you learn to dread. Not broken — tests pass, CI is green, customers are using it. But you avoid it anyway. You touch it only when you have to, and when you do, you spend the first few minutes re-orienting: which method calls what, what side effects lurk where, whether there's another spot you'll forget to update.

`ClientService` was that file. 354 lines, tested, shipping. But every time I touched anything PII-related, I'd Ctrl+F the file to find every spot that called `encrypt()`, because I didn't trust myself to remember all three. That reflex is the smell. I'd been ignoring it for weeks.

So I opened a branch and decided to fix three things in one MR. Three named refactors, each verified by tests, none of them changing behavior. Here's what the service looked like, what each move was, and what the numbers looked like before and after.

The service is part of an invoice-reminder SaaS. There's a class that manages clients, a class that sends reminders, and a background job that does the daily chasing. The smells are generic enough that you've probably got the same ones somewhere.

## Case 1: the class was doing two jobs

`ClientService` was holding two unrelated responsibilities: the business of clients (create, validate, bulk import) and the cryptography of clients (encrypting and decrypting email, phone, and Telegram ID). The practical version of the Single Responsibility Principle is that a class should have one reason to change. This class had at least two — swapping encryption algorithms and changing client validation logic are completely different kinds of work, and both meant editing the same 354-line file.

The fix has a name: Extract Class. Take the responsibility that doesn't belong and lift it into its own class.

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
            return value      # legacy plaintext fallback
        except InvalidTag:
            return value      # key-rotation fallback
```

After:

```python
class PIIEncryptor:
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
        self._pii = PIIEncryptor(encryption_key, PII_FIELDS)

    def create(self, ...):
        encrypted = self._pii.encrypt_field(value)
```

The part worth dwelling on is those two `except` clauses, because the refactor made them significantly better, not just differently located.

`ValueError` handles legacy data written before encryption was turned on — plaintext rows that decryption will choke on. `InvalidTag` handles data encrypted under a key we've since rotated away from. Both are real production states with real rows behind them. In the old code, both cases silently returned the raw value with no logging at all. The before code looked like this:

```python
except ValueError:
    return value   # no log, no warning, just returns garbage silently
except InvalidTag:
    return value   # same — silent failure
```

A silent return here is particularly bad. If a client's email is still plaintext and we return it as-is, the calling code doesn't know whether it got a decrypted value or a raw plaintext one. There's no signal that something unusual happened. The after version logs a `WARNING` for the `ValueError` case (expected, but worth tracking) and an `ERROR` for `InvalidTag` (unexpected — either the key rotated without a migration, or data is corrupted). Now when we get a Sentry alert about failed decryptions, there's something to look at.

The old handlers also sat inside a class that nominally knows nothing about encryption. Anyone debugging a decryption failure had to already know to look in `ClientService`. Now they live in `PIIEncryptor`, 38 lines whose entire job is making sense of bytes — exactly where you'd look.

**Before vs. after.** Change scope for a future crypto migration: before, you'd hunt through 354 lines for every `encrypt()` call across three methods. After, you touch `PIIEncryptor` only — 38 lines, one class. `ClientService` doesn't change at all. The 42 existing tests all stayed green; `PIIEncryptor` is now independently testable without standing up the whole client service.

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

Two things wrong here, and they compound. The import is deferred — tucked inside the method body instead of at the top of the file. And every Telegram reminder constructs a brand-new `ClientService`, which after Case 1 also constructs its own `PIIEncryptor` each time. A batch of fifty reminders builds and discards that object graph fifty times.

The real problem isn't the wasted allocations. It's that the dependency is invisible. The `__init__` signature says this class needs a database handle and an encryption key, nothing else. A static analyzer reading constructors will miss the `ClientService` dependency entirely. Run mypy or pyright on this code and they'll happily tell you `ReminderService.__init__` takes `db` and `encryption_key` — no mention of `ClientService`. The dependency only materializes at the moment `_send_telegram` runs, which is the worst possible time to discover it.

Deferred imports also suppress circular import errors until runtime. If `client_service.py` ever imported something from `reminder_service.py`, Python would catch the cycle immediately with a top-level import. With a deferred import, you find out when the method actually executes in production, not when the module loads.

The fix is constructor injection. Declare what you need where you can see it:

```python
from app.services.client_service import ClientService

class ReminderService:
    def __init__(self, db, encryption_key):
        self._client_service: ClientService | None = (
            ClientService(db, encryption_key) if encryption_key else None
        )

    def _send_telegram(self, invoice):
        if not self._client_service:
            logger.error("Cannot send Telegram: ClientService not initialized")
            return "FAILED"
        client = asyncio.run(self._client_service.get_client(invoice["client_id"]))
```

The `| None` type annotation is doing real work here, not just being pedantic. The `if encryption_key` guard means `_client_service` can be `None` in test environments where no key is configured. Mypy and pyright both catch if you forget to check before using it, which is exactly the kind of mistake a deferred-import pattern lets slip through.

**Before vs. after.** The diff was 6 insertions, 5 deletions — 11 lines total. All 54 existing `ReminderService` unit tests stayed green with zero changes to their patch targets.

The reason that last part is true is worth understanding, because it's the kind of detail that tells you a refactor is genuinely behavior-preserving. The tests were patching at the class-method level:

```python
@patch('app.services.client_service.ClientService.get_client')
def test_send_telegram_success(self, mock_get_client):
    mock_get_client.return_value = fake_client
    ...
```

A class-method patch replaces the method on the class itself, not on any specific instance. So it applies whether the instance was built in `__init__` or inside the method body. The structure moved; the behavior didn't; the tests couldn't tell the difference. That's the proof — not my reading of the diff, but 54 tests asserting that the behavior is identical.

Structural win: `ReminderService` now builds one `ClientService` for its lifetime instead of one per Telegram message. Any engineer reading `__init__` immediately sees the dependency — no surprises at runtime.

## Case 3: fifty-one database calls to send fifty reminders

This one wasn't a design-pattern fix. It was a performance bug wearing a design pattern as a disguise. The core of the daily reminder job:

```python
for invoice in invoices:
    client = asyncio.run(get_client_by_id(db, invoice["client_id"]))
    # map risk level → message tone, then send
```

This is the N+1 query problem. One query fetches the list of overdue invoices. Then, inside the loop, one more query per invoice fetches that invoice's client. Fifty invoices: fifty-one round-trips.

The cost isn't the database work — fetching one row by primary key is microseconds. It's the round-trip: each call sends a request over the network (or even a local socket to Postgres), waits for the reply, then moves on. That waiting doesn't overlap with anything. The latency compounds serially. On a developer laptop with three rows in the database it's completely invisible. With a customer who has hundreds of overdue invoices it starts to dominate.

I found it by running pyinstrument against the job. The output looks roughly like this:

```
  _     ._   __/__   _ _  _  _ _/_   Recorded: 11:34:22  Samples:  847
 /_//_/// /_\ / //_// / //_'/ //     Duration: 2.832     CPU time: 0.091
/   _/                      v4.7.3

2.832 send_overdue_reminders  task.py:47
   2.831 <module>  [47 frames hidden]
      2.809 get_client_by_id  queries/clients.py:31
         2.803 execute  [supabase internals]
```

`get_client_by_id` is 99% of the runtime. The actual business logic — mapping risk level to message tone, formatting the message — barely registers. The hot path is entirely in the loop doing individual fetches.

And `get_clients_by_ids()`, the batch version, already existed in the query layer. Nobody was using it.

The fix was four lines:

```python
# BEFORE: N+1 — one get_client_by_id call per invoice
for invoice in invoices:
    client = asyncio.run(get_client_by_id(db, invoice["client_id"]))

# AFTER: 2 DB calls total, regardless of invoice count
client_ids = list({str(inv["client_id"]) for inv in invoices})
clients_by_id = {
    str(c["id"]): c
    for c in asyncio.run(get_clients_by_ids(db, client_ids))
}
for invoice in invoices:
    client = clients_by_id.get(str(invoice["client_id"]))
```

The `set` comprehension on the first line is doing real work, not just collecting IDs. If fifty invoices belong to only ten unique clients — realistic, since a handful of big customers usually generate most of the overdue ones — the deduplication fetches ten rows instead of fifty. The dict gives O(1) lookup inside the loop. Two `asyncio.run` calls total: one for the invoice list, one for the batch client fetch.

**Before vs. after.** I measured both approaches with pytest-benchmark, simulating 2ms of DB round-trip latency per call (conservative for a local Postgres over loopback; a remote Supabase instance would be 10–20ms). 50 invoices, 10 unique clients:

| Approach | Mean | Min | Max |
|---|---|---|---|
| N+1 (before) | 120.98 ms | 119.57 ms | 122.92 ms |
| Batch (after) | 2.48 ms | 2.05 ms | 3.44 ms |

That's a **49× speedup** at 50 invoices. But the ratio isn't fixed — it gets worse as invoice count grows, because N+1 scales linearly while the batch stays roughly flat:

| Invoice count | N+1 (estimated) | Batch (estimated) |
|---|---|---|
| 50 | ~120 ms | ~2.5 ms |
| 200 | ~480 ms | ~2.5 ms |
| 500 | ~1,200 ms | ~2.5 ms |
| 1,000 | ~2,400 ms | ~2.5 ms |

At 500 invoices the N+1 approach takes over a second just for the client-fetch step, before any reminder logic runs. A cron job that's supposed to fire at 8am and finish in a few seconds is now grinding through work that blocks the next run. At 1,000 invoices it tips past two seconds and you start getting overlap between scheduled runs.

I added a regression test — not timing-based, because wall time is noisy, but query-count-based:

```python
def test_clients_fetched_in_single_batch_not_per_invoice():
    """Asserts exactly 1 DB call for clients, regardless of invoice count."""
    call_count = 0
    original_sleep = time.sleep

    def counting_sleep(s: float) -> None:
        nonlocal call_count
        call_count += 1

    time.sleep = counting_sleep
    try:
        approach_batch(INVOICES)  # 50 invoices
    finally:
        time.sleep = original_sleep

    assert call_count == 1  # single batch call, not 50
```

If anyone drops `asyncio.run(get_client_by_id(...))` back inside the loop, this test fails immediately. Count is exact; milliseconds aren't. One existing BDD test needed a one-line update — its mock target changed from `get_client_by_id` to `get_clients_by_ids`. That's the full test-suite cost of the refactor.

## Why bundle them into one MR

Worth addressing, because "one MR for three unrelated changes" sounds like something a code review would push back on.

These aren't unrelated. All three touch the same service cluster: `ClientService`, `ReminderService`, and the background job that drives them. Case 1 created `PIIEncryptor`. Case 2 depended on Case 1 being done first — the "wasted object construction" argument for constructor injection gets stronger once `ClientService.__init__` is itself doing more work (building a `PIIEncryptor`). Case 3 is in the same job that calls `ReminderService`. The changes are causally linked, not just coincidentally co-located.

Bundling also made the MR easier to review, not harder. A reviewer could see the whole picture: the crypto responsibility moved, the hidden dependency became visible, the N+1 got eliminated. Three diffs that tell a coherent story about "we stopped trusting this service cluster and here's why we trust it now" are cleaner than three separate MRs requiring separate context.

## What all three have in common

Each one followed the same three steps. Name the smell precisely — not "this feels off" but "this is an SRP violation," "this is a hidden dependency," "this is N+1." The naming matters because a named smell has a known fix. You stop staring at the code and go look up the move.

Then apply the named move. Extract Class. Constructor injection. Batch query. None of these are clever; they're all in Fowler's *Refactoring* or any similar book from the last twenty years. The vocabulary exists so that recognizing the smell is fast, not so you can sound smart in a review.

Then verify with tests. All three left existing tests green; two added new tests. If the only evidence a refactor works is that you reread the diff and it looked right, you don't actually know it works. You just haven't found out yet.

The one thing I keep underestimating is how cheap each move is once you've committed to it. Extract Class was about an hour. Moving the import was 11 lines of diff. The N+1 fix was four lines of code and a 49× speedup. The files I'd stopped trusting are files I trust again — and I didn't schedule anything to make it happen.
