#!/usr/bin/env python3
"""Offline tests for the frsync hardening changes — no MUD connection required.

Covers the pure-Python logic of the four hardening items:
  1. crc32-based change detection beats order-independent byte_sum.
  2. _write_once waits for a split RC<n>RC sentinel, so echoed file content
     (e.g. a literal "Returns 1" comment) cannot spoof the write result.
  3. UTF-8 text (Norwegian, em dash, smart quotes) round-trips through the
     escape/transport layer and the MCP encode/decode rules.

Run:  python3 test_frsync.py        (also: load via a FRESH import so the
patched frsync.py on disk is exercised, not a stale long-running process.)

Live checks that genuinely need the server (crc32 efun against a known file,
uploading a 'Returns 1' file, UTF-8 round-trip over the wire) live in
validate_frsync_live.py and are gated on $FRCHAR/$FRPASS.
"""
import os, sys, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frsync


# --- helper: mimic how LPC un-escapes the string literal write_file() stores ---
def lpc_unescape(s):
    """Reverse esc_token: the driver parses the "..." literal we splice into
    write_file(), turning \\n \\t \\" \\\\ back into their bytes; high bytes and
    printable ASCII pass through unchanged (\\r was already dropped pre-chunk)."""
    out, i = [], 0
    repl = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(repl.get(s[i + 1], s[i + 1])); i += 2
        else:
            out.append(c); i += 1
    return "".join(out)


def transport_roundtrip(raw: bytes, budget=200) -> bytes:
    """Push `raw` through exactly the steps write_file_chunks uses (CRLF norm,
    latin-1 byte view, chunk_escaped), reassemble what the server would store,
    and return those bytes. Mirrors the real upload without a socket."""
    text = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("latin-1")
    chunks = list(frsync.chunk_escaped(text, budget)) or [("", 0)]
    stored = lpc_unescape("".join(esc for esc, _n in chunks))
    return stored.encode("latin-1")


def mcp_write_encode(content: str) -> bytes:
    """What mud_write now hands to the transport layer."""
    return content.encode("utf-8")


