#!/usr/bin/env python3
"""
frsync MCP server — exposes the Final Realms MUD to Claude Code (or any MCP
client) as tools, reusing the same engine as frsync.py. No FTP; everything
goes over the normal creator login via `exec` + read_bytes/write_file.

WHY: lets an agent read/write MUD files and run commands agentically against
the live game — read a room, fix a bug, write it back, reload, test.

SETUP
  1. pip install mcp            # the only dependency (the human CLI has none)
  2. Store your creator credentials once, securely, in the OS secret store
     (macOS Keychain / Windows Credential Locker / Linux Secret Service, via
     the cross-platform `keyring` package — `pip install keyring`).
     Run this in a terminal (NOT through the MCP client — the password is
     read with no echo and never touches the agent/model context):
        python3 /path/to/frsync_mcp.py --login
     It asks for your creator name + password and saves them to the secret
     store. Nothing is written to any config file in plaintext.
        --login    (re)enter and store credentials
        --reset    forget the stored credentials (then --login again)
        --status   show whether credentials are configured (no secrets shown)
  3. Register with Claude Code — NO credentials in the registration:
        claude mcp add frsync-mud \\
          -- python3 /path/to/frsync_mcp.py
     (or add an entry to .mcp.json / settings — command: python3, args: [this file])

  Credential resolution order at connect time:
     1. FRCHAR + FRPASS in the environment  (escape hatch / CI override)
     2. the macOS Keychain entry written by --login
     3. otherwise tools fail with a message telling you to run --login

TOOLS
  mud_command(command)        run any MUD/creator command, return its output
  mud_exec(lpc)               run LPC code (e.g. "return file_size(\"/std/room.c\");")
  mud_ls(path)                list a server directory
  mud_read(path[, max_bytes]) read a server file as text
  mud_write(path, content)    write a server file (verified); never deletes
  mud_update(path[, inherit]) reload a .c in-game; reports compile errors
  mud_errors([lines])         tail your creator error log (runtime/compile)
  mud_download(remote, local) save a server file to local disk
  mud_upload(local, remote)   upload a local file to the server (verified)
  mud_delete(path)            delete a file/empty dir — REQUIRES a human Y/N
  mud_rename(src, dst)        rename/move a file — Y/N only if dst is overwritten
  mud_whoami()                show connection / who we're logged in as

DELETING: mud_delete (and an overwriting mud_rename) never act on their own —
they call MCP elicitation to ask the human user to confirm, and refuse if the
client can't show that prompt. The agent cannot delete server files unilaterally.
"""
import functools, json, os, re, subprocess, sys, threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frsync  # the engine: Mud, read_file_bytes, write_file_chunks, etc.

HOST = os.environ.get("FRHOST", frsync.DEF_HOST)
PORT = int(os.environ.get("FRPORT") or frsync.DEF_PORT)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# --------------------------------------------------------------- credentials
# Credentials live in the OS secret store, never in a config file:
#     macOS   -> login Keychain
#     Windows -> Credential Locker
#     Linux   -> Secret Service (libsecret / KWallet)
# The cross-platform `keyring` package handles all three. If keyring is not
# installed we fall back to the built-in `security` CLI on macOS (zero extra
# deps there); on Windows/Linux without keyring, --login explains the fix.
#
# The MCP server is a headless stdio subprocess and cannot prompt, so the
# password is entered once via the interactive `--login` CLI below.
SECRET_SERVICE = "frsync-mud"
SECRET_ACCOUNT = "frsync-mud"   # fixed label; the stored blob holds the char


