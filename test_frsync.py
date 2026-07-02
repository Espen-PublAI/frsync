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


def test_local_glob_expansion(tmpdir):
    for n in ("a.c", "b.c", "cloud1.c", "cloud2.c", "note.txt"):
        open(os.path.join(tmpdir, n), "w").close()
    bn = lambda L: sorted(os.path.basename(p) for p in L)
    assert bn(frsync.expand_local_arg(tmpdir, "*.c")) == ["a.c", "b.c", "cloud1.c", "cloud2.c"]
    assert bn(frsync.expand_local_arg(tmpdir, "cloud*.c")) == ["cloud1.c", "cloud2.c"]
    assert bn(frsync.expand_local_arg(tmpdir, "*.*")) == ["a.c", "b.c", "cloud1.c", "cloud2.c", "note.txt"]
    assert bn(frsync.expand_local_arg(tmpdir, "a.c")) == ["a.c"]              # literal that exists
    assert frsync.expand_local_arg(tmpdir, "*.xyz") == []                     # glob, no match
    # a plain (non-glob) name returns its path even if missing, for a clean error
    assert frsync.expand_local_arg(tmpdir, "gone.c") == [os.path.join(tmpdir, "gone.c")]


def test_remote_glob_expansion():
    class FakeMud:   # listdir returns (name, size); -2 marks a directory
        def listdir(self, path):
            return [("a.c", 10), ("b.c", 20), ("sub", -2), ("cloud1.c", 5), ("note.txt", 3)]
    m = FakeMud()
    assert frsync.expand_remote_arg(m, "*.c", "/w/x") == ["/w/x/a.c", "/w/x/b.c", "/w/x/cloud1.c"]
    assert frsync.expand_remote_arg(m, "cloud*.c", "/w/x") == ["/w/x/cloud1.c"]
    # *.* matches files with a dot; the 'sub' directory is excluded
    assert frsync.expand_remote_arg(m, "*.*", "/w/x") == ["/w/x/a.c", "/w/x/b.c", "/w/x/cloud1.c", "/w/x/note.txt"]
    assert frsync.expand_remote_arg(m, "a.c", "/w/x") == ["/w/x/a.c"]         # literal, no listing needed


def test_dropped_files_detection(tmpdir):
    # the terminal pastes file paths onto the input line on drag-and-drop
    f1 = os.path.join(tmpdir, "foo.c"); open(f1, "w").close()
    f2 = os.path.join(tmpdir, "my room.c"); open(f2, "w").close()   # a space in the name
    # single absolute path, with and without a trailing space the terminal may add
    assert frsync.dropped_files(f1) == [f1]
    assert frsync.dropped_files(f1 + "  ") == [f1]
    # macOS/Linux escape spaces with a backslash; iTerm/Terminal may also quote
    assert frsync.dropped_files(f2.replace(" ", r"\ ")) == [f2]
    assert frsync.dropped_files(f'"{f2}"') == [f2]
    # several files dropped at once
    assert frsync.dropped_files(f'{f1} "{f2}"') == [f1, f2]
    # some terminals paste a file:// URI (percent-encoded)
    assert frsync.dropped_files("file://" + f2.replace(" ", "%20")) == [f2]
    # ordinary typed lines and /commands must NOT be mistaken for a drop
    assert frsync.dropped_files("look") is None
    assert frsync.dropped_files("get sword from bag") is None
    assert frsync.dropped_files("/upload foo.c") is None
    assert frsync.dropped_files("/no/such/file.c") is None
    assert frsync.dropped_files("") is None


def test_recursive_plan_with_subfolders(tmpdir):
    # what `/sync to-mud` relies on: plan() must walk subfolders and classify
    # every file as new / diff / same / remote_only across the whole tree.
    root = os.path.join(tmpdir, "syncroot")          # isolated tree (runner shares tmp)
    os.makedirs(os.path.join(root, "rooms"))
    open(os.path.join(root, "area.c"), "w").write("area")          # exists remote, differs
    open(os.path.join(root, "rooms", "hut.c"), "w").write("hut")   # new (not on remote)
    open(os.path.join(root, "rooms", "well.c"), "w").write("well") # matches remote exactly
    wsize, wcrc = frsync.local_size_crc(os.path.join(root, "rooms", "well.c"))
    ROOT = "/w/x"

    class FakeMud:
        def walk(self, root):                       # remote has an extra file too
            return ["area.c", "rooms/well.c", "rooms/old.c"]
        def file_size(self, rp):
            return {f"{ROOT}/area.c": 999,          # size differs from local -> diff
                    f"{ROOT}/rooms/well.c": wsize,
                    f"{ROOT}/rooms/old.c": 5}.get(rp, -1)
        def crc32_remote(self, rp):
            return wcrc if rp.endswith("well.c") else 0xDEAD

    st = frsync.plan(FakeMud(), root, ROOT, [], is_file=False)
    assert st["area.c"] == "diff"
    assert st["rooms/hut.c"] == "new"
    assert st["rooms/well.c"] == "same"
    assert st["rooms/old.c"] == "remote_only"
    # the sync would upload only new + diff, recreating subfolders on the far side
    todo = sorted(r for r, s in st.items() if s in ("new", "diff"))
    assert todo == ["area.c", "rooms/hut.c"]


