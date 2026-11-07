#!/usr/bin/env python3
"""中枢模式（headless hub）：给家里常开的 Mac（如 Mac mini）用的精简服务端。

只跑三样东西——收件口（HTTP）、Qwen3-ASR（whisper.cpp 兜底）、持久转写日志；
没有热键监听、没有悬浮指示器、不需要辅助功能 / 输入监控 / 麦克风权限。
由 install.sh 以 WIN_ROLE=hub 安装时，LaunchAgent 启动的是本文件而不是 main.py。

其它设备怎么连：
  * MacBook 等客户端：在其 .env 配 HUB_URL=http://<本机>:8765 与 HUB_TOKEN（kit 的 scripts/connect_hub.sh 可自动配置）
  * iPhone App / 录音笔：POST /v1/text 或 /v1/audio（见 inbox_server.py）
token 在 audio_archive/hub/.inbox_token。
"""

from __future__ import annotations

import os
import signal
import threading

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("INBOX_HOST", "0.0.0.0")  # 中枢默认对局域网 / Tailscale 开放（有 token 保护）
os.environ.pop("HUB_URL", None)                 # 中枢自己就是总账，不再往别处转发

from src.transcription import transcript_log  # noqa: E402
from src.transcription.inbox_server import start_inbox  # noqa: E402
from src.utils.logger import logger  # noqa: E402


def _make_processor():
    try:
        from src.transcription.local_whisper import LocalWhisperProcessor
        from src.transcription.qwen3_asr import make_local_processor

        return make_local_processor(LocalWhisperProcessor())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[中枢] 本地转写引擎不可用（只能收文字，不能转写音频）：{exc}")
        return None


def main() -> None:
    processor = _make_processor()
    app_dir = os.path.dirname(os.path.abspath(__file__))
    transcript_log.backfill_from_cache(os.path.join(app_dir, "audio_archive", "cache.json"))
    server = start_inbox(processor)
    if server is None:
        raise SystemExit("[中枢] 收件口没有启动（INBOX_ENABLED=false 或端口被占用），退出让 launchd 重试")
    logger.info("=== 语音中枢已启动（headless hub）===")

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    logger.info("=== 语音中枢已停止 ===")


if __name__ == "__main__":
    main()
