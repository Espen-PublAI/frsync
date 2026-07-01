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
import argparse, difflib, fnmatch, getpass, glob, os, queue, re, select, shlex, socket, sys, threading, time, zlib
try:
    import termios, tty          # POSIX-only; absent on Windows
except ImportError:
    termios = tty = None
try:
    import msvcrt                 # Windows-only console I/O; absent on POSIX
except ImportError:
    msvcrt = None

DEF_HOST, DEF_PORT = "fr.hyssing.net", 4010
PUSH_CMD_BUDGET = 900     # max chars in one `exec write_file(...)` line
# Fixed chars in the per-chunk exec wrapper, excluding <path> and <chunk>:
#   exec int _r=write_file("<path>", "<chunk>", <flag>); return "R"+"C"+sprintf("%d",_r)+"R"+"C";
# That boilerplate is ~75 chars; reserve a bit more so a full chunk can never
# push the command past PUSH_CMD_BUDGET. An over-long line is truncated by the
# driver — which lops off the `return`, leaving `_r` unused (a compile error),
# so the write silently never lands and write_file_chunks resends forever.
WRITE_WRAPPER_RESERVE = 90
PULL_CHUNK      = 4096    # source bytes per read_bytes() call

# ---------------------------------------------------------------- telnet layer
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def _utf8_incomplete_tail(b):
    """How many trailing bytes of `b` are the start of a UTF-8 multibyte
    character that isn't complete yet — so a streaming reader can hold them
    until the rest arrives. Returns 0 on a clean boundary, or when the tail
    isn't a valid lead+continuation run (left for the decoder to handle)."""
    n = len(b)
    if not n:
        return 0
    i, steps = n - 1, 0
    while i >= 0 and 0x80 <= b[i] <= 0xBF and steps < 3:   # back over continuations
        i -= 1; steps += 1
    if i < 0:
        return 0
    lead = b[i]
    if   0xC0 <= lead <= 0xDF: need = 2
    elif 0xE0 <= lead <= 0xEF: need = 3
    elif 0xF0 <= lead <= 0xF7: need = 4
    else:
        return 0                       # ASCII or invalid lead — nothing to hold
    have = n - i                       # bytes present from the lead through the end
    return have if have < need else 0

class Mud:
    SOCK_TIMEOUT = 90   # no single send/recv may block longer than this

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.char = self.pw = None
        self._rx_carry = b""   # bytes held back mid-UTF-8-char / mid-telnet-seq
        self.s = socket.create_connection((host, port), timeout=25)
        self.s.settimeout(self.SOCK_TIMEOUT)

    def reconnect(self):
        """Re-establish a dropped session (e.g. after the server resets us)."""
        try: self.s.close()
        except OSError: pass
        self._rx_carry = b""        # new socket -> drop any half-decoded bytes
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
        # Prepend bytes held back from the previous read: a telnet sequence or a
        # UTF-8 multibyte character that was split across two network reads.
        data = self._rx_carry + data
        self._rx_carry = b""
        out, reply, i, n = bytearray(), bytearray(), 0, len(data)
        iac_tail = b""
        while i < n:
            b = data[i]
            if b == IAC:
                if i + 1 >= n:                          # IAC split across reads
                    iac_tail = bytes(data[i:]); break
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT):
                    if i + 2 >= n:                      # option byte not here yet
                        iac_tail = bytes(data[i:]); break
                    opt = data[i + 2]
                    if cmd == DO:   reply += bytes([IAC, WONT, opt])
                    elif cmd == WILL: reply += bytes([IAC, DONT, opt])
                    i += 3; continue
                if cmd == SB:
                    j = i + 2
                    while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                        j += 1
                    if j + 1 >= n:                      # subnegotiation not terminated yet
                        iac_tail = bytes(data[i:]); break
                    i = j + 2; continue
                i += 2; continue
            out.append(b); i += 1
        if reply:
            try: self.s.sendall(bytes(reply))
            except OSError: pass
        # Decode the clean bytes as UTF-8, holding back an incomplete trailing
        # multibyte char; fall back to latin-1 for legacy 8-bit content.
        clean = bytes(out)
        m = len(clean)
        hold = _utf8_incomplete_tail(clean)
        body, utf8_tail = (clean[:m - hold], clean[m - hold:]) if hold else (clean, b"")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
        # leftover, in stream order: the held UTF-8 tail, then any split telnet seq
        self._rx_carry = utf8_tail + iac_tail
        return text

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
        # latin-1 / byte-preserving: used for the login, exec, and file-transfer
        # protocol where each char maps 1:1 to a server byte. Do NOT switch this
        # to UTF-8 — write_file_chunks relies on the latin-1 round-trip.
        self.s.sendall((line + "\n").encode("latin-1"))

    def send_text(self, line):
        # UTF-8: for human-typed input in the interactive shell, so a creator can
        # type accented/Unicode text. ASCII is identical to send(); only non-ASCII
        # input differs (and would otherwise raise on the latin-1 encode).
        self.s.sendall((line + "\n").encode("utf-8"))

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
        self._rx_carry = b""        # also drop any half-decoded bytes we were holding
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

    def rm(self, path):
        """Delete a server file. Returns True on success (rm() efun -> 1=ok)."""
        return self.exec_int(f'return rm("{path}");') == 1

    def rmdir(self, path):
        """Remove an EMPTY server directory. True on success (rmdir() -> 1=ok)."""
        return self.exec_int(f'return rmdir("{path}");') == 1

    def rename(self, src, dst):
        """Rename/move a server file. True on success (rename() efun -> 0=ok)."""
        return self.exec_int(f'return rename("{src}", "{dst}");') == 0

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

    def write_file_chunks(self, path, data: bytes, progress=None):
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
        chunks = list(chunk_escaped(text, PUSH_CMD_BUDGET - len(path) - WRITE_WRAPPER_RESERVE)) or [("", 0)]
        total = len(text)     # normalised byte count the file will hold when done
        expected = 0          # bytes the file should hold after each landed chunk
        i, restarts = 0, 0
        if progress: progress(0, total)
        while i < len(chunks):
            ch, nbytes = chunks[i]
            flag = 1 if i == 0 else 0
            res = self._write_once(path, ch, flag)
            if res == 1:
                expected += nbytes; i += 1
                if progress: progress(expected, total)
                continue
            if res == 0:
                raise RuntimeError(f"write_file refused chunk {i} of {path} (returned 0)")
            # res is None -> uncertain. file_size() heals a silent session itself.
            fsz = self.file_size(path)
            size = fsz if fsz >= 0 else 0
            if size == expected + nbytes:            # landed; only the reply was lost
                expected += nbytes; i += 1
                if progress: progress(expected, total)
            elif size == expected:                   # nothing written; resend same chunk
                continue
            else:                                    # unknown state -> rebuild from scratch
                if restarts >= 2:
                    raise RuntimeError(
                        f"write_file: unrecoverable state for {path} "
                        f"(size={size}, expected~{expected} at chunk {i})")
                restarts += 1; expected = 0; i = 0
                if progress: progress(expected, total)

    def _read_range(self, path, start, end, progress=None):
        """Read server file bytes [start, end) via read_bytes, hex-framed."""
        buf = bytearray()
        off = start
        if progress: progress(0, end - start)
        while off < end:
            n = min(PULL_CHUNK, end - off)
            code = (f'string c=read_bytes("{path}", {off}, {n}); string r=""; int i; '
                    f'for(i=0;i<strlen(c);i++) r+=sprintf("%02x",c[i]); '
                    f'return "M1"+"M2"+r+"M2"+"M1";')
            buf += self.exec_hex(code)
            off += n
            if progress: progress(off - start, end - start)
        return bytes(buf)

    def read_file_bytes(self, path, size, progress=None):
        return self._read_range(path, 0, size, progress)

    def read_tail(self, path, nbytes):
        """Read up to the last `nbytes` bytes of a server file — for tailing logs.
        Returns b'' if the file is missing/unreadable."""
        size = self.file_size(path)
        if size < 0: return b""
        return self._read_range(path, max(0, size - nbytes), size)

    def update(self, paths, inherit=False):
        """Recompile (reload) server files in-game via the creator `update`
        command. `paths` is a list of remote paths. Returns the parsed result
        from parse_update_output()."""
        cmd = "update " + ("-o " if inherit else "") + " ".join(paths)
        self._flush(); self.send(cmd)
        return parse_update_output(ANSI.sub("", self.drain(1.8)))

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
    made = set(); jobs = []
    for rel in sorted(todo):
        lp = args.local if is_file else os.path.join(args.local, rel)
        rp = args.remote if is_file else f"{args.remote}/{rel}"
        label = rel or os.path.basename(rp)
        sz = len(local_norm(open(lp, "rb").read()))
        def send(progress, lp=lp, rp=rp):
            ensure_remote_dirs(mud, rp, made)
            mud.write_file_chunks(rp, open(lp, "rb").read(), progress=progress)
            return verify_remote(mud, rp, open(lp, "rb").read())
        jobs.append({"base": label, "size": sz, "exists": False, "run": send})
    pinned_batch(jobs, "↑", args.remote, write=_stdout_write, bar=sys.stdout.isatty())
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
    jobs = []
    for rel, rp, lp, rs in todo:
        label = rel or os.path.basename(rp)
        def fetch(progress, rp=rp, rs=rs, lp=lp):
            data = mud.read_file_bytes(rp, rs, progress=progress)
            os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
            open(lp, "wb").write(data)
            return len(data) == rs
        jobs.append({"base": label, "size": rs, "exists": False, "run": fetch})
    pinned_batch(jobs, "↓", args.local, write=_stdout_write, bar=sys.stdout.isatty())
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