def test_area_lint_parsing():
    # Real Drifting Forest conventions: #define path macros built by +, and the
    # FR quirk that some inherits/loads omit the .c (the file on disk is X.c).
    paths_h = (
        '#define DF_AREA "/w/malric/drifting_forest/"\n'
        '#define DF_ROOMS   (DF_AREA + "rooms/")\n'
        '#define DF_OBJ     (DF_AREA + "obj/")\n'
        '#define DF_HUT     (DF_ROOMS + "hut.c")\n'
        '#define DF_HUT_ROOM (DF_ROOMS + "hut")\n'          # no .c -> load form
        '#define DF_ROOM_BASE "/std/rooms/room"\n'          # no .c -> real file is .c
        '#define DF_DUSKROOT (DF_OBJ + "duskroot.c")\n')
    defs = frsync.collect_defines([paths_h])
    assert defs["DF_AREA"] == '"/w/malric/drifting_forest/"'
    assert frsync.eval_str_expr("DF_HUT", defs) == "/w/malric/drifting_forest/rooms/hut.c"
    assert frsync.eval_str_expr('DF_ROOMS + "x.c"', defs) == "/w/malric/drifting_forest/rooms/x.c"
    assert frsync.eval_str_expr("this_object()", defs) is None      # dynamic -> unresolved
    assert frsync.eval_str_expr("some_var", defs) is None

    src = (
        '#include "/w/malric/drifting_forest/rooms/df_local.h"\n'
        "inherit DF_ROOM_BASE;\n"
        "void setup() {\n"
        '  add_exit("in", DF_HUT, "door");\n'
        '  add_exit("hub", "/w/malric/dev_hub/rooms/nexus.c", "portal");\n'
        "  add_clone(DF_HUT_ROOM, 1);\n"
        "}\n"
        "int go(string p) { return clone_object(p); }  // dynamic: skipped\n"
        "/* prose: add_exit(x, DF_ROOMS + \"ghost.c\") must be ignored */\n")
    refs = frsync.extract_references(frsync._strip_c_comments(src), defs)
    by = {(r["kind"], r["line"]): r["path"] for r in refs}
    # inherit "/std/rooms/room" normalises to the real .c file
    assert by[("inherit", 2)] == "/std/rooms/room.c"
    assert by[("exit", 4)] == "/w/malric/drifting_forest/rooms/hut.c"
    assert by[("exit", 5)] == "/w/malric/dev_hub/rooms/nexus.c"
    # add_clone target is a no-.c macro -> resolves to the .c file on disk
    assert by[("add_clone", 6)] == "/w/malric/drifting_forest/rooms/hut.c"
    assert by[("include", 1)] == "/w/malric/drifting_forest/rooms/df_local.h"
    # the clone_object(p) variable is unresolved (path None), the commented
    # add_exit in the /* */ block is stripped and never seen
    assert any(r["kind"] == "clone_object" and r["path"] is None for r in refs)
    assert not any("ghost.c" in (r["path"] or "") for r in refs)