def _keyring():
    """Return the keyring module if it imports with a usable backend, else None."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _Fail
        if isinstance(keyring.get_keyring(), _Fail):
            return None       # imported, but no real backend on this box
        return keyring
    except Exception:
        return None


def _security_cli_ok():
    return sys.platform == "darwin"


def _store_label():
    """Human-readable name of where credentials are/would be stored."""
    if _keyring() is not None:
        name = {"darwin": "macOS Keychain",
                "win32": "Windows Credential Locker"}.get(sys.platform,
                                                          "system Secret Service")
        return f"the {name} (via keyring)"
    if _security_cli_ok():
        return "the macOS Keychain"
    return "the system secret store"


def _decode(blob):
    try:
        d = json.loads(blob)
        return d.get("char"), d.get("pass")
    except (ValueError, TypeError):
        return None, None


def load_credentials():
    """Resolve (char, password). Order: env override -> secret store -> (None, None)."""
    env_char, env_pass = os.environ.get("FRCHAR"), os.environ.get("FRPASS")
    if env_char and env_pass:
        return env_char, env_pass
    kr = _keyring()
    if kr is not None:
        try:
            blob = kr.get_password(SECRET_SERVICE, SECRET_ACCOUNT)
            if blob:
                return _decode(blob)
        except Exception:
            pass
    if _security_cli_ok():
        try:
            r = subprocess.run(
                ["security", "find-generic-password",
                 "-s", SECRET_SERVICE, "-a", SECRET_ACCOUNT, "-w"],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return _decode(r.stdout.strip())
        except OSError:
            pass
    return None, None


def save_credentials(char, password):
    """Store credentials in the OS secret store via keyring; on macOS without
    keyring, use the `security` CLI (trusting this interpreter so the MCP
    subprocess reads them without a per-call access dialog)."""
    blob = json.dumps({"char": char, "pass": password})
    kr = _keyring()
    if kr is not None:
        kr.set_password(SECRET_SERVICE, SECRET_ACCOUNT, blob)
        return
    if _security_cli_ok():
        r = subprocess.run(
            ["security", "add-generic-password",
             "-s", SECRET_SERVICE, "-a", SECRET_ACCOUNT,
             "-l", "frsync-mud creator login",
             "-T", sys.executable,         # trust the python that runs the server
             "-w", blob, "-U"],            # -U: update if it already exists
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Keychain store failed: {r.stderr.strip()}")
        return
    raise RuntimeError(
        "No OS secret store available on this platform. Install the "
        "cross-platform keyring package:\n    pip install keyring")


def clear_credentials():
    """Remove the stored credentials from every store we might have used.
    Returns True if anything was deleted."""
    removed = False
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SECRET_SERVICE, SECRET_ACCOUNT)
            removed = True
        except Exception:
            pass   # not present in keyring; fall through to the CLI check
    if _security_cli_ok():
        r = subprocess.run(
            ["security", "delete-generic-password",
             "-s", SECRET_SERVICE, "-a", SECRET_ACCOUNT],
            capture_output=True, text=True)
        if r.returncode == 0:
            removed = True
    return removed


try:
    import anyio
    import anyio.to_thread          # noqa: F401  (ensure the submodule is loaded)
    from mcp.server.fastmcp import Context, FastMCP
    from pydantic import BaseModel
except ImportError:
    sys.stderr.write("frsync_mcp: the 'mcp' package is required — run: pip install mcp\n")
    raise SystemExit(1)

mcp = FastMCP("frsync-mud")


class _Confirm(BaseModel):
    """Elicitation schema for a destructive action. `confirm` defaults True so a
    plain accept means yes; the user cancels by declining the prompt (or by
    setting confirm=false in clients that render the field)."""
    confirm: bool = True

_mud = None
_lock = threading.Lock()   # serialise access to the single MUD connection

def _get():
    """Lazily connect (and keep) one creator session."""
    global _mud
    if _mud is None:
        char, pw = load_credentials()
        if not char or not pw:
            raise RuntimeError(
                "No frsync credentials configured. In a terminal, run:\n"
                f"    python3 {os.path.abspath(__file__)} --login\n"
                "to store your creator name + password in the OS secret store, "
                "then restart the frsync-mud MCP server.")
        m = frsync.Mud(HOST, PORT)
        # take_over=True: the MCP has no terminal to prompt at, and must not
        # print to stdout (it would corrupt the MCP protocol). If this creator
        # is logged in elsewhere, reclaim the session silently.
        m.login(char, pw, take_over=True)
        _mud = m
    return _mud

def _clean(text):
    return ANSI.sub("", text).replace("\r\n", "\n")

def _reset_session():
    """Drop the cached connection after a hard socket failure so the next tool
    call reconnects from scratch. The engine self-heals exec_* mid-call, but a
    fully dead socket (e.g. on the raw mud_command path) otherwise leaves _mud
    unusable until the whole MCP server is restarted."""
    global _mud
    with _lock:
        m, _mud = _mud, None
    if m is not None:
        try: m.close()
        except OSError: pass

def _heals(fn):
    """Wrap a tool so a hard connection failure clears the cached session.
    Engine errors (refused/unrecoverable writes) are RuntimeErrors that are NOT
    connection failures — only reconnect-exhaustion clears the session."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except OSError:
            _reset_session(); raise
        except RuntimeError as e:
            if "reconnect" in str(e).lower():
                _reset_session()
            raise
    return wrapper