def _has_glob(s):
    return any(c in s for c in "*?[")

def expand_local_arg(localdir, pattern):
    """Expand one /upload argument (a filename or a glob like *.c, cloud*.c, *.*)
    to a list of existing local file paths. A plain name returns [its path] even
    if missing, so the caller can report 'no such file'."""
    full = os.path.join(localdir, pattern)        # an absolute pattern overrides localdir
    if not _has_glob(pattern):
        return [full]
    return sorted(p for p in glob.glob(full) if os.path.isfile(p))

def _strip_file_uri(tok):
    """A few terminals paste a file:// URI on drop; turn one back into a path."""
    if tok.startswith("file://"):
        import urllib.parse
        return urllib.parse.unquote(urllib.parse.urlsplit(tok).path)
    return tok

def _ws_split_quoted(s):
    """Whitespace split that respects '…' / "…" grouping but leaves backslashes
    untouched — needed for bare Windows paths (C:\\dir\\f) that posix shlex would
    mangle by treating each backslash as an escape."""
    out, cur, quote = [], [], None
    for ch in s:
        if quote:
            if ch == quote: quote = None
            else: cur.append(ch)
        elif ch in ("'", '"'): quote = ch
        elif ch.isspace():
            if cur: out.append("".join(cur)); cur = []
        else: cur.append(ch)
    if cur: out.append("".join(cur))
    return out

def dropped_files(line):
    """If `line` is one or more existing local file paths — as a terminal inserts
    when you drag-and-drop files onto it — return that list of paths; else None.
    Handles backslash-escaped spaces and quotes (macOS/Linux), quoted or bare
    Windows paths, and file:// URIs. Requires every token to be an existing file
    AND at least one to look path-like, so ordinary typed lines (`look`, `get
    sword`, `/upload x`) never trip it."""
    s = line.strip()
    if not s:
        return None
    candidates = []
    try: candidates.append(shlex.split(s, posix=True))
    except ValueError: pass
    candidates.append(_ws_split_quoted(s))
    for toks in candidates:
        if not toks:
            continue
        paths = [os.path.expanduser(_strip_file_uri(t)) for t in toks]
        if all(os.path.isfile(p) for p in paths) and \
           any(os.path.isabs(p) or os.sep in p or "/" in p for p in paths):
            return paths
    return None

def expand_remote_arg(mud, pattern, rcwd):
    """Expand one /download argument against the server. A glob in the final
    path component is matched by listing its directory; a plain name returns
    [its resolved path]. Returns absolute remote file paths (directories
    excluded)."""
    full = resolve_remote(pattern, rcwd)
    base = full.rsplit("/", 1)[-1]
    if not _has_glob(base):
        return [full]
    rdir = full[: len(full) - len(base)].rstrip("/") or "/"
    names = [n for n, s in mud.listdir(rdir) if s != -2]    # files only, skip dirs
    return [f"{rdir.rstrip('/')}/{n}" for n in sorted(fnmatch.filter(names, base))]

def human_size(n):
    return f"{n/1024:.1f}K" if n >= 1024 else f"{n}B"

def render_progress(label, done, total, width=22):
    """A single-line progress bar that overwrites itself via leading '\\r'.
    The caller clears the line (\\r\\x1b[K) and prints the result when done."""
    frac = 1.0 if total <= 0 else min(max(done, 0) / total, 1.0)
    fill = int(frac * width)
    bar = "█" * fill + "░" * (width - fill)
    return f"\r  {label[:18]:<18} [{bar}] {int(frac*100):3d}%  {human_size(done)}/{human_size(total)}"

def render_batch_bar(done, total, idx, count, label, width=22):
    """One overall progress bar for a whole multi-file transfer: total bytes
    across every file, plus which file (idx/count) is moving right now."""
    frac = 1.0 if total <= 0 else min(max(done, 0) / total, 1.0)
    fill = int(frac * width)
    bar = "█" * fill + "░" * (width - fill)
    return (f"  [{bar}] {int(frac*100):3d}%  ({idx}/{count}) "
            f"{label[:16]:<16} {human_size(done)}/{human_size(total)}")

def _stdout_write(s):
    sys.stdout.write(s); sys.stdout.flush()

def pinned_batch(jobs, arrow, dest, write, ask_key=None, bar=True):
    """Run transfer `jobs` with ONE progress bar pinned at the bottom (when
    `bar`), or plain per-file lines when `bar` is off (piped / non-tty). Used by
    both the interactive shell and the scriptable push/pull.
      jobs: list of {base, size, exists(bool), run(progress)->ok_bool}.
      ask_key: optional overwrite key-asker (y/n/a=all-yes/s=no-all/q=quit);
               None transfers everything without asking (caller already chose).
    Returns the counts dict."""
    st = {"total": sum(j["size"] for j in jobs), "done": 0,
          "did": 0, "skip": 0, "fail": 0, "mode": None, "shown": False}
    n = len(jobs); nl = "\r\n" if bar else "\n"
    def draw(extra, label):
        if not bar: return
        idx = min(st["did"] + st["skip"] + st["fail"] + 1, n)
        write("\r\x1b[K" + render_batch_bar(st["done"] + extra, st["total"], idx, n, label))
        st["shown"] = True
    def note(msg):                       # print a line above the bar
        write(("\r\x1b[K" if st["shown"] else "") + msg + nl)
        st["shown"] = False
    def skip(base, size):
        note(f"  • skipped {base}"); st["skip"] += 1; st["total"] -= size
    aborted = False
    for j in jobs:
        base, size = j["base"], j["size"]
        if ask_key and j["exists"] and st["mode"] != "all":
            if st["mode"] == "none": skip(base, size); continue
            if st["shown"]: write("\r\x1b[K"); st["shown"] = False   # clear bar to ask
            ans = ask_key(f"  {base} already exists — overwrite? "
                          f"[y]es [n]o [a]ll-yes [s]kip-all [q]uit ")
            if ans == "q": aborted = True; break
            if ans == "a": st["mode"] = "all"
            elif ans == "s": st["mode"] = "none"; skip(base, size); continue
            elif ans != "y": skip(base, size); continue
        draw(0, base)
        try:
            ok = j["run"](lambda d, t, b=base: draw(d, b))
        except Exception as e:
            note(f"  ✗ {base}: {str(e)[:60]}"); st["fail"] += 1; continue
        st["done"] += size
        if ok:
            st["did"] += 1
            if not bar: note(f"  {arrow} {base} ({human_size(size)})")
        else:
            note(f"  ✗ {base}: verify failed"); st["fail"] += 1
    if st["shown"]: write("\r\x1b[K")    # wipe the bar, leave the summary
    msg = f"  {arrow} {st['did']} file(s), {human_size(st['done'])} -> {dest}"
    if st["skip"]: msg += f"   ·  {st['skip']} skipped"
    if st["fail"]: msg += f"   ·  {st['fail']} failed"
    if aborted:    msg += "   ·  quit"
    write(msg + nl)
    return st

