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
ENGINE_STATUS_PATH = APP_DIR / "logs" / "asr_engine.json"  # written by src/transcription/qwen3_asr.py
ASR_ENGINES = ("qwen3", "whisper")

# Only these .env keys may be written via set_config (safety allowlist).
ALLOWED_KEYS = {
    "WHISPER_MODEL_PATH", "WHISPER_PROMPT", "WHISPER_FULLWIDTH_PUNCT",
    "AUDIO_ARCHIVE_RETENTION_HOURS", "AUDIO_ARCHIVE_CLEANUP_INTERVAL_HOURS",
    "SOUND_START", "SOUND_STOP", "SOUND_DONE", "SOUND_ERROR", "SOUND_WARNING",
    "SHOW_INDICATOR", "INDICATOR_STYLE",
    "POLISH_ENABLED", "POLISH_MODEL", "POLISH_MODE", "POLISH_TIMEOUT", "OLLAMA_URL",
    "TRANSCRIPTIONS_BUTTON", "TRANSLATIONS_BUTTON", "TRANSCRIPTION_SERVICE",
    "CONVERT_TO_SIMPLIFIED", "ADD_SYMBOL", "OPTIMIZE_RESULT", "AUTO_RETRY_LIMIT",
    "LOCAL_ASR_ENGINE", "QWEN_ASR_MODEL", "QWEN_ASR_TIMEOUT",
    "VOCAB_ENABLED", "VOCAB_FILE", "VOCAB_MAX_REPLACEMENTS",
    "TRANSCRIPTS_ENABLED", "TRANSCRIPTS_FILE",
    "INBOX_ENABLED", "INBOX_HOST", "INBOX_PORT", "INBOX_MAX_MB",
}
INBOX_TOKEN_PATH = APP_DIR / "audio_archive" / "hub" / ".inbox_token"  # git-ignored; the value is never returned by any tool

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


def _engine_state() -> dict:
    """Live engine state as reported by the running service (logs/asr_engine.json)."""
    try:
        return json.loads(ENGINE_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _vocab_path() -> Path:
    return Path(os.path.expanduser(_read_env().get("VOCAB_FILE") or str(APP_DIR / "vocabulary.txt")))


def _vocab_entries() -> tuple[list[str], list[tuple[str, str]]]:
    """Parse vocabulary.txt exactly like src/transcription/local_vocab.py does."""
    terms: list[str] = []
    rules: list[tuple[str, str]] = []
    path = _vocab_path()
    if not path.exists():
        return terms, rules
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            wrong, right = (s.strip() for s in line.split("=>", 1))
            if len(wrong) >= 2 and right and wrong != right:
                rules.append((wrong, right))
        elif line not in terms:
            terms.append(line)
    return terms, rules


def _transcripts_path() -> Path:
    default = APP_DIR / "audio_archive" / "hub" / "transcripts.jsonl"
    return Path(os.path.expanduser(_read_env().get("TRANSCRIPTS_FILE") or str(default)))


def _iter_transcripts():
    """Yield records from transcripts.jsonl (skips malformed lines)."""
    path = _transcripts_path()
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_when(value: str, *, end: bool = False):
    """'today' | 'yesterday' | '7d' / '12h' | 'YYYY-MM-DD' | ISO datetime -> aware datetime (local tz)."""
    from datetime import datetime, timedelta

    v = (value or "").strip().lower()
    if not v:
        return None
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "today":
        return midnight + timedelta(days=1) if end else midnight
    if v == "yesterday":
        return midnight if end else midnight - timedelta(days=1)
    m = re.fullmatch(r"(\d+)\s*([dh])", v)
    if m:
        n = int(m.group(1))
        return now - (timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n))
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"can't parse time {value!r}; use today / yesterday / 7d / 12h / YYYY-MM-DD") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    if end and len(value.strip()) == 10:  # a bare date as `until` means the end of that day
        dt += timedelta(days=1)
    return dt


def _rec_time(rec: dict):
    from datetime import datetime

    try:
        return datetime.fromisoformat(rec.get("ts", ""))
    except ValueError:
        return None


@mcp.tool()
def transcripts_recent(limit: int = 20, source: str = "") -> dict:
    """The most recent dictations / voice notes from the persistent transcript log (newest first).

    source: '' for all, or 'mac' (hotkey dictation on this Mac), 'iphone', 'pendant' (voice recorder), ...
    """
    limit = max(1, min(int(limit), 200))
    rows = [r for r in _iter_transcripts() if not source or r.get("source") == source]
    rows = rows[-limit:][::-1]
    return {"file": str(_transcripts_path()), "count": len(rows), "transcripts": rows}


