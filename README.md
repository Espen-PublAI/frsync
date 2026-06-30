# FRsync

**A file transfer and immort editor system for [Final Realms: Legacy](https://fr.hyssing.net).**

FRsync lets builders move files between their own computer and the MUD over the
normal creator login — **no FTP required.** It works because FR's creator shell
exposes `exec` (run LPC) and the `write_file` / `read_bytes` efuns; FRsync logs
in as you and pushes/pulls files straight over that connection. The MUD's own
permission system (`valid_write`) decides what you may touch — exactly as it
does in-game — so this works for any builder, for their `/w/<name>/` home and
any `/d/<zone>/` they're granted.

It comes in two parts:

1. **`frsync.py`** — a zero-dependency command-line tool (interactive shell +
   scriptable `push`/`pull`/`status`/`mirror`/`watch`).
2. **`frsync_mcp.py`** — an MCP server that exposes the MUD to
   [Claude Code](https://claude.com/claude-code) (or any MCP client) as tools,
   so an AI agent can read a room, fix a bug, write it back, reload, and test.

---

## Requirements

- **Python 3.8+** (the CLI uses only the standard library).
- A **creator/builder account** on Final Realms: Legacy.
- For the MCP server only: `pip install mcp keyring`
  (`keyring` provides cross-platform secure credential storage).

The default server is `fr.hyssing.net:4010`; override with `--host` / `--port`
(CLI) or `FRHOST` / `FRPORT` (MCP).

---

## Get started

```bash
git clone <repo-url> frsync
cd frsync
```

Nothing to build — `frsync.py` runs as-is. Everything below is driven through
the `Makefile`; run `make help` to see every target.

---

## Part 1 — The command-line tool

### Interactive shell

```bash
make start
```

You'll be prompted for your creator name and password, then dropped into an
interactive shell that behaves **just like telnet** — every normal MUD and
creator command goes straight to the server (`look`, `ed myroom.c`,
`update myroom.c`, `ls /w/you`, …). On top of that you get local `/commands`
for moving files:

| Command | Does |
|---|---|
| `/upload <file>`   | send a local file → the remote folder |
| `/download <file>` | fetch a remote file → this computer |
| `/where`           | show your current local + remote folders |
| `/lcd <dir>`       | change the **local** folder (downloads land here) |
| `/rcd <dir>`       | change the **remote** folder (transfers use this) |
| `/lpwd`, `/lls`    | print / list the local folder |
| `/help`, `/quit`   | full command list / leave |

Downloaded files land in **`./downloads`** by default (created automatically,
relative to where you launched), so they don't clutter your working folder.
Change it any time with `/lcd <dir>`, or start elsewhere with
`make start DIR=./myarea`.

### Scriptable commands

For automation and one-shot transfers, call the subcommands directly. Every
write is **verified** (length + checksum) after upload.

```bash
# compare a local area against the MUD, change nothing
python3 frsync.py status ./drifting_forest /w/<creator>/drifting_forest

# upload an area (or a single file); --dry-run to preview, --delete to prune
python3 frsync.py push ./drifting_forest /w/<creator>/drifting_forest

# download an area
python3 frsync.py pull /w/<creator>/drifting_forest ./backup

# bulk-download whole server subtrees into ./MUD (resumable — re-run to continue)
python3 frsync.py mirror ./MUD /std /d /w/<creator>

# "synced folder": auto-push a local dir as files change
python3 frsync.py watch ./myarea /w/<creator>/myarea
```

Handy global flags: `--char NAME` (skip the name prompt), `--ext .c,.h`
(directories: only these extensions), `--dry-run`, `--delete`, `-y`/`--yes`.
Run `python3 frsync.py --help` for the complete list.

The same operations are wrapped as Make targets:

```bash
make mirror-code CHAR=yourname                 # mirror the standard mudlib roots
make status DIR=./myarea REMOTE=/w/you/myarea
make watch  DIR=./myarea REMOTE=/w/you/myarea
```

### Install on your PATH (optional)

```bash
make install            # installs `frsync` to /usr/local/bin (set PREFIX= to change)
frsync --help
make uninstall
```

---

## Part 2 — The MCP server for Claude Code

This exposes the MUD to an AI agent as tools: `mud_ls`, `mud_read`,
`mud_write`, `mud_download`, `mud_upload`, `mud_command`, `mud_exec`,
`mud_whoami`. (There is deliberately **no delete tool** — the server never
removes server files.)

### 1. Install the dependencies

```bash
make mcp-deps           # pip install mcp
pip install keyring     # for secure credential storage
```

### 2. Register the server with Claude Code

```bash
make mcp-register
```

This registers the server **without any credentials** in the config:

```
claude mcp add frsync-mud -- python3 /path/to/frsync_mcp.py
```

### 3. Store your credentials securely (one time)

```bash
make mcp-login
```

You'll be prompted for your creator name and password (**hidden — no echo**).
They're saved to your operating system's secure secret store, **never** to any
config file. Then **restart the frsync-mud MCP server** for it to take effect.

That's it — the agent now has the `mud_*` tools available.

---

## Credentials & security

Credentials are stored in your OS secret store via the cross-platform
[`keyring`](https://pypi.org/project/keyring/) package:

| OS | Where it's stored |
|---|---|
| macOS   | login Keychain |
| Windows | Credential Locker |
| Linux   | Secret Service (libsecret / GNOME Keyring / KWallet) |

Manage them with:

```bash
make mcp-login      # enter / update credentials
make mcp-status     # show whether credentials are configured (no secrets shown)
make mcp-reset      # forget the stored credentials
```

(or call `python3 frsync_mcp.py --login | --status | --reset` directly.)

**Resolution order at connect time:**
`FRCHAR` + `FRPASS` environment variables (an escape hatch for CI) →
the OS secret store → otherwise the tools fail with a message telling you to
run `--login`. **No password is ever written to a config file in plaintext,**
and the interactive CLI tools read the password with no echo, so it never
passes through the agent or model context.

> The command-line tool (`frsync.py`) prompts for credentials on each run (or
> reads `$FRPASS` / `--char` if you set them); only the long-running MCP server
> uses the secret store.

---

## Troubleshooting

- **Only one session per creator at a time.** A creator can only be connected
  once. If you're already logged in elsewhere (e.g. the MCP server holds the
  session, or you're in the game normally), FRsync detects it and uses FR's
  built-in takeover: the interactive shell asks *"<name> is already logged in
  elsewhere. Throw out that session and take over here? [Y/n]"*, and the MCP
  server reclaims the session automatically. Note this means the two can play
  tug-of-war — use one at a time.
- **Restart the MCP server after `make mcp-login` / `mcp-reset`.** A running
  server keeps the old credentials until it's respawned.
- **"No frsync credentials configured."** Run `make mcp-login`, then restart the
  server.
- **Editing `frsync.py` doesn't change MCP behavior?** The MCP server loads the
  engine once at startup — restart it after editing.
- Syntax-check the tools any time with `make check`.

---

## How it works (in brief)

FRsync speaks the creator shell's `exec` channel rather than a file-transfer
protocol. Reads use `read_bytes` (chunked, since it's capped per call); writes
use `write_file` and are verified after the fact with a length + CRC check.
Everything you can do is bounded by your in-game permissions — FRsync grants no
access you don't already have as a builder.

---

Built by **Malric** and **Silbago** for Final Realms: Legacy.
For questions, contact the Final Realms admin.
