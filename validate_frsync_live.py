#!/usr/bin/env python3
"""Live validation of the frsync hardening changes against the real MUD.

Gated on $FRCHAR/$FRPASS and bounded by a hard 90s alarm so it can never hang
the terminal if the single creator slot is already held (e.g. by the running
MCP server). Per the project constraints, free that slot first (quit the MCP
session / log out) before running this.

  FRCHAR=<creator> FRPASS=... python3 validate_frsync_live.py

What it proves on the wire (the parts the offline tests can't):
  1. crc32 efun: crc32_remote(<known file>) == zlib.crc32 of the bytes we read
     back — i.e. the efun is present and our unsigned-32 normalisation matches.
  2. A file whose content contains the literal text 'Returns 1' uploads and
     verifies (the _write_once sentinel is not fooled by the echo).
  3. UTF-8 text (Norwegian / em dash / smart quotes) round-trips byte-exact and
     verify_remote() passes.

It writes ONE scratch file under your home (/w/<char>/.frsync_selftest.c) and
removes it again via exec. It never touches anything else.
"""
import os, signal, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frsync

CHAR = os.environ.get("FRCHAR")
PW   = os.environ.get("FRPASS")
HOST = os.environ.get("FRHOST", frsync.DEF_HOST)
PORT = int(os.environ.get("FRPORT") or frsync.DEF_PORT)

# A small file that exists on essentially every FR install; override with
# $FRPROBE if it's missing on yours.
PROBE = os.environ.get("FRPROBE", "/secure/save/banish")


def die(msg, code=1):
    print(msg); sys.exit(code)


def main():
    if not CHAR or not PW:
        die("Set FRCHAR and FRPASS to run the live validation "
            "(offline coverage: python3 test_frsync.py).", 2)

    signal.signal(signal.SIGALRM, lambda *_: die(
        "\nTIMEOUT (90s): could not complete — is the single creator slot "
        "held by the running MCP? Free it and retry.", 3))
    signal.alarm(90)

    print(f"Connecting to {HOST}:{PORT} as {CHAR} …")
    m = frsync.Mud(HOST, PORT)
    m.login(CHAR, PW)
    print("Logged in.\n")
    failed = 0

    # 1) crc32 efun against a known file ------------------------------------
    probe = PROBE
    sz = m.file_size(probe)
    if sz is None or sz < 0:
        print(f"  SKIP  crc32 efun check: probe {probe} not found "
              f"(set $FRPROBE to a readable file)")
    else:
        remote_crc = m.crc32_remote(probe)
        local_crc = frsync.jamcrc(m.read_file_bytes(probe, sz))   # driver = JAMCRC, not zlib
        if remote_crc == local_crc:
            print(f"  PASS  crc32 efun: {probe} crc=0x{remote_crc:08x} "
                  f"matches bytes read back")
        else:
            failed += 1
            print(f"  FAIL  crc32 efun: remote=0x{remote_crc:08x} "
                  f"local=0x{local_crc:08x} (CRLF or read_file/read_bytes "
                  f"divergence?) — do NOT rely on crc verify until resolved")

    # 2) + 3) upload a 'Returns 1' + UTF-8 file, verify, read back ----------
    home = f"/w/{CHAR.lower()}"
    scratch = f"{home}/.frsync_selftest.c"
    payload = (
        "// frsync self-test — safe to delete\n"
        "// Returns 1   <- literal sentinel-bait; must not fool the writer\n"
        "// Returns 0   and another one for good measure\n"
        "string note = \"blåbær — æ ø å — “smart” ‘quotes’ …\";\n"
        "int x; // ok\n"
    ).encode("utf-8")

    try:
        frsync.ensure_remote_dirs(m, scratch, set())
        m.write_file_chunks(scratch, payload)
        ok = frsync.verify_remote(m, scratch, payload)
        print(f"  {'PASS' if ok else 'FAIL'}  upload+verify of 'Returns 1' "
              f"+ UTF-8 file ({len(payload)} bytes)")
        failed += 0 if ok else 1

        back = m.read_file_bytes(scratch, m.file_size(scratch))
        expect = frsync.local_norm(payload)
        if back == expect:
            print(f"  PASS  byte-exact read-back, UTF-8 decodes: "
                  f"{back.decode('utf-8').splitlines()[3][:40]!r}…")
        else:
            failed += 1
            print(f"  FAIL  read-back differs: got {len(back)} bytes, "
                  f"expected {len(expect)}")
    finally:
        m.exec_raw(f'return rm("{scratch}");', 1.5)
        print(f"  (removed {scratch})")
        m.close()

    signal.alarm(0)
    print(f"\n{'ALL LIVE CHECKS PASSED' if not failed else f'{failed} LIVE CHECK(S) FAILED'}.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
