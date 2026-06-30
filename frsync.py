#!/usr/bin/env python3
"""
frsync — sync files between your local disk and the Final Realms MUD,
over the normal creator login (no FTP required).

It works because FR's creator shell exposes `exec` (run LPC) and LPC's
`write_file` / `read_bytes` efuns. frsync logs in as you, then pushes or
pulls files straight over that connection. The MUD's own permission system
(`valid_write`) decides what you're allowed to touch — so this works for any
builder, for their `/w/<name>/` home and any `/d/<zone>/` they're granted.

USAGE
  ./frsync.py status <local> <remote>     # compare, change nothing
  ./frsync.py push   <local> <remote>     # local  -> MUD  (upload)
  ./frsync.py pull   <remote> <local>     # MUD    -> local (download)

  <local> / <remote> may be a single file or a directory (synced recursively).

  Examples
    ./frsync.py push   ./drifting_forest         /w/<creator>/drifting_forest
    ./frsync.py pull   /w/<creator>/drifting_forest ./backup
    ./frsync.py status ./drifting_forest         /w/<creator>/drifting_forest
    ./frsync.py push   ./rooms/hut.c             /w/<creator>/drifting_forest/rooms/hut.c

OPTIONS
  --char NAME        creator name (else prompts)
  --host HOST        default fr.hyssing.net
  --port PORT        default 4010
  --ext .c,.h        when syncing a dir, only these extensions (default: all)
  --delete           remove dest files that don't exist at the source
  --dry-run          show what push/pull WOULD do, change nothing
  -y / --yes         don't ask for confirmation before writing

Password: read from $FRPASS if set, otherwise prompted (never stored).
Pure Python 3 standard library — one file, share it freely.
"""
import argparse, getpass, os, re, select, socket, sys, time, zlib

DEF_HOST, DEF_PORT = "fr.hyssing.net", 4010
PUSH_CMD_BUDGET = 900     # max chars in one `exec write_file(...)` line
PULL_CHUNK      = 4096    # source bytes per read_bytes() call

# ---------------------------------------------------------------- telnet layer
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

