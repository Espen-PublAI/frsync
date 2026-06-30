# frsync — file transfer for Final Realms (no FTP)
#
#   make start          open the interactive MUD shell (/download, /upload, /lcd)
#   make install        install `frsync` to PREFIX/bin (default /usr/local)
#   make uninstall      remove it
#   make mirror-code    download the mudlib code into ./MUD (resumable)
#   make watch          live-sync DIR -> REMOTE  (DIR=… REMOTE=…)
#   make mcp-deps       install the MCP dependency (pip install mcp)
#   make mcp-register   register the MUD with Claude Code (no credentials)
#   make mcp-login      store creator name + password in the OS secret store
#   make mcp-reset      forget the stored credentials
#   make mcp-status     show whether credentials are configured
#   make help           show targets
#
# Auth: you'll be prompted for your creator name+password, or set FRPASS / CHAR:
#   make mirror-code CHAR=yourname
#   FRPASS=secret make watch DIR=./area REMOTE=/w/yourname/area
# The MCP server instead reads credentials from the OS secret store; set them
# once with `make mcp-login`.

PREFIX ?= /usr/local
BINDIR  = $(PREFIX)/bin
PYTHON ?= python3

# Standard mudlib code roots to mirror (override on the command line if needed)
ROOTS ?= /std /cmds /include /d /global /obj /room /net /table /open /doc /baseobs
CHAR_ARG = $(if $(CHAR),--char $(CHAR),)

.PHONY: help start install uninstall mirror-code watch status check \
        mcp-deps mcp-register mcp-login mcp-reset mcp-status

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

# Interactive MUD shell — telnet-like, plus /download /upload /lcd
start:
	$(PYTHON) frsync.py $(CHAR_ARG) connect $(if $(DIR),--dir $(DIR),)

install:
	install -d $(BINDIR)
	install -m 0755 frsync.py $(BINDIR)/frsync
	@echo "Installed: $(BINDIR)/frsync   (run 'frsync --help')"

uninstall:
	rm -f $(BINDIR)/frsync
	@echo "Removed $(BINDIR)/frsync"

# Bulk-download the mudlib code into ./MUD (resumable; re-run to continue)
mirror-code:
	$(PYTHON) frsync.py $(CHAR_ARG) mirror ./MUD $(ROOTS)

# Live "synced folder": make watch DIR=./myarea REMOTE=/w/yourname/myarea
watch:
	@test -n "$(DIR)"    || { echo "set DIR=./local/dir"; exit 1; }
	@test -n "$(REMOTE)" || { echo "set REMOTE=/w/yourname/dir"; exit 1; }
	$(PYTHON) frsync.py $(CHAR_ARG) watch $(DIR) $(REMOTE)

# Compare a dir: make status DIR=./myarea REMOTE=/w/yourname/myarea
status:
	@test -n "$(DIR)"    || { echo "set DIR=./local/dir"; exit 1; }
	@test -n "$(REMOTE)" || { echo "set REMOTE=/w/yourname/dir"; exit 1; }
	$(PYTHON) frsync.py $(CHAR_ARG) status $(DIR) $(REMOTE)

# Syntax-check the tools
check:
	$(PYTHON) -c "import ast; ast.parse(open('frsync.py').read()); print('frsync.py: syntax OK')"
	$(PYTHON) -c "import ast; ast.parse(open('frsync_mcp.py').read()); print('frsync_mcp.py: syntax OK')"

# --- MCP server for Claude Code ---------------------------------------------
# Install the one dependency (only the MCP server needs it; the CLI has none)
mcp-deps:
	$(PYTHON) -m pip install mcp

# Register the MUD with Claude Code as an MCP server.
# No credentials here — they're stored securely in the OS secret store, set
# once with `make mcp-login` (or `frsync_mcp.py --login`).
mcp-register:
	claude mcp add frsync-mud -- $(PYTHON) $(CURDIR)/frsync_mcp.py
	@echo "Registered (no credentials in config)."
	@echo "Next: run 'make mcp-login' to store your creator name + password,"
	@echo "then restart the frsync-mud MCP server."
	@echo "In Claude Code you'll then have mud_ls / mud_read / mud_write / mud_command …"

# Store / reset / check MUD credentials in the OS secret store (macOS Keychain,
# Windows Credential Locker, Linux Secret Service). The password is prompted
# with no echo and never written to any config file.
mcp-login:
	$(PYTHON) $(CURDIR)/frsync_mcp.py --login

mcp-reset:
	$(PYTHON) $(CURDIR)/frsync_mcp.py --reset

mcp-status:
	$(PYTHON) $(CURDIR)/frsync_mcp.py --status