# --------------------------------------------------------------------- tools
@mcp.tool()
@_heals
def mud_whoami() -> str:
    """Report the MUD host/port and the creator we're logged in as."""
    with _lock:
        m = _get()
        return f"Connected to {HOST}:{PORT} as {m.char}. exec check: 1+1={m.exec_int('return 1+1;')}"

@mcp.tool()
@_heals
def mud_command(command: str, wait: float = 2.0) -> str:
    """Send a raw MUD/creator command (e.g. 'ls /std', 'update /std/room.c',
    'look') and return its textual output."""
    with _lock:
        m = _get()
        m._flush(); m.send(command)
        return _clean(m.drain(wait)).strip() or "(no output)"

@mcp.tool()
@_heals
def mud_exec(lpc: str) -> str:
    """Run a line of LPC in the creator's context via the `exec` command and
    return the driver's output, e.g. lpc='return file_size("/std/room.c");'."""
    with _lock:
        return _clean(_get().exec_raw(lpc)).strip()

@mcp.tool()
@_heals
def mud_ls(path: str) -> str:
    """List a server directory. Returns one entry per line; directories end '/'."""
    with _lock:
        items = _get().listdir(path)
    if not items:
        return f"(empty or unreadable: {path})"
    return "\n".join(f"{n}/" if s == -2 else f"{n}\t{s}" for n, s in sorted(items))

@mcp.tool()
@_heals
def mud_read(path: str, max_bytes: int = 200_000) -> str:
    """Read a server file and return its text. Refuses files larger than
    max_bytes (raise it explicitly if you really need a huge file)."""
    with _lock:
        m = _get()
        sz = m.file_size(path)
        if sz < 0:
            return f"(not found: {path})"
        if sz == -2:
            return f"(that is a directory: {path})"
        if sz > max_bytes:
            return f"(file is {sz} bytes > max_bytes={max_bytes}; raise max_bytes to read it)"
        data = m.read_file_bytes(path, sz)
    if len(data) != sz:
        return f"(unreadable — no permission? got {len(data)}/{sz} bytes)"
    # Most MUD source is UTF-8; fall back to latin-1 for legacy/8-bit files.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")

@mcp.tool()
@_heals
def mud_write(path: str, content: str) -> str:
    """Create or overwrite a server file with `content` (verified by size +
    checksum). Never deletes anything."""
    with _lock:
        m = _get()
        # Encode as UTF-8 so non-Latin-1 text (Norwegian, em dashes, smart
        # quotes) is preserved exactly rather than silently replaced with '?'.
        # The transport layer treats the bytes opaquely, so they round-trip.
        data = content.encode("utf-8")
        frsync.ensure_remote_dirs(m, path, set())   # create parent dirs (as mud_upload does)
        m.write_file_chunks(path, data)
        ok = frsync.verify_remote(m, path, data)
    return f"{'OK' if ok else 'FAILED VERIFY'}: wrote {len(data)} bytes to {path}"

