"""持久转写日志：每一条听写都追加到一个 JSONL 文件，供 MCP / Claude 查询。

文件：audio_archive/hub/transcripts.jsonl。和 audio_archive/cache.json 不同：cache.json 是 24 小时滚动缓存（跟着录音一起被清理），
这里是 append-only 的长期记录——不会被归档清理删除，是"个人 wiki / 语音笔记"的原始语料。

每行一条 JSON：
  {"id": "3f9c0a1b2c4d", "ts": "2026-11-06T21:33:31-08:00", "source": "mac", "text": "...",
   "engine": "qwen3", "model": "mlx-community/Qwen3-ASR-1.7B-bf16", "duration_s": 8.0,
   "mode": "transcriptions", "audio": "recording_20261106_213329.wav"}
source：mac（本机热键）| iphone | pendant（录音笔）| 其它自定义来源（[a-z0-9_-]{1,32}）。

TRANSCRIPTS_ENABLED=false 关闭；TRANSCRIPTS_FILE 改路径。任何异常只记日志，绝不影响听写。
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import uuid
from datetime import datetime

from src.utils.logger import logger

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOCK = threading.Lock()
SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def log_path() -> str:
    # audio_archive/hub/：已被 git 忽略，且被 archive.py 的旧版迁移逻辑放行（不会被挪进 audio/）
    default = os.path.join(_APP_DIR, "audio_archive", "hub", "transcripts.jsonl")
    return os.path.expanduser(os.getenv("TRANSCRIPTS_FILE", default) or default)


def enabled() -> bool:
    return os.getenv("TRANSCRIPTS_ENABLED", "true").lower() == "true"


def engine_of(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    if "qwen" in m:
        return "qwen3"
    if "ggml" in m or "whisper" in m:
        return "whisper"
    return None


def wav_seconds(audio_bytes: bytes | None) -> float | None:
    if not audio_bytes:
        return None
    try:
        import soundfile as sf

        return round(sf.info(io.BytesIO(audio_bytes)).duration, 2)
    except Exception:  # noqa: BLE001
        return None


def append(
    text: str,
    *,
    source: str = "mac",
    model: str | None = None,
    engine: str | None = None,
    duration_s: float | None = None,
    mode: str = "transcriptions",
    audio: str | None = None,
    ts: str | None = None,
) -> dict | None:
    """追加一条记录，返回写入的记录；关闭 / 空文本 / 出错时返回 None。"""
    if not enabled() or not text or not text.strip():
        return None
    if not SOURCE_RE.match(source or ""):
        source = "unknown"
    rec: dict = {
        "id": uuid.uuid4().hex[:12],
        "ts": ts or datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source,
        "text": text.strip(),
    }
    eng = engine or engine_of(model)
    if eng:
        rec["engine"] = eng
    if model:
        rec["model"] = model
    if duration_s is not None:
        rec["duration_s"] = round(float(duration_s), 2)
    if mode and mode != "transcriptions":
        rec["mode"] = mode
    if audio:
        rec["audio"] = audio
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return rec
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[转写日志] 写入失败：{exc}")
        return None


def backfill_from_cache(cache_path: str) -> int:
    """日志还不存在时，把 24h 缓存里现有的记录导入一次（按时间排序），返回导入条数。"""
    path = log_path()
    if not enabled() or os.path.exists(path) or not os.path.exists(cache_path):
        return 0
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        items = sorted(cache.items(), key=lambda kv: kv[1].get("timestamp", ""))
        n = 0
        for audio_name, v in items:
            ts = v.get("timestamp")
            if ts:
                try:
                    ts = datetime.fromisoformat(ts).astimezone().isoformat(timespec="seconds")
                except ValueError:
                    ts = None
            if append(v.get("transcription", ""), source="mac", model=v.get("model"),
                      mode=v.get("mode", "transcriptions"), audio=audio_name, ts=ts):
                n += 1
        if n:
            logger.info(f"[转写日志] 已从 cache.json 导入 {n} 条历史记录 → {path}")
        return n
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[转写日志] 导入 cache.json 失败：{exc}")
        return 0
