"""本地 Qwen3-ASR 转录引擎（MLX / Apple Silicon），whisper.cpp 自动兜底。

make_local_processor(whisper_processor) 返回一个与 LocalWhisperProcessor 接口完全相同的处理器，
main.py 里其余逻辑（Ctrl+F / 右⌘ 路由、任务队列、缓存、重试）一行不用改：

  * LOCAL_ASR_ENGINE=qwen3（默认）：服务启动时在专用线程里后台加载 + 预热 Qwen3-ASR，模型常驻内存。
    MLX 的 stream 绑定线程，所以「加载」和「每次推理」都在同一个线程上执行。
  * 模型还没加载好、依赖缺失、推理出错或超时（QWEN_ASR_TIMEOUT）：自动退回 whisper.cpp，听写永不中断。
  * 词库 vocabulary.txt：里面的词作为 context（热词）注入 Qwen3-ASR；`错 => 对` 规则对两种引擎都生效。
  * LOCAL_ASR_ENGINE=whisper：保持原来的 whisper.cpp 行为（只多一层词库规则纠错）。

引擎状态写到 logs/asr_engine.json，供 MCP 的 status() 读取。

为什么换：在 15 句真实口语（中文 / 英文 / 中英混说 / 专有名词）上 A/B，M5 · 24 GB：
  whisper large-v3 错 19 处、中位耗时 2.6s；Qwen3-ASR-1.7B + 词库 错 11 处、中位耗时 0.8s。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

from src.transcription.local_vocab import from_env as _vocab_from_env
from src.utils.logger import logger

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STATUS_PATH = os.path.join(_APP_DIR, "logs", "asr_engine.json")
_TARGET_SR = 16_000
_DEFAULT_MODEL = "mlx-community/Qwen3-ASR-1.7B-bf16"
_INSTALL_HINT = "uv pip install --python .venv/bin/python 'qwen3-asr-mlx>=0.3.0' 'soxr>=0.5.0'"


def _write_status(**fields) -> None:
    try:
        os.makedirs(os.path.dirname(_STATUS_PATH), exist_ok=True)
        fields["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = _STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fields, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATUS_PATH)
    except Exception:  # noqa: BLE001
        pass


class _EngineThread:
    """单个常驻 daemon 线程，按顺序执行提交的任务（加载与推理同线程）。"""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="qwen3-asr", daemon=True).start()

    def _run(self) -> None:
        while True:
            fn, args, fut = self._jobs.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        self._jobs.put((fn, args, fut))
        return fut


class LocalASRProcessor:
    """LocalWhisperProcessor 的替身：同样的 process_audio()，Qwen3-ASR 优先、whisper.cpp 兜底。"""

    def __init__(self, whisper_processor, engine: str, vocab) -> None:
        self.whisper = whisper_processor
        self.engine = engine
        self.vocab = vocab
        self.qwen_model_id = os.getenv("QWEN_ASR_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
        self.qwen_timeout = float(os.getenv("QWEN_ASR_TIMEOUT", "60"))
        self.fullwidth_punct = getattr(whisper_processor, "fullwidth_punct", True)
        # main.py 用 model_path 记录缓存里的模型名：每次转录后指向实际使用的引擎
        self.whisper_model_path = getattr(whisper_processor, "model_path", "")
        self.model_path = self.whisper_model_path
        self._model = None
        self._healthy = False
        self._state = "loading" if engine == "qwen3" else "off"  # loading | ready | failed | off
        self._thread: _EngineThread | None = None
        if engine == "qwen3":
            _write_status(engine="qwen3", state="loading", model=self.qwen_model_id, fallback="whisper")
            self._thread = _EngineThread()
            self._thread.submit(self._load)
        else:
            _write_status(engine="whisper", state="ready", model=os.path.basename(self.whisper_model_path))

    def __getattr__(self, name):
        # 其余属性（translate_processor、enable_kimi_polish …）透传给 whisper 处理器
        whisper = self.__dict__.get("whisper")
        if whisper is None:
            raise AttributeError(name)
        return getattr(whisper, name)

    # ---------------- 加载（在引擎线程上执行） ----------------

    def _resolve_model_path(self) -> str:
        if os.path.isdir(os.path.expanduser(self.qwen_model_id)):
            return os.path.expanduser(self.qwen_model_id)
        from huggingface_hub import snapshot_download

        try:  # 先只用本地缓存：启动不联网、不卡
            return snapshot_download(repo_id=self.qwen_model_id, local_files_only=True)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[Qwen3-ASR] 本地没有 {self.qwen_model_id}，开始下载（约 4 GB，只需一次）…")
        _write_status(engine="qwen3", state="downloading", model=self.qwen_model_id, fallback="whisper")
        try:
            return snapshot_download(repo_id=self.qwen_model_id)
        except Exception:  # noqa: BLE001
            if os.getenv("HF_ENDPOINT"):
                raise
            logger.info("[Qwen3-ASR] Hugging Face 官方源失败，改用 hf-mirror.com …")
            return snapshot_download(repo_id=self.qwen_model_id, endpoint="https://hf-mirror.com")

    def _load(self) -> None:
        t0 = time.time()
        try:
            import soxr  # noqa: F401
            from qwen3_asr_mlx import Qwen3ASR

            import numpy as np

            model = Qwen3ASR.from_pretrained(self._resolve_model_path())
            with np.errstate(all="ignore"):
                model.warm_up()
            self._model, self._healthy, self._state = model, True, "ready"
            secs = time.time() - t0
            logger.info(f"[Qwen3-ASR] 已就绪：{self.qwen_model_id}（加载+预热 {secs:.1f}s），之后转录都走 Qwen3-ASR")
            _write_status(engine="qwen3", state="ready", model=self.qwen_model_id, load_seconds=round(secs, 1))
        except ImportError as exc:
            self._fail(f"缺少依赖（{exc}），安装：{_INSTALL_HINT}")
        except Exception as exc:  # noqa: BLE001
            self._fail(f"{type(exc).__name__}: {exc}")

    def _fail(self, msg: str) -> None:
        self._healthy, self._state = False, "failed"
        logger.warning(f"[Qwen3-ASR] 不可用，继续使用 whisper.cpp：{msg}")
        _write_status(engine="qwen3", state="failed", model=self.qwen_model_id, error=msg, fallback="whisper")

    # ---------------- 推理（在引擎线程上执行） ----------------

    def _qwen_transcribe(self, raw: bytes) -> str:
        import numpy as np
        import soundfile as sf
        import soxr

        audio, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if sr != _TARGET_SR:  # 高质量重采样（库自带的是无抗混叠的线性插值）
            audio = soxr.resample(audio, sr, _TARGET_SR)
        context = self.vocab.context() if self.vocab is not None else None
        # macOS Accelerate 的 matmul 会误报浮点警告（divide/overflow/invalid），结果是对的，静音之
        with np.errstate(all="ignore"):
            result = self._model.transcribe(np.ascontiguousarray(audio, dtype=np.float32), context=context)
        return result.text.strip()

    # ---------------- 对外接口（与 LocalWhisperProcessor 相同） ----------------

    def process_audio(self, audio_buffer, mode="transcriptions", prompt="", archive_path=None):
        raw = audio_buffer.getvalue() if hasattr(audio_buffer, "getvalue") else audio_buffer.read()
        with contextlib.suppress(Exception):
            audio_buffer.close()

        if self.engine == "qwen3" and mode == "transcriptions":
            if self._healthy and self._thread is not None:
                t0 = time.time()
                logger.info("正在使用本地 Qwen3-ASR 处理音频...")
                try:
                    text = self._thread.submit(self._qwen_transcribe, raw).result(timeout=self.qwen_timeout)
                    logger.info(f"本地处理成功 (Qwen3-ASR), 耗时: {time.time() - t0:.1f}秒")
                    logger.info(f"转录结果: {text}")
                    self.model_path = self.qwen_model_id
                    return self._finish(text), None
                except FutureTimeout:
                    self._healthy, self._state = False, "failed"
                    logger.error(f"[Qwen3-ASR] 超过 {self.qwen_timeout:g}s 未返回：本次及之后改用 whisper.cpp（重启服务可恢复）")
                    _write_status(engine="qwen3", state="failed", model=self.qwen_model_id,
                                  error=f"timeout > {self.qwen_timeout:g}s", fallback="whisper")
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[Qwen3-ASR] 转录出错，本次改用 whisper.cpp：{exc}", exc_info=True)
            elif self._state == "loading":
                logger.info("[Qwen3-ASR] 尚未就绪（后台加载中），本次使用 whisper.cpp")
            else:
                logger.info("[Qwen3-ASR] 不可用，本次使用 whisper.cpp（原因见 logs/asr_engine.json）")

        return self._via_whisper(raw, mode, prompt, archive_path)

    def _via_whisper(self, raw: bytes, mode: str, prompt: str, archive_path):
        self.model_path = self.whisper_model_path
        result = self.whisper.process_audio(io.BytesIO(raw), mode=mode, prompt=prompt, archive_path=archive_path)
        text, err = result if isinstance(result, tuple) else (result, None)
        if text and not err and mode == "transcriptions" and self.vocab is not None:
            text = self.vocab.correct(text)
        return text, err

    def _finish(self, text: str) -> str:
        if self.vocab is not None:
            text = self.vocab.correct(text)
        if self.fullwidth_punct and hasattr(self.whisper, "_to_fullwidth_punct"):
            text = self.whisper._to_fullwidth_punct(text)
        return text


def make_local_processor(whisper_processor):
    """按 LOCAL_ASR_ENGINE（qwen3 | whisper，默认 qwen3）包装本地 whisper 处理器。"""
    engine = os.getenv("LOCAL_ASR_ENGINE", "qwen3").strip().lower()
    if engine not in ("qwen3", "whisper"):
        logger.warning(f"未知的 LOCAL_ASR_ENGINE={engine!r}，改用 qwen3")
        engine = "qwen3"
    try:
        vocab = _vocab_from_env(_APP_DIR)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[词库] 初始化失败，已禁用：{exc}")
        vocab = None
    label = "Qwen3-ASR（后台加载中，就绪前由 whisper.cpp 兜底）" if engine == "qwen3" else "whisper.cpp"
    logger.info(f"本地转录引擎：{label}")
    return LocalASRProcessor(whisper_processor, engine, vocab)
