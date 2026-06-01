#!/usr/bin/env bash
#
# whisper-input-next-mac-kit — register the MCP server with Claude Desktop
#
# Installs the `mcp` SDK into the app venv and adds a "whisper-input" entry to
# Claude Desktop's config, so you can monitor/configure the dictation service by
# just asking. Idempotent — safe to re-run. See mcp/README.md.
#
set -euo pipefail

# ---------- config (override via env vars; match install.sh) ----------
APP_DIR="${WIN_APP_DIR:-$HOME/Whisper-Input-Next}"
LABEL="${WIN_LABEL:-com.whisper-input-next.kit}"
SERVER_NAME="${WIN_MCP_NAME:-whisper-input}"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$APP_DIR/.venv/bin/python"
SERVER_PY="$KIT_DIR/mcp/server.py"
CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

c()    { printf '\033[1;36m[win-mcp]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[win-mcp]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[win-mcp ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- preflight ----------
[ "$(uname -s)" = "Darwin" ] || die "macOS only."
[ -x "$VENV_PY" ] || die "App venv not found at $VENV_PY — run ./install.sh first."
[ -f "$SERVER_PY" ] || die "MCP server not found at $SERVER_PY"

# ---------- 1) refuse to do anything while Claude Desktop is running ----------
# Claude Desktop rewrites its own config file while running and will drop our edit,
# so fail fast BEFORE mutating the venv.
if pgrep -x "Claude" >/dev/null 2>&1; then
  warn "Claude Desktop appears to be running."
  warn "It rewrites its config on quit and WILL clobber this edit."
  die  "Fully quit Claude Desktop (Cmd-Q), then re-run this script."
fi

# ---------- 2) install the MCP SDK into the app venv ----------
c "Installing the MCP SDK into the app venv ..."
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_PY" mcp
else
  "$VENV_PY" -m pip install mcp
fi

# ---------- 3) merge the server entry into the config (create if missing) ----------
c "Registering '$SERVER_NAME' in Claude Desktop config ..."
mkdir -p "$(dirname "$CFG")"
WIN_CFG="$CFG" WIN_SERVER_NAME="$SERVER_NAME" WIN_VENV_PY="$VENV_PY" \
WIN_SERVER_PY="$SERVER_PY" WIN_APP_DIR="$APP_DIR" WIN_LABEL="$LABEL" \
"$VENV_PY" - <<'PY'
import json, os, sys

cfg_path = os.environ["WIN_CFG"]
name = os.environ["WIN_SERVER_NAME"]

data = {}
if os.path.exists(cfg_path):
    try:
        with open(cfg_path, encoding="utf-8") as f:
            text = f.read().strip()
            data = json.loads(text) if text else {}
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"existing config is not valid JSON ({exc}); fix or remove:\n  {cfg_path}")
    # back up the PRISTINE config once — never overwrite an earlier good backup
    bak = cfg_path + ".bak"
    if not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

servers = data.setdefault("mcpServers", {})
servers[name] = {
    "command": os.environ["WIN_VENV_PY"],
    "args": [os.environ["WIN_SERVER_PY"]],
    "env": {
        "WIN_APP_DIR": os.environ["WIN_APP_DIR"],
        "WIN_LABEL": os.environ["WIN_LABEL"],
    },
}
tmp = cfg_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, cfg_path)  # atomic swap — never leave a truncated config
print(f"  wrote '{name}' -> {cfg_path}")
PY

cat <<BANNER

============================================================
 ✅ MCP server registered as '$SERVER_NAME'.
============================================================
 OPEN Claude Desktop and ask, e.g.:
     "What's my dictation service status?"
 It should call the status tool.

 If the tools never appear, Claude Desktop was probably
 running during the edit — fully quit it (Cmd-Q) and re-run
 this script. Details: mcp/README.md
============================================================
BANNER