class Mud:
    SOCK_TIMEOUT = 90   # no single send/recv may block longer than this

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.char = self.pw = None
        self.s = socket.create_connection((host, port), timeout=25)
        self.s.settimeout(self.SOCK_TIMEOUT)

    def reconnect(self):
        """Re-establish a dropped session (e.g. after the server resets us)."""
        try: self.s.close()
        except OSError: pass
        for attempt in range(5):
            try:
                self.s = socket.create_connection((self.host, self.port), timeout=25)
                self.s.settimeout(self.SOCK_TIMEOUT)
                # reclaiming our own dropped link: take over silently if FR
                # still considers the old (now-dead) session "playing".
                self.login(self.char, self.pw, _store=False, take_over=True)
                return
            except OSError:
                time.sleep(2 + attempt * 2)
        raise RuntimeError("could not reconnect to the MUD")

    def _strip_iac(self, data):
        out, reply, i = bytearray(), bytearray(), 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == DO:   reply += bytes([IAC, WONT, opt])
                    elif cmd == WILL: reply += bytes([IAC, DONT, opt])
                    i += 3; continue
                if cmd == SB:
                    j = i + 2
                    while j + 1 < len(data) and not (data[j] == IAC and data[j + 1] == SE):
                        j += 1
                    i = j + 2; continue
                i += 2; continue
            out.append(b); i += 1
        if reply:
            try: self.s.sendall(bytes(reply))
            except OSError: pass
        return out.decode("latin-1", "replace")

    def drain(self, quiet=1.2):
        buf, end = [], time.time() + quiet
        while time.time() < end:
            r, _, _ = select.select([self.s], [], [], 0.3)
            if r:
                data = self.s.recv(65536)
                if not data: break
                buf.append(self._strip_iac(data))
                end = time.time() + quiet
        return "".join(buf)

    def expect(self, pattern, timeout=18):
        buf, end = [], time.time() + timeout
        rx = re.compile(pattern, re.I)
        while time.time() < end:
            r, _, _ = select.select([self.s], [], [], 0.3)
            if r:
                data = self.s.recv(65536)
                if not data: break
                buf.append(self._strip_iac(data))
                if rx.search("".join(buf)): break
        return "".join(buf)

    def send(self, line):
        self.s.sendall((line + "\n").encode("latin-1"))

    def close(self):
        try: self.s.close()
        except OSError: pass

    # ------------------------------------------------------------ login
    def login(self, char, pw, _store=True, take_over: "bool | str" = "ask"):
        if _store:
            self.char, self.pw = char, pw
        self.expect(r"Your choice|<name>:", 20)
        self.send(char)
        self.expect(r"passage|password|:\s*$", 12)
        self.send(pw)
        out = self.expect(r">|command|wimpy|word of passage|"
                          r"already playing|throw the other copy", 12)
        # FR's built-in duplicate-login handling. When the character is already
        # connected, FR asks: "You are already playing. Throw the other copy
        # out (y/n) ?" — surface it and use that feature to replace the other
        # (or stale) session instead of hanging on a prompt we never answer.
        if re.search(r"already playing|throw the other copy", out, re.I):
            self._take_over(char, take_over)
            out = self.expect(r">|command|wimpy", 12)
        self.drain(1.0)
        # crude auth check: a creator prompt / room line should appear
        if re.search(r"(invalid|incorrect|no player|not a valid)", out, re.I):
            raise SystemExit("Login failed — check character name / password.")
        # confirm exec works (i.e. we really are a creator)
        if self.exec_int("return 1+1;") != 2:
            raise SystemExit("Logged in, but `exec` is unavailable — are you a creator?")

    def _take_over(self, char, take_over):
        """Answer FR's 'You are already playing. Throw the other copy out
        (y/n)?' prompt. take_over: True = always take over, False = never,
        'ask' = prompt the user when interactive (default Yes), and take over
        automatically when there's no terminal to ask (e.g. the MCP server)."""
        import sys
        decide = take_over
        if take_over == "ask":
            if sys.stdin.isatty():
                ans = input(
                    f"\n  ⚠  {char} is already logged in elsewhere.\n"
                    f"     Throw out that session and take over here? [Y/n] "
                ).strip().lower()
                decide = ans in ("", "y", "yes")
            else:
                print(f"  ⚠  {char} is already logged in elsewhere — taking over.")
                decide = True
        if decide:
            self.send("y")
        else:
            self.send("n")
            raise SystemExit(
                f"{char} is already logged in elsewhere; left that session "
                "alone — not connecting.")

    # ------------------------------------------------------------ exec helpers
    def _flush(self):
        """Discard any pending socket data (stale prompt/spam) before a command."""
        while True:
            r, _, _ = select.select([self.s], [], [], 0)
            if not r: break
            try:
                if not self.s.recv(65536): break
            except OSError: break

    def exec_raw(self, code, quiet=1.5):
        self._flush(); self.send("exec " + code)
        return self.drain(quiet)

    # Marker-based reads: return the instant the result lands (immune to MUD
    # spam resetting any quiet-timer). Markers are split in the source as
    # "M1"+"M2" so the command echo never contains them contiguously.
    INT_RX = re.compile(r"Returns\s+(-?\d+)")
    ERR_RX = re.compile(r"exec_tmp\.c|Undefined|syntax error|no write permission|Bad argument", re.I)

    def exec_int(self, code, timeout=12):
        # Retry on BOTH socket errors AND a silent/empty response (the flaky
        # Legacy server goes quiet without closing — that needs a reconnect,
        # not just a longer wait). A real LPC error (ERR_RX) is returned as-is.
        for attempt in range(3):
            try:
                self._flush(); self.send("exec " + code)
                out = self.expect(r"Returns\s+-?\d+|exec_tmp\.c|Undefined|syntax error|"
                                  r"no write permission|Bad argument", timeout)
            except OSError:
                out = ""
            m = self.INT_RX.search(out)
            if m: return int(m.group(1))
            if self.ERR_RX.search(out): return None          # genuine LPC error
            if attempt < 2 and self.char: self.reconnect()   # silent session -> heal
            else: return None
        return None

    def exec_hex(self, code, timeout=20):
        """code must `return "M1"+"M2"+<hexstring>+"M2"+"M1";` — returns bytes.
        An unreadable file legitimately returns the markers with empty hex (we
        return b''); a *missing marker* means a silent/dead session -> reconnect."""
        for attempt in range(3):
            try:
                self._flush(); self.send("exec " + code)
                out = self.expect(r"M2M1|exec_tmp\.c|Undefined|syntax error", timeout)
            except OSError:
                out = ""
            clean = ANSI.sub("", out)
            m = re.search(r"M1M2(.*?)M2M1", clean, re.S)
            if m:
                return bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", m.group(1)))
            if re.search(r"exec_tmp\.c|Undefined|syntax error", clean):
                raise RuntimeError("exec error:\n" + out[-300:])   # real LPC error
            if attempt < 2 and self.char: self.reconnect()         # silent session -> heal
            else: raise RuntimeError("download: marker not found (session dead?):\n" + out[-300:])
        raise RuntimeError("download: exhausted retries")

    # ------------------------------------------------------------ fs ops
    def file_size(self, path):
        n = self.exec_int(f'return file_size("{path}");')
        return n if n is not None else -1     # -1 missing, -2 dir

    def byte_sum(self, path):
        return self.exec_int(
            f'string c=read_file("{path}"); int i,t; '
            f'for(i=0;i<strlen(c);i++) t+=c[i]; return t;')

    def crc32_remote(self, path):
        """CRC-32 of the server file's contents via the MudOS crc32() efun,
        as unsigned 32-bit. Unlike byte_sum this is order-sensitive — it detects
        reordered/transposed content of the same length. Returns None if the
        file can't be read.

        NOTE: this driver's crc32() is CRC-32/JAMCRC (same polynomial as zlib
        but with NO final XOR), so the matching local value is jamcrc(), not
        zlib.crc32(). Verified live: crc32("abc") == 0xCADBBE3D."""
        n = self.exec_int(f'return crc32(read_file("{path}"));')
        return None if n is None else n & 0xffffffff

    def mkdir(self, path):
        self.exec_int(f'return mkdir("{path}");')

    def listdir(self, path):
        """Return list of (name, size) for one dir. size -2 == subdirectory."""
        code = ('string r=""; string h=""; mixed *a=get_dir("%s/", -1); int i; '
                'for(i=0;i<sizeof(a);i++) r+=a[i][0]+"|"+a[i][1]+"\\n"; '
                'for(i=0;i<strlen(r);i++) h+=sprintf("%%02x",r[i]); '
                'return "M1"+"M2"+h+"M2"+"M1";' % path.rstrip("/"))
        raw = self.exec_hex(code).decode("latin-1")
        items = []
        for ln in raw.splitlines():
            if "|" in ln:
                name, _, size = ln.rpartition("|")
                items.append((name, int(size)))
        return items

    def walk(self, root):
        """Yield relative file paths under server dir `root`."""
        return [rel for rel, _sz in self.walk_sized(root)]

    def walk_sized(self, root, on_dir=None):
        """Recursively list `root`. Returns [(relpath, size)]. Sizes come from
        get_dir, so no extra per-file calls are needed. Unreadable dirs are
        skipped (reported via on_dir as (path, None))."""
        out = []
        def rec(rel):
            base = root if not rel else f"{root}/{rel}"
            try:
                items = self.listdir(base)
            except Exception:
                if on_dir: on_dir(base, None)
                return
            if on_dir: on_dir(base, len(items))
            for name, size in items:
                r = name if not rel else f"{rel}/{name}"
                if size == -2:
                    rec(r)
                else:
                    out.append((r, size))
        rec("")
        return out

    def _write_once(self, path, chunk, flag, timeout=12):
        """Send ONE write_file() call and DO NOT blindly resend it — a resent
        append (flag 0) would duplicate the chunk in the file. Returns the
        driver's int result (1 = ok, 0 = refused), or None when no result was
        seen (timeout / silent session / socket error) so the caller reconciles
        against the file's actual size.

        We wait for a SPLIT sentinel result marker — emitted as "R"+"C" so it
        can never appear contiguously in the echo — not a generic `Returns N`.
        The server echoes the command back (and thus the file content), so a
        chunk holding the literal text 'Returns 1' would otherwise spoof the
        result and falsely advance the upload. With the sentinel the only
        RC<n>RC in the stream is the driver's real write_file() result; a
        genuine failure yields no marker -> None -> the size-reconcile loop
        resends or restarts."""
        try:
            self._flush()
            self.send(f'exec int _r=write_file("{path}", "{chunk}", {flag}); '
                      f'return "R"+"C"+sprintf("%d",_r)+"R"+"C";')
            out = ANSI.sub("", self.expect(r"RC-?\d+RC", timeout))
        except OSError:
            return None
        m = re.search(r"RC(-?\d+)RC", out)
        if m: return int(m.group(1))
        return None                                  # no result -> caller reconciles

    def write_file_chunks(self, path, data: bytes):
        """Upload `data` as a sequence of write_file() calls: chunk 0 overwrites
        (flag 1, truncating any old content), the rest append (flag 0).

        Appends are NOT idempotent, so we never blindly resend one — that is how
        a lost/empty response duplicates a chunk and breaks verify. Instead, an
        uncertain result is reconciled against the server's real file size:
          size == expected + len(chunk)  -> it landed, the reply was just lost
          size == expected               -> nothing written, safe to resend
          anything else                  -> unknown state, rebuild from chunk 0
        (chunk 0's flag-1 truncate makes a rebuild clean)."""
        text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("latin-1")
        chunks = list(chunk_escaped(text, PUSH_CMD_BUDGET - len(path) - 40)) or [("", 0)]
        expected = 0          # bytes the file should hold after each landed chunk
        i, restarts = 0, 0
        while i < len(chunks):
            ch, nbytes = chunks[i]
            flag = 1 if i == 0 else 0
            res = self._write_once(path, ch, flag)
            if res == 1:
                expected += nbytes; i += 1; continue
            if res == 0:
                raise RuntimeError(f"write_file refused chunk {i} of {path} (returned 0)")
            # res is None -> uncertain. file_size() heals a silent session itself.
            fsz = self.file_size(path)
            size = fsz if fsz >= 0 else 0
            if size == expected + nbytes:            # landed; only the reply was lost
                expected += nbytes; i += 1
            elif size == expected:                   # nothing written; resend same chunk
                continue
            else:                                    # unknown state -> rebuild from scratch
                if restarts >= 2:
                    raise RuntimeError(
                        f"write_file: unrecoverable state for {path} "
                        f"(size={size}, expected~{expected} at chunk {i})")
                restarts += 1; expected = 0; i = 0

    def read_file_bytes(self, path, size):
        buf = bytearray()
        off = 0
        while off < size:
            n = min(PULL_CHUNK, size - off)
            code = (f'string c=read_bytes("{path}", {off}, {n}); string r=""; int i; '
                    f'for(i=0;i<strlen(c);i++) r+=sprintf("%02x",c[i]); '
                    f'return "M1"+"M2"+r+"M2"+"M1";')
            buf += self.exec_hex(code)
            off += n
        return bytes(buf)