def test_lint_custom_exit_wrapper():
    # Drifting Forest wires its dream rooms through a custom add_dream_exit()
    # helper (113 calls vs 5 raw add_exit) — lint must learn the wrapper or it
    # checks almost none of the real exit graph.
    defs = frsync.collect_defines(['#define DF_ROOMS "/w/malric/df/rooms/"\n'
                                   '#define DF_DREAM05 (DF_ROOMS + "dream05.c")\n'])
    base = ("void add_dream_exit(string dir, string dest, string type) {\n"
            "  base_exits[dir] = ({dest, type});\n"
            "  add_exit(dir, dest, type);\n"
            "}\n")
    room = 'void setup() { add_dream_exit("north", DF_DREAM05, "path"); }\n'
    wrappers = frsync.find_ref_wrappers([base, room])
    # detected: add_dream_exit forwards its 2nd param (index 1) to add_exit's dest
    assert wrappers.get("add_dream_exit") == ("exit", 1)
    # and 'if (...) {' etc. must NOT be mistaken for a wrapper
    assert "if" not in wrappers and "for" not in wrappers
    # now the wrapped call resolves like a real exit
    refs = frsync.extract_references(room, defs, wrappers)
    hit = [r for r in refs if r["kind"] == "exit"]
    assert hit and hit[0]["path"] == "/w/malric/df/rooms/dream05.c"
    # without the wrapper it would be invisible (not even counted)
    assert not frsync.extract_references(room, defs)
    # the wrapper's OWN definition line ("string dest") must not be mistaken for
    # a call to itself — a parameter declaration is not a reference. (The body's
    # internal add_exit(dir, dest) is still seen, but as a dynamic path=None ref.)
    base_refs = frsync.extract_references(base, defs, wrappers)
    assert not any(r["expr"] == "string dest" for r in base_refs)
    assert frsync._is_param_decl("string dest") and frsync._is_param_decl("object *who")
    assert not frsync._is_param_decl("DF_DREAM05") and not frsync._is_param_decl('"north"')


def test_lineeditor_autocomplete():
    cmds = ["/upload", "/download", "/push", "/pull", "/diff", "/put"]
    writes = []
    ed = frsync.LineEditor(lambda s: writes.append(s), lambda l: True, commands=cmds)

    # the hint narrows as the /prefix grows, and vanishes once unambiguous/spaced
    ed.buf = list("/p")
    assert set(ed._matches()) == {"/push", "/pull", "/put"}
    assert "/push" in ed._hint() and "/pull" in ed._hint()
    ed.buf = list("/pu");  assert ed._hint()  # /push /pull /put still
    ed.buf = list("/dif"); assert ed._hint() == "  /diff"          # single candidate shown
    ed.buf = list("/diff"); assert ed._hint() == ""                # exact -> nothing to add
    ed.buf = list("/diff x"); assert ed._hint() == ""              # past the command -> off
    ed.buf = list("look");   assert ed._hint() == "" and ed._matches() == []

    # a keystroke repaints buffer + dim hint and parks the cursor after the buffer
    writes.clear(); ed.buf = list("/d"); ed.key("text", "o")
    painted = writes[-1]
    assert painted.startswith("\r\x1b[K/do")     # line cleared, buffer shown
    assert "\x1b[90m" in painted                  # dim hint follows
    assert painted.endswith("D")                  # cursor moved back left over the hint

    # Tab: unique match completes and appends a space for args
    ed.buf = list("/dif"); ed.key("tab"); assert "".join(ed.buf) == "/diff "
    # Tab: several matches -> extend to the common prefix only
    ed.buf = list("/d"); ed.key("tab"); assert "".join(ed.buf) == "/d"      # /download vs /diff -> "/d"
    ed.buf = list("/pu"); ed.key("tab"); assert "".join(ed.buf) == "/pu"    # /push/pull/put
    # Tab on a non-command line does nothing
    ed.buf = list("kill orc"); ed.key("tab"); assert "".join(ed.buf) == "kill orc"

    # --- argument completion (Tab on a file-name arg via arg_completer) ---
    def completer(cmd, partial, live=False):     # completer takes a live flag
        pool = {"/upload": ["area.c", "rooms/", "obj/"],
                "/download": ["clearing.c", "clone_helper.c", "hut.c"]}.get(cmd, [])
        d, _, base = partial.rpartition("/")
        return sorted(p for p in pool if p.startswith(base))
    ed2 = frsync.LineEditor(lambda s: None, lambda l: True,
                            commands=cmds + ["/upload", "/download"], arg_completer=completer)
    # unique file -> fill in + trailing space (ready for the next arg)
    ed2.buf = list("/upload ar"); ed2.key("tab"); assert "".join(ed2.buf) == "/upload area.c "
    # a directory candidate keeps its slash and gets NO space (keep descending)
    ed2.buf = list("/upload ro"); ed2.key("tab"); assert "".join(ed2.buf) == "/upload rooms/"
    # several candidates -> extend to common prefix, and show them in the strip
    ed2.buf = list("/download cl"); ed2.key("tab")
    assert "".join(ed2.buf) == "/download cl"                 # "clearing"/"clone_helper" share "cl"
    assert ed2._suggest == ["clearing.c", "clone_helper.c"]
    # the candidate strip is transient — cleared by the next keystroke
    ed2.key("text", "e"); assert ed2._suggest is None

    # --- live local-file narrowing in the hint (cheap), remote stays Tab-only ---
    def live_completer(cmd, partial, live=False):
        if live and cmd in ("/download", "/goto"):     # remote: too costly per keystroke
            return None
        pool = {"/upload": ["area.c", "rooms/", "obj/"],
                "/download": ["clearing.c", "hut.c"]}.get(cmd, [])
        d, _, base = partial.rpartition("/")
        return sorted(p for p in pool if p.startswith(base))
    ed3 = frsync.LineEditor(lambda s: None, lambda l: True,
                            commands=["/upload", "/download", "/goto"], arg_completer=live_completer)
    ed3.buf = list("/upload ");  assert ed3._hint() == "  area.c  obj/  rooms/"   # all, live
    ed3.buf = list("/upload r"); assert ed3._hint() == "  rooms/"                 # narrows live
    ed3.buf = list("/upload area.c"); assert ed3._hint() == ""                    # exact single -> quiet
    ed3.buf = list("/download c"); assert ed3._hint() == ""                       # remote -> silent live
    ed3.buf = list("/goto x/y");   assert ed3._hint() == ""                       # remote -> silent live
    # but Tab on a remote arg still lists (live defaults False)
    ed3.buf = list("/download c"); ed3.key("tab"); assert "".join(ed3.buf) == "/download clearing.c "


