"""持久转写日志：每一条听写都追加到一个 JSONL 文件，供 MCP / Claude 查询。

文件：audio_archive/hub/transcripts.jsonl。和 audio_archive/cache.json 不同：cache.json 是 24 小时滚动缓存（跟着录音一起被清理），
这里是 append-only 的长期记录——不会被归档清理删除，是"个人 wiki / 语音笔记"的原始语料。

每行一条 JSON：
  {"id": "3f9c0a1b2c4d", "ts": "2026-11-06T21:33:31-08:00", "source": "mac", "text": "...",
   "engine": "qwen3", "model": "mlx-community/Qwen3-ASR-1.7B-bf16", "duration_s": 8.0,
   "mode": "transcriptions", "audio": "recording_20261106_213329.wav"}
source：mac（本机热键）| iphone | pendant（录音笔）| 其它自定义来源（[a-z0-9_-]{1,32}）。

TRANSCRIPTS_ENABLED=false 关闭；TRANSCRIPTS_FILE 改路径。任何异常只记日志，绝不影响听写。

多机（中枢模式）：
  * 中枢（如家里常开的 Mac mini）跑 hub_main.py，收件口对局域网 / Tailscale 开放，是"总账"。
  * 客户端（如随身的 MacBook）在 .env 里配 HUB_URL + HUB_TOKEN：本机照常听写、照常写本地日志，
    同时由 HubForwarder 在后台把新记录转发给中枢。连不上就留在本地（按字节偏移记进度），
    恢复后自动补发；记录带 id，中枢按 id 去重，重发不会产生重复。
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from src.utils.logger import logger

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOCK = threading.Lock()
SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_IDS: set | None = None          # 已写入记录的 id（中枢去重用），首次用到时从文件加载
_FORWARDER = None


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
    rec_id: str | None = None,
) -> dict | None:
    """追加一条记录，返回写入的记录；关闭 / 空文本 / 出错时返回 None。

    rec_id：转发来的记录自带 id；已存在则不重复写入，返回 {"id": ..., "duplicate": True}。
    """
    if not enabled() or not text or not text.strip():
        return None
    if not SOURCE_RE.match(source or ""):
        source = "unknown"
    if rec_id is not None and not ID_RE.match(str(rec_id)):
        rec_id = None
    rec: dict = {
        "id": rec_id or uuid.uuid4().hex[:12],
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
        with _LOCK:
            ids = _known_ids_locked()
            if rec["id"] in ids:
                return {"id": rec["id"], "duplicate": True}
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            ids.add(rec["id"])
        if _FORWARDER is not None:
            _FORWARDER.poke()
        return rec
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[转写日志] 写入失败：{exc}")
        return None


def _known_ids_locked() -> set:
    global _IDS
    if _IDS is None:
        _IDS = {r.get("id") for r in iter_records()}
    return _IDS


def iter_records(path: str | None = None):
    """逐条读取日志（跳过损坏的行）。"""
    path = path or log_path()
    try:
        f = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_when(value: str, *, end: bool = False):
    """'today' | 'yesterday' | '7d' / '12h' | 'YYYY-MM-DD' | ISO datetime → 带时区的 datetime。"""
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
    if end and len(value.strip()) == 10:  # 只给日期的 until = 当天结束
        dt += timedelta(days=1)
    return dt


def search(query: str = "", since: str = "", until: str = "", source: str = "", limit: int = 50) -> dict:
    """全文检索（所有词都要出现，不区分大小写），按时间倒序。语义与 MCP transcripts_search 一致。"""
    t0, t1 = parse_when(since), parse_when(until, end=True)
    terms = [t.lower() for t in (query or "").split() if t]
    limit = max(1, min(int(limit), 500))
    hits, total = [], 0
    for r in iter_records():
        if source and r.get("source") != source:
            continue
        if t0 or t1:
            try:
                ts = datetime.fromisoformat(r.get("ts", ""))
            except ValueError:
                continue
            if (t0 and ts < t0) or (t1 and ts >= t1):
                continue
        text = str(r.get("text", "")).lower()
        if all(t in text for t in terms):
            total += 1
            hits.append(r)
    hits = hits[-limit:][::-1]
    return {"ok": True, "matches": total, "returned": len(hits), "transcripts": hits}


# ---------------------------------------------------------------------------
# 客户端 → 中枢 转发（HUB_URL 配置后启用）
# ---------------------------------------------------------------------------


class HubForwarder:
    """把本地日志里还没转发的记录逐条 POST 给中枢；断网就等，恢复后从上次的位置继续。"""

    def __init__(self, url: str, token: str, interval: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.interval = interval
        self.state_path = os.path.join(os.path.dirname(log_path()), "sync_state.json")
        self._wake = threading.Event()
        self.last_ok: str | None = None
        self.last_error: str | None = None

    def poke(self) -> None:
        self._wake.set()

    def _load_offset(self) -> int:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return int(json.load(f).get("offset", 0))
        except Exception:  # noqa: BLE001
            return 0

    def _save_offset(self, offset: int) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offset": offset, "hub": self.url, "updated": datetime.now().astimezone().isoformat(timespec="seconds")}, f)
        os.replace(tmp, self.state_path)

    def _post(self, rec: dict) -> None:
        import urllib.request

        body = json.dumps({k: rec[k] for k in ("id", "ts", "text", "source", "engine", "model", "duration_s", "mode")
                           if k in rec}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url + "/v1/text", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")

    def sync_once(self) -> int:
        """转发所有待发记录，返回本次成功条数；遇到网络错误就停在原处（下次重试）。"""
        path = log_path()
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0
        offset = self._load_offset()
        if offset > size:  # 日志被换掉 / 截断：从头再来，中枢按 id 去重
            offset = 0
        if offset == size:
            return 0
        sent = 0
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):  # 半行（正在写）：下次再说
                    break
                line = raw.strip()
                if line:
                    try:
                        rec = json.loads(line.decode("utf-8"))
                    except Exception:  # noqa: BLE001
                        rec = None  # 损坏的行：跳过
                    if rec is not None:
                        self._post(rec)  # 失败抛异常 → offset 不前进
                        sent += 1
                offset += len(raw)
                self._save_offset(offset)
        return sent

    def run(self) -> None:
        while True:
            try:
                n = self.sync_once()
                self.last_ok, self.last_error = datetime.now().astimezone().isoformat(timespec="seconds"), None
                if n:
                    logger.info(f"[中枢同步] 已同步 {n} 条到 {self.url}")
            except Exception as exc:  # noqa: BLE001
                if self.last_error is None:
                    logger.info(f"[中枢同步] 暂时连不上 {self.url}（{exc}），记录留在本地，稍后自动补发")
                self.last_error = str(exc)
            self._wake.wait(self.interval)
            self._wake.clear()
            time.sleep(0.5)  # 合并连续写入


def start_forwarder():
    """配置了 HUB_URL 时启动后台转发线程（本机就是中枢时不要配置）。"""
    global _FORWARDER
    url = os.getenv("HUB_URL", "").strip()
    if not url or _FORWARDER is not None:
        return _FORWARDER
    token = os.getenv("HUB_TOKEN", "").strip()
    if not token:
        logger.warning("[中枢同步] 配了 HUB_URL 但没有 HUB_TOKEN，不转发")
        return None
    _FORWARDER = HubForwarder(url, token, float(os.getenv("HUB_SYNC_INTERVAL", "30")))
    threading.Thread(target=_FORWARDER.run, name="hub-forwarder", daemon=True).start()
    logger.info(f"[中枢同步] 已开启 → {url}")
    return _FORWARDER


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
