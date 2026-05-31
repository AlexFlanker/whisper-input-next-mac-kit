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
echo "Left in place (remove manually if you want):"
echo "  • App checkout:   rm -rf \"$APP_DIR\""
echo "  • whisper-cpp:    brew uninstall whisper-cpp"
echo "  • Permissions:    revoke in System Settings → Privacy & Security"
