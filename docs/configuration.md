# Configuration

Every setting is an environment variable. There is no config file format of its
own — [`.env.example`](../.env.example) is the template, and
[deployment.md](./deployment.md) covers where that file lives and what it should
be chmod'ed to.

## Variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MAIL_USERNAME` | **yes** | — | The full address, e.g. `example@mail.ru`. Also the `mailbox` key in the state database, so changing it starts fresh. |
| `MAIL_PASSWORD` | **yes** | — | A Mail.ru **application password**, not the account password. |
| `TELEGRAM_BOT_TOKEN` | **yes** | — | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHAT_ID` | **yes** | — | The destination group. Usually negative, e.g. `-1001234567890`. |
| `MAIL_HOST` | no | `imap.mail.ru` | IMAP over TLS only; there is no STARTTLS path. |
| `MAIL_PORT` | no | `993` | Integer, 1–65535. |
| `MAIL_FOLDER` | no | `INBOX` | One folder. Multi-folder monitoring is out of scope for the MVP. |
| `DATABASE_PATH` | no | `./data/mailbridge.db` | Parent directories are created on first run. |
| `LOG_LEVEL` | no | `INFO` | One of `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`. Case-insensitive. |

Validation reports **every** problem at once rather than stopping at the first,
and never echoes a value:

```
configuration is invalid:
  - MAIL_USERNAME is required but missing or empty
  - TELEGRAM_BOT_TOKEN is required but missing or empty
see .env.example for the expected variables
```

Check a configuration without touching the network:

```sh
mailbridge --check      # exit 0 valid, exit 2 invalid
```

## How the values are found

`.env` in the working directory is read first, then the real environment is
merged over it — **an exported variable beats the file**. That is what lets a
container or a systemd unit inject credentials without a file on disk.

The `.env` parser is deliberately small: `KEY=value` per line, `#` comments, an
optional `export ` prefix, and one layer of matching single or double quotes
stripped. There is no interpolation (`$OTHER` stays literal) and no multi-line
values. A malformed line is skipped rather than raised, so one stray line cannot
stop the daemon from starting.

Because the working directory decides which `.env` is read, both the container
and the systemd unit deliberately run from a directory that has none — see
[deployment.md](./deployment.md).

## Getting the credentials

### Mail.ru application password

Mail.ru rejects the ordinary account password on IMAP outright:

```
AUTHENTICATIONFAILED ... Application password is REQUIRED
```

Create one under **Security → Passwords for external applications**, and give it
mail access. It can be revoked on its own without touching the account password.
IMAP also has to be enabled for the mailbox.

### Telegram bot token

Message [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token it
returns. Treat it as a credential: it is enough to post as the bot anywhere the
bot is a member.

### Telegram chat id

Add the bot to the destination group first — a bot cannot look up a group it is
not in. Then post any message to the group and read the id back:

```sh
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | grep -o '"chat":{"id":[-0-9]*'
```

Supergroup ids start with `-100`. If `getUpdates` comes back empty, the bot's
privacy mode is hiding group messages from it — turn it off in BotFather under
**Bot Settings → Group Privacy**, then remove and re-add the bot.

The bot needs permission to send messages and to send files in that group.

## Command-line flags

Flags cover how a run behaves, never what it connects to; connection details are
environment variables only.

| Flag | Effect |
|---|---|
| *(none)* | Run continuously: catch up, then watch with IMAP IDLE until stopped. |
| `--check` | Validate configuration and exit. No network, no database writes. |
| `--once` | One pass over the mailbox, then exit. |
| `--dry-run` | Fetch from the mailbox and stop there: nothing is parsed, sent, or recorded. Implies `--once`. |
| `--limit N` | Forward at most N messages per pass (default 10). |
| `--version` | Print the version and exit. |

Exit codes: `0` success, `1` a mailbox, state or delivery failure, `2` invalid
configuration.

## What is never logged

Bodies, attachment contents, passwords and tokens stay out of the log. The
`Secret` wrapper renders as `***` in f-strings and tracebacks, and the log
formatter additionally redacts both secret values out of every rendered line, so
a library exception that embeds the token in a URL cannot leak it either.

`LOG_LEVEL=DEBUG` raises the volume — per-part decode failures, IDLE responses —
but does not lower that bar.
