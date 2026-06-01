#!/usr/bin/env bash
#
# whisper-input-next-mac-kit — uninstaller
# Stops and removes the launchd service. Leaves the cloned app and model in place
# unless you remove them yourself (see the printed hints).
#
set -euo pipefail

LABEL="${WIN_LABEL:-com.whisper-input-next.kit}"
APP_DIR="${WIN_APP_DIR:-$HOME/Whisper-Input-Next}"
UID_NUM="$(id -u)"

echo "Stopping & removing LaunchAgent ($LABEL) ..."
launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/${LABEL}.plist"
echo "Done — the service will no longer run."
echo

# ---------- Claude Desktop MCP entry (only present if you ran install-mcp.sh) ----------
SERVER_NAME="${WIN_MCP_NAME:-whisper-input}"
CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [ -f "$CFG" ] && grep -q "\"${SERVER_NAME}\"" "$CFG" 2>/dev/null; then
  if pgrep -x "Claude" >/dev/null 2>&1; then
    echo "Claude Desktop is running — not editing its config (it would clobber the change)."
    echo "  Quit Claude Desktop (Cmd-Q), then re-run this uninstaller to drop the '${SERVER_NAME}' MCP entry."
  elif command -v python3 >/dev/null 2>&1; then
    WIN_CFG="$CFG" WIN_SERVER_NAME="$SERVER_NAME" python3 - <<'PY'
import json, os, sys
cfg = os.environ["WIN_CFG"]; name = os.environ["WIN_SERVER_NAME"]
try:
    with open(cfg, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
servers = data.get("mcpServers", {})
if name in servers:
    del servers[name]
    tmp = cfg + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False); f.write("\n")
    os.replace(tmp, cfg)
    print(f"Removed MCP server '{name}' from Claude Desktop config.")
PY
  else
    echo "Remove the '${SERVER_NAME}' entry under mcpServers in: $CFG"
  fi
  echo
fi
echo "Left in place (remove manually if you want):"
echo "  • App checkout:   rm -rf \"$APP_DIR\""
echo "  • whisper-cpp:    brew uninstall whisper-cpp"
echo "  • Permissions:    revoke in System Settings → Privacy & Security"