@mcp.tool()
def transcripts_search(query: str = "", since: str = "", until: str = "", source: str = "", limit: int = 50) -> dict:
    """Search everything you've dictated or recorded (persistent transcript log), newest first.

    query: words that must all appear (case-insensitive; Chinese works as plain substrings). Empty = any.
    since / until: 'today', 'yesterday', '7d', '12h', 'YYYY-MM-DD' or an ISO datetime.
    source: '' for all, or 'mac' / 'iphone' / 'pendant' / ...
    Use this to answer things like "what did I say about X this week" or to collect voice notes to summarize.
    """
    try:
        t0 = _parse_when(since)
        t1 = _parse_when(until, end=True)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    terms = [t.lower() for t in (query or "").split() if t]
    limit = max(1, min(int(limit), 500))
    hits, total = [], 0
    for r in _iter_transcripts():
        if source and r.get("source") != source:
            continue
        ts = _rec_time(r)
        if (t0 or t1) and ts is None:
            continue
        if t0 and ts < t0:
            continue
        if t1 and ts >= t1:
            continue
        text = str(r.get("text", "")).lower()
        if all(t in text for t in terms):
            total += 1
            hits.append(r)
    hits = hits[-limit:][::-1]
    return {"ok": True, "matches": total, "returned": len(hits), "transcripts": hits}


@mcp.tool()
def inbox_info() -> dict:
    """How an iPhone app or voice recorder can send text/audio to this Mac (the inbox endpoint).

    Never returns the token itself — it lives in a file on this Mac (chmod 600).
    """
    cfg = _read_env()
    host = cfg.get("INBOX_HOST", "127.0.0.1") or "127.0.0.1"
    port = cfg.get("INBOX_PORT", "8765") or "8765"
    base = f"http://{host if host not in ('0.0.0.0', '::') else '<this-mac-ip>'}:{port}"
    tok = f"$(cat {INBOX_TOKEN_PATH})"
    return {
        "enabled": cfg.get("INBOX_ENABLED", "true"),
        "listen": f"{host}:{port}",
        "reachable_from": "this Mac only" if host in ("127.0.0.1", "localhost", "::1") else "your network",
        "token_file": str(INBOX_TOKEN_PATH),
        "token_present": INBOX_TOKEN_PATH.exists() or bool(cfg.get("INBOX_TOKEN")),
        "endpoints": {
            "health": f"GET {base}/v1/health",
            "text": f"POST {base}/v1/text  JSON {{\"text\": \"...\", \"source\": \"iphone\"}}",
            "audio": f"POST {base}/v1/audio?source=pendant  (body = WAV/FLAC/MP3 file)",
        },
        "examples": [
            f"curl -s -H \"Authorization: Bearer {tok}\" {base}/v1/health",
            f"curl -s -H \"Authorization: Bearer {tok}\" -H 'Content-Type: application/json' "
            f"-d '{{\"text\":\"hello from my phone\",\"source\":\"iphone\"}}' {base}/v1/text",
            f"curl -s -H \"Authorization: Bearer {tok}\" --data-binary @note.wav '{base}/v1/audio?source=pendant'",
        ],
        "to_open_for_phone": "set INBOX_HOST=0.0.0.0 (LAN) or your Tailscale IP, then restart()",
    }