# ---------------------------------------------------------------- escaping
def esc_token(ch):
    o = ord(ch)
    if ch == "\\": return "\\\\"
    if ch == '"':  return '\\"'
    if ch == "\n": return "\\n"
    if ch == "\t": return "\\t"
    if ch == "\r": return ""
    if 32 <= o <= 126: return ch
    if o == 255: raise ValueError("byte 0xFF (telnet IAC) in file — cannot transfer")
    if o >= 128: return ch          # high bytes (UTF-8 etc.) pass through literally
    raise ValueError(f"control byte 0x{o:02x} in file — cannot transfer")

def chunk_escaped(text, budget):
    """Yield (escaped_cmd, nbytes) chunks fitting `budget` escaped chars.
    `escaped_cmd` is spliced into a write_file() call; `nbytes` is how many
    bytes the chunk adds to the file once LPC un-escapes it. Each source char
    becomes exactly one file byte (\\r is dropped before chunking, so it never
    contributes), hence nbytes == number of source chars packed into the chunk."""
    cur, cur_len, cur_bytes = [], 0, 0
    budget = max(budget, 40)
    for ch in text:
        tok = esc_token(ch)
        if not tok:
            continue
        if cur_len + len(tok) > budget and cur:
            yield "".join(cur), cur_bytes
            cur, cur_len, cur_bytes = [], 0, 0
        cur.append(tok)
        cur_len += len(tok)
        cur_bytes += 1
    if cur:
        yield "".join(cur), cur_bytes

