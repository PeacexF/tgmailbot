# Architecture

One process, one mailbox, one Telegram group, one SQLite file. No queue, no
worker pool, no web service, nothing listening on a port. Both network
connections are outbound.

```text
                    ┌──────────────────────────────────────┐
  Mail.ru ──IMAP──► │  imap.py      connect, IDLE, fetch   │
   :993 TLS         │       ↓                              │
                    │  parser.py    MIME → Email           │
                    │       ↓                              │
                    │  database.py  claim before send      │ ──► mailbridge.db
                    │       ↓                              │
                    │  telegram.py  format, split, upload  │
                    └──────────────────┬───────────────────┘
                                       │ HTTPS
                                       ▼
                          Telegram Bot API ──► group
```

The dependency graph is shallow and acyclic. `config.py`, `log.py`, `parser.py`
and `database.py` import nothing from the package. `imap.py` reads `Config`;
`telegram.py` reads `Config` plus the `Email` and `Attachment` shapes it
renders. Only `main.py` sees everything, which is why it is the only place the
delivery ordering lives.

## Modules

| Module | Responsibility | Deliberately not its job |
|---|---|---|
| `config.py` | Load and validate environment, wrap secrets | Deciding defaults for *behaviour* — only for connection details |
| `log.py` | One stderr handler; redact secrets from every line | Log routing, files, rotation |
| `imap.py` | Connect, select folder, search, fetch, IDLE | Knowing what a message means |
| `parser.py` | Raw bytes → `Email`; never raises | Anything network- or Telegram-shaped |
| `database.py` | Delivery state, dedup keys, crash recovery | Deciding *when* to send |
| `telegram.py` | Formatting, escaping, splitting, retries, rate limit | Knowing about mailboxes or UIDs |
| `main.py` | CLI, the loop, and the ordering that makes delivery safe | Any protocol detail |

## One pass

```text
resume_uid ──► UID SEARCH ──► FETCH  ──►  parse   ──► dedup ──► claim ──►    send    ──► mark sent
   (db)          (imap)       (imap)     (parser)      (db)      (db)     (telegram)        (db)
```

1. **Where to start.** `resume_uid()` returns the highest UID that is safely
   behind us. Anything still `pending`, `sending` or `failed` drags that mark
   back below itself, so an unresolved message is re-fetched rather than
   stranded. With no state at all it returns `None` and the first pass searches
   `UNSEEN` instead of replaying the whole mailbox.
2. **Search.** `UID n:*` rather than `UNSEEN`, so mail that someone read in the
   webmail while the daemon was down is still forwarded. The server answers
   `n:*` with the folder's highest UID even when that is below `n`, so the
   result is filtered again on the client.
3. **Fetch.** `BODY.PEEK[]` against a read-only folder selection: a pass never
   sets `\Seen`, so the flag keeps meaning whatever it means to a human reading
   the same account.
4. **Parse.** A MIME walk producing sender, recipients, subject, date, a text
   body, and attachment descriptors. It never raises — a malformed part becomes
   an empty string, and an unknown charset falls back to UTF-8 with replacement.
5. **Dedup.** Two keys, described below.
6. **Deliver.** Claim, send, mark — in that order, for the reason below.

## The delivery guarantee

**At-least-once.** A duplicate is preferable to a lost email, so the ordering is
chosen to fail in that direction:

```text
record  → pending    committed
claim   → sending    committed  ← crash here re-delivers
        → sendMessage
mark    → sent       committed  ← crash here re-delivers
```

The row is written and committed *before* the Telegram call, never after. A
crash anywhere in the middle leaves a row in `sending`, which `resume_uid()`
treats as unresolved and the next pass retries. The cost is one duplicate; the
alternative ordering would silently drop mail.

Rows left in `sending` by a crash are reported at startup rather than cleaned up
quietly, so an operator sees that a retry is coming.

### Two dedup keys

**`(mailbox, uidvalidity, uid)`** is the primary key. A bare UID is not enough:
UIDs are only unique within one `UIDVALIDITY`, and the server may renumber the
folder at any time.

**`Message-ID`** is the fallback for exactly that renumbering. When the same
message reappears under a new UID, a delivered `Message-ID` for the mailbox
marks it sent without re-sending.

A message with **no `Message-ID` header** cannot be recognised across a
renumbering and will be delivered twice. That is a known, tested limitation, and
it is the at-least-once bargain working as intended.

## Staying up

| Failure | Response |
|---|---|
| IMAP connection lost | Reconnect with exponential backoff and jitter, capped at 300 s |
| `UIDVALIDITY` changed | Re-read on every folder open; state is keyed by it, so old rows simply stop matching |
| Server has no `IDLE` | Fall back to polling with `NOOP` |
| `IDLE` outstanding too long | Re-issue every 29 minutes, per RFC 2177 |
| Telegram `429` | Honour `retry_after`; a global limiter spaces sends ~3 s apart |
| Telegram `5xx` | Retry with backoff, up to 5 attempts |
| Telegram `4xx` | Permanent: mark the row `failed` rather than blocking the queue |
| Attachment upload fails | Isolated per file; the email stays `sent` and a note goes to the group |
| `SIGTERM` / `SIGINT` | Finish the in-flight message, then exit 0 |

Liveness is a heartbeat log line every 15 minutes carrying counts since start —
enough to tell an idle daemon from a wedged one.

## Formatting decisions

**Parse mode is `HTML`.** Three characters need escaping (`&`, `<`, `>`) against
MarkdownV2's eighteen, all of which occur freely in real subject lines.

**Bodies are plain text.** `text/plain` when the message has it, otherwise the
HTML part run through a tag stripper. Raw HTML is never forwarded.

**Long emails are split** on line boundaries at Telegram's 4096-character limit,
backing off a cut that would land inside an entity or a tag. The first chunk's
message id is what gets stored.

**Inline parts below 16 KB are dropped** — signature logos and tracking pixels,
not documents. `Content-Disposition: attachment` is always forwarded regardless
of size, and anything above the Bot API's 50 MB ceiling is named in the body
with its size instead of being uploaded.

**Filenames are sanitized** to a bare printable basename: attachment names are
untrusted input from the network.

## Known bounds

**Memory is one message plus its largest attachment.** `email.message_from_bytes`
materialises the whole message before any part can be read, so spooling decoded
payloads to disk would add a copy without lowering the peak. Real streaming
needs per-part IMAP fetch (`BODY.PEEK[n]`) and an incremental MIME decode —
worth doing only if real traffic shows it matters.

**One mailbox, one folder, one destination.** Multiple accounts, extra folders,
filters and Telegram topics are all post-MVP, and none of them is stubbed for
anywhere in the code.

**Configuration is read once at startup.** A rotated credential needs a restart;
the daemon will keep retrying with backoff until it gets one.