def test_saved_logins(tmpdir):
    # in-memory keychain + temp index, so the real OS keychain/home are untouched
    store = {}
    orig = (frsync._kc_set, frsync._kc_get, frsync._kc_del,
            frsync.LOGINS_INDEX, frsync.FRSYNC_DIR)
    frsync._kc_set = lambda n, p: (store.__setitem__(n, p) or True)
    frsync._kc_get = lambda n: store.get(n)
    frsync._kc_del = lambda n: store.pop(n, None)
    frsync.LOGINS_INDEX = os.path.join(tmpdir, "logins.json")
    frsync.FRSYNC_DIR = tmpdir
    try:
        assert frsync.saved_logins() == [] and frsync.asking_to_save() is True
        assert frsync.save_login("Malric", "pw1") is True
        assert frsync.save_login("Ducky", "pw2") is True
        assert frsync.saved_logins() == ["Malric", "Ducky"]        # index holds names, in order
        assert frsync.get_login_password("Malric") == "pw1"        # password from the keychain
        assert "pw1" not in open(frsync.LOGINS_INDEX).read()       # never written to the file
        # re-saving updates the password but not the name list (no dupes)
        frsync.save_login("Malric", "pw1b")
        assert frsync.saved_logins() == ["Malric", "Ducky"]
        assert frsync.get_login_password("Malric") == "pw1b"
        # don't-ask persists across loads
        frsync.set_ask(False); assert frsync.asking_to_save() is False
        # forget drops it from both index and keychain
        frsync.forget_login("Malric")
        assert frsync.saved_logins() == ["Ducky"]
        assert frsync.get_login_password("Malric") is None
        # if the keychain write fails, the name is NOT recorded
        frsync._kc_set = lambda n, p: False
        assert frsync.save_login("Nope", "x") is False
        assert "Nope" not in frsync.saved_logins()
    finally:
        (frsync._kc_set, frsync._kc_get, frsync._kc_del,
         frsync.LOGINS_INDEX, frsync.FRSYNC_DIR) = orig


def test_unified_diff_lines():
    # MUD copy is the 'before', local is the 'after': + is what a push would
    # apply, - is MUD-only content (e.g. a live `ed` edit that drifted).
    remote = "look = \"a forest\";\nexits = ([\"north\":ROOM+\"hut\"]);\n"
    local  = "look = \"a misty forest\";\nexits = ([\"north\":ROOM+\"hut\"]);\n"
    d = frsync.unified_diff_lines(remote, local, "clearing.c")
    assert d[0] == "--- clearing.c (MUD)"
    assert d[1] == "+++ clearing.c (local)"
    assert '-look = "a forest";' in d
    assert '+look = "a misty forest";' in d
    assert '  exits = (["north":ROOM+"hut"]);'.strip() in [x.strip() for x in d]  # context kept
    # identical files -> empty diff (the "in sync" case)
    assert frsync.unified_diff_lines("x\ny", "x\ny", "f") == []
    # colouring: + green, - red, headers bold; plain passthrough when not a tty
    assert frsync.colorize_diff("+add", True).startswith("\x1b[32m")
    assert frsync.colorize_diff("-del", True).startswith("\x1b[31m")
    assert frsync.colorize_diff("--- f (MUD)", True).startswith("\x1b[1m")
    assert frsync.colorize_diff("+add", False) == "+add"