# ---------------------------------------------------------------- local fs
def local_norm(data: bytes):
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

def local_size_sum(path):
    data = local_norm(open(path, "rb").read())
    return len(data), sum(data) & 0x7fffffffffffffff

def jamcrc(data: bytes) -> int:
    """CRC-32 in this driver's flavour: same polynomial as zlib's CRC-32 but
    with NO final XOR (CRC-32/JAMCRC) — the bitwise complement of zlib.crc32.
    Matches the MudOS crc32() efun (verified live: crc32("abc")==0xCADBBE3D)."""
    return (zlib.crc32(data) ^ 0xffffffff) & 0xffffffff

def local_size_crc(path):
    """(length, JAMCRC-32) of the normalised local file — pairs with
    Mud.crc32_remote for order-sensitive change detection."""
    data = local_norm(open(path, "rb").read())
    return len(data), jamcrc(data)

def local_walk(root, exts):
    out = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn == ".DS_Store": continue
            if exts and not fn.endswith(tuple(exts)): continue
            out.append(os.path.relpath(os.path.join(dp, fn), root))
    return sorted(out)

# ---------------------------------------------------------------- planning
def plan(mud, local_root, remote_root, exts, is_file):
    """Return dict rel -> state in {'new','diff','same','remote_only'}."""
    states = {}
    if is_file:
        lsize, lcrc = local_size_crc(local_root)
        rsize = mud.file_size(remote_root)
        if rsize < 0:
            states[""] = "new"
        elif rsize != lsize or mud.crc32_remote(remote_root) != lcrc:
            states[""] = "diff"
        else:
            states[""] = "same"
        return states

    lfiles = set(local_walk(local_root, exts))
    rfiles = set(f for f in mud.walk(remote_root)
                 if not exts or f.endswith(tuple(exts)))
    for rel in sorted(lfiles | rfiles):
        if rel not in rfiles:
            states[rel] = "new"
        elif rel not in lfiles:
            states[rel] = "remote_only"
        else:
            lsize, lcrc = local_size_crc(os.path.join(local_root, rel))
            rp = f"{remote_root}/{rel}"
            if mud.file_size(rp) != lsize or mud.crc32_remote(rp) != lcrc:
                states[rel] = "diff"
            else:
                states[rel] = "same"
    return states

# ---------------------------------------------------------------- commands
def connect(args):
    char = args.char or input("Creator name: ").strip()
    pw = os.environ.get("FRPASS") or getpass.getpass(f"Password for {char}: ")
    print(f"Connecting to {args.host} {args.port} as {char}…")
    mud = Mud(args.host, args.port)
    mud.login(char, pw)
    print("Logged in.\n")
    return mud

def cmd_status(args):
    mud = connect(args)
    is_file = os.path.isfile(args.local)
    st = plan(mud, args.local, args.remote, parse_exts(args.ext), is_file)
    show_plan(st, "status")
    mud.close()

def cmd_push(args):
    mud = connect(args)
    is_file = os.path.isfile(args.local)
    st = plan(mud, args.local, args.remote, parse_exts(args.ext), is_file)
    todo = [r for r, s in st.items() if s in ("new", "diff")]
    extra = [r for r, s in st.items() if s == "remote_only"]
    show_plan(st, "push")
    if not todo and not (args.delete and extra):
        print("Nothing to push — already in sync."); mud.close(); return
    if args.dry_run: mud.close(); return
    if not confirm(args, f"Push {len(todo)} file(s) to {args.remote}?"):
        mud.close(); return
    made = set()
    for rel in sorted(todo):
        lp = args.local if is_file else os.path.join(args.local, rel)
        rp = args.remote if is_file else f"{args.remote}/{rel}"
        ensure_remote_dirs(mud, rp, made)
        data = open(lp, "rb").read()
        mud.write_file_chunks(rp, data)
        ok = verify_remote(mud, rp, data)
        print(f"  {'OK ' if ok else 'FAIL'}  push  {rel or os.path.basename(rp)}")
    if args.delete and extra:
        for rel in sorted(extra):
            rp = f"{args.remote}/{rel}"
            mud.exec_raw(f'return rm("{rp}");', 1.0)
            print(f"  DEL   {rel}")
    mud.close()

