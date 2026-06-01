#!/usr/bin/env python3
"""whisper-input-mcp — a thin MCP server to monitor & configure the local
Whisper-Input dictation service (github.com/Mor-Li/Whisper-Input-Next) from any
MCP client such as Claude Desktop.

It is deliberately thin: it wraps the app's `.env` file and `launchctl`. No UI,
no database, no long-running daemon (the MCP client spawns it on demand over stdio).

Environment:
  WIN_APP_DIR   app checkout dir   (default: ~/Whisper-Input-Next)
  WIN_LABEL     launchd label      (default: com.whisper-input-next.kit)
"""
import json
import os
import re
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

APP_DIR = Path(os.environ.get("WIN_APP_DIR", os.path.expanduser("~/Whisper-Input-Next")))
LABEL = os.environ.get("WIN_LABEL", "com.whisper-input-next.kit")
ENV_PATH = APP_DIR / ".env"
LOG_PATH = APP_DIR / "logs" / "launchd.err.log"
CACHE_PATH = APP_DIR / "audio_archive" / "cache.json"
MODELS_DIR = APP_DIR / "models"

# Only these .env keys may be written via set_config (safety allowlist).
ALLOWED_KEYS = {
    "WHISPER_MODEL_PATH", "WHISPER_PROMPT", "WHISPER_FULLWIDTH_PUNCT",
    "AUDIO_ARCHIVE_RETENTION_HOURS", "AUDIO_ARCHIVE_CLEANUP_INTERVAL_HOURS",
    "SOUND_START", "SOUND_STOP", "SOUND_DONE", "SOUND_ERROR", "SOUND_WARNING",
    "SHOW_INDICATOR",
    "TRANSCRIPTIONS_BUTTON", "TRANSLATIONS_BUTTON", "TRANSCRIPTION_SERVICE",
    "CONVERT_TO_SIMPLIFIED", "ADD_SYMBOL", "OPTIMIZE_RESULT", "AUTO_RETRY_LIMIT",
}

mcp = FastMCP("whisper-input")


def _uid() -> str:
    return str(os.getuid())


def _launchctl(*args) -> str:
    try:
        return subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return f"(launchctl error: {exc})"


def _read_env() -> dict:
    cfg: dict = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def _write_env_key(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for line in lines:
        if not line.lstrip().startswith("#") and re.match(rf"\s*{re.escape(key)}\s*=", line):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


@mcp.tool()
def status() -> dict:
    """Is the dictation service running? Returns pid, current model, and a config summary."""
    info = _launchctl("print", f"gui/{_uid()}/{LABEL}")
    pid_match = re.search(r"pid = (\d+)", info)
    cfg = _read_env()
    return {
        "running": "state = running" in info,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "label": LABEL,
        "app_dir": str(APP_DIR),
        "model": os.path.basename(cfg.get("WHISPER_MODEL_PATH", "?")),
        "transcription_service": cfg.get("TRANSCRIPTION_SERVICE", "?"),
        "sounds": {k: cfg.get(k) for k in ("SOUND_START", "SOUND_STOP", "SOUND_DONE", "SOUND_ERROR")},
        "punctuation_prompt_on": bool(cfg.get("WHISPER_PROMPT")),
        "fullwidth_punct": cfg.get("WHISPER_FULLWIDTH_PUNCT", "true"),
        "archive_retention_hours": cfg.get("AUDIO_ARCHIVE_RETENTION_HOURS", "24"),
        "hotkey": "tap Right-Cmd (or "
                  f"{cfg.get('TRANSLATIONS_BUTTON', 'ctrl')}+{cfg.get('TRANSCRIPTIONS_BUTTON', 'f')})",
    }


@mcp.tool()
def logs(lines: int = 40) -> str:
    """Return the last N lines of the service's error log."""
    if not LOG_PATH.exists():
        return "(no log file yet)"
    rows = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(rows[-max(1, min(lines, 500)):])


@mcp.tool()
def get_config() -> dict:
    """Return the relevant settings from the app's .env."""
    cfg = _read_env()
    return {k: cfg[k] for k in sorted(ALLOWED_KEYS) if k in cfg}


@mcp.tool()
def set_config(key: str, value: str) -> dict:
    """Set one allowlisted .env key. Call restart() afterwards to apply it."""
    if key not in ALLOWED_KEYS:
        return {"ok": False, "error": f"key not allowed: {key}", "allowed": sorted(ALLOWED_KEYS)}
    _write_env_key(key, value)
    return {"ok": True, "key": key, "value": value, "note": "call restart() to apply"}


@mcp.tool()
def restart() -> dict:
    """Restart the dictation service so config changes take effect."""
    _launchctl("kickstart", "-k", f"gui/{_uid()}/{LABEL}")
    return {"ok": True, "note": "service restarted"}


@mcp.tool()
def recent_transcriptions(limit: int = 10) -> list:
    """Return the most recent transcription results the service produced (from cache.json)."""
    if not CACHE_PATH.exists():
        return []
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    items = sorted(cache.items(), key=lambda kv: kv[1].get("timestamp", ""), reverse=True)
    return [
        {"time": v.get("timestamp"), "text": v.get("transcription"), "model": v.get("model")}
        for _, v in items[: max(1, min(limit, 100))]
    ]


@mcp.tool()
def list_models() -> dict:
    """List downloaded whisper models and which one is currently configured."""
    downloaded = sorted(p.name for p in MODELS_DIR.glob("ggml-*.bin")) if MODELS_DIR.exists() else []
    return {"downloaded": downloaded, "current": os.path.basename(_read_env().get("WHISPER_MODEL_PATH", "?"))}


@mcp.tool()
def download_model(name: str) -> dict:
    """Download a whisper.cpp ggml model (e.g. 'large-v3-turbo'); falls back to hf-mirror.com."""
    base = name[len("ggml-"):-4] if name.startswith("ggml-") and name.endswith(".bin") else name
    fname = f"ggml-{base}.bin"
    target = MODELS_DIR / fname
    if target.exists():
        return {"ok": True, "model": fname, "note": "already present"}
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    urls = [
        f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{fname}",
        f"https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/{fname}",  # GFW-friendly fallback
    ]
    last_err = ""
    for url in urls:
        r = subprocess.run(["curl", "-L", "--fail", "-o", str(target), url], capture_output=True, text=True)
        if r.returncode == 0:
            return {"ok": True, "model": fname, "size_mb": round(target.stat().st_size / 1e6, 1), "source": url}
        last_err = (r.stderr or "download failed")[-300:]
        if target.exists():
            target.unlink()
    return {"ok": False, "error": last_err, "urls": urls}


@mcp.tool()
def set_model(name: str) -> dict:
    """Switch the whisper model by name (e.g. 'large-v3-turbo'); downloads it first if missing. Call restart() after."""
    base = name[len("ggml-"):-4] if name.startswith("ggml-") and name.endswith(".bin") else name
    target = MODELS_DIR / f"ggml-{base}.bin"
    if not target.exists():
        dl = download_model(base)
        if not dl.get("ok"):
            return dl
    _write_env_key("WHISPER_MODEL_PATH", str(target))
    return {"ok": True, "model": target.name, "note": "call restart() to apply"}


if __name__ == "__main__":
    mcp.run()
