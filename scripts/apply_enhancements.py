#!/usr/bin/env python3
"""Apply whisper-input-next-mac-kit enhancements onto an upstream Whisper-Input-Next checkout.

This script does NOT ship or redistribute upstream source. It performs a small set of
well-defined, idempotent insertions into the files of a checkout you cloned yourself from
the official repository. Safe to run multiple times.

Enhancements applied:
  * Sound cues (macOS system sounds via afplay), each overridable by env var
    (SOUND_START/SOUND_STOP/SOUND_DONE/SOUND_ERROR/SOUND_WARNING; defaults
    Submarine/Submarine/Glass/Basso/Funk).
  * Single-tap Right-Command toggle (tap right cmd to start/stop recording).
  * Ctrl+F also routes to local whisper.cpp when TRANSCRIPTION_SERVICE=local.
  * start.sh dependency-check fix.
  * Local-mode punctuation: prompt-guided punctuation + half->full-width CJK punctuation.
  * Audio-archive auto-cleanup: delete recordings older than
    AUDIO_ARCHIVE_RETENTION_HOURS (default 24) at startup + every
    AUDIO_ARCHIVE_CLEANUP_INTERVAL_HOURS (default 6); prunes cache.json.
  * Bottom-center "listening indicator" overlay, two styles chosen by INDICATOR_STYLE:
    'ring' (breathing ring -> spinner -> green burst) or 'capsule' (pill that retracts
    to a spinner -> green). SHOW_INDICATOR=false disables it. The UI components
    (src/ui/listening_indicator.py, src/ui/capsule_indicator.py) are kit-owned (MIT)
    and copied in by install.sh; this patcher only wires the main.py hooks + factory.

Targets upstream commit 5edec44bd66ca0a75c75c485d5af2fd201ce8c17. If you pin a different
commit and an anchor no longer matches, this script tells you which one and stops.
"""
import os
import sys
import py_compile

PIN_COMMIT = "5edec44bd66ca0a75c75c485d5af2fd201ce8c17"

_SOUND_BLOCK = '''# ── 状态提示音（macOS 系统音，afplay 异步播放，不阻塞主流程）────────────────
# 提示音可用环境变量覆盖（macOS 系统音名，见 /System/Library/Sounds）
_SOUND_START = os.getenv("SOUND_START", "Submarine")
_SOUND_STOP = os.getenv("SOUND_STOP", "Submarine")
_SOUND_DONE = os.getenv("SOUND_DONE", "Glass")
_SOUND_ERROR = os.getenv("SOUND_ERROR", "Basso")
_SOUND_WARNING = os.getenv("SOUND_WARNING", "Funk")
_SOUND_FOR_STATE = {
    InputState.RECORDING: _SOUND_START,            # 开始录音
    InputState.RECORDING_TRANSLATE: _SOUND_START,
    InputState.RECORDING_KIMI: _SOUND_START,
    InputState.DOUBAO_STREAMING: _SOUND_START,
    InputState.PROCESSING: _SOUND_STOP,            # 停止录音、开始转录
    InputState.PROCESSING_KIMI: _SOUND_STOP,
    InputState.TRANSLATING: _SOUND_STOP,
    InputState.ERROR: _SOUND_ERROR,                # 出错
    InputState.WARNING: _SOUND_WARNING,            # 警告（如录音过短）
}


def _play_sound(name: str) -> None:
    """异步播放 macOS 系统提示音，失败时静默忽略。"""
    try:
        subprocess.Popen(
            ["afplay", f"/System/Library/Sounds/{name}.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass'''