@mcp.tool()
@_heals
def mud_update(path: str, inherit: bool = False) -> str:
    """Recompile (reload) a server .c file in-game so the live MUD uses the
    latest version, and report any compile errors as 'file line N: message'.
    Run this after mud_write/mud_upload to make a code change take effect and to
    confirm it compiled. inherit=True adds the -o flag (update the whole inherit
    chain — needed when you changed a base/.h that other objects inherit)."""
    with _lock:
        res = _get().update([path], inherit=inherit)
    if res["errors"]:
        return "COMPILE ERROR:\n" + "\n".join(
            f"  {p} line {ln}: {m}" for p, ln, m in res["errors"])
    if res["failed"]:
        return "FAILED TO LOAD: " + ", ".join(res["failed"])
    if res["loaded"]:
        return "OK reloaded: " + ", ".join(res["loaded"])
    return "(update sent, no confirmation seen — check mud_errors)"

@mcp.tool()
@_heals
def mud_errors(lines: int = 20, include_tool_noise: bool = False) -> str:
    """Tail your creator error log (/w/<you>/error-log) — the runtime and compile
    errors the driver dumped, newest last. Use this to debug a room/object that
    loads but misbehaves. By default hides frsync's own exec_tmp.c compile noise;
    set include_tool_noise=True to see everything."""
    with _lock:
        m = _get()
        logpath = f"/w/{(m.char or '').lower()}/error-log"
        raw = m.read_tail(logpath, 20000).decode("latin-1", "replace")
    if not raw:
        return f"(no error log at {logpath})"
    ls = [ln for ln in raw.splitlines() if ln.strip()]
    if not include_tool_noise:
        ls = [ln for ln in ls if "exec_tmp.c" not in ln]
    ls = ls[-lines:]
    return "\n".join(ls) if ls else "(no recent errors — only tool exec noise)"

@mcp.tool()
@_heals
def mud_download(remote: str, local: str) -> str:
    """Download a server file to a local path (verified)."""
    with _lock:
        m = _get()
        sz = m.file_size(remote)
        if sz < 0:
            return f"(not found: {remote})"
        data = m.read_file_bytes(remote, sz)
    if len(data) != sz:
        return f"(unreadable: got {len(data)}/{sz} bytes)"
    os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
    with open(local, "wb") as fh:
        fh.write(data)
    return f"OK: {remote} -> {local} ({len(data)} bytes)"

@mcp.tool()
@_heals
def mud_upload(local: str, remote: str) -> str:
    """Upload a local file to the server (verified). Never deletes anything."""
    if not os.path.isfile(local):
        return f"(no such local file: {local})"
    data = open(local, "rb").read()
    with _lock:
        m = _get()
        frsync.ensure_remote_dirs(m, remote, set())
        m.write_file_chunks(remote, data)
        ok = frsync.verify_remote(m, remote, data)
    return f"{'OK' if ok else 'FAILED VERIFY'}: {local} -> {remote} ({len(data)} bytes)"

# --- destructive tools: gated on a real human Y/N via MCP elicitation ---------
async def _ask_user(ctx, message):
    """Ask the human to confirm a destructive action. Returns True only on an
    explicit accept. If the client can't show a prompt (no elicitation support),
    returns None so the caller refuses rather than deleting blind."""
    try:
        res = await ctx.elicit(message=message, schema=_Confirm)
    except Exception:
        return None                       # client can't prompt -> fail safe
    return res.action == "accept" and getattr(res.data, "confirm", True)

@mcp.tool()
async def mud_delete(path: str, ctx: Context) -> str:
    """Delete a server file (or an EMPTY directory). This asks the human user to
    confirm first via a prompt — the agent cannot delete on its own, and if the
    client can't show the prompt the deletion is refused. There is no undo."""
    def _size():
        with _lock: return _get().file_size(path)
    try:
        sz = await anyio.to_thread.run_sync(_size)
    except OSError:
        _reset_session(); raise
    if sz < 0:
        return f"(not found: {path})"
    kind = "empty directory" if sz == -2 else f"file ({sz} bytes)"
    ok = await _ask_user(ctx, f"Delete this {kind} on the MUD? There is no undo:\n  {path}")
    if ok is None:
        return ("Refused: this client can't show a confirmation prompt, so the "
                "delete was not performed. Delete it from the FRsync interactive "
                "shell with /rm instead.")
    if not ok:
        return f"Cancelled by user — {path} was NOT deleted."
    def _do():
        with _lock:
            m = _get()
            return m.rmdir(path) if sz == -2 else m.rm(path)
    try:
        done = await anyio.to_thread.run_sync(_do)
    except OSError:
        _reset_session(); raise
    if done:
        return f"OK: deleted {path}"
    return f"FAILED to delete {path}" + (" (directory not empty?)" if sz == -2 else "")

