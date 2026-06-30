# frsync — file transfer for Final Realms (no FTP)

Move files between your machine and the MUD over the **normal creator login**.
No FTP, no extra ports, nothing to install on the server. You log in with your
own creator name + password, and the MUD's permissions decide what you can
touch — exactly like in-game.

It works by using the creator `exec` command to call the driver's `write_file`
/ `read_bytes` efuns over the connection you already log in on (port 4010).

---

## Install

You need **Python 3** (already on macOS/Linux). Then:

```sh
make install        # copies frsync to /usr/local/bin/frsync
# or just run it in place:
./frsync.py --help
```

No pip, no dependencies — it's a single standard-library file you can copy or
paste anywhere.

---

## The 5 commands

| Command  | Direction        | What it does                                        |
|----------|------------------|-----------------------------------------------------|
| `status` | compare          | Show what differs between local and MUD. Changes nothing. |
| `push`   | local → MUD      | Upload. Verifies every file (size + checksum).      |
| `pull`   | MUD → local      | Download. Verified.                                 |
| `watch`  | local → MUD live | Auto-upload files as you save/drop them in a folder. |
| `mirror` | MUD → local bulk | Download whole subtrees for a local working copy. Resumable. |

Each takes a **local** path and a **server** path. Both can be a single file
or a directory (directories sync recursively).

---

## Examples

```sh
# Upload your area (asks before writing, verifies each file)
frsync push ./drifting_forest /w/yourname/drifting_forest

# Just see what's different — touches nothing
frsync status ./drifting_forest /w/yourname/drifting_forest

# One file
frsync push ./rooms/hut.c /w/yourname/drifting_forest/rooms/hut.c

# Download an area to work on locally
frsync pull /w/yourname/drifting_forest ./drifting_forest

# "Synced folder": leave it running; every file you save/drop into
# ./work uploads automatically. Ctrl-C to stop.
frsync watch ./work /w/yourname/work

# Grab a local copy of the mudlib code to read/grep/edit offline
frsync mirror ./MUD /std /cmds /include /d /w/yourname
```

### Typical workflow

```sh
frsync pull  /w/yourname/myarea ./myarea     # get the current version
# …edit files locally in your editor…
frsync status ./myarea /w/yourname/myarea    # check what changed
frsync push   ./myarea /w/yourname/myarea    # ship it (verified)
```

Or just run `frsync watch ./myarea /w/yourname/myarea` and every save uploads.

---

## Login & passwords

- You're prompted for your **creator name** and **password** (same as logging
  in to the game). Password input is hidden.
- For scripting/automation set `FRPASS` instead of typing it:
  ```sh
  FRPASS='yourpassword' frsync push ./area /w/yourname/area -y
  ```
- Add `--char yourname` to skip the name prompt.

You can only read/write what your character is allowed to in-game. Some dirs
(player accounts, server scripts, `/secure`) are read-protected — frsync skips
those cleanly and lists them in `.frsync_skipped.txt`.

---

## Useful flags

| Flag              | Meaning                                                       |
|-------------------|---------------------------------------------------------------|
| `--char NAME`     | Creator name (skip the prompt).                               |
| `--ext .c,.h`     | Only these file types (directory syncs).                      |
| `--dry-run`       | Show what *would* happen; change nothing.                     |
| `-y` / `--yes`    | Don't ask for confirmation (for scripts).                     |
| `--delete`        | **push only:** remove server files missing locally. Off by default. |
| `--include-backup`| **mirror only:** include old `BACKUP/`/`ATTIC/` archives (skipped by default). |
| `--rewalk`        | **mirror only:** re-list the tree instead of using the cached manifest. |

---

## Notes & safety

- **Nothing is deleted on the server** unless you explicitly use `push --delete`.
- Every `push`/`pull` is **verified** by comparing file size and a byte
  checksum on both ends.
- `mirror` is **resumable** — stop it any time; re-run and it skips what's
  already downloaded (it caches the file list in `.frmanifest_*.tsv`).
- It auto-reconnects if the MUD drops the connection.
- Commands are kept short on purpose — the driver drops the connection on very
  long input lines, so file contents are streamed in safe chunks.

Questions or a path that won't transfer? Check `.frsync_skipped.txt` in your
mirror folder, or ask in the creator channel.