def cmd_pull(args):
    mud = connect(args)
    # remote is source, local is dest
    rsize = mud.file_size(args.remote)
    is_file = rsize >= 0 and rsize != -2
    exts = parse_exts(args.ext)
    if is_file:
        rels = [""]
    else:
        rels = [f for f in mud.walk(args.remote) if not exts or f.endswith(tuple(exts))]
    todo = []
    for rel in rels:
        rp = args.remote if rel == "" else f"{args.remote}/{rel}"
        lp = args.local if rel == "" else os.path.join(args.local, rel)
        rs = mud.file_size(rp)
        if os.path.isfile(lp):
            lsize, lsum = local_size_sum(lp)
            if lsize == rs and lsum == mud.byte_sum(rp):
                print(f"  same  {rel or os.path.basename(rp)}"); continue
        todo.append((rel, rp, lp, rs))
    print(f"\n{len(todo)} file(s) to download.")
    if not todo: mud.close(); return
    if args.dry_run:
        for rel, *_ in todo: print(f"  would pull  {rel or args.remote}")
        mud.close(); return
    if not confirm(args, f"Download {len(todo)} file(s) to {args.local}?"):
        mud.close(); return
    for rel, rp, lp, rs in todo:
        data = mud.read_file_bytes(rp, rs)
        os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
        open(lp, "wb").write(data)
        ok = (len(data) == rs)
        print(f"  {'OK ' if ok else 'FAIL'}  pull  {rel or os.path.basename(rp)} ({len(data)} b)")
    mud.close()

def resolve_remote(arg, rcwd):
    """Resolve a (possibly relative) server path against the tracked cwd."""
    if arg.startswith("/"):
        path = arg
    else:
        path = f"{rcwd.rstrip('/')}/{arg}"
    # normalise . and ..
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."): continue
        if seg == "..":
            if parts: parts.pop()
        else:
            parts.append(seg)
    return "/" + "/".join(parts)

def cmd_connect(args):
    """Interactive MUD shell: you're connected like telnet (all normal MUD and
    creator commands go straight to the server), plus local /commands to move
    files. Line-based (type a command, press Enter)."""
    import sys
    def out(s): sys.stdout.write(s); sys.stdout.flush()
    out(welcome_banner())               # greeting + how-to, before the login prompt
    mud = connect(args)
    home = f"/w/{(args.char or mud.char or '').lower()}"
    # Default downloads land in ./downloads (created on demand) so they don't
    # clutter the folder you launched from; override with --dir or /lcd.
    local = os.path.abspath(args.dir or os.path.join(os.getcwd(), "downloads"))
    os.makedirs(local, exist_ok=True)
    state = {"local": local, "rcwd": home or "/"}
    made = set()

    HELP = (
        "\r\n  Local commands (everything else goes to the MUD):\r\n"
        "    /lcd <dir>         set your local folder (downloads land here)\r\n"
        "    /lpwd  /lls        show / list your local folder\r\n"
        "    /rcd <dir>         set the remote folder used for transfers\r\n"
        "    /download f [..]   (/get) pull file(s) from remote folder -> local\r\n"
        "    /upload f [..]     (/put) push file(s) from local -> remote folder\r\n"
        "    /where             show current local + remote folders\r\n"
        "    /help   /quit\r\n")

    def do_local(line):
        parts = line.split()
        cmd, a = parts[0], parts[1:]
        if cmd in ("/quit", "/exit"): return False
        elif cmd == "/help": out(HELP)
        elif cmd == "/where":
            out(f"\r\n  local : {state['local']}\r\n  remote: {state['rcwd']}\r\n")
        elif cmd == "/lcd":
            d = os.path.abspath(os.path.expanduser(a[0])) if a else os.getcwd()
            if os.path.isdir(d): state["local"] = d; out(f"\r\n  local dir = {d}\r\n")
            else: out(f"\r\n  no such local dir: {d}\r\n")
        elif cmd == "/lpwd": out(f"\r\n  {state['local']}\r\n")
        elif cmd == "/lls":
            try: out("\r\n  " + "  ".join(sorted(os.listdir(state["local"]))) + "\r\n")
            except OSError as e: out(f"\r\n  {e}\r\n")
        elif cmd == "/rcd":
            state["rcwd"] = resolve_remote(a[0], state["rcwd"]) if a else home
            out(f"\r\n  remote dir = {state['rcwd']}\r\n")
        elif cmd in ("/download", "/get"):
            for f in a:
                rp = resolve_remote(f, state["rcwd"])
                sz = mud.file_size(rp)
                if sz < 0: out(f"\r\n  ✗ {f}: not found on MUD\r\n"); continue
                data = mud.read_file_bytes(rp, sz)
                lp = os.path.join(state["local"], os.path.basename(rp))
                open(lp, "wb").write(data)
                ok = len(data) == sz
                out(f"\r\n  {'↓' if ok else '✗'} {os.path.basename(rp)} ({len(data)} b) -> {lp}\r\n")
        elif cmd in ("/upload", "/put"):
            for f in a:
                lp = f if os.path.isabs(f) else os.path.join(state["local"], f)
                if not os.path.isfile(lp): out(f"\r\n  ✗ {f}: no such local file\r\n"); continue
                rp = resolve_remote(os.path.basename(f), state["rcwd"])
                try:
                    ok = push_one(mud, lp, rp, made)
                    out(f"\r\n  {'↑' if ok else '✗'} {os.path.basename(f)} -> {rp}\r\n")
                except Exception as e: out(f"\r\n  ✗ {f}: {str(e)[:70]}\r\n")
        else:
            out(f"\r\n  unknown /command: {cmd}  (try /help)\r\n")
        return True

    out(do_where_banner(state))
    sock = mud.s
    try:
        while True:
            r, _, _ = select.select([sys.stdin, sock], [], [], 0.3)
            if sock in r:
                data = sock.recv(65536)
                if not data: out("\r\n*** MUD closed the connection. ***\r\n"); break
                out(mud._strip_iac(data))
            if sys.stdin in r:
                line = sys.stdin.readline()
                if not line: break                      # Ctrl-D
                line = line.rstrip("\n")
                # keep remote cwd in sync when you cd around the file tree
                if line.strip().startswith("cd ") or line.strip() == "cd":
                    arg = line.strip()[2:].strip()
                    state["rcwd"] = (home if not arg else resolve_remote(arg, state["rcwd"]))
                if line.startswith("/"):
                    if not do_local(line): break
                else:
                    mud.send(line)
    except KeyboardInterrupt:
        pass
    finally:
        out("\r\nDisconnected.\r\n")
        mud.close()