@mcp.tool()
async def mud_rename(src: str, dst: str, ctx: Context) -> str:
    """Rename or move a server file from `src` to `dst`. Non-destructive when
    `dst` is new; if `dst` already exists it would be overwritten, so that case
    asks the human user to confirm first (and refuses if it can't prompt)."""
    def _sizes():
        with _lock:
            m = _get(); return m.file_size(src), m.file_size(dst)
    try:
        ssz, dsz = await anyio.to_thread.run_sync(_sizes)
    except OSError:
        _reset_session(); raise
    if ssz < 0:
        return f"(not found: {src})"
    if dsz >= 0:                                   # would clobber an existing dst
        ok = await _ask_user(ctx, f"{dst} already exists — overwrite it by moving "
                                  f"{src} onto it? There is no undo.")
        if ok is None:
            return ("Refused: this client can't show a confirmation prompt and "
                    f"{dst} exists. Do the move from the FRsync shell with /mv.")
        if not ok:
            return f"Cancelled by user — {src} was NOT moved."
    def _do():
        with _lock:
            m = _get()
            if dsz >= 0: m.rm(dst)                  # rename won't clobber; clear first
            return m.rename(src, dst)
    try:
        done = await anyio.to_thread.run_sync(_do)
    except OSError:
        _reset_session(); raise
    return f"OK: moved {src} -> {dst}" if done else f"FAILED to move {src} -> {dst}"

def _cli(argv):
    """Interactive credential management — run from a real terminal, not the MCP
    client. Returns True if a CLI subcommand handled the invocation."""
    cmd = argv[0] if argv else None
    if cmd in ("--login", "login"):
        import getpass
        cur_char, _ = load_credentials()
        prompt = f"FR creator name [{cur_char}]: " if cur_char else "FR creator name: "
        char = input(prompt).strip() or (cur_char or "")
        if not char:
            sys.exit("Aborted: no creator name given.")
        pw = getpass.getpass(f"FR password for {char} (hidden): ")
        if not pw:
            sys.exit("Aborted: empty password.")
        if getpass.getpass("Confirm password: ") != pw:
            sys.exit("Aborted: passwords did not match.")
        save_credentials(char, pw)
        print(f"Stored credentials for '{char}' in {_store_label()} "
              f"(service '{SECRET_SERVICE}'). No plaintext written to disk.")
        print("Now restart the frsync-mud MCP server for it to take effect.")
        return True
    if cmd in ("--reset", "--logout", "reset", "logout"):
        print("Credentials removed from the Keychain."
              if clear_credentials() else "No stored credentials to remove.")
        return True
    if cmd in ("--status", "status"):
        char, _ = load_credentials()
        if char:
            src = "environment override" if os.environ.get("FRCHAR") else _store_label()
            print(f"Configured: creator '{char}' (from {src}). Host {HOST}:{PORT}.")
        else:
            print("Not configured. Run: "
                  f"python3 {os.path.abspath(__file__)} --login")
        return True
    if cmd in ("-h", "--help", "help"):
        print("Usage: frsync_mcp.py [--login | --reset | --status]\n"
              "  (no args)  run the MCP stdio server\n"
              "  --login    enter + store creator credentials in the Keychain\n"
              "  --reset    forget stored credentials\n"
              "  --status   show whether credentials are configured")
        return True
    return False


if __name__ == "__main__":
    if not _cli(sys.argv[1:]):
        mcp.run()   # stdio transport
