"""收件口（Inbox）：让 iPhone App / 录音笔把文字或音频交给这台 Mac，统一记进持久转写日志。

一个很小的本机 HTTP 服务（标准库，无额外依赖），跑在听写服务进程里，所以能直接复用已加载的
Qwen3-ASR。所有请求都要带 token（Authorization: Bearer <token>，或 X-Inbox-Token 头）。

  GET  /v1/health                     → {"ok": true, "engine": ..., "transcripts": <条数>}
  POST /v1/text   JSON {"text": "...", "source": "iphone", "ts"?, "duration_s"?, "engine"?, "model"?}
                                      → 设备已在本地转写好，只记日志
  POST /v1/audio?source=pendant       → 请求体是音频文件（WAV / FLAC / MP3 等 libsndfile 能读的格式），
                                        用本机 Qwen3-ASR 转写后记日志，返回 {"text": ...}

配置（.env）：
  INBOX_ENABLED=true        关闭则不监听
  INBOX_HOST=127.0.0.1      只本机可访问；给手机用时改成 0.0.0.0（局域网）或 Tailscale 地址
  INBOX_PORT=8765
  INBOX_TOKEN=              留空则自动生成并保存在 <app>/audio_archive/hub/.inbox_token（权限 600，不进 git）
  INBOX_MAX_MB=200          单个上传上限
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.transcription import transcript_log
from src.utils.logger import logger

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKEN_PATH = os.path.join(_APP_DIR, "audio_archive", "hub", ".inbox_token")  # 被 git 忽略、不会被归档迁移挪走


def load_or_create_token() -> str:
    env = os.getenv("INBOX_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    logger.info(f"[收件口] 已生成访问 token：{TOKEN_PATH}")
    return tok


def _count_transcripts() -> int:
    try:
        with open(transcript_log.log_path(), "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def make_handler(processor, token: str, max_bytes: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WhisperInputInbox/1"

        def log_message(self, fmt, *args):  # 不往 stderr 刷访问日志
            logger.debug("[收件口] " + fmt % args)

        # ---- helpers ----
        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            got = self.headers.get("X-Inbox-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            return bool(got) and hmac.compare_digest(got.encode(), token.encode())

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0:
                self._json(411, {"ok": False, "error": "Content-Length required"})
                return None
            if length > max_bytes:
                self._json(413, {"ok": False, "error": f"too large (> {max_bytes // (1024 * 1024)} MB)"})
                return None
            return self.rfile.read(length)

        # ---- routes ----
        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            if urlparse(self.path).path == "/v1/health":
                engine = None
                try:
                    with open(os.path.join(_APP_DIR, "logs", "asr_engine.json"), encoding="utf-8") as f:
                        engine = json.load(f)
                except Exception:  # noqa: BLE001
                    pass
                return self._json(200, {"ok": True, "engine": engine, "transcripts": _count_transcripts()})
            return self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            url = urlparse(self.path)
            if url.path == "/v1/text":
                return self._post_text()
            if url.path == "/v1/audio":
                return self._post_audio(parse_qs(url.query))
            return self._json(404, {"ok": False, "error": "not found"})

        def _post_text(self):
            body = self._read_body()
            if body is None:
                return None
            try:
                data = json.loads(body.decode("utf-8"))
                text = str(data.get("text", "")).strip()
            except Exception:  # noqa: BLE001
                return self._json(400, {"ok": False, "error": "body must be JSON with a 'text' field"})
            if not text:
                return self._json(400, {"ok": False, "error": "empty text"})
            source = str(data.get("source", "iphone"))
            if not transcript_log.SOURCE_RE.match(source):
                return self._json(400, {"ok": False, "error": "source must match [a-z0-9_-]{1,32}"})
            dur = data.get("duration_s")
            rec = transcript_log.append(
                text, source=source, model=data.get("model"), engine=data.get("engine"),
                duration_s=float(dur) if isinstance(dur, (int, float)) else None, ts=data.get("ts"))
            if rec is None:
                return self._json(503, {"ok": False, "error": "transcript log disabled or not writable"})
            logger.info(f"[收件口] 收到 {source} 文字（{len(text)} 字）")
            return self._json(200, {"ok": True, "id": rec["id"]})

        def _post_audio(self, query):
            source = (query.get("source") or ["pendant"])[0]
            if not transcript_log.SOURCE_RE.match(source):
                return self._json(400, {"ok": False, "error": "source must match [a-z0-9_-]{1,32}"})
            if processor is None or not hasattr(processor, "transcribe_bulk"):
                return self._json(503, {"ok": False, "error": "no local ASR engine available"})
            body = self._read_body()
            if body is None:
                return None
            t0 = time.time()
            try:
                text, engine, model, dur = processor.transcribe_bulk(body)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[收件口] 转写 {source} 音频失败：{exc}")
                return self._json(422, {"ok": False, "error": f"transcription failed: {exc}"})
            rec = transcript_log.append(text, source=source, model=model, engine=engine, duration_s=dur) if text else None
            logger.info(f"[收件口] {source} 音频 {dur or 0:.0f}s → {len(text or '')} 字（{engine}，{time.time() - t0:.1f}s）")
            return self._json(200, {"ok": True, "id": rec["id"] if rec else None, "text": text,
                                    "engine": engine, "duration_s": dur})

    return Handler


def start_inbox(processor):
    """在后台线程启动收件口；关闭或启动失败都只记日志，绝不影响听写。"""
    if os.getenv("INBOX_ENABLED", "true").lower() != "true":
        return None
    host = os.getenv("INBOX_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("INBOX_PORT", "8765"))
        max_bytes = int(float(os.getenv("INBOX_MAX_MB", "200")) * 1024 * 1024)
        token = load_or_create_token()
        server = ThreadingHTTPServer((host, port), make_handler(processor, token, max_bytes))
        server.daemon_threads = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[收件口] 启动失败（{host}:{os.getenv('INBOX_PORT', '8765')}）：{exc}")
        return None
    threading.Thread(target=server.serve_forever, name="inbox", daemon=True).start()
    logger.info(f"[收件口] 正在监听 http://{host}:{port}（token：{TOKEN_PATH if not os.getenv('INBOX_TOKEN') else 'INBOX_TOKEN'}）")
    return server