def do_where_banner(state):
    return f"  local : {state['local']}\r\n  remote: {state['rcwd']}\r\n"

def welcome_banner():
    """Greeting + a short how-to, shown when the interactive shell starts —
    printed before the login prompt."""
    rule = "  " + "═" * 64
    return (
        "\r\n"
        f"{rule}\r\n"
        "   Welcome to FRsync\r\n"
        "   A file transfer and immort editor system for Final Realms: Legacy\r\n"
        f"{rule}\r\n"
        "\r\n"
        "  This works just like telnet: once you log in, type any MUD or creator\r\n"
        "  command normally (look, ed myroom.c, update myroom.c, ls /w/you), plus\r\n"
        "  these local commands to move files:\r\n"
        "\r\n"
        "        /upload <file>      send a local file  -> remote folder\r\n"
        "        /download <file>    fetch a remote file -> this computer\r\n"
        "        /where              show your local + remote folders\r\n"
        "        /lcd <dir>          change the local folder (downloads land here)\r\n"
        "        /rcd <dir>          change the remote folder (transfers use this)\r\n"
        "\r\n"
        "        /help  full command list        /quit  leave\r\n"
        "\r\n"
        "  Log in with your creator name + password to begin.\r\n"
        "\r\n"
    )

TEMP_SUFFIXES = ("~", ".swp", ".swo", ".tmp", ".orig")
# archives / images / binaries — not source, skipped by `mirror` by default
BINARY_EXTS = (".tar.gz", ".tgz", ".gz", ".tar", ".zip", ".z", ".bz2", ".xz",
               ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf",
               ".o", ".so", ".a", ".bin", ".dump", ".db", ".wav", ".mp3")

def push_one(mud, lp, rp, made):
    """Push a single local file to the server; return True if verified."""
    ensure_remote_dirs(mud, rp, made)
    data = open(lp, "rb").read()
    mud.write_file_chunks(rp, data)
    return verify_remote(mud, rp, data)

def cmd_watch(args):
    """Watch a local dir and auto-push files as they're saved/dropped in —
    the closest thing to a 'synced folder'. Only files that change AFTER
    start are pushed (run `push` first to sync what's already there).
    Server files are never deleted."""
    mud = connect(args)
    local = os.path.abspath(args.local)
    remote = args.remote.rstrip("/")
    exts = parse_exts(args.ext)
    made = set()

    def scan():
        state = {}
        for rel in local_walk(local, exts):
            if rel.endswith(TEMP_SUFFIXES): continue
            fp = os.path.join(local, rel)
            try: st = os.stat(fp)
            except OSError: continue
            state[rel] = (st.st_mtime, st.st_size)
        return state

    baseline = scan()
    print(f"Watching {local}  ->  {remote}")
    print(f"  ({len(baseline)} files baselined; drop or save files here to upload. Ctrl-C to stop.)\n")
    last_ping = time.time()
    try:
        while True:
            time.sleep(args.interval)
            cur = scan()
            changed = [rel for rel, sig in cur.items() if baseline.get(rel) != sig]
            if changed:
                for rel in sorted(changed):
                    lp = os.path.join(local, rel)
                    rp = f"{remote}/{rel}"
                    try:
                        ok = push_one(mud, lp, rp, made)
                        ts = time.strftime("%H:%M:%S")
                        print(f"  [{ts}] {'OK ' if ok else 'FAIL'}  ↑ {rel}")
                    except Exception as e:
                        print(f"  FAIL ↑ {rel}: {str(e)[:80]}")
                baseline = cur
                last_ping = time.time()
            elif time.time() - last_ping > 45:
                mud.exec_int("return 1;")   # keepalive so the session doesn't idle out
                last_ping = time.time()
    except KeyboardInterrupt:
        print("\nStopped watching.")
        mud.close()