def test_delete_rename_return_codes():
    # The subtle bit: rm()/rmdir() return 1 on success, but rename() returns 0 on
    # success (verified live on FR:Legacy). The Mud wrappers must map each to bool
    # correctly, and a lost result (exec_int -> None) must read as failure.
    class FakeExec:
        def __init__(self, val): self.val, self.sent = val, None
        def exec_int(self, code): self.sent = code; return self.val
    assert frsync.Mud.rm(FakeExec(1), "/w/x/a.c") is True
    assert frsync.Mud.rm(FakeExec(0), "/w/x/a.c") is False
    assert frsync.Mud.rm(FakeExec(None), "/w/x/a.c") is False
    assert frsync.Mud.rmdir(FakeExec(1), "/w/x/d") is True
    assert frsync.Mud.rmdir(FakeExec(0), "/w/x/d") is False
    assert frsync.Mud.rename(FakeExec(0), "/a", "/b") is True    # 0 == success here
    assert frsync.Mud.rename(FakeExec(1), "/a", "/b") is False
    assert frsync.Mud.rename(FakeExec(None), "/a", "/b") is False
    # and the right efuns are actually invoked
    f = FakeExec(1); frsync.Mud.rm(f, "/w/x/a.c");   assert 'rm("/w/x/a.c")' in f.sent
    f = FakeExec(1); frsync.Mud.rmdir(f, "/w/x/d");  assert 'rmdir("/w/x/d")' in f.sent
    f = FakeExec(0); frsync.Mud.rename(f, "/a", "/b"); assert 'rename("/a", "/b")' in f.sent


def test_parse_update_output():
    # exact strings the live FR:Legacy `update` command emits (captured 2026-07)
    fail = ("E /w/malric/room.c at line 2 : Undefined variable 'x' before  ; }\n"
            "E /w/malric/room.c at line 2 : syntax error before  }\n"
            "Failed to load /w/malric/room.c, error: *Error in loading object\n")
    r = frsync.parse_update_output(fail)
    assert r["errors"] == [("/w/malric/room.c", 2, "Undefined variable 'x' before  ; }"),
                           ("/w/malric/room.c", 2, "syntax error before  }")]
    assert r["failed"] == []          # same file already reported via errors -> not double-counted

    ok = "Loaded /w/malric/workroom.c"
    assert frsync.parse_update_output(ok) == {"errors": [], "failed": [],
                                              "loaded": ["/w/malric/workroom.c"]}

    multi = ("[Deprecated #7414 saved]\nLoaded /w/malric/b.c\n"
             "Updated malric's workroom(/w/malric/workroom).")
    r = frsync.parse_update_output(multi)
    assert r["errors"] == [] and r["loaded"] == ["/w/malric/b.c", "/w/malric/workroom"]

    # a pure load failure with no per-line compile error still shows up
    r = frsync.parse_update_output("Failed to load /w/malric/c.c, error: no such file\n")
    assert r["failed"] == ["/w/malric/c.c"]


# --------------------------------------------------------------------- runner
class _FakeSelf:
    """Stand-in for a Mud instance: _write_once only touches _flush/send/expect."""
    def __init__(self, canned):
        self.sent, self._canned = [], canned
    def _flush(self): pass
    def send(self, line): self.sent.append(line)
    def expect(self, pattern, timeout=12): return self._canned


# Under pytest, the `monkey_self` parameter on the _write_once tests is a fixture
# request: hand back the _FakeSelf factory the tests call as monkey_self(canned).
# main() injects the same value by hand, so the standalone runner needs no pytest.
try:
    import pytest

    @pytest.fixture(name="monkey_self")
    def _monkey_self():
        return _FakeSelf
except ImportError:                      # standalone `python3 test_frsync.py`
    pass


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
        ("local glob expansion (*.c, cloud*.c)",     lambda: test_local_glob_expansion(tmp)),
        ("remote glob expansion (*.c, *.*)",         lambda: test_remote_glob_expansion()),
        ("drag-and-drop path detection",             lambda: test_dropped_files_detection(tmp)),
        ("recursive plan across subfolders",         lambda: test_recursive_plan_with_subfolders(tmp)),
        ("parse update/reload output",               lambda: test_parse_update_output()),
        ("delete/rename return codes",               lambda: test_delete_rename_return_codes()),
        ("unified diff local vs MUD",                lambda: test_unified_diff_lines()),
        ("area lint: macros + references",           lambda: test_area_lint_parsing()),
        ("area lint: custom exit wrapper",           lambda: test_lint_custom_exit_wrapper()),
        ("line editor autocomplete + hint",          lambda: test_lineeditor_autocomplete()),
        ("saved logins index + keychain",            lambda: test_saved_logins(tmp)),
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
