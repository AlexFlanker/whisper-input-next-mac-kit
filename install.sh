#!/usr/bin/env bash
#
# whisper-input-next-mac-kit — one-shot installer
#
# Sets up Mor-Li/Whisper-Input-Next in local whisper.cpp mode with this kit's
# enhancements (single-tap Right-Command toggle, Submarine/Glass sound cues,
# Ctrl+F -> local routing) and registers it as a launchd auto-start service.
#
# IMPORTANT: this installer does NOT contain upstream source code. It clones the
# official Whisper-Input-Next repository onto your machine and applies a small,
# idempotent set of enhancements. See CREDITS.md.
#
set -euo pipefail

# ---------- config (override via env vars) ----------
UPSTREAM_REPO="${WIN_UPSTREAM:-https://github.com/Mor-Li/Whisper-Input-Next.git}"
PIN_COMMIT="${WIN_COMMIT:-5edec44bd66ca0a75c75c485d5af2fd201ce8c17}"
APP_DIR="${WIN_APP_DIR:-$HOME/Whisper-Input-Next}"
MODEL="${WIN_MODEL:-large-v3-turbo}"          # large-v3-turbo | large-v3 | medium | small | base ...
LABEL="${WIN_LABEL:-com.whisper-input-next.kit}"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c()    { printf '\033[1;36m[win-kit]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[win-kit]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[win-kit ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- preflight ----------
[ "$(uname -s)" = "Darwin" ] || die "This kit is macOS-only."
command -v git  >/dev/null 2>&1 || die "git not found — run: xcode-select --install"
command -v brew >/dev/null 2>&1 || die "Homebrew required — see https://brew.sh"

if ! command -v uv >/dev/null 2>&1; then
  c "Installing uv (fast Python package manager) ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv install failed — see https://docs.astral.sh/uv/"

command -v ffmpeg >/dev/null 2>&1 || { c "Installing ffmpeg ..."; brew install ffmpeg; }

WHISPER_CLI="$(command -v whisper-cli || true)"
if [ -z "$WHISPER_CLI" ]; then
  c "Installing whisper-cpp ..."
  brew install whisper-cpp
  WHISPER_CLI="$(command -v whisper-cli || echo /opt/homebrew/bin/whisper-cli)"
fi
[ -x "$WHISPER_CLI" ] || die "whisper-cli not found after install."
c "whisper-cli: $WHISPER_CLI"

# ---------- clone upstream (pinned) ----------
if [ -d "$APP_DIR/.git" ]; then
  c "Found existing checkout at $APP_DIR (skipping clone)."
else
  c "Cloning upstream Whisper-Input-Next -> $APP_DIR ..."
  git clone "$UPSTREAM_REPO" "$APP_DIR"
fi
c "Pinning to commit ${PIN_COMMIT:0:12} (so the patches apply cleanly) ..."
git -C "$APP_DIR" checkout -q "$PIN_COMMIT" 2>/dev/null \
  || warn "Could not checkout pinned commit; using current HEAD (patches may need adjusting)."

# ---------- venv + deps ----------
c "Creating uv venv (Python 3.12) and installing dependencies ..."
uv venv --python 3.12 "$APP_DIR/.venv"
( cd "$APP_DIR" && uv pip install --python "$APP_DIR/.venv/bin/python" -r requirements.txt )
VENV_PY="$APP_DIR/.venv/bin/python"
REAL_PY="$("$VENV_PY" -c 'import os,sys;print(os.path.realpath(sys.executable))')"

# ---------- model ----------
MODEL_FILE="$APP_DIR/models/ggml-${MODEL}.bin"
if [ -f "$MODEL_FILE" ]; then
  c "Model already present: $MODEL_FILE"
else
  mkdir -p "$APP_DIR/models"
  c "Downloading whisper model ggml-${MODEL}.bin (this can be large) ..."
  if ! curl -L --fail -o "$MODEL_FILE" \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL}.bin"; then
    warn "HuggingFace download failed; trying hf-mirror.com ..."
    curl -L --fail -o "$MODEL_FILE" \
        "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL}.bin" \
        || die "Model download failed. Set WIN_MODEL to a smaller model and retry."
  fi
fi

# ---------- kit-owned UI components (listening indicators; not upstream code) ----------
c "Installing the listening-indicator overlays (ring + capsule) ..."
mkdir -p "$APP_DIR/src/ui"
cp "$KIT_DIR/payload/listening_indicator.py" "$APP_DIR/src/ui/listening_indicator.py"
cp "$KIT_DIR/payload/capsule_indicator.py"  "$APP_DIR/src/ui/capsule_indicator.py"

# ---------- enhancements ----------
c "Applying enhancements (sounds / right-Cmd toggle / Ctrl+F->local / punctuation / cleanup / indicator) ..."
"$VENV_PY" "$KIT_DIR/scripts/apply_enhancements.py" "$APP_DIR"

# ---------- .env ----------
if [ -f "$APP_DIR/.env" ]; then
  c ".env already exists (leaving it untouched)."
else
  c "Writing .env ..."
  sed -e "s#__WHISPER_CLI_PATH__#${WHISPER_CLI}#g" \
      -e "s#__WHISPER_MODEL_PATH__#${MODEL_FILE}#g" \
      "$KIT_DIR/templates/env.local.template" > "$APP_DIR/.env"
fi

# ---------- LaunchAgent ----------
mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR/logs"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
c "Writing LaunchAgent -> $PLIST ..."
sed -e "s#__LABEL__#${LABEL}#g" \
    -e "s#__VENV_PY__#${VENV_PY}#g" \
    -e "s#__APP_DIR__#${APP_DIR}#g" \
    "$KIT_DIR/templates/launchagent.plist.template" > "$PLIST"

UID_NUM="$(id -u)"
launchctl bootout    "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
launchctl bootstrap  "gui/${UID_NUM}" "$PLIST"
launchctl enable     "gui/${UID_NUM}/${LABEL}"
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"

# ---------- done ----------
cat <<BANNER

============================================================
 ✅ Installed. The service is running and auto-starts at login.
============================================================

 ⚠️  ONE-TIME PERMISSIONS — grant these to this Python binary:
       $REAL_PY

 System Settings → Privacy & Security → enable the binary under:
     • Input Monitoring   (listen to the hotkey)
     • Accessibility      (paste text at the cursor)
     • Microphone         (record audio)
 (If it isn't listed, click +, press Cmd-Shift-G, paste the path above.)

 Then restart the service:
     launchctl kickstart -k gui/${UID_NUM}/${LABEL}

 ▶️  USE IT: focus any text field → tap RIGHT ⌘ (Submarine sound) →
     speak → tap RIGHT ⌘ → wait for the Glass sound → text is pasted.

 Manage:
     launchctl kickstart -k gui/${UID_NUM}/${LABEL}   # start / restart
     launchctl bootout    gui/${UID_NUM}/${LABEL}     # stop
     tail -f "$APP_DIR/logs/launchd.err.log"          # view logs
     "$KIT_DIR/install-mcp.sh"                         # (optional) manage from Claude Desktop via MCP
     "$KIT_DIR/uninstall.sh"                           # uninstall

============================================================
BANNER

open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent" 2>/dev/null || true