def cmd_mirror(args):
    """Interleaved recursive mirror: list a directory, download its files
    immediately, then recurse into subdirectories. No upfront full walk, so
    files start landing right away and a server stall costs minimal progress.
    Resumable: already-present files are skipped, fully-completed directories
    are skipped via a done-dirs cache, and unreadable/too-big/colliding files
    are recorded so they're never re-attempted."""
    mud = connect(args)
    base = os.path.abspath(args.localbase)
    os.makedirs(base, exist_ok=True)
    exts = parse_exts(args.ext)

    # File-level skip-cache: unreadable / too-big / colliding files.
    skip_path = os.path.join(base, ".frsync_skipped.txt")
    known_skip = {}
    if os.path.isfile(skip_path):
        for ln in open(skip_path, encoding="utf-8").read().splitlines():
            if "\t" in ln:
                p, why = ln.split("\t", 1); known_skip[p] = why
    skip_fh = open(skip_path, "a", encoding="utf-8")
    def record_skip(path, why):
        if path not in known_skip:
            known_skip[path] = why
            skip_fh.write(f"{path}\t{why}\n"); skip_fh.flush()

    # Dir-level cache: directories whose entire subtree is already done. On a
    # resume these are skipped without even re-listing them.
    done_path = os.path.join(base, ".frsync_done_dirs.txt")
    done_dirs = set()
    if os.path.isfile(done_path):
        done_dirs = set(open(done_path, encoding="utf-8").read().split("\n")) - {""}
    done_fh = open(done_path, "a", encoding="utf-8")
    def mark_done(d):
        if d not in done_dirs:
            done_dirs.add(d); done_fh.write(d + "\n"); done_fh.flush()

    if known_skip or done_dirs:
        print(f"(resume cache: {len(done_dirs)} completed dirs, "
              f"{len(known_skip)} known-skip files)")

    st = {"done": 0, "skip": 0, "unreadable": 0, "toobig": 0,
          "collision": 0, "fail": 0, "n": 0}
    t0 = time.time()
    def tick(curdir):
        st["n"] += 1
        if st["n"] % 25 == 0:
            r = st["n"] / max(time.time() - t0, 0.1)
            print(f"    [{curdir}] ok={st['done']} skip={st['skip']} "
                  f"unreadable={st['unreadable']} toobig={st['toobig']} "
                  f"collision={st['collision']} fail={st['fail']}  {r:.1f}/s", flush=True)

    skip_segs = set() if args.include_backup else {"BACKUP", "ATTIC"}

    def process_file(rpath, size):
        name = os.path.basename(rpath)
        if (exts and not rpath.endswith(tuple(exts))) or name == "exec_tmp.c" \
                or rpath.endswith(BINARY_EXTS) or rpath in known_skip:
            return
        lp = os.path.join(base, rpath.lstrip("/"))
        if os.path.isfile(lp) and os.path.getsize(lp) == size:
            st["skip"] += 1; tick(os.path.dirname(rpath)); return
        if args.max_bytes and size > args.max_bytes:
            st["toobig"] += 1; record_skip(rpath, f"too large ({size} > {args.max_bytes})")
            tick(os.path.dirname(rpath)); return
        try:
            data = mud.read_file_bytes(rpath, size)
        except Exception as e:
            st["fail"] += 1; record_skip(rpath, f"read error: {str(e)[:60]}")
            tick(os.path.dirname(rpath)); return
        if len(data) != size:
            st["unreadable"] += 1; record_skip(rpath, f"unreadable (got {len(data)}/{size})")
            tick(os.path.dirname(rpath)); return
        try:
            os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
            with open(lp, "wb") as fh:
                fh.write(data)
            st["done"] += 1
        except (FileExistsError, NotADirectoryError, OSError) as e:
            st["collision"] += 1; record_skip(rpath, f"path collision: {str(e)[:60]}")
        tick(os.path.dirname(rpath))

    def mirror_tree(dpath):
        if dpath in done_dirs:
            return True
        try:
            items = mud.listdir(dpath)
        except Exception as e:
            record_skip(dpath, f"dir unreadable: {str(e)[:50]}")
            return False
        # 1) download this directory's files first (interleaved)
        for name, size in items:
            if size != -2:
                process_file(f"{dpath}/{name}", size)
        # 2) then recurse into subdirectories
        subtree_ok = True
        for name, size in items:
            if size == -2:
                if name in skip_segs:        # skip BACKUP/ATTIC archive dirs
                    continue
                if not mirror_tree(f"{dpath}/{name}"):
                    subtree_ok = False
        if subtree_ok:                       # whole subtree handled -> cache it
            mark_done(dpath)
        return subtree_ok

    for root in args.roots:
        root = "/" + root.strip("/")
        print(f"\n=== {root} ===", flush=True)
        mirror_tree(root)
        print(f"  done {root}: ok={st['done']} skip={st['skip']} "
              f"unreadable={st['unreadable']} toobig={st['toobig']} "
              f"collision={st['collision']} fail={st['fail']}", flush=True)

    skip_fh.close(); done_fh.close()
    print(f"\nMIRROR COMPLETE into {base}: {st['done']} downloaded, {st['skip']} already-present, "
          f"{st['unreadable']} unreadable, {st['toobig']} too-big, {st['collision']} collisions, "
          f"{st['fail']} errors. ({len(done_dirs)} dirs cached complete)")
    mud.close()