@mcp.tool()
def status() -> dict:
    """Is the dictation service running? Returns pid, the active ASR engine/model, and a config summary."""
    info = _launchctl("print", f"gui/{_uid()}/{LABEL}")
    pid_match = re.search(r"pid = (\d+)", info)
    cfg = _read_env()
    engine = cfg.get("LOCAL_ASR_ENGINE", "qwen3")
    live = _engine_state()
    whisper_model = os.path.basename(cfg.get("WHISPER_MODEL_PATH", "?"))
    qwen_ready = live.get("engine") == "qwen3" and live.get("state") == "ready"
    terms, rules = _vocab_entries()
    return {
        "running": "state = running" in info,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "label": LABEL,
        "app_dir": str(APP_DIR),
        "model": live.get("model", "?") if qwen_ready else whisper_model,
        "asr_engine": {
            "configured": engine,
            "state": live.get("state", "unknown"),  # loading | downloading | ready | failed
            "in_use": "qwen3" if qwen_ready else "whisper",
            "qwen_model": cfg.get("QWEN_ASR_MODEL", "mlx-community/Qwen3-ASR-1.7B-bf16"),
            "whisper_model": whisper_model,
            **({"error": live["error"]} if live.get("error") else {}),
            **({"load_seconds": live["load_seconds"]} if live.get("load_seconds") else {}),
        },
        "vocabulary": {"file": str(_vocab_path()), "terms": len(terms), "rules": len(rules),
                       "enabled": cfg.get("VOCAB_ENABLED", "true")},
        "transcripts": {"file": str(_transcripts_path()), "count": sum(1 for _ in _iter_transcripts()),
                        "enabled": cfg.get("TRANSCRIPTS_ENABLED", "true")},
        "inbox": {"enabled": cfg.get("INBOX_ENABLED", "true"),
                  "listen": f"{cfg.get('INBOX_HOST', '127.0.0.1')}:{cfg.get('INBOX_PORT', '8765')}"},
        "transcription_service": cfg.get("TRANSCRIPTION_SERVICE", "?"),
        "sounds": {k: cfg.get(k) for k in ("SOUND_START", "SOUND_STOP", "SOUND_DONE", "SOUND_ERROR")},
        "punctuation_prompt_on": bool(cfg.get("WHISPER_PROMPT")),
        "fullwidth_punct": cfg.get("WHISPER_FULLWIDTH_PUNCT", "true"),
        "archive_retention_hours": cfg.get("AUDIO_ARCHIVE_RETENTION_HOURS", "24"),
        "indicator": {
            "show": cfg.get("SHOW_INDICATOR", "true"),
            "style": cfg.get("INDICATOR_STYLE", "wave"),
        },
        "polish": {
            "enabled": cfg.get("POLISH_ENABLED", "false"),
            "model": cfg.get("POLISH_MODEL", "?"),
            "mode": cfg.get("POLISH_MODE", "light"),
        },
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
def set_indicator_style(style: str, restart_now: bool = True) -> dict:
    """Switch the on-screen listening indicator and (by default) restart to apply it.

    style:
      'wave'    — three translucent sine waves that follow your live voice level (bottom-center pill)
      'ring'    — transparent glowing breathing ring (bottom-center)
      'capsule' — a pill that retracts to a spinner, green on done
      'off'     — hide the indicator entirely
    """
    s = (style or "").strip().lower()
    if s in ("waveform", "流光", "流光波形"):
        s = "wave"
    if s in ("off", "none", "false", "hidden", "hide"):
        _write_env_key("SHOW_INDICATOR", "false")
        applied = {"SHOW_INDICATOR": "false"}
    elif s in ("wave", "ring", "capsule"):
        _write_env_key("SHOW_INDICATOR", "true")
        _write_env_key("INDICATOR_STYLE", s)
        applied = {"SHOW_INDICATOR": "true", "INDICATOR_STYLE": s}
    else:
        return {"ok": False, "error": f"unknown style: {style!r}", "valid": ["wave", "ring", "capsule", "off"]}
    result = {"ok": True, "applied": applied}
    if restart_now:
        _launchctl("kickstart", "-k", f"gui/{_uid()}/{LABEL}")
        result["restarted"] = True
    else:
        result["note"] = "call restart() to apply"
    return result


@mcp.tool()
def set_polish(mode: str = "", model: str = "", enabled: str = "", restart_now: bool = True) -> dict:
    """Configure the post-transcription local-LLM polish (Ollama) and restart to apply.

    mode:    'light' (faithful — only fixes punctuation/typos) | 'concise' (trims filler, more concise)
    model:   an Ollama tag, e.g. 'glm4' (GLM-4-9B), 'qwen2.5:3b'
    enabled: 'on' | 'off' to toggle the whole feature
    Leave any field empty to keep it unchanged. Polish needs Ollama running + the model pulled.
    """
    applied: dict = {}
    e = enabled.strip().lower()
    if e in ("on", "true", "1", "yes"):
        _write_env_key("POLISH_ENABLED", "true"); applied["POLISH_ENABLED"] = "true"
    elif e in ("off", "false", "0", "no"):
        _write_env_key("POLISH_ENABLED", "false"); applied["POLISH_ENABLED"] = "false"
    elif e:
        return {"ok": False, "error": f"enabled must be on/off, got {enabled!r}"}
    if mode:
        m = mode.strip().lower()
        if m not in ("light", "concise"):
            return {"ok": False, "error": f"unknown mode: {mode!r}", "valid": ["light", "concise"]}
        _write_env_key("POLISH_MODE", m); applied["POLISH_MODE"] = m
    if model:
        _write_env_key("POLISH_MODEL", model.strip()); applied["POLISH_MODEL"] = model.strip()
    if not applied:
        return {"ok": False, "error": "nothing to set; provide mode / model / enabled"}
    result = {"ok": True, "applied": applied}
    if restart_now:
        _launchctl("kickstart", "-k", f"gui/{_uid()}/{LABEL}")
        result["restarted"] = True
    else:
        result["note"] = "call restart() to apply"
    return result


@mcp.tool()
def set_asr_engine(engine: str, restart_now: bool = True) -> dict:
    """Switch the local speech-recognition engine and (by default) restart to apply it.

    engine:
      'qwen3'   — Qwen3-ASR via MLX (Apple Silicon). More accurate on Chinese/English code-switching
                  and your own terms, ~3x faster; stays resident (~5 GB RAM). whisper.cpp is the
                  automatic fallback while it loads or if it fails.
      'whisper' — whisper.cpp only (the original behaviour; no resident model).
    """
    e = (engine or "").strip().lower()
    if e in ("qwen", "qwen3-asr"):
        e = "qwen3"
    if e not in ASR_ENGINES:
        return {"ok": False, "error": f"unknown engine: {engine!r}", "valid": list(ASR_ENGINES)}
    _write_env_key("LOCAL_ASR_ENGINE", e)
    result = {"ok": True, "applied": {"LOCAL_ASR_ENGINE": e}}
    if restart_now:
        _launchctl("kickstart", "-k", f"gui/{_uid()}/{LABEL}")
        result["restarted"] = True
        if e == "qwen3":
            result["note"] = "Qwen3-ASR loads in the background (seconds when cached); whisper.cpp covers until then. Check status()."
    else:
        result["note"] = "call restart() to apply"
    return result


@mcp.tool()
def vocab_list() -> dict:
    """List the local vocabulary: terms (hotwords fed to Qwen3-ASR) and `wrong => right` correction rules."""
    terms, rules = _vocab_entries()
    return {
        "file": str(_vocab_path()),
        "terms": terms,
        "rules": [{"wrong": w, "right": r} for w, r in rules],
        "note": "edits take effect on the next dictation (hot-reloaded, no restart needed)",
    }


@mcp.tool()
def vocab_add(entry: str) -> dict:
    """Add a term (e.g. 'Kubernetes') or a correction rule (e.g. 'Cloud => Claude') to vocabulary.txt.

    Terms are hotwords for Qwen3-ASR. Rules rewrite the transcript for either engine: English matches
    whole words case-insensitively, Chinese matches literally; the left side needs >= 2 characters.
    Rules apply everywhere they match, so only add one when the left side is (almost) always a mishearing.
    """
    line = (entry or "").strip()
    if not line or line.startswith("#") or "\n" in line:
        return {"ok": False, "error": "give one term, or one rule like 'wrong => right'"}
    terms, rules = _vocab_entries()
    if "=>" in line:
        wrong, right = (s.strip() for s in line.split("=>", 1))
        if len(wrong) < 2 or not right or wrong == right:
            return {"ok": False, "error": "rule needs a left side of >= 2 characters and a different right side"}
        if any(w.lower() == wrong.lower() for w, _ in rules):
            return {"ok": False, "error": f"a rule for {wrong!r} already exists; vocab_remove it first"}
        line = f"{wrong} => {right}"
    elif line in terms:
        return {"ok": True, "note": "already present", "entry": line}
    path = _vocab_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + sep + line + "\n", encoding="utf-8")
    return {"ok": True, "added": line, "file": str(path), "note": "active from the next dictation"}


@mcp.tool()
def vocab_remove(entry: str) -> dict:
    """Remove a term, or a rule (pass 'wrong => right' or just its left side), from vocabulary.txt."""
    target = (entry or "").strip()
    path = _vocab_path()
    if not target or not path.exists():
        return {"ok": False, "error": "nothing to remove"}
    want_rule = target.split("=>", 1)[0].strip().lower() if "=>" in target else None
    kept, removed = [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        hit = False
        if line and not line.startswith("#"):
            if "=>" in line:
                left = line.split("=>", 1)[0].strip().lower()
                hit = left == (want_rule if want_rule is not None else target.lower())
            else:
                hit = want_rule is None and line == target
        (removed if hit else kept).append(raw)
    if not removed:
        return {"ok": False, "error": f"not found: {target!r}"}
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return {"ok": True, "removed": removed, "note": "active from the next dictation"}


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