EDITS = [
    # ---- main.py ----
    dict(
        file="main.py",
        marker="\nimport subprocess\n",
        old="import io\nimport os\nimport queue\nimport sys\nimport threading\nimport asyncio\n",
        new="import io\nimport os\nimport queue\nimport subprocess\nimport sys\nimport threading\nimport asyncio\n",
        desc="import subprocess",
    ),
    dict(
        file="main.py",
        marker="_SOUND_FOR_STATE",
        old='__description__ = "Enhanced Voice Transcription Tool with OpenAI GPT-4o Transcribe"\n\n\n@dataclass\nclass TranscriptionJob:',
        new='__description__ = "Enhanced Voice Transcription Tool with OpenAI GPT-4o Transcribe"\n\n\n'
            + _SOUND_BLOCK
            + '\n\n\n@dataclass\nclass TranscriptionJob:',
        desc="sound table + _play_sound() helper",
    ),
    dict(
        file="main.py",
        marker='self.transcription_service == "local"',
        old='            ctrl_f_start = self.start_doubao_streaming\n'
            '            ctrl_f_stop = self.stop_doubao_streaming\n'
            '            logger.info("Ctrl+F 使用豆包流式识别")\n'
            '        else:\n'
            '            ctrl_f_start = self.start_openai_recording',
        new='            ctrl_f_start = self.start_doubao_streaming\n'
            '            ctrl_f_stop = self.stop_doubao_streaming\n'
            '            logger.info("Ctrl+F 使用豆包流式识别")\n'
            '        elif self.transcription_service == "local" and self.local_processor:\n'
            '            # 本地模式补丁：让 Ctrl+F 也走本地 whisper.cpp（与 Ctrl+I 相同）\n'
            '            ctrl_f_start = self.start_local_recording\n'
            '            ctrl_f_stop = self.stop_local_recording\n'
            '            logger.info("Ctrl+F 使用本地 whisper.cpp 转录")\n'
            '        else:\n'
            '            ctrl_f_start = self.start_openai_recording',
        desc="Ctrl+F routes to local whisper.cpp",
    ),
    dict(
        file="main.py",
        marker="_play_state_sound",
        old='    def _on_state_change(self, new_state: InputState):\n'
            '        self._current_state = new_state\n'
            '        self._notify_status()',
        new='    def _on_state_change(self, new_state: InputState):\n'
            '        prev_state = self._current_state\n'
            '        self._current_state = new_state\n'
            '        self._notify_status()\n'
            '        self._play_state_sound(prev_state, new_state)\n'
            '\n'
            '    def _play_state_sound(self, prev_state: InputState, new_state: InputState):\n'
            '        """根据状态变化播放提示音：开始 / 处理中 / 完成 / 错误。"""\n'
            '        processing = (\n'
            '            InputState.PROCESSING,\n'
            '            InputState.PROCESSING_KIMI,\n'
            '            InputState.TRANSLATING,\n'
            '        )\n'
            '        # 处理中 -> 空闲：转录成功并已粘贴 → 成功音\n'
            '        if new_state == InputState.IDLE and prev_state in processing:\n'
            '            _play_sound(_SOUND_DONE)\n'
            '            return\n'
            '        sound = _SOUND_FOR_STATE.get(new_state)\n'
            '        if sound:\n'
            '            _play_sound(sound)',
        desc="_on_state_change hook + _play_state_sound()",
    ),
    # ---- src/keyboard/listener.py ----
    dict(
        file="src/keyboard/listener.py",
        marker="tap_toggle_key",
        old="        self._original_clipboard = None  # 保存原始剪贴板内容\n",
        new="        self._original_clipboard = None  # 保存原始剪贴板内容\n"
            "\n"
            "        # 单独轻点「右 Command」键切换录音（省手、零冲突；改 self.tap_toggle_key 可换键，如 Key.alt_r）\n"
            "        self.tap_toggle_key = Key.cmd_r\n"
            "        self._tap_key_down = False   # 触发键当前是否按下\n"
            "        self._tap_combo = False      # 触发键按住期间是否还按了别的键（是则视为组合键，不触发单点）\n",
        desc="tap-toggle state vars",
    ),
    dict(
        file="src/keyboard/listener.py",
        marker="self._tap_key_down = True",
        old='    def on_press(self, key):\n'
            '        """按键按下时的回调"""\n'
            '        try:\n'
            '            # 检查转录按钮（字符键或特殊键）\n'
            '            is_transcription_key = False',
        new='    def on_press(self, key):\n'
            '        """按键按下时的回调"""\n'
            '        try:\n'
            '            # 单独轻点「右 Command」键切换录音的检测\n'
            '            if key == self.tap_toggle_key:\n'
            '                self._tap_key_down = True\n'
            '                self._tap_combo = False\n'
            '            elif self._tap_key_down:\n'
            '                # 触发键按住期间又按了别的键 → 视为组合键，不触发单点\n'
            '                self._tap_combo = True\n'
            '\n'
            '            # 检查转录按钮（字符键或特殊键）\n'
            '            is_transcription_key = False',
        desc="on_press: tap detection",
    ),
    dict(
        file="src/keyboard/listener.py",
        marker="was_solo_tap",
        old='    def on_release(self, key):\n'
            '        """按键释放时的回调"""\n'
            '        try:\n'
            '            # 检查转录按钮（字符键或特殊键）\n'
            '            is_transcription_key = False',
        new='    def on_release(self, key):\n'
            '        """按键释放时的回调"""\n'
            '        try:\n'
            '            # 单独轻点「右 Command」键释放：期间未按其他键则切换录音\n'
            '            if key == self.tap_toggle_key:\n'
            '                was_solo_tap = self._tap_key_down and not self._tap_combo\n'
            '                self._tap_key_down = False\n'
            '                self._tap_combo = False\n'
            '                if was_solo_tap:\n'
            '                    self.toggle_recording()\n'
            '\n'
            '            # 检查转录按钮（字符键或特殊键）\n'
            '            is_transcription_key = False',
        desc="on_release: tap toggle",
    ),
    # ---- start.sh ----
    dict(
        file="start.sh",
        marker='import openai" >/dev/null 2>&1; then',
        old='# 检查依赖是否已安装\n'
            'if [ ! -f ".venv/pyvenv.cfg" ] || [ ! -f "venv/lib/python*/site-packages/openai" ]; then',
        new='# 检查依赖是否已安装（修复原脚本错误的检测路径，避免每次启动都重装）\n'
            'if ! .venv/bin/python -c "import openai" >/dev/null 2>&1; then',
        desc="start.sh dependency-check fix",
    ),
    # ---- src/transcription/local_whisper.py (punctuation) ----
    dict(
        file="src/transcription/local_whisper.py",
        marker="self.initial_prompt = os.getenv",
        old='        # 是否启用Kimi润色功能（默认关闭，通过快捷键动态控制）\n'
            '        self.enable_kimi_polish = os.getenv("ENABLE_KIMI_POLISH", "false").lower() == "true"',
        new='        # 是否启用Kimi润色功能（默认关闭，通过快捷键动态控制）\n'
            '        self.enable_kimi_polish = os.getenv("ENABLE_KIMI_POLISH", "false").lower() == "true"\n'
            '        # 初始 prompt：带标点的引导让 whisper 输出标点（纯本地、免费；WHISPER_PROMPT 置空可关闭）\n'
            '        self.initial_prompt = os.getenv("WHISPER_PROMPT", "以下是一段普通话，请正确断句并加上标点符号。")\n'
            '        # 是否把与中文相邻的半角标点转为全角（WHISPER_FULLWIDTH_PUNCT=false 可关闭）\n'
            '        self.fullwidth_punct = os.getenv("WHISPER_FULLWIDTH_PUNCT", "true").lower() == "true"',
        desc="local: WHISPER_PROMPT + fullwidth-punct config",
    ),
    dict(
        file="src/transcription/local_whisper.py",
        marker='cmd += ["--prompt"',
        old='                "--no-prints",\n'
            '            ]',
        new='                "--no-prints",\n'
            '            ]\n'
            '            # 带标点引导：让本地 whisper 输出标点（WHISPER_PROMPT 置空则关闭）\n'
            '            if self.initial_prompt:\n'
            '                cmd += ["--prompt", self.initial_prompt]',
        desc="local: pass --prompt to whisper-cli",
    ),
    dict(
        file="src/transcription/local_whisper.py",
        marker="def _to_fullwidth_punct",
        old='    def process_audio(self, audio_buffer, mode="transcriptions", prompt="", archive_path=None):',
        new='    @staticmethod\n'
            '    def _to_fullwidth_punct(text):\n'
            '        """把与中文相邻的半角标点转为全角（不影响纯英文与小数）。"""\n'
            "        mapping = {',': '，', '.': '。', '!': '！', '?': '？', ':': '：', ';': '；'}\n"
            '        chars = list(text)\n'
            '        out = []\n'
            '        for i, ch in enumerate(chars):\n'
            '            if ch in mapping:\n'
            "                prev_ch = chars[i - 1] if i > 0 else ''\n"
            "                next_ch = chars[i + 1] if i + 1 < len(chars) else ''\n"
            "                if ('一' <= prev_ch <= '鿿') or ('一' <= next_ch <= '鿿'):\n"
            '                    out.append(mapping[ch])\n'
            '                    continue\n'
            '            out.append(ch)\n'
            "        return ''.join(out)\n"
            '\n'
            '    def process_audio(self, audio_buffer, mode="transcriptions", prompt="", archive_path=None):',
        desc="local: _to_fullwidth_punct() helper",
    ),
    dict(
        file="src/transcription/local_whisper.py",
        marker="if self.fullwidth_punct:",
        old='            return full_txt.strip()',
        new='            result_txt = full_txt.strip()\n'
            '            if self.fullwidth_punct:\n'
            '                result_txt = self._to_fullwidth_punct(result_txt)\n'
            '            return result_txt',
        desc="local: apply fullwidth punctuation to result",
    ),
    # ---- src/audio/archive.py (auto-cleanup of old recordings) ----
    dict(
        file="src/audio/archive.py",
        marker="import threading\nimport time",  # combined: a partial-divergent upstream can't skip half this edit
        old="import os\nimport json\nimport shutil\nfrom datetime import datetime\n",
        new="import os\nimport json\nimport shutil\nimport threading\nimport time\nfrom datetime import datetime\n",
        desc="archive: import threading + time",
    ),
    dict(
        file="src/audio/archive.py",
        marker="self.retention_hours",
        old='        self.audio_dir = os.path.join(self.archive_dir, "audio")\n'
            '        self.ensure_directory()\n'
            '\n'
            '    def ensure_directory(self) -> None:',
        new='        self.audio_dir = os.path.join(self.archive_dir, "audio")\n'
            '        self.ensure_directory()\n'
            '        # 自动清理：删除超过保留时长的归档（启动时清一次 + 定期后台线程）\n'
            '        self.retention_hours = float(os.getenv("AUDIO_ARCHIVE_RETENTION_HOURS", "24"))\n'
            '        self.cleanup_interval_hours = float(os.getenv("AUDIO_ARCHIVE_CLEANUP_INTERVAL_HOURS", "6"))\n'
            '        self.cleanup()\n'
            '        self._start_periodic_cleanup()\n'
            '\n'
            '    def ensure_directory(self) -> None:',
        desc="archive: __init__ runs cleanup + starts periodic thread",
    ),
    dict(
        file="src/audio/archive.py",
        marker="def _start_periodic_cleanup",
        old='        self.save_transcription_cache(cache)',
        new='        self.save_transcription_cache(cache)\n'
            '\n'
            '    def cleanup(self, retention_hours: Optional[float] = None) -> None:\n'
            '        """删除超过保留时长的归档 WAV，并同步清理 cache.json（retention_hours<0 表示不清理）。"""\n'
            '        if retention_hours is None:\n'
            '            retention_hours = getattr(self, "retention_hours", 24.0)\n'
            '        if retention_hours < 0 or not os.path.isdir(self.audio_dir):\n'
            '            return\n'
            '        cutoff = time.time() - retention_hours * 3600\n'
            '        deleted = 0\n'
            '        for name in os.listdir(self.audio_dir):\n'
            '            if not name.endswith(".wav"):\n'
            '                continue\n'
            '            path = os.path.join(self.audio_dir, name)\n'
            '            try:\n'
            '                if os.path.getmtime(path) < cutoff:\n'
            '                    os.remove(path)\n'
            '                    deleted += 1\n'
            '            except OSError:\n'
            '                pass\n'
            '        if deleted:\n'
            '            try:\n'
            '                existing = {n for n in os.listdir(self.audio_dir) if n.endswith(".wav")}\n'
            '                cache = self.load_transcription_cache()\n'
            '                pruned = {k: v for k, v in cache.items() if k in existing}\n'
            '                if len(pruned) != len(cache):\n'
            '                    self.save_transcription_cache(pruned)\n'
            '            except Exception:  # noqa: BLE001\n'
            '                pass\n'
            '            logger.info(f"音频归档清理：删除 {deleted} 个过期录音（保留 {retention_hours:g}h）")\n'
            '\n'
            '    def _start_periodic_cleanup(self) -> None:\n'
            '        interval = getattr(self, "cleanup_interval_hours", 6.0)\n'
            '        if interval <= 0:\n'
            '            return\n'
            '\n'
            '        def _loop() -> None:\n'
            '            while True:\n'
            '                time.sleep(interval * 3600)\n'
            '                try:\n'
            '                    self.cleanup()\n'
            '                except Exception as exc:  # noqa: BLE001\n'
            '                    logger.debug(f"定期清理异常: {exc}")\n'
            '\n'
            '        threading.Thread(target=_loop, name="archive-cleanup", daemon=True).start()',
        desc="archive: cleanup() + _start_periodic_cleanup()",
    ),
    # ---- 听写指示器集成（main.py 钩子；UI 组件文件由 install.sh 单独拷贝进 src/ui/）----
    dict(
        file="main.py",
        marker="import ListeningIndicator",
        old="from src.ui.floating_preview import FloatingPreviewWindow\n",
        new="from src.ui.floating_preview import FloatingPreviewWindow\n"
            "from src.ui.listening_indicator import ListeningIndicator\n",
        desc="indicator: import ListeningIndicator",
    ),
    dict(
        file="main.py",
        marker="_SHOW_INDICATOR",
        old="_SOUND_FOR_STATE = {",
        new="# 底部居中的听写指示器浮窗（呼吸光环/旋转弧）；SHOW_INDICATOR=false 关闭\n"
            "_SHOW_INDICATOR = os.getenv(\"SHOW_INDICATOR\", \"true\").lower() == \"true\"\n"
            "# 指示器风格：ring（呼吸光环，默认）| capsule（胶囊条）\n"
            "_INDICATOR_STYLE = os.getenv(\"INDICATOR_STYLE\", \"ring\").lower()\n"
            "_SOUND_FOR_STATE = {",
        desc="indicator: SHOW_INDICATOR + INDICATOR_STYLE flags",
    ),
    dict(
        file="main.py",
        marker="self.listening_indicator =",
        old="        self.floating_preview = FloatingPreviewWindow()",
        new="        self.floating_preview = FloatingPreviewWindow()\n"
            "        self.listening_indicator = _make_indicator()",
        desc="indicator: instantiate via _make_indicator() factory",
    ),
    dict(
        file="main.py",
        marker="def _update_indicator",
        old="        self._play_state_sound(prev_state, new_state)\n"
            "\n"
            "    def _play_state_sound(self, prev_state: InputState, new_state: InputState):",
        new="        self._play_state_sound(prev_state, new_state)\n"
            "        self._update_indicator(prev_state, new_state)\n"
            "\n"
            "    def _update_indicator(self, prev_state: InputState, new_state: InputState):\n"
            '        """底部听写指示器：录音→呼吸光环，转录中→旋转弧，空闲/错误→隐藏。"""\n'
            "        if self.listening_indicator is None:\n"
            "            return\n"
            "        recording = (\n"
            "            InputState.RECORDING,\n"
            "            InputState.RECORDING_TRANSLATE,\n"
            "            InputState.RECORDING_KIMI,\n"
            "            InputState.DOUBAO_STREAMING,\n"
            "        )\n"
            "        processing = (\n"
            "            InputState.PROCESSING,\n"
            "            InputState.PROCESSING_KIMI,\n"
            "            InputState.TRANSLATING,\n"
            "        )\n"
            "        if new_state in recording:\n"
            "            self.listening_indicator.show_recording()\n"
            "        elif new_state in processing:\n"
            "            self.listening_indicator.show_processing()\n"
            "        elif new_state == InputState.IDLE and prev_state in processing:\n"
            '            # 转录成功并已粘贴：播放"完成"扩散淡出动画\n'
            "            self.listening_indicator.complete()\n"
            "        else:\n"
            "            self.listening_indicator.hide()\n"
            "\n"
            "    def _play_state_sound(self, prev_state: InputState, new_state: InputState):",
        desc="indicator: _on_state_change hook + _update_indicator()",
    ),
    dict(
        file="main.py",
        marker="def _make_indicator",
        old="        pass\n\n\n@dataclass\nclass TranscriptionJob:",
        new="        pass\n\n\n"
            "def _make_indicator():\n"
            '    """根据 SHOW_INDICATOR / INDICATOR_STYLE 选择听写指示器实现（ring|capsule）。"""\n'
            "    if not _SHOW_INDICATOR:\n"
            "        return None\n"
            "    try:\n"
            '        if _INDICATOR_STYLE == "capsule":\n'
            "            from src.ui.capsule_indicator import CapsuleIndicator\n"
            "            return CapsuleIndicator()\n"
            "        return ListeningIndicator()\n"
            "    except Exception as exc:  # noqa: BLE001\n"
            '        logger.warning(f"指示器创建失败 (style={_INDICATOR_STYLE}): {exc}")\n'
            "        return None\n"
            "\n\n@dataclass\nclass TranscriptionJob:",
        desc="indicator: _make_indicator() style factory (ring|capsule)",
    ),
]


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: apply_enhancements.py <APP_DIR>")
    app_dir = os.path.abspath(sys.argv[1])
    if not os.path.isdir(app_dir):
        sys.exit(f"not a directory: {app_dir}")

    print(f"[patcher] target: {app_dir}")
    print(f"[patcher] designed for upstream commit {PIN_COMMIT}")
    errors = 0
    touched = set()
    for e in EDITS:
        path = os.path.join(app_dir, e["file"])
        if not os.path.isfile(path):
            print(f"  [MISS] {e['file']}: file not found")
            errors += 1
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if e["marker"] in content:
            print(f"  [skip] {e['file']}: {e['desc']} (already applied)")
            continue
        if e["old"] not in content:
            print(f"  [FAIL] {e['file']}: anchor not found for: {e['desc']}")
            print("         (upstream may have changed; pin to the supported commit)")
            errors += 1
            continue
        content = content.replace(e["old"], e["new"], 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        touched.add(e["file"])
        print(f"  [ok]   {e['file']}: {e['desc']}")

    # syntax check the python files we touched
    for rel in ("main.py", "src/keyboard/listener.py", "src/transcription/local_whisper.py",
                "src/audio/archive.py", "src/ui/listening_indicator.py", "src/ui/capsule_indicator.py"):
        p = os.path.join(app_dir, rel)
        if os.path.isfile(p):
            try:
                py_compile.compile(p, doraise=True)
                print(f"  [py]   {rel}: compiles OK")
            except py_compile.PyCompileError as exc:
                print(f"  [PYERR] {rel}: {exc}")
                errors += 1

    if errors:
        sys.exit(f"[patcher] FAILED with {errors} error(s).")
    print("[patcher] all enhancements applied successfully.")


if __name__ == "__main__":
    main()