def _enable_windows_console():
    """On Windows, turn on ANSI escape processing (so our \\r\\x1b[K line redraws
    and the progress bars render instead of printing literal `←[K`) and switch
    stdout to UTF-8 (so non-ASCII — Norwegian, CJK — echoes and prints intact).
    A no-op on POSIX, and harmless if already enabled. Called once at startup so
    EVERY command gets working bars, not just the interactive `connect`."""
    if msvcrt is None:
        return
    try:
        import ctypes
        kern = ctypes.windll.kernel32                   # type: ignore[attr-defined]  (Windows-only)
        hcon = kern.GetStdHandle(-11)                   # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kern.GetConsoleMode(hcon, ctypes.byref(mode)):
            kern.SetConsoleMode(hcon, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception: pass

class LineEditor:
    """The pinned input line for the interactive shell: holds the line being
    typed plus Up/Down history, echoes edits, and submits completed lines.

    Shared by the POSIX (termios) and Windows (msvcrt) raw-mode loops, which
    differ only in how they READ keys — both translate a keystroke into one
    `key()` call. The cooked fallback uses only `echo_output` (its buffer stays
    empty) and submits whole lines itself.

    When the line begins with '/', a dim list of matching commands is shown after
    the cursor and narrows as you type. Tab completes: on the command word it
    fills a unique match (or extends to the common prefix); on an argument it asks
    `arg_completer(cmd, partial)` for file names and completes those, showing the
    candidates in the dim strip."""
    def __init__(self, write, submit, commands=None, arg_completer=None):
        self._write = write       # out(str) -> None
        self._submit = submit     # handle_line(str) -> bool  (False = quit)
        self.commands = commands or []          # "/foo" names for autocomplete
        self._arg_completer = arg_completer     # (cmd, partial) -> [candidate, …]
        self.buf = []             # chars of the line being typed
        self.history = []         # submitted lines, oldest first (Up/Down recalls)
        self.hidx = 0             # cursor into history; == len means the live line
        self.pending = ""         # live line stashed while scrolling up
        self._suggest = None      # transient arg candidates to show (set by Tab)

    def _matches(self):
        """Commands that complete the current (space-free) /prefix, else []."""
        line = "".join(self.buf)
        if not line.startswith("/") or " " in line:
            return []
        return [c for c in self.commands if c.startswith(line)]

    @staticmethod
    def _strip(items):
        """A width-bounded, dim-strip-ready join of `items` (so the line can't
        wrap): '  a  b  c' plus '  …' when the list was truncated."""
        shown = []
        for c in items:
            if sum(len(x) + 2 for x in shown) + len(c) > 56:
                break
            shown.append(c)
        return "  " + "  ".join(shown) + ("  …" if len(shown) < len(items) else "")

    def _hint(self):
        """The dim suggestion strip shown after the cursor: transient argument
        candidates from the last Tab if any, else the commands still matching the
        /prefix. '' when there's nothing to suggest."""
        if self._suggest:
            return self._strip(self._suggest)
        line = "".join(self.buf)
        m = self._matches()
        return self._strip(m) if m and m != [line] else ""

    def _complete_command(self):
        line = "".join(self.buf)
        m = self._matches()
        if len(m) == 1:
            self.buf[:] = list(m[0] + " ")             # unique -> fill in + space
        elif m:
            lcp = os.path.commonprefix(m)              # many -> extend shared prefix
            if len(lcp) > len(line): self.buf[:] = list(lcp)

    def _complete_arg(self):
        """Complete a file-name argument via the caller's arg_completer."""
        if not self._arg_completer:
            return
        parts = "".join(self.buf).split(" ")
        cands = self._arg_completer(parts[0], parts[-1]) or []
        if len(cands) == 1:
            c = cands[0]
            parts[-1] = c if c.endswith("/") else c + " "   # dir -> keep going; file -> next arg
            self.buf[:] = list(" ".join(parts))
        elif cands:
            lcp = os.path.commonprefix(cands)
            if len(lcp) > len(parts[-1]):
                parts[-1] = lcp; self.buf[:] = list(" ".join(parts))
            self._suggest = [c.rstrip("/").rsplit("/", 1)[-1] + ("/" if c.endswith("/") else "")
                             for c in cands]            # show basenames to pick from

    def _tail(self):
        """Buffer + dim hint, with the cursor left sitting right after the buffer
        (the hint's ANSI/reposition are non-printing width-wise)."""
        line = "".join(self.buf)
        hint = self._hint()
        if not hint:
            return line
        return line + "\x1b[90m" + hint + "\x1b[0m" + f"\x1b[{len(hint)}D"

    def _refresh(self):
        """Clear the input line and repaint buffer + hint."""
        self._write("\r\x1b[K" + self._tail())

    def echo_output(self, text):
        """Print async MUD output, keeping the typed line pinned at the bottom."""
        if self.buf:
            self._write("\r\x1b[K" + text + self._tail())   # lift input, print, redraw
        else:
            self._write(text)

    def key(self, kind, ch=""):
        """Apply one logical key event to the line. Returns False to quit."""
        if kind != "tab":
            self._suggest = None                    # Tab's candidate strip is transient
        if kind == "text":                          # printable (incl. Unicode)
            self.buf.append(ch); self._refresh()
        elif kind == "enter":                       # submit the line
            self._write("\x1b[K\r\n")               # \x1b[K wipes the dim hint to the right
            line = "".join(self.buf); self.buf.clear()
            if line and (not self.history or self.history[-1] != line):
                self.history.append(line)           # keep it; skip blanks + dups
            self.hidx = len(self.history); self.pending = ""   # back to a fresh live line
            return self._submit(line)
        elif kind == "tab":                         # complete command word or argument
            line = "".join(self.buf)
            if line.startswith("/") and " " not in line:
                self._complete_command()
            elif line.startswith("/") and self._arg_completer:
                self._complete_arg()
            self._refresh()
        elif kind == "backspace":
            if self.buf: self.buf.pop(); self._refresh()
        elif kind == "clear":                       # Ctrl-U -> wipe the line
            if self.buf: self.buf.clear(); self._refresh()
        elif kind == "eof":                         # Ctrl-D / Ctrl-Z on empty line
            if not self.buf: return False
        elif kind in ("up", "down") and self.history:   # recall older / newer line
            if kind == "up":
                if self.hidx == len(self.history): self.pending = "".join(self.buf)
                if self.hidx > 0: self.hidx -= 1
            elif self.hidx < len(self.history):
                self.hidx += 1
            recalled = self.pending if self.hidx == len(self.history) else self.history[self.hidx]
            self.buf[:] = list(recalled)
            self._refresh()
        return True

# Local /commands, for Tab-completion and the live type-ahead hint. Primary
# names first (so a bare "/" suggests those), then aliases so they complete too.
SLASH_COMMANDS = [
    "/upload", "/download", "/push", "/pull", "/diff", "/lint", "/update",
    "/goto", "/clone", "/errors", "/autoupdate", "/rm", "/mv",
    "/lcd", "/rcd", "/lpwd", "/lls", "/where", "/help", "/quit",
    "/put", "/get", "/log", "/del", "/rename",          # aliases
]

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
    state = {"local": local, "rcwd": home or "/",
             "autoupdate": True,   # reload each .c on the MUD right after pushing it
             "last_push": []}      # remote paths from the most recent upload (for /update, /goto)
    made = set()

    # Overwrite confirmation. Each terminal mode installs its own key reader
    # below (single keypress in raw mode, a line read in the fallback) that
    # returns one lowercased char.
    ask_key = None
    def batch_transfer(jobs, arrow, dest):
        pinned_batch(jobs, arrow, dest, write=out, ask_key=ask_key, bar=sys.stdout.isatty())

    def confirm_key(prompt, default_yes=True):
        """Ask a yes/no question. With default_yes, Enter means yes (used for
        transfers); with default_yes=False, only an explicit 'y' confirms (used
        for destructive actions like /rm)."""
        if not ask_key: return default_yes
        ans = ask_key(prompt)
        return ans in ("y", "\r", "\n", "") if default_yes else ans == "y"

    def reload_files(rpaths, inherit=False):
        """Recompile the given remote .c files in-game and report per-file: a
        clean reload count, plus any compile error as file:line: message."""
        cfiles = [p for p in rpaths if p.endswith(".c")]
        hdrs = [p for p in rpaths if p.endswith(".h")]
        if not cfiles:
            if hdrs: out("  (changed .h only — reload dependents with /update -o <file>)\r\n")
            return
        out(f"  ⟳ update {len(cfiles)} file(s)…\r\n")
        try:
            res = mud.update(cfiles, inherit=inherit)
        except OSError as e:
            out(f"    ✗ update failed: {str(e)[:60]}\r\n"); return
        bad = set()
        for p, line, msg in res["errors"]:
            bad.add(p); out(f"    ✗ {os.path.basename(p)}:{line}: {msg}\r\n")
        for p in res["failed"]:
            bad.add(p); out(f"    ✗ {os.path.basename(p)}: failed to load\r\n")
        ok = len(cfiles) - len(bad)
        if ok > 0: out(f"    ✓ reloaded {ok} file(s)\r\n")

    def show_errors(a):
        """Tail the creator error log (/w/<you>/error-log). `all` keeps the
        tool's own exec_tmp.c noise; a bare number sets how many lines to show."""
        n, noise = 20, False
        for x in a:
            if x in ("all", "-a"): noise = True
            elif x.isdigit(): n = int(x)
        logpath = f"{home}/error-log"
        raw = mud.read_tail(logpath, 20000).decode("latin-1", "replace")
        if not raw:
            out(f"\r\n  no error log at {logpath}\r\n"); return
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not noise:
            lines = [ln for ln in lines if "exec_tmp.c" not in ln]
        lines = lines[-n:]
        out("\r\n")
        if not lines:
            out("  (no recent errors — just tool exec noise; /errors all to see it)\r\n"); return
        for ln in lines: out("  " + ln + "\r\n")

    def _emit_diff(label, lp, rp, rsz, tty):
        """Print the unified diff of one existing-both-sides file. Returns False
        (and prints nothing) if the two copies are byte-identical after norm."""
        ltext = local_norm(open(lp, "rb").read()).decode("utf-8", "replace")
        rtext = local_norm(mud.read_file_bytes(rp, rsz)).decode("utf-8", "replace")
        if ltext == rtext:
            return False
        out("\r\n")
        for ln in unified_diff_lines(rtext, ltext, label):
            out("  " + colorize_diff(ln, tty) + "\r\n")
        return True

    def show_diff(arg):
        """Show a unified diff of one file: your local copy vs the copy live on
        the MUD. '+' is what a push would apply, '-' is MUD-only content (drift)."""
        lp = arg if os.path.isabs(arg) else os.path.join(state["local"], arg)
        rp = resolve_remote(arg, state["rcwd"])
        label = arg.replace(os.sep, "/")
        l_ok = os.path.isfile(lp)
        rsz = mud.file_size(rp)
        if not l_ok and rsz < 0:
            out(f"\r\n  ✗ {arg}: not found locally or on the MUD\r\n")
        elif rsz < 0:
            out(f"\r\n  {label}: only local — not on the MUD yet (/upload to push it)\r\n")
        elif not l_ok:
            out(f"\r\n  {label}: only on the MUD — no local copy (/download to fetch it)\r\n")
        elif not _emit_diff(label, lp, rp, rsz, sys.stdout.isatty()):
            out(f"\r\n  {label}: identical — local and MUD are in sync\r\n")

    def diff_all():
        """Collectively diff the whole current local folder against the remote
        folder (recursive). Uses plan() (size+CRC) so only files that actually
        differ are fetched and shown; local-only and MUD-only files are listed,
        not dumped."""
        lroot, rroot = state["local"], state["rcwd"]
        out(f"\r\n  scanning {lroot}  vs  {rroot} …\r\n")
        st = plan(mud, lroot, rroot, [], is_file=False)
        changed = sorted(r for r, s in st.items() if s == "diff")
        new = sorted(r for r, s in st.items() if s == "new")
        mud_only = sorted(r for r, s in st.items() if s == "remote_only")
        same = sum(1 for s in st.values() if s == "same")
        if not (changed or new or mud_only):
            out(f"  in sync — {same} file(s) match.\r\n"); return
        tty = sys.stdout.isatty()
        for rel in changed:
            lp = os.path.join(lroot, *rel.split("/"))
            rp = f"{rroot}/{rel.replace(os.sep, '/')}"
            _emit_diff(rel.replace(os.sep, "/"), lp, rp, mud.file_size(rp), tty)
        if new:      out(f"\r\n  only local (not on MUD): {', '.join(new)}\r\n")
        if mud_only: out(f"  only on MUD (not local): {', '.join(mud_only)}\r\n")
        out(f"\r\n  {len(changed)} changed · {len(new)} local-only · "
            f"{len(mud_only)} MUD-only · {same} in sync\r\n")

    def do_lint(arg, verbose=False):
        """Validate an area: read its local .c source, resolve every referenced
        path (inherit / add_exit / clone_object / add_clone / load_object /
        find_object / #include) through the area's #define macros, and check each
        target exists on the MUD — catching broken exits and typos before players
        do. References whose path is a runtime variable or a macro defined outside
        the area (e.g. a standard mudlib path) can't be resolved statically and
        are skipped — listed with -v so you can confirm none hides a real bug."""
        root = state["local"] if not arg else os.path.abspath(os.path.join(state["local"], arg))
        if not os.path.isdir(root):
            out(f"\r\n  ✗ not a local folder: {root}\r\n"); return
        srcs = {}
        for rel in local_walk(root, [".c", ".h"]):
            try:
                raw = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
                srcs[rel] = _strip_c_comments(raw)     # strip once; used for defines AND refs
            except OSError: pass
        defines = collect_defines(srcs.values())       # comment-free, so trailing // can't break a macro
        wrappers = find_ref_wrappers(srcs.values())     # learn custom add_exit/clone helpers
        cfiles = sorted(r for r in srcs if r.endswith(".c"))
        out(f"\r\n  linting {len(cfiles)} file(s) under {root} (targets checked on the MUD) …\r\n")
        if wrappers:
            out("  recognised wrapper(s): "
                + ", ".join(f"{n}()→{k}" for n, (k, _) in sorted(wrappers.items())) + "\r\n")
        cache, rc = {}, state["rcwd"].rstrip("/")
        def on_mud(p):
            if p not in cache: cache[p] = mud.file_size(p) >= 0
            return cache[p]
        broken = checked = 0
        skipped = []                                    # (rel, line, kind, expr) we couldn't resolve
        for rel in cfiles:
            for ref in extract_references(srcs[rel], defines, wrappers):
                if ref["path"] is None:
                    skipped.append((rel, ref["line"], ref["kind"], ref["expr"])); continue
                checked += 1
                if on_mud(ref["path"]): continue
                broken += 1
                hint = ""
                if ref["path"].startswith(rc + "/"):     # inside this area?
                    lp = os.path.join(state["local"], *ref["path"][len(rc) + 1:].split("/"))
                    hint = "  (exists locally — push it)" if os.path.isfile(lp) else ""
                out(f"    ✗ {rel}:{ref['line']}  {ref['kind']} → {ref['path']}{hint}\r\n")
        if verbose and skipped:
            out("\r\n  skipped (couldn't resolve statically — verify by hand):\r\n")
            for rel, line, kind, expr in skipped:
                out(f"    ? {rel}:{line}  {kind} → {expr}\r\n")
        out(f"\r\n  {broken} broken reference(s) · {checked} checked"
            + (f" · {len(skipped)} skipped{'' if verbose else ' (use /lint -v to see them)'}"
               if skipped else "") + "\r\n")
        if checked == 0:
            out("  ⚠ nothing was checkable — is this an area folder? (lint knows "
                "add_exit/inherit/clone/load/#include)\r\n")
        elif not broken:
            out("  ✓ every checked reference resolves on the MUD.\r\n")

    def upload_local_files(paths):
        """Push a list of local files to the current remote folder, one batch
        bar, overwrite prompt per existing file. Shared by /upload and the
        drag-and-drop handler."""
        jobs = []; rpaths = []
        for lp in paths:
            base = os.path.basename(lp); rp = resolve_remote(base, state["rcwd"])
            rpaths.append(rp)
            sz = len(local_norm(open(lp, "rb").read()))
            def send(progress, lp=lp, rp=rp):
                return push_one(mud, lp, rp, made, progress=progress)
            jobs.append({"base": base, "size": sz,
                         "exists": mud.file_size(rp) >= 0, "run": send})
        if not jobs: out("\r\n"); return
        out("\r\n"); batch_transfer(jobs, "↑", state["rcwd"])
        state["last_push"] = rpaths
        if state["autoupdate"]: reload_files(rpaths)

    def offer_upload(paths):
        """A drag-and-drop landed one or more local file paths on the input line —
        confirm, then upload them to the current remote folder."""
        n = len(paths)
        names = "  ".join(os.path.basename(p) for p in paths)
        out(f"\r\n  ↑ dropped {n} file(s) → {state['rcwd']}\r\n    {names}\r\n")
        if confirm_key("  upload? [Y/n] "):
            upload_local_files(paths)
        else:
            out("  cancelled.\r\n")
        return True

    def do_sync(direction):
        """Recursively mirror the whole current folder — every file and subfolder,
        recreating the tree on the far side. Only new/changed files move (matching
        files are skipped), and nothing is deleted. `direction` is "up" (local →
        MUD) or "down" (MUD → local)."""
        lroot, rroot = state["local"], state["rcwd"]
        if direction == "up":
            out(f"\r\n  scanning {lroot} …\r\n")
            st = plan(mud, lroot, rroot, [], is_file=False)
            todo = sorted(r for r, s in st.items() if s in ("new", "diff"))
            same = sum(1 for s in st.values() if s == "same")
            if not todo:
                out(f"  already in sync — {same} file(s) match.\r\n"); return
            out(f"  {len(todo)} file(s) to upload → {rroot}"
                + (f"   ({same} already match)" if same else "") + "\r\n")
            if not confirm_key(f"  sync {len(todo)} file(s) to the MUD? [Y/n] "):
                out("  cancelled.\r\n"); return
            jobs = []; rpaths = []
            for rel in todo:
                lp = os.path.join(lroot, rel); rp = f"{rroot}/{rel.replace(os.sep, '/')}"
                rpaths.append(rp)
                sz = len(local_norm(open(lp, "rb").read()))
                def send(progress, lp=lp, rp=rp):
                    return push_one(mud, lp, rp, made, progress=progress)
                jobs.append({"base": rel, "size": sz, "exists": False, "run": send})
            out("\r\n"); batch_transfer(jobs, "↑", rroot)
            state["last_push"] = rpaths
            if state["autoupdate"]: reload_files(rpaths)
        else:
            out(f"\r\n  scanning {rroot} …\r\n")
            rels = mud.walk(rroot)
            if not rels:
                out(f"  nothing to download from {rroot}.\r\n"); return
            todo = []
            for rel in rels:
                rp = f"{rroot}/{rel}"; lp = os.path.join(lroot, *rel.split("/"))
                rs = mud.file_size(rp)
                if rs < 0: continue
                if os.path.isfile(lp):
                    lsize, lsum = local_size_sum(lp)
                    if lsize == rs and lsum == mud.byte_sum(rp): continue
                todo.append((rel, rp, lp, rs))
            if not todo:
                out(f"  already in sync — {len(rels)} file(s) match.\r\n"); return
            out(f"  {len(todo)} file(s) to download → {lroot}\r\n")
            if not confirm_key(f"  sync {len(todo)} file(s) to this computer? [Y/n] "):
                out("  cancelled.\r\n"); return
            jobs = []
            for rel, rp, lp, rs in todo:
                def fetch(progress, rp=rp, rs=rs, lp=lp):
                    data = mud.read_file_bytes(rp, rs, progress=progress)
                    os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
                    open(lp, "wb").write(data); return len(data) == rs
                jobs.append({"base": rel, "size": rs, "exists": False, "run": fetch})
            out("\r\n"); batch_transfer(jobs, "↓", lroot)

    HELP = (
        "\r\n"
        "  ── FRsync commands ──   (anything else you type goes straight to the MUD)\r\n"
        "  type '/' to see commands (they narrow as you type); Tab completes a\r\n"
        "  command or a file-name argument (local or remote)\r\n"
        "\r\n"
        "   Local folder\r\n"
        "     /lcd <dir>        set your local folder (downloads land here)\r\n"
        "     /lpwd             print the local folder\r\n"
        "     /lls              list the local folder\r\n"
        "\r\n"
        "   Remote folder\r\n"
        "     /rcd <dir>        set the remote folder used for transfers\r\n"
        "\r\n"
        "   Transfer files   (names or globs: *.c, cloud*.c, *.* — space-separated)\r\n"
        "     /download f [..]  get file(s)    remote → local   (alias /get)\r\n"
        "     /upload   f [..]  send file(s)   local → remote   (alias /put)\r\n"
        "                       …or drag files onto this window to upload them\r\n"
        "\r\n"
        "   Sync a whole folder tree   (recursive: every file + subfolder)\r\n"
        "     /push             upload everything    local → remote\r\n"
        "     /pull             download everything  remote → local\r\n"
        "     /diff [file|glob] what differs between local and the MUD; no args\r\n"
        "                       diffs the whole folder (only changed files shown)\r\n"
        "\r\n"
        "   Remove & rename on the MUD   (asks before it deletes anything)\r\n"
        "     /rm f [..]        delete remote file(s) or empty dir(s)   (alias /del)\r\n"
        "     /mv <src> <dst>   rename / move a remote file             (alias /rename)\r\n"
        "\r\n"
        "   Build & test   (pushed .c files auto-reload; see /autoupdate)\r\n"
        "     /update [-o] f    recompile file(s) in-game; shows compile errors\r\n"
        "     /goto <file>      teleport into a room by its file\r\n"
        "     /clone <file>     clone an object to test it\r\n"
        "     /lint [dir] [-v]  check the area's exits/inherits/loads resolve on the\r\n"
        "                       MUD; -v also lists references it couldn't resolve\r\n"
        "     /errors [n|all]   show the last n lines of your MUD error log\r\n"
        "     /autoupdate on|off  toggle auto-reload after each push\r\n"
        "\r\n"
        "   Session\r\n"
        "     /where            show current local + remote folders\r\n"
        "     /help             show this help\r\n"
        "     /quit             leave FRsync\r\n"
        "\r\n")

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
            if not a: out("\r\n  usage: /download <file|glob> [..]   (e.g. *.c)\r\n"); return True
            jobs = []
            for pat in a:
                t = expand_remote_arg(mud, pat, state["rcwd"])
                if not t: out(f"\r\n  ✗ {pat}: no remote files match\r\n")
                for rp in t:
                    sz = mud.file_size(rp)
                    if sz < 0: out(f"\r\n  ✗ {os.path.basename(rp)}: not found on MUD\r\n"); continue
                    base = os.path.basename(rp); lp = os.path.join(state["local"], base)
                    def fetch(progress, rp=rp, sz=sz, lp=lp):
                        data = mud.read_file_bytes(rp, sz, progress=progress)
                        open(lp, "wb").write(data); return len(data) == sz
                    jobs.append({"base": base, "size": sz,
                                 "exists": os.path.exists(lp), "run": fetch})
            if not jobs: out("\r\n"); return True
            out("\r\n"); batch_transfer(jobs, "↓", state["local"])
        elif cmd in ("/upload", "/put"):
            if not a: out("\r\n  usage: /upload <file|glob> [..]   (e.g. *.c)\r\n"); return True
            paths = []
            for pat in a:
                m = expand_local_arg(state["local"], pat)
                if not m: out(f"\r\n  ✗ {pat}: no local files match\r\n")
                for lp in m:
                    if not os.path.isfile(lp): out(f"\r\n  ✗ {os.path.basename(lp)}: no such local file\r\n"); continue
                    paths.append(lp)
            upload_local_files(paths)
        elif cmd == "/push": do_sync("up")
        elif cmd == "/pull": do_sync("down")
        elif cmd == "/lint":
            verbose = any(x in ("-v", "--verbose") for x in a)
            dirs = [x for x in a if not x.startswith("-")]
            do_lint(dirs[0] if dirs else None, verbose=verbose)
        elif cmd == "/diff":
            if not a:
                diff_all()                     # no args → diff the whole folder tree
            else:
                for arg in a:
                    if _has_glob(arg):         # e.g. /diff *.c → diff each local match
                        rels = [os.path.relpath(p, state["local"])
                                for p in expand_local_arg(state["local"], arg)]
                        if not rels: out(f"\r\n  ✗ {arg}: no local files match\r\n")
                        for rel in rels: show_diff(rel)
                    else:
                        show_diff(arg)
        elif cmd == "/update":
            inherit = bool(a) and a[0] in ("-o", "--inherit")
            names = a[1:] if inherit else a
            if names:
                rpaths = [resolve_remote(x, state["rcwd"]) for x in names]
            elif state["last_push"]:
                rpaths = state["last_push"]
            else:
                out("\r\n  usage: /update [-o] <file> [..]   (or push something first)\r\n"); return True
            out("\r\n"); reload_files(rpaths, inherit=inherit)
        elif cmd == "/goto":
            if not a: out("\r\n  usage: /goto <file|room>\r\n"); return True
            tgt = resolve_remote(a[0], state["rcwd"])
            if tgt.endswith(".c"): tgt = tgt[:-2]
            mud._flush(); mud.send(f"goto {tgt}")
            out("\r\n" + mud.drain(1.5))
        elif cmd == "/clone":
            if not a: out("\r\n  usage: /clone <file> [..]\r\n"); return True
            rpaths = [resolve_remote(x, state["rcwd"]) for x in a]
            mud._flush(); mud.send("clone " + " ".join(rpaths))
            out("\r\n" + mud.drain(1.5))
        elif cmd in ("/errors", "/log"):
            show_errors(a)
        elif cmd == "/autoupdate":
            state["autoupdate"] = (a[0] == "on") if a and a[0] in ("on", "off") \
                                  else not state["autoupdate"]
            out(f"\r\n  auto-update after push: {'on' if state['autoupdate'] else 'off'}\r\n")
        elif cmd in ("/rm", "/del", "/delete"):
            if not a: out("\r\n  usage: /rm <file|glob> [..]   (deletes on the MUD)\r\n"); return True
            targets = []
            for pat in a:
                t = expand_remote_arg(mud, pat, state["rcwd"])
                # expand_remote_arg only lists files for a glob; a plain name may be
                # a directory, so resolve it directly and let file_size classify.
                for rp in (t or [resolve_remote(pat, state["rcwd"])]):
                    if rp not in targets: targets.append(rp)
            existing = [(rp, mud.file_size(rp)) for rp in targets]
            missing = [rp for rp, sz in existing if sz == -1]          # -1 = not found
            live = [(rp, sz) for rp, sz in existing if sz >= 0 or sz == -2]  # file | dir
            for rp in missing: out(f"\r\n  ✗ {rp}: not found on MUD")
            if not live: out("\r\n"); return True
            out("\r\n  about to DELETE on the MUD:\r\n")
            for rp, sz in live:
                out(f"    {rp}{'/  (dir)' if sz == -2 else ''}\r\n")
            if not confirm_key(f"  delete {len(live)} item(s)? [y/N] ", default_yes=False):
                out("  cancelled — nothing deleted.\r\n"); return True
            done = 0
            for rp, sz in live:
                ok = mud.rmdir(rp) if sz == -2 else mud.rm(rp)
                if ok: done += 1; out(f"    ✓ deleted {rp}\r\n")
                else: out(f"    ✗ could not delete {rp}"
                          f"{' (dir not empty?)' if sz == -2 else ''}\r\n")
            out(f"  deleted {done}/{len(live)} item(s).\r\n")
        elif cmd in ("/mv", "/rename", "/move"):
            if len(a) != 2: out("\r\n  usage: /mv <src> <dst>   (renames a remote file)\r\n"); return True
            src = resolve_remote(a[0], state["rcwd"]); dst = resolve_remote(a[1], state["rcwd"])
            if mud.file_size(src) < 0: out(f"\r\n  ✗ {src}: not found on MUD\r\n"); return True
            out(f"\r\n  move on the MUD:  {src}  →  {dst}\r\n")
            if mud.file_size(dst) >= 0:
                if not confirm_key(f"  {dst} exists — overwrite? [y/N] ", default_yes=False):
                    out("  cancelled.\r\n"); return True
                mud.rm(dst)                       # rename won't clobber, so clear it first
            out(f"  ✓ moved to {dst}\r\n" if mud.rename(src, dst)
                else "  ✗ move failed.\r\n")
        else:
            out(f"\r\n  unknown /command: {cmd}  (try /help)\r\n")
        return True

    def handle_line(line):
        """Process one submitted input line. Returns False to quit the shell."""
        # drag-and-drop: the terminal pastes the path(s) of dropped files onto the
        # input line — if the whole line is existing local file(s), offer to upload
        drop = dropped_files(line)
        if drop:
            return offer_upload(drop)
        # keep remote cwd in sync when you cd around the file tree
        if line.strip().startswith("cd ") or line.strip() == "cd":
            arg = line.strip()[2:].strip()
            state["rcwd"] = (home if not arg else resolve_remote(arg, state["rcwd"]))
        if line.startswith("/"):
            return do_local(line)
        mud.send_text(line)                    # UTF-8: allow typing Unicode
        return True

    out(do_where_banner(state))
    # auto-look on entry so you can see where you are right away
    mud._flush(); mud.send("look")
    out("\r\n" + mud.drain(1.0))

    # Keep what you're typing pinned at the bottom and redraw it when async MUD
    # output arrives, so input and server text never interleave. The line editor
    # (input buffer + Up/Down history) is shared by both raw-mode loops below —
    # POSIX (termios) and Windows (msvcrt); the cooked fallback uses only its
    # echo_output and submits whole lines directly.
    # Tab-complete a file-name argument: list the local folder for upload-side
    # commands, the remote folder (over the MUD) for download-side ones, honouring
    # a dir prefix in the partial (e.g. "rooms/hu") and offering only dirs to /lcd,
    # /rcd. Called on demand (Tab), so the per-keystroke hint stays cheap+local.
    REMOTE_ARG = {"/download", "/get", "/rm", "/del", "/delete", "/mv", "/rename",
                  "/move", "/update", "/goto", "/clone", "/rcd"}
    def complete_arg(cmd, partial):
        dpart, _, base = partial.rpartition("/")
        dirs_only = cmd in ("/lcd", "/rcd")
        try:
            if cmd in REMOTE_ARG:
                ents = [(n, sz == -2) for n, sz in mud.listdir(resolve_remote(dpart or ".", state["rcwd"]))]
            else:
                ld = os.path.join(state["local"], dpart)
                ents = [(n, os.path.isdir(os.path.join(ld, n))) for n in os.listdir(ld)]
        except Exception:
            return []
        prefix = dpart + "/" if dpart else ""
        return sorted(prefix + n + ("/" if is_dir else "")
                      for n, is_dir in ents
                      if n.startswith(base) and (base[:1] == "." or not n.startswith("."))
                      and (not dirs_only or is_dir))

    editor = LineEditor(out, handle_line, commands=SLASH_COMMANDS, arg_completer=complete_arg)

    def pump_socket(sk):
        """Print pending MUD output and redraw the pinned input. False = closed."""
        data = sk.recv(65536)
        if not data:
            out("\r\n*** MUD closed the connection. ***\r\n"); return False
        text = mud._strip_iac(data)
        if text:
            editor.echo_output(text)
        return True

    _termios, _tty = termios, tty   # locals so the None-guards below narrow cleanly

    # ---- POSIX raw mode: select() on stdin+socket, decode UTF-8 byte stream ----
    if _termios is not None and _tty is not None and sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old_attr = _termios.tcgetattr(fd)
        in_carry = b""        # incomplete trailing UTF-8 bytes from a read
        _tty.setcbreak(fd)    # char-at-a-time, no echo (we echo manually); Ctrl-C still signals
        restore_term = lambda: _termios.tcsetattr(fd, _termios.TCSADRAIN, old_attr)
        def _ask_key_raw(prompt):
            out(prompt)
            try: ch = os.read(fd, 1)        # single keypress, no Enter needed
            except OSError: ch = b""
            ans = ch.decode("latin-1", "replace")
            out((ans if ans.isprintable() else "") + "\r\n")
            return ans.lower()
        ask_key = _ask_key_raw
        def feed(s):
            """Translate a decoded keystroke string (incl. ANSI escapes) to key
            events. Returns False to quit the shell."""
            j = 0
            while j < len(s):
                c = s[j]
                if c in ("\r", "\n"):
                    if editor.key("enter") is False: return False
                elif c in ("\x7f", "\x08"):
                    editor.key("backspace")
                elif c == "\x04":
                    if editor.key("eof") is False: return False
                elif c == "\x15":
                    editor.key("clear")
                elif c == "\t":                    # Tab: complete the /command
                    editor.key("tab")
                elif c == "\x1b":                  # escape seq: Up/Down recall history
                    j += 1; final = ""
                    if j < len(s) and s[j] == "[":
                        j += 1
                        while j < len(s) and not ("@" <= s[j] <= "~"): j += 1
                        if j < len(s): final = s[j]
                    if final == "A": editor.key("up")
                    elif final == "B": editor.key("down")
                elif c >= " ":
                    editor.key("text", c)
                j += 1                             # other control chars: ignored
            return True
        try:
            while True:
                sk = mud.s              # re-read each loop: a transfer may have reconnected
                try:
                    r, _, _ = select.select([sys.stdin, sk], [], [], 0.3)
                except (ValueError, OSError):
                    out("\r\n*** MUD connection lost. ***\r\n"); break
                if sk in r and not pump_socket(sk): break
                if sys.stdin in r:
                    chunk = os.read(fd, 4096)
                    if not chunk: break                # EOF
                    buf = in_carry + chunk
                    hold = _utf8_incomplete_tail(buf)
                    in_carry = buf[len(buf) - hold:] if hold else b""
                    s = (buf[:len(buf) - hold] if hold else buf).decode("utf-8", "replace")
                    if feed(s) is False: break
        except KeyboardInterrupt:
            pass
        finally:
            try: restore_term()          # always restore the terminal, even on error
            except Exception: pass
            out("\r\nDisconnected.\r\n"); mud.close()
        return

    # ---- Windows raw mode: select() on the socket only (Windows can't select a
    # console handle), poll the keyboard via msvcrt. Works in Windows Terminal,
    # WARP, PowerShell, cmd — anywhere stdin is a real console. ----
    if msvcrt is not None and sys.stdin.isatty():
        # ANSI escapes + UTF-8 stdout were enabled once at startup
        # (_enable_windows_console), so the \r\x1b[K redraws and non-ASCII echo
        # render correctly here without per-command setup.
        def _ask_key_win(prompt):
            out(prompt)
            try: ch = msvcrt.getwch()       # type: ignore[union-attr]  single keypress, no Enter
            except Exception: ch = ""
            if ch in ("\x00", "\xe0"):      # arrow/function key: drop the trailing code
                try: msvcrt.getwch()        # type: ignore[union-attr]
                except Exception: pass
                ch = ""
            out((ch if ch.isprintable() else "") + "\r\n")
            return ch.lower()
        ask_key = _ask_key_win
        try:
            while True:
                sk = mud.s              # re-read each loop: a transfer may have reconnected
                try:
                    r, _, _ = select.select([sk], [], [], 0.05)
                except (ValueError, OSError):
                    out("\r\n*** MUD connection lost. ***\r\n"); break
                if r and not pump_socket(sk): break
                quit_now = False
                while msvcrt.kbhit():               # type: ignore[union-attr]  drain keyboard, no block
                    ch = msvcrt.getwch()            # type: ignore[union-attr]
                    if ch in ("\x00", "\xe0"):      # arrow / function key prefix
                        code = msvcrt.getwch()      # type: ignore[union-attr]
                        if code == "H": editor.key("up")
                        elif code == "P": editor.key("down")
                    elif ch in ("\r", "\n"):
                        if editor.key("enter") is False: quit_now = True; break
                    elif ch in ("\x08", "\x7f"):
                        editor.key("backspace")
                    elif ch == "\x03":              # Ctrl-C
                        raise KeyboardInterrupt
                    elif ch in ("\x04", "\x1a"):    # Ctrl-D / Ctrl-Z on empty line -> quit
                        if editor.key("eof") is False: quit_now = True; break
                    elif ch == "\x15":
                        editor.key("clear")
                    elif ch == "\t":               # Tab: complete the /command
                        editor.key("tab")
                    elif ch >= " ":
                        editor.key("text", ch)
                if quit_now: break
        except KeyboardInterrupt:
            pass
        finally:
            out("\r\nDisconnected.\r\n"); mud.close()
        return

    # ---- Plain line-mode fallback: no raw terminal (pipes, redirected input).
    # stdin can't go through select() on Windows, so a daemon thread reads lines
    # onto a queue while the main loop selects on the socket — one code path that
    # works on both OSes. ----
    line_q = queue.Queue()
    def _stdin_reader():
        try:
            for line in sys.stdin:
                line_q.put(line)
        except Exception:
            pass
        line_q.put(None)                  # EOF sentinel
    threading.Thread(target=_stdin_reader, daemon=True).start()
    def _ask_key_cooked(prompt):
        out(prompt)
        line = line_q.get()               # block for the next typed/piped line
        return "" if line is None else line.strip().lower()[:1]
    ask_key = _ask_key_cooked
    try:
        while True:
            sk = mud.s              # re-read each loop: a transfer may have reconnected
            try:
                r, _, _ = select.select([sk], [], [], 0.1)
            except (ValueError, OSError):
                out("\r\n*** MUD connection lost. ***\r\n"); break
            if r and not pump_socket(sk): break
            done = False
            while True:                    # drain whatever the reader has queued
                try: line = line_q.get_nowait()
                except queue.Empty: break
                if line is None: done = True; break        # stdin EOF
                if not handle_line(line.rstrip("\n")): done = True; break
            if done: break
    except KeyboardInterrupt:
        pass
    finally:
        out("\r\nDisconnected.\r\n"); mud.close()

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
        "        (or just drag files onto this window to upload them)\r\n"
        "        /download <file>    fetch a remote file -> this computer\r\n"
        "        /push               upload the whole folder tree (recursive)\r\n"
        "        /pull               download the whole folder tree (recursive)\r\n"
        "        (pushed .c files auto-reload in-game; /errors shows compile errors)\r\n"
        "        /goto <file>        teleport into a room  ·  /clone <file>  test an object\r\n"
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

def push_one(mud, lp, rp, made, progress=None):
    """Push a single local file to the server; return True if verified."""
    ensure_remote_dirs(mud, rp, made)
    data = open(lp, "rb").read()
    mud.write_file_chunks(rp, data, progress=progress)
    return verify_remote(mud, rp, data)

def watch_reload(mud, cfiles):
    """Recompile the pushed .c files in-game and print the outcome — compile
    errors as file:line: message, else a reloaded count. Used by `watch` so a
    save reloads the object live. Indented under the ↑ upload lines."""
    if not cfiles:
        return
    try:
        res = mud.update(cfiles)
    except Exception as e:
        print(f"           ✗ update failed: {str(e)[:60]}"); return
    bad = {p for p, _, _ in res["errors"]} | set(res["failed"])
    for p, ln, msg in res["errors"]:
        print(f"           ✗ {os.path.basename(p)}:{ln}: {msg}")
    for p in res["failed"]:
        print(f"           ✗ {os.path.basename(p)}: failed to load")
    good = len(cfiles) - len(bad)
    if good:
        print(f"           ⟳ reloaded {good} file(s)")

def cmd_watch(args):
    """Watch a local dir and auto-push files as they're saved/dropped in —
    the closest thing to a 'synced folder'. Only files that change AFTER
    start are pushed (run `push` first to sync what's already there).
    Each pushed .c is then recompiled in-game (update) and any compile error is
    printed as file:line: message — so save-in-editor reloads the object live,
    with no MUD-window typing. Pass --no-update to push without reloading.
    Server files are never deleted."""
    mud = connect(args)
    local = os.path.abspath(args.local)
    remote = args.remote.rstrip("/")
    exts = parse_exts(args.ext)
    auto_update = not getattr(args, "no_update", False)
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
    print(f"  ({len(baseline)} files baselined; save/drop files here to upload"
          f"{' + auto-reload' if auto_update else ''}. Ctrl-C to stop.)\n")
    last_ping = time.time()
    try:
        while True:
            time.sleep(args.interval)
            cur = scan()
            changed = [rel for rel, sig in cur.items() if baseline.get(rel) != sig]
            if changed:
                pushed = []
                for rel in sorted(changed):
                    lp = os.path.join(local, rel)
                    rp = f"{remote}/{rel}"
                    ts = time.strftime("%H:%M:%S")
                    try:
                        ok = push_one(mud, lp, rp, made)
                        print(f"  [{ts}] {'OK ' if ok else 'FAIL'}  ↑ {rel}")
                        if ok: pushed.append(rp)
                    except Exception as e:
                        print(f"  [{ts}] FAIL ↑ {rel}: {str(e)[:80]}")
                if auto_update:
                    watch_reload(mud, [p for p in pushed if p.endswith(".c")])
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

# The creator `update` command prints one of these per file it (re)compiles:
#   Loaded /w/x/room.c                         -> freshly loaded ok
#   Updated x's workroom(/w/x/workroom).       -> reloaded an already-live object
#   E /w/x/room.c at line 8 : syntax error ... -> a compile error on that line
#   Failed to load /w/x/room.c, error: ...     -> the object did not load
_UPD_ERR  = re.compile(r"^E (\S+) at line (\d+)\s*:\s*(.+?)\s*$", re.M)
_UPD_FAIL = re.compile(r"^Failed to load (\S+?),?\s", re.M)
_UPD_LOAD = re.compile(r"^Loaded (\S+)", re.M)
_UPD_UPD  = re.compile(r"^Updated .*?\((/\S+?)\)", re.M)

def parse_update_output(text):
    """Classify `update` output. Returns
    {'errors': [(path, line, msg)], 'failed': [path], 'loaded': [path]}.
    `failed` excludes paths already listed in `errors` (same file)."""
    errors = [(p, int(n), m) for p, n, m in _UPD_ERR.findall(text)]
    err_paths = {p for p, _, _ in errors}
    failed = [p for p in _UPD_FAIL.findall(text) if p not in err_paths]
    loaded = _UPD_LOAD.findall(text) + _UPD_UPD.findall(text)
    return {"errors": errors, "failed": failed, "loaded": loaded}

def unified_diff_lines(remote_text, local_text, label):
    """Unified diff with the MUD copy as the 'before' and your local copy as the
    'after' — so a '+' line is what your local would push and a '-' line is what's
    live on the MUD but missing locally (e.g. someone edited it with `ed`).
    Returns a list of lines with no trailing newlines (empty if identical)."""
    return list(difflib.unified_diff(
        remote_text.splitlines(), local_text.splitlines(),
        fromfile=f"{label} (MUD)", tofile=f"{label} (local)", lineterm=""))

def colorize_diff(line, tty=True):
    """Colour one unified-diff line for a terminal (headers bold, +green, -red,
    @@ cyan); returns it unchanged when not a tty."""
    if not tty: return line
    if line.startswith(("+++", "---")): return f"\x1b[1m{line}\x1b[0m"   # file headers
    if line.startswith("+"):  return f"\x1b[32m{line}\x1b[0m"
    if line.startswith("-"):  return f"\x1b[31m{line}\x1b[0m"
    if line.startswith("@@"): return f"\x1b[36m{line}\x1b[0m"
    return line

# ---------------------------------------------------------------- area lint
# Verify that the paths an area's LPC code references (room exits, inherits,
# clone/load targets, includes) actually resolve to a file — catching broken
# exits and typos before players hit them. Paths are usually #define macros or
# macro+"literal" expressions, so we build a define table and evaluate them.

_DEFINE_RX = re.compile(r'^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+?)[ \t]*$', re.M)

def collect_defines(texts):
    """Build {NAME: raw_value} from #define lines across the given file texts.
    Object-like macros only (a name followed by '(' is function-like — skipped).
    First definition of a name wins."""
    out = {}
    for t in texts:
        for name, val in _DEFINE_RX.findall(t):
            out.setdefault(name, val.strip())
    return out

def _split_top(s, sep):
    """Split `s` on single-char `sep` at paren/bracket depth 0, outside strings."""
    parts, buf, depth, instr, esc = [], [], 0, False, False
    for c in s:
        if instr:
            buf.append(c)
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': instr = False
            continue
        if c == '"': instr = True; buf.append(c); continue
        if c in "([{": depth += 1
        elif c in ")]}": depth -= 1
        if c == sep and depth == 0: parts.append("".join(buf)); buf = []
        else: buf.append(c)
    parts.append("".join(buf))
    return parts

def _strip_parens(e):
    e = e.strip()
    while e.startswith("(") and e.endswith(")"):
        depth = 0; ok = True
        for i, c in enumerate(e):
            if c == "(": depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and i != len(e) - 1: ok = False; break
        if ok: e = e[1:-1].strip()
        else: break
    return e

def eval_str_expr(expr, defines, _depth=0):
    """Evaluate an LPC string expression built from "literals" and #define names
    joined by '+' (with parens), expanding macros recursively. Returns the string,
    or None if any part is a variable/function/unknown macro (i.e. not resolvable
    statically)."""
    if _depth > 25: return None
    expr = _strip_parens(expr)
    if not expr: return None
    out = []
    for p in _split_top(expr, "+"):
        p = p.strip()
        if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
            out.append(p[1:-1])
        elif re.fullmatch(r"[A-Za-z_]\w*", p) and p in defines:
            sub = eval_str_expr(defines[p], defines, _depth + 1)
            if sub is None: return None
            out.append(sub)
        else:
            return None
    return "".join(out)

def _strip_c_comments(text):
    """Drop /*…*/ and //… comments so commented-out or prose references aren't
    linted. Block comments become equal newlines so line numbers stay accurate."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)

def _find_calls(text, funcname):
    """Yield (args, offset) for each funcname(...) call — args is the top-level
    comma-split argument list (raw strings), offset is where the call starts."""
    for m in re.finditer(r"\b" + re.escape(funcname) + r"\s*\(", text):
        depth, j, instr, esc = 1, m.end(), False, False
        while j < len(text) and depth:
            c = text[j]
            if instr:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': instr = False
            elif c == '"': instr = True
            elif c == "(": depth += 1
            elif c == ")": depth -= 1
            j += 1
        yield _split_top(text[m.end():j - 1], ","), m.start()

# (function, argument index, reported kind) for each call whose arg names a file.
_REF_CALLS = [("add_exit", 1, "exit"), ("clone_object", 0, "clone_object"),
              ("add_clone", 0, "add_clone"), ("load_object", 0, "load_object"),
              ("find_object", 0, "find_object")]

_FUNC_RX = re.compile(r"\b([A-Za-z_]\w*)\s*\(([^;{)]*)\)\s*\{")
_NOT_FUNCS = {"if", "while", "for", "foreach", "switch", "catch", "do", "return"}
# LPC type/modifier keywords that begin a parameter declaration.
_LPC_TYPES = {"void", "int", "string", "object", "mapping", "mixed", "float",
              "status", "buffer", "function", "array", "class", "varargs",
              "nomask", "static", "private", "public", "protected", "nosave", "ref"}

def _is_param_decl(arg):
    """True if `arg` looks like a parameter declaration (`string dest`) rather
    than a call argument — i.e. a function definition/prototype matched as a call
    to itself. Its first token is an LPC type keyword and a name follows."""
    toks = arg.replace("*", " ").split()
    return len(toks) >= 2 and toks[0] in _LPC_TYPES

def _param_names(paramstr):
    """Names of a function's parameters (last identifier of each `type name`)."""
    names = []
    for p in _split_top(paramstr, ","):
        ids = re.findall(r"[A-Za-z_]\w*", p)
        if ids: names.append(ids[-1])
    return names

def _brace_body(text, open_idx):
    """Substring between the '{' at text[open_idx] and its matching '}'."""
    depth, j, instr, esc = 0, open_idx, False, False
    while j < len(text):
        c = text[j]
        if instr:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': instr = False
        elif c == '"': instr = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0: return text[open_idx + 1:j]
        j += 1
    return text[open_idx + 1:]

def find_ref_wrappers(texts):
    """Discover project helper functions that forward one of their parameters
    straight into a known reference call — e.g. add_dream_exit(dir,dest,type)
    that calls add_exit(dir,dest,type). Returns {wrapper_name: (kind, arg_index)}
    so calls to the wrapper get linted like the call they wrap. Without this an
    area that wires its exits through a custom helper would go largely unchecked."""
    wrappers = {}
    for text in texts:
        for m in _FUNC_RX.finditer(text):
            name, params = m.group(1), m.group(2)
            if name in _NOT_FUNCS or name in {c[0] for c in _REF_CALLS}:
                continue
            pnames = _param_names(params)
            if not pnames:
                continue
            body = _brace_body(text, m.end() - 1)
            for fn, idx, kind in _REF_CALLS:
                for args, _off in _find_calls(body, fn):
                    if len(args) > idx and args[idx].strip() in pnames:
                        wrappers[name] = (kind, pnames.index(args[idx].strip()))
    return wrappers

def extract_references(text, defines, wrappers=None):
    """Find file references in one (comment-stripped) LPC source. Returns a list
    of {kind, expr, path, line}: `path` is the resolved target normalised to a
    real '<base>.c' file (FR omits .c on some inherits/loads), or None when the
    expression can't be resolved statically (a variable — skipped by the linter).
    `wrappers` (from find_ref_wrappers) adds project exit/clone/load helpers."""
    refs = []
    def add(kind, expr, off):
        resolved = eval_str_expr(expr, defines)
        path = None
        if resolved and resolved.startswith("/"):
            base = resolved[:-2] if resolved.endswith(".c") else resolved.rstrip("/")
            path = base + ".c"
        refs.append({"kind": kind, "expr": expr.strip(),
                     "path": path, "line": text.count("\n", 0, off) + 1})
    for m in re.finditer(r"\binherit\s+([^;{]+);", text):
        add("inherit", m.group(1), m.start())
    calls = [(fn, idx, kind) for fn, idx, kind in _REF_CALLS]
    calls += [(fn, idx, kind) for fn, (kind, idx) in (wrappers or {}).items()]
    for fn, idx, kind in calls:
        for args, off in _find_calls(text, fn):
            arg = args[idx].strip() if len(args) > idx else ""
            # skip the function's OWN definition/prototype: there the "argument"
            # is a parameter declaration ("string dest"), not a real call arg.
            if arg and not _is_param_decl(arg):
                add(kind, args[idx], off)
    for m in re.finditer(r'#\s*include\s+"([^"]+)"', text):
        p = m.group(1)
        refs.append({"kind": "include", "expr": p,
                     "path": p if p.startswith("/") else None,
                     "line": text.count("\n", 0, m.start()) + 1})
    return refs

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
    _enable_windows_console()   # ANSI + UTF-8 stdout for progress bars on Windows
    # Shared flags live on a parent parser so they're accepted in BOTH positions —
    # before the subcommand (`frsync -y push a b`) and after the positional args
    # (`frsync push a b -y`). argument_default=SUPPRESS means an unspecified flag
    # leaves the namespace untouched, so the parent's copy never clobbers a value
    # set by the other; p.set_defaults() below supplies the baselines.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--char", metavar="NAME", help="creator name (else prompts)")
    common.add_argument("--host", help=f"MUD host (default {DEF_HOST})")
    common.add_argument("--port", type=int, help=f"MUD port (default {DEF_PORT})")
    common.add_argument("--ext", metavar="LIST", help="only these extensions, e.g. .c,.h (dirs only)")
    common.add_argument("--delete", action="store_true", help="push: remove dest files missing at source")
    common.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    common.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = argparse.ArgumentParser(
        prog="frsync", parents=[common],
        description="Sync files between your machine and the Final Realms MUD "
                    "over the normal creator login — no FTP required.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{status,push,pull,mirror}")

    sp = sub.add_parser("status", parents=[common], help="compare local vs MUD, change nothing")
    sp.add_argument("local", help="local file or directory")
    sp.add_argument("remote", help="server file or directory")
    sp = sub.add_parser("push", parents=[common], help="upload local -> MUD (verified)")
    sp.add_argument("local", help="local file or directory")
    sp.add_argument("remote", help="server file or directory")
    sp = sub.add_parser("pull", parents=[common], help="download MUD -> local (verified)")
    sp.add_argument("remote", help="server file or directory")
    sp.add_argument("local", help="local file or directory")
    sp = sub.add_parser("watch", parents=[common], help="auto-push a local folder as files change (synced-folder mode)")
    sp.add_argument("local", help="local directory to watch")
    sp.add_argument("remote", help="server directory to upload into")
    sp.add_argument("--interval", type=float, default=2.0, help="poll seconds (default 2)")
    sp.add_argument("--no-update", action="store_true", help="just push; don't recompile (update) each pushed .c in-game")
    sp = sub.add_parser("connect", parents=[common], help="interactive MUD shell with /download and /upload")
    sp.add_argument("--dir", help="initial local folder for transfers (default: current dir)")
    mp = sub.add_parser("mirror", parents=[common], help="bulk-download server subtrees, resumable")
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
    # The shared flags use SUPPRESS (so a flag given in one position never gets
    # reset by the parser in the other), so fill the baselines for any not given.
    for k, v in dict(char=None, host=DEF_HOST, port=DEF_PORT, ext=None,
                     delete=False, dry_run=False, yes=False).items():
        if not hasattr(args, k):
            setattr(args, k, v)
    {"status": cmd_status, "push": cmd_push, "pull": cmd_pull,
     "watch": cmd_watch, "mirror": cmd_mirror, "connect": cmd_connect}[args.cmd](args)

if __name__ == "__main__":
    main()
