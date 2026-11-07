#!/usr/bin/env bash
# Connect this Mac (a dictation client, e.g. your MacBook) to a voice hub (e.g. a Mac mini at home).
#
#   ./scripts/connect_hub.sh <hub-host> [port]
#     <hub-host>  mac-mini.local (home Wi-Fi only) or the hub's Tailscale name / 100.x IP (works anywhere)
#
# Fetches the hub's inbox token over ssh (enable System Settings → General → Sharing → Remote Login
# on the hub), checks the hub answers, writes HUB_URL / HUB_TOKEN into this Mac's app .env, copies
# your vocabulary.txt to the hub and restarts the dictation service. From then on every dictation is also synced to the hub (queued
# locally while the hub is unreachable). The token is never printed.
#
# No ssh? Copy the token yourself and run:  HUB_TOKEN=<token> ./scripts/connect_hub.sh <hub-host>
set -euo pipefail

HOST="${1:?usage: connect_hub.sh <hub-host> [port]}"
PORT="${2:-8765}"
APP_DIR="${WIN_APP_DIR:-$HOME/Whisper-Input-Next}"
REMOTE_APP_DIR="${WIN_REMOTE_APP_DIR:-Whisper-Input-Next}"   # relative to the hub user's home
URL="http://${HOST}:${PORT}"

[ -f "$APP_DIR/.env" ] || { echo "❌ $APP_DIR/.env not found — install the kit on this Mac first"; exit 1; }

if [ -n "${HUB_TOKEN:-}" ]; then
  TOKEN="$HUB_TOKEN"
else
  echo "🔑 Fetching the hub token from ${HOST} over ssh ..."
  TOKEN="$(ssh -o ConnectTimeout=8 "$HOST" "cat ${REMOTE_APP_DIR}/audio_archive/hub/.inbox_token")" || {
    echo "❌ ssh failed. Enable Remote Login on the hub, or pass HUB_TOKEN=... yourself."; exit 1; }
fi
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
[ -n "$TOKEN" ] || { echo "❌ empty token"; exit 1; }

echo "🩺 Checking ${URL}/v1/health ..."
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H "Authorization: Bearer ${TOKEN}" "${URL}/v1/health" || true)"
if [ "$CODE" != "200" ]; then
  echo "❌ The hub didn't answer (HTTP ${CODE:-none}). Is it running, and is ${HOST} reachable from here?"
  exit 1
fi

HUB_URL_VAL="$URL" HUB_TOKEN_VAL="$TOKEN" python3 - "$APP_DIR/.env" <<'PY'
import os, re, sys
path = sys.argv[1]
want = {"HUB_URL": os.environ["HUB_URL_VAL"], "HUB_TOKEN": os.environ["HUB_TOKEN_VAL"]}
lines = open(path, encoding="utf-8").read().splitlines()
done = set()
for i, line in enumerate(lines):
    for k, v in want.items():
        if not line.lstrip().startswith("#") and re.match(rf"\s*{k}\s*=", line):
            lines[i] = f"{k}={v}"; done.add(k)
for k, v in want.items():
    if k not in done:
        lines.append(f"{k}={v}")
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
chmod 600 "$APP_DIR/.env"

# Give the hub the same vocabulary, so audio it transcribes (recorder, phone uploads) gets your terms
# right too. Only when we have ssh; the hub hot-reloads the file, no restart needed.
if [ -z "${HUB_TOKEN:-}" ] && [ -f "$APP_DIR/vocabulary.txt" ]; then
  if scp -q -o ConnectTimeout=8 "$APP_DIR/vocabulary.txt" "${HOST}:${REMOTE_APP_DIR}/vocabulary.txt"; then
    echo "📖 Copied your vocabulary.txt to the hub"
  else
    echo "⚠️  Couldn't copy vocabulary.txt to the hub (copy it yourself to keep recordings' terms right)"
  fi
fi

# Restart the dictation service (find its LaunchAgent by the app path it runs).
PLIST="$(grep -l "${APP_DIR}/main.py" "$HOME"/Library/LaunchAgents/*.plist 2>/dev/null | head -1 || true)"
if [ -n "$PLIST" ]; then
  LABEL="$(basename "$PLIST" .plist)"
  launchctl kickstart -k "gui/$(id -u)/${LABEL}"
  echo "🔄 Restarted ${LABEL}"
else
  echo "⚠️  Couldn't find the dictation LaunchAgent — restart the service yourself."
fi
echo "✅ Connected to ${URL}. Dictations sync there from now on (queued while it's unreachable)."
