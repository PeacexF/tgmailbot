# Security Policy

## Reporting a Vulnerability

Please **do not** report security vulnerabilities through GitHub Issues

Instead contact through the provided contact info at `github.com/PeacexF`:
* Telegram: `https://t.me/peaceful_origin`
* Email: `peace_work@tuta.io`

### Include:

- steps to reproduce
- proof of concept

We will respond within 30 minutes

---

## Threat model

The bridge holds two credentials that each grant real access: a Mail.ru
application password that can read the mailbox, and a Telegram bot token that
can post as the bot in every group it belongs to. It also processes email, which
is attacker-controlled input by definition — anyone who knows the address can
send arbitrary MIME to it.

It runs as a single local process. It listens on nothing, exposes no HTTP
service, and stores no data outside one SQLite file.

## Credentials

**Never committed.** `.gitignore` excludes `.env` and `.env.*` while keeping
`.env.example`, and `.dockerignore` keeps them out of the build context, so a
credential file cannot reach an image layer.

**Never printed.** Secrets are wrapped in a `Secret` type that renders as `***`
from `__str__` and `__repr__`, so an f-string or a traceback cannot leak one.
The log formatter is a second, independent layer: it redacts both secret values
out of every rendered line, which catches the case where a library embeds the
bot token in a URL inside an exception message.

**Permissions are the operator's job.** The application does not chmod anything.
`.env` should be `0600` and owned by the account that runs the bridge; the
systemd unit reads its credentials from an `EnvironmentFile` expected to be the
same. See [docs/deployment.md](docs/deployment.md).

**Use an application password.** Mail.ru rejects the account password for IMAP
anyway, and an app password can be revoked without touching the account.

**Rotation needs a restart** — configuration is read once at startup.

## Transport

IMAP is TLS-only. `ssl=True` is hardcoded; there is no configuration option that
turns it off and no STARTTLS path. Telegram is reached over HTTPS through
`httpx`, with the default certificate verification left on.

## Untrusted input

Email is treated as hostile throughout:

- The parser never raises. A malformed part, a broken header or an unknown
  charset degrades to an empty string or a replacement character rather than
  taking down the daemon.
- Attachment filenames are sanitized to a bare printable basename — path
  separators, traversal segments and control characters are stripped — before
  they are used in a message or an upload.
- Attachment payloads are never written to disk, executed, or interpreted. They
  are forwarded as opaque bytes.
- HTML bodies are stripped to text; raw HTML is never forwarded, and `<script>`
  and `<style>` content is discarded rather than rendered.
- Outgoing text is escaped for Telegram's HTML parse mode, so a crafted subject
  cannot inject markup into the message.

Message size is bounded by Telegram's limits rather than the sender's: bodies
are split at 4096 characters, and attachments above the Bot API's 50 MB ceiling
are named in the message instead of uploaded.

## Logging

Email bodies and attachment contents are not logged. What gets logged about a
message is metadata only — UID, byte count, body length, attachment count.

**One caveat, stated plainly:** when the Telegram API rejects a request, its
error `description` is logged and, on a permanent failure, stored in the
`error` column. Telegram's parse-error descriptions can quote a short fragment
of the text they rejected, so a snippet of an email body can reach the log and
the database that way. If that matters in your environment, treat the log
stream and `mailbridge.db` as containing message content.

`LOG_LEVEL=DEBUG` increases volume but does not lower this bar.

## Stored data

One SQLite file, local, never sent anywhere. It holds sender addresses,
`Message-ID`s, IMAP UIDs, Telegram message ids, delivery status and the error
caveat above. It does **not** hold bodies or attachments.

Back it up with the same care as `.env` if you treat message metadata as
sensitive.

## Supply chain

Dependencies are pinned in `uv.lock` and installed with `uv sync --locked`, in
CI and in the Docker build alike, so a build resolves to exactly the audited
set. The runtime image carries no build tooling, and the whole runtime closure
is eight packages: two direct (`imapclient`, `httpx`) plus `anyio`, `certifi`,
`h11`, `httpcore`, `idna` and `typing_extensions`.

## Container posture

The image runs as a non-root user (uid 10001). Compose adds a read-only root
filesystem, `cap_drop: ALL`, `no-new-privileges`, and a tmpfs `/tmp`; `/data` is
the only writable path. No ports are published — both connections are outbound.
The systemd unit mirrors this with `ProtectSystem=strict`, an empty capability
bounding set, and a single `ReadWritePaths`.

## Out of scope

Encryption at rest for the SQLite file, multi-tenant isolation, and any form of
authenticated control surface. There is no control surface — the process takes
no input other than its configuration and the mailbox.