def load_or_build_manifest(mud, root, base, rewalk):
    """Return [(abs_server_path, size)] for everything under `root`, caching
    the listing to a local file so resumes skip the (slow) re-walk."""
    safe = root.strip("/").replace("/", "_") or "root"
    cache = os.path.join(base, f".frmanifest_{safe}.tsv")
    if os.path.isfile(cache) and not rewalk:
        out = []
        for ln in open(cache, encoding="utf-8").read().splitlines():
            if "\t" in ln:
                p, s = ln.rsplit("\t", 1)
                out.append((p, int(s)))
        print(f"  using cached manifest ({len(out)} files) — pass --rewalk to refresh")
        return out
    print(f"  walking {root} (one request per directory)…")
    ndirs = [0]
    def on_dir(path, count):
        ndirs[0] += 1
        if count is None:
            print(f"    (skipped unreadable {path})")
        elif ndirs[0] % 20 == 0:
            print(f"    …{ndirs[0]} dirs walked", flush=True)
    sized = mud.walk_sized(root, on_dir=on_dir)
    # store as absolute server paths
    abs_list = [(f"{root}/{rel}", sz) for rel, sz in sized]
    with open(cache, "w", encoding="utf-8") as fh:
        for p, s in abs_list:
            fh.write(f"{p}\t{s}\n")
    print(f"    {len(abs_list)} files across {ndirs[0]} dirs")
    return abs_list

# ---------------------------------------------------------------- helpers
def parse_exts(ext):
    return [e if e.startswith(".") else "." + e
            for e in ext.split(",")] if ext else None

def ensure_remote_dirs(mud, remote_path, made):
    parts = remote_path.strip("/").split("/")[:-1]
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}"
        if cur not in made:
            mud.mkdir(cur); made.add(cur)

def verify_remote(mud, path, data):
    norm = local_norm(data)
    return mud.file_size(path) == len(norm) and \
           mud.crc32_remote(path) == jamcrc(norm)

def show_plan(states, mode):
    order = ["new", "diff", "remote_only", "same"]
    label = {"new": "new (local only)", "diff": "differs",
             "remote_only": "on MUD only", "same": "in sync"}
    for k in order:
        rels = sorted(r for r, s in states.items() if s == k)
        if rels:
            print(f"  {label[k]}: {len(rels)}")
            for r in rels:
                print(f"      {r or '(file)'}")
    print()

def confirm(args, msg):
    if args.yes: return True
    return input(msg + " [y/N] ").strip().lower() in ("y", "yes")

# ---------------------------------------------------------------- cli
EPILOG = """\
examples:
  # upload your area (local -> MUD); verifies every file
  frsync push ./drifting_forest /w/<creator>/drifting_forest

  # see what differs without changing anything
  frsync status ./drifting_forest /w/<creator>/drifting_forest

  # download one area (MUD -> local)
  frsync pull /w/<creator>/drifting_forest ./drifting_forest

  # bulk-download whole subtrees into a local copy (resumable)
  frsync mirror ./MUD /std /d /w/<creator>

  # one file
  frsync push ./rooms/hut.c /w/<creator>/drifting_forest/rooms/hut.c

You log in with your own creator name + password (prompted, or via $FRPASS).
The MUD's permissions decide what you can read/write — exactly like in-game.
"""

def main():
    p = argparse.ArgumentParser(
        prog="frsync",
        description="Sync files between your machine and the Final Realms MUD "
                    "over the normal creator login — no FTP required.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--char", metavar="NAME", help="creator name (else prompts)")
    p.add_argument("--host", default=DEF_HOST, help=f"MUD host (default {DEF_HOST})")
    p.add_argument("--port", type=int, default=DEF_PORT, help=f"MUD port (default {DEF_PORT})")
    p.add_argument("--ext", metavar="LIST", help="only these extensions, e.g. .c,.h (dirs only)")
    p.add_argument("--delete", action="store_true", help="push: remove dest files missing at source")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{status,push,pull,mirror}")

    sp = sub.add_parser("status", help="compare local vs MUD, change nothing")
    sp.add_argument("local", help="local file or directory")
    sp.add_argument("remote", help="server file or directory")
    sp = sub.add_parser("push", help="upload local -> MUD (verified)")
    sp.add_argument("local", help="local file or directory")
    sp.add_argument("remote", help="server file or directory")
    sp = sub.add_parser("pull", help="download MUD -> local (verified)")
    sp.add_argument("remote", help="server file or directory")
    sp.add_argument("local", help="local file or directory")
    sp = sub.add_parser("watch", help="auto-push a local folder as files change (synced-folder mode)")
    sp.add_argument("local", help="local directory to watch")
    sp.add_argument("remote", help="server directory to upload into")
    sp.add_argument("--interval", type=float, default=2.0, help="poll seconds (default 2)")
    sp = sub.add_parser("connect", help="interactive MUD shell with /download and /upload")
    sp.add_argument("--dir", help="initial local folder for transfers (default: current dir)")
    mp = sub.add_parser("mirror", help="bulk-download server subtrees, resumable")
    mp.add_argument("localbase", help="local dir to mirror into (server paths preserved under it)")
    mp.add_argument("roots", nargs="+", metavar="root",
                    help="server dirs to mirror, e.g. /std /d /w/<creator>")
    mp.add_argument("--rewalk", action="store_true", help="ignore cached manifest, re-list the tree")
    mp.add_argument("--include-backup", action="store_true",
                    help="include BACKUP/ and ATTIC/ archive dirs (excluded by default)")
    mp.add_argument("--max-bytes", type=int, default=1_048_576,
                    help="skip files larger than this (default 1 MB; 0 = no limit)")
    sub.metavar = "{connect,status,push,pull,watch,mirror}"
    args = p.parse_args()
    {"status": cmd_status, "push": cmd_push, "pull": cmd_pull,
     "watch": cmd_watch, "mirror": cmd_mirror, "connect": cmd_connect}[args.cmd](args)

if __name__ == "__main__":
    main()