def mcp_read_decode(data: bytes) -> str:
    """What mud_read now returns to the model."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --------------------------------------------------------------------- tests
def test_crc_detects_reordered_same_bytesum():
    # Two payloads, identical length AND identical byte-sum, different order.
    a = b"AB\n"
    b = b"BA\n"
    assert len(a) == len(b)
    assert (sum(a) & 0x7fffffffffffffff) == (sum(b) & 0x7fffffffffffffff), \
        "precondition: byte_sum must collide for this test to mean anything"
    # The OLD check (length + byte_sum) would call these 'same'. crc32 differs:
    assert (zlib.crc32(a) & 0xffffffff) != (zlib.crc32(b) & 0xffffffff)


def test_local_size_crc_flags_reorder(tmpdir):
    pa = os.path.join(tmpdir, "a.c"); pb = os.path.join(tmpdir, "b.c")
    # Same characters, reordered lines -> same length, same byte_sum.
    open(pa, "wb").write(b"int x;\nint y;\n")
    open(pb, "wb").write(b"int y;\nint x;\n")
    la, sa = frsync.local_size_sum(pa)
    lb, sb = frsync.local_size_sum(pb)
    assert (la, sa) == (lb, sb), "byte_sum is blind to this reorder (as expected)"
    ca = frsync.local_size_crc(pa)
    cb = frsync.local_size_crc(pb)
    assert ca[0] == cb[0]            # same length
    assert ca[1] != cb[1]            # crc distinguishes them -> diff, not 'same'


def test_jamcrc_matches_driver():
    # The MudOS crc32() efun is CRC-32/JAMCRC (zlib polynomial, NO final XOR).
    # These are real values read off the live driver — frsync.jamcrc must equal
    # them, or verify_remote/plan would mismatch on every file.
    assert frsync.jamcrc(b"abc") == 3403398717          # 0xCADBBE3D
    assert frsync.jamcrc(b"123456789") == 873187033     # 0x340BC6D9
    assert frsync.jamcrc(b"") == 4294967295             # 0xFFFFFFFF
    # And the exact self-test payload uploaded live: driver crc32(read_file)
    # returned 3438759988 for these bytes.
    payload = (
        "// frsync self-test — safe to delete\n"
        "// Returns 1   <- literal sentinel-bait; must not fool the writer\n"
        "// Returns 0   and another one for good measure\n"
        "string note = \"blåbær — æ ø å — “smart” ‘quotes’ …\";\n"
        "int x; // ok\n"
    ).encode("utf-8")
    assert frsync.jamcrc(frsync.local_norm(payload)) == 3438759988

def test_jamcrc_is_unsigned():
    # jamcrc always returns a non-negative 32-bit value (the driver may report a
    # signed int; our masking normalises both sides to the same space).
    for s in (b"", b"a", b"x" * 1000, b"\xff\x00\x80mixed"):
        v = frsync.jamcrc(s)
        assert 0 <= v <= 0xffffffff


def test_write_once_ignores_echoed_returns(monkey_self):
    # The server echoes the command (and file content). A chunk holding the
    # literal text 'Returns 1' must NOT be read as the driver result; only the
    # real split sentinel RC<n>RC counts.
    chunk = "// Returns 1   (this line is part of the file content)\\n"
    echo = (f'exec int _r=write_file("/w/x/f.c", "{chunk}", 0); '
            f'return "R"+"C"+sprintf("%d",_r)+"R"+"C";\n'
            f'// Returns 1\n'           # echoed file content — must be ignored
            f'RC0RC\n')                 # the driver's real result: write refused
    fake = monkey_self(echo)
    res = frsync.Mud._write_once(fake, "/w/x/f.c", chunk, 0)
    # The echo holds "Returns 1" (in the chunk we sent) AND the real RC0RC
    # result. Reading 0, not 1, proves the sentinel wins over the echo.
    assert res == 0, f"sentinel must win over echoed 'Returns 1'; got {res!r}"
    # The command asks the driver for the split sentinel (emitted as "R"+"C"
    # so it can never appear contiguously in any echoed content).
    assert any('"R"+"C"' in line for line in fake.sent)


def test_write_once_reports_success(monkey_self):
    echo = ('exec int _r=write_file(...); ...\n'
            '// a comment that says Returns 1 for good measure\n'
            'RC1RC\n')
    fake = monkey_self(echo)
    assert frsync.Mud._write_once(fake, "/w/x/f.c", "data", 1) == 1


def test_write_once_lost_result_is_none(monkey_self):
    # No sentinel in the stream (lost/silent response) -> None so the caller
    # reconciles against the real file size instead of guessing.
    fake = monkey_self('exec ... echo only, no marker\n// Returns 1\n')
    assert frsync.Mud._write_once(fake, "/w/x/f.c", "data", 0) is None


def test_utf8_roundtrip_through_transport():
    samples = [
        "Norwegian: blåbær, æ ø å, Æ Ø Å, smørbrød",
        "Em dash — and en dash – plus ellipsis…",
        "Smart quotes: “hello” and ‘world’",
        '// Returns 1  — a comment containing the sentinel-bait phrase\n',
        "Mixed: rådhus “quote” — done. int x; // ok\n",
    ]
    for s in samples:
        stored = transport_roundtrip(mcp_write_encode(s))
        back = mcp_read_decode(stored)
        assert back == s, f"round-trip changed text:\n  in : {s!r}\n  out: {back!r}"


def test_no_silent_replacement():
    # The old path used .encode('latin-1','replace') which turned every char
    # above U+00FF into '?'. The new UTF-8 path must keep them intact.
    s = "résumé — naïve “smart” café blåbær"
    assert "?" not in mcp_read_decode(transport_roundtrip(mcp_write_encode(s)))


def test_latin1_fallback_on_read():
    # A legacy file that is NOT valid UTF-8 must still decode (latin-1) rather
    # than raise — mud_read falls back.
    legacy = b"caf\xe9 \xff-ish 8-bit bytes"   # 0xe9, 0xff are invalid UTF-8 lead
    out = mcp_read_decode(legacy)
    assert out == legacy.decode("latin-1")


def _stream_decode(raw: bytes, sizes):
    """Feed `raw` to Mud._strip_iac in chunks of the given sizes (cycled),
    mirroring the interactive read loop, and return (decoded_text, leftover)."""
    class _Sock:
        def sendall(self, b): pass            # swallow IAC negotiation replies
    m = object.__new__(frsync.Mud)
    m.s = _Sock(); m._rx_carry = b""
    out, i, k = [], 0, 0
    while i < len(raw):
        step = sizes[k % len(sizes)]; k += 1
        out.append(m._strip_iac(raw[i:i + step])); i += step
    return "".join(out), m._rx_carry


def test_stream_utf8_split_across_reads():
    # The interactive shell decodes the live MUD stream; a multibyte char split
    # across two recv() reads must still render, not turn into mojibake.
    text = "floors — blåbær & smørbrød. 日本語 ok.\r\n"
    raw = text.encode("utf-8")
    got, carry = _stream_decode(raw, [1])          # byte-at-a-time = worst case
    assert got == text and carry == b"", (got, carry)
    for sizes in ([2], [3], [5], [7, 1, 4]):
        got, carry = _stream_decode(raw, sizes)
        assert got == text and carry == b"", (sizes, got, carry)


def test_stream_iac_inside_multibyte_char():
    # A telnet negotiation (IAC WILL ECHO) injected between the two bytes of 'ø'
    # must be stripped and the char reassembled.
    IAC, WILL, ECHO = 255, 251, 1
    raw = b"sm\xc3" + bytes([IAC, WILL, ECHO]) + b"\xb8r \xe2\x80\x94"   # "smør —"
    got, carry = _stream_decode(raw, [len(raw)])
    assert got == "smør —" and carry == b"", (got, carry)


def test_write_command_fits_line_budget():
    # A full-size chunk plus its exec wrapper must stay within PUSH_CMD_BUDGET.
    # If the wrapper reserve is too small (it once was 40, but the real wrapper
    # is ~75), a short remote path yields a near-max chunk whose command line is
    # truncated by the driver — the `return` is lost, _r is unused (a compile
    # error), the write never lands, and write_file_chunks resends forever.
    path = "/w/x/f.c"                       # short path -> largest chunk = worst case
    text = ("a" * 6000)                     # packs into a full first chunk
    budget = frsync.PUSH_CMD_BUDGET - len(path) - frsync.WRITE_WRAPPER_RESERVE
    chunk, _n = next(iter(frsync.chunk_escaped(text, budget)))
    for flag in (0, 1):                     # both flags produce the same length
        cmd = (f'exec int _r=write_file("{path}", "{chunk}", {flag}); '
               f'return "R"+"C"+sprintf("%d",_r)+"R"+"C";')
        assert len(cmd) <= frsync.PUSH_CMD_BUDGET, \
            f"wrapper reserve too small: command is {len(cmd)} > {frsync.PUSH_CMD_BUDGET}"


# --------------------------------------------------------------------- runner
class _FakeSelf:
    """Stand-in for a Mud instance: _write_once only touches _flush/send/expect."""
    def __init__(self, canned):
        self.sent, self._canned = [], canned
    def _flush(self): pass
    def send(self, line): self.sent.append(line)
    def expect(self, pattern, timeout=12): return self._canned


def main():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="frsync_test_")
    monkey_self = _FakeSelf
    tests = [
        ("crc detects reordered same-byte-sum",     lambda: test_crc_detects_reordered_same_bytesum()),
        ("local_size_crc flags reorder",            lambda: test_local_size_crc_flags_reorder(tmp)),
        ("jamcrc matches live driver values",       lambda: test_jamcrc_matches_driver()),
        ("jamcrc is unsigned 32-bit",               lambda: test_jamcrc_is_unsigned()),
        ("_write_once ignores echoed 'Returns 1'",  lambda: test_write_once_ignores_echoed_returns(monkey_self)),
        ("_write_once reports success",             lambda: test_write_once_reports_success(monkey_self)),
        ("_write_once lost result -> None",         lambda: test_write_once_lost_result_is_none(monkey_self)),
        ("UTF-8 round-trips through transport",     lambda: test_utf8_roundtrip_through_transport()),
        ("no silent '?' replacement",               lambda: test_no_silent_replacement()),
        ("latin-1 fallback on read",                lambda: test_latin1_fallback_on_read()),
        ("stream UTF-8 split across reads",         lambda: test_stream_utf8_split_across_reads()),
        ("stream IAC inside multibyte char",        lambda: test_stream_iac_inside_multibyte_char()),
        ("write command fits line budget",          lambda: test_write_command_fits_line_budget()),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn(); print(f"  PASS  {name}")
        except Exception as e:
            failed += 1; print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests)-failed}/{len(tests)} passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
