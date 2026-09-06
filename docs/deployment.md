# Deployment

Three ways to run the bridge. All of them are the same single process — the
choice is only about what keeps it alive and where the SQLite file lives.

| | State | Restarts | Best for |
|---|---|---|---|
| [Docker Compose](#docker-compose) | `./data` bind mount | `unless-stopped` | a VPS you already run containers on |
| [systemd](#systemd) | `/var/lib/mailbridge` | `Restart=always` | a plain VPS |
| [Foreground](#foreground) | `./data` | none | development and first setup |

Before any of them, create the configuration file and lock it down —
see [Credential files](#credential-files). Every variable is described in
[configuration.md](./configuration.md).

---

## Docker Compose

```sh
cp .env.example .env
chmod 600 .env
$EDITOR .env

docker compose run --rm mailbridge --check   # validate before going resident
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` mounts `./data` at `/data` and pins
`DATABASE_PATH=/data/mailbridge.db`, so the database and its `-wal`/`-shm`
sidecars survive `docker compose down` and any rebuild.

**On Linux, give the volume to the container's user first.** The image runs as
uid 10001, and a bind mount keeps the host's ownership, so a `./data` owned by
your login account is not writable inside the container:

```sh
sudo chown -R 10001:10001 ./data
```

Docker Desktop on macOS and Windows maps ownership for you and does not need this.

The container is deliberately confined: read-only root filesystem, all
capabilities dropped, `no-new-privileges`, and a tmpfs `/tmp`. It publishes no
ports — both connections are outbound.

To upgrade, rebuild and recreate; the state volume is untouched:

```sh
git pull && docker compose up -d --build
```

---

## systemd

[`deploy/mailbridge.service`](../deploy/mailbridge.service) is a working unit —
copy it, adjust the paths, and it verifies clean under `systemd-analyze verify`.

```sh
# A service account that owns nothing else and cannot log in.
sudo useradd --system --home /opt/mailbridge --shell /usr/sbin/nologin mailbridge
sudo install -d -o mailbridge -g mailbridge -m 0750 /opt/mailbridge /var/lib/mailbridge

# Install the code and its virtualenv.
sudo -u mailbridge git clone https://github.com/PeacexF/tgmailbot /opt/mailbridge
cd /opt/mailbridge && sudo -u mailbridge uv sync --locked --no-dev

# Credentials: root-readable is not enough, the service account needs them.
sudo install -o mailbridge -g mailbridge -m 0600 .env /etc/mailbridge.env

sudo install -m 0644 deploy/mailbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mailbridge
```

Check it, then watch it:

```sh
systemctl status mailbridge
journalctl -u mailbridge -f
```

The unit sets `TimeoutStopSec=60` with `KillSignal=SIGTERM` because the bridge
finishes the message it is delivering before exiting; a shorter timeout risks a
`SIGKILL` mid-delivery, which costs a duplicate on the next start rather than a
lost email, but there is no reason to invite it. `StartLimitIntervalSec=0`
disables systemd's start-rate limiter so a long outage cannot leave the service
stopped for good.

`ProtectSystem=strict` makes the whole filesystem read-only apart from
`ReadWritePaths=/var/lib/mailbridge`. If you move `DATABASE_PATH`, move that
line with it or the bridge will fail to open its state.

---

## Foreground

```sh
uv sync
cp .env.example .env && chmod 600 .env
uv run mailbridge --check      # configuration only, no network
uv run mailbridge --dry-run    # fetches from the mailbox, sends nothing
uv run mailbridge              # runs until Ctrl-C
```

---

## Credential files

`.env` holds the mailbox application password and the Telegram bot token. Both
are enough to read the mailbox and post to the group, so the file is the most
sensitive thing in the deployment.

* **Mode `0600`, owned by the account that runs the bridge.** `chmod 600 .env`.
  Under systemd that account is `mailbridge`, not root — `EnvironmentFile` is
  read as the service user.
* **Never commit it.** `.gitignore` already excludes `.env` and `.env.*` while
  keeping `.env.example`. Check with `git check-ignore -v .env`.
* **Keep it out of images.** `.dockerignore` excludes `.env` from the build
  context, and `docker-compose.yml` passes it at runtime via `env_file`, so it
  is never written into a layer. Verify with
  `docker run --rm --entrypoint sh mailbridge:latest -c 'ls -a /app /data'`.
* **Prefer a Mail.ru application password.** Mail.ru rejects the account
  password on IMAP outright (`AUTHENTICATIONFAILED ... Application password is
  REQUIRED`), and an app password can be revoked on its own.
* **Rotating a credential requires a restart.** Configuration is read once at
  startup and never re-read, so editing the file alone changes nothing:
  `systemctl restart mailbridge` or `docker compose restart`. A daemon whose
  credential was revoked under it keeps retrying with backoff instead of
  exiting, so the service stays up and the restart can wait for you.

Secrets never reach the logs: `Secret` renders as `***` in tracebacks and
f-strings, and the log formatter redacts both values out of every line. Email
bodies and attachment contents are never logged either.

---

## Backing up the SQLite file

The database is small — a row per message — but losing it means the bridge no
longer knows what it has already forwarded. On a fresh database the first pass
starts from the unseen mail rather than replaying the mailbox, so the damage is
bounded; a restored backup avoids even that.

**Do not copy `mailbridge.db` with `cp` while the bridge is running.** WAL mode
keeps recent commits in `mailbridge.db-wal`, so a bare file copy can be missing
the newest rows or be torn mid-write. Use SQLite's own online backup, which is
consistent against a live writer:

```sh
sqlite3 /var/lib/mailbridge/mailbridge.db \
  ".backup '/var/backups/mailbridge-$(date +%F).db'"
```

`VACUUM INTO` works too and compacts as it goes:

```sh
sqlite3 /var/lib/mailbridge/mailbridge.db \
  "VACUUM INTO '/var/backups/mailbridge-$(date +%F).db'"
```

The image ships no `sqlite3` binary, but it has Python, whose stdlib exposes the
same online backup and needs nothing installed:

```sh
docker compose exec -T mailbridge python -c "
import sqlite3
src = sqlite3.connect('file:/data/mailbridge.db?mode=ro', uri=True)
dst = sqlite3.connect('/data/backup.db')
with dst: src.backup(dst)
dst.close(); src.close()
"
```

That writes into the mounted volume, so the copy lands in `./data/backup.db` on
the host. Move it off the machine from there.

Stopping the bridge first is the other option: with the process down, a plain
copy of `mailbridge.db`, `-wal` and `-shm` together is safe.

A daily copy kept for a week is ample. Back the file up with the same care as
`.env` if you treat message metadata as sensitive: it stores sender addresses,
`Message-ID`s and Telegram message ids, but no bodies and no attachments.

Restoring is a file copy back into place with the bridge stopped. On the next
start the daemon resumes from the highest delivered UID in the restored file and
re-fetches anything above it, so a slightly stale backup costs duplicates, never
lost mail.
