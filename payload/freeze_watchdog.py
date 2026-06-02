"""卡死诊断看门狗：当服务卡在某个忙碌状态太久，把所有线程的调用栈快照下来。

挂在状态机上（main.py 的 _on_state_change 调 note_state）。后台线程定期检查：
若当前停在"转录中/录音中"等忙碌态超过阈值（正常转录才几秒），就把
**全部线程的调用栈** + 现场上下文追加写进 logs/freeze_diagnostics.log，
每次卡死只 dump 一次。纯诊断、不自动重启、绝不影响主流程。
"""

from __future__ import annotations

import faulthandler
import os
import threading
import time

from src.keyboard.inputState import InputState
from src.utils.logger import logger

# 转录态：正常 3-10s，超过阈值基本就是卡了
_PROCESSING = {
    InputState.PROCESSING,
    InputState.PROCESSING_KIMI,
    InputState.TRANSLATING,
}
# 录音态：用户自己控制时长，阈值放宽（长听写也可能几分钟）
_RECORDING = {
    InputState.RECORDING,
    InputState.RECORDING_TRANSLATE,
    InputState.RECORDING_KIMI,
    InputState.DOUBAO_STREAMING,
}


class FreezeWatchdog:
    """检测"卡在忙碌态"并 dump 全线程栈。"""

    def __init__(self, log_path: str, processing_secs: float = 45.0,
                 recording_secs: float = 180.0, poll_secs: float = 5.0) -> None:
        self.log_path = log_path
        self.processing_secs = float(processing_secs)
        self.recording_secs = float(recording_secs)
        self.poll_secs = float(poll_secs)
        self._state = InputState.IDLE
        self._since = time.monotonic()
        self._dumped = False  # 每次卡死只 dump 一次
        self._lock = threading.Lock()

    def note_state(self, state: InputState) -> None:
        """状态机每次切换都通知（重置计时与 dump 标记）。"""
        with self._lock:
            self._state = state
            self._since = time.monotonic()
            self._dumped = False

    def start(self) -> None:
        threading.Thread(target=self._loop, name="freeze-watchdog", daemon=True).start()
        logger.info(
            f"[watchdog] 卡死诊断已开启（转录>{self.processing_secs:g}s / 录音>{self.recording_secs:g}s "
            f"→ dump 到 {self.log_path}）"
        )

    def _threshold(self, state) -> float | None:
        if state in _PROCESSING:
            return self.processing_secs
        if state in _RECORDING:
            return self.recording_secs
        return None  # IDLE/ERROR/WARNING 等非忙碌态不算卡

    def _loop(self) -> None:
        while True:
            time.sleep(self.poll_secs)
            try:
                with self._lock:
                    state, since, dumped = self._state, self._since, self._dumped
                threshold = self._threshold(state)
                if threshold is None or dumped:
                    continue
                elapsed = time.monotonic() - since
                if elapsed >= threshold:
                    self._dump(state, elapsed)
                    with self._lock:
                        # 只在仍是同一次卡死时标记（期间没切状态）
                        if self._since == since:
                            self._dumped = True
            except Exception as exc:  # noqa: BLE001 — 看门狗自身绝不能崩
                logger.debug(f"[watchdog] 检查异常: {exc}")

    def _dump(self, state, elapsed: float) -> None:
        logger.warning(
            f"[watchdog] ⚠️ 疑似卡死：停在 {state} 已 {elapsed:.0f}s，"
            f"dump 全线程栈 → {self.log_path}"
        )
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n===== FREEZE @ {time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"state={state} | stuck={elapsed:.0f}s =====\n"
                )
                faulthandler.dump_traceback(file=f, all_threads=True)
                f.write("===== end =====\n")
                f.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[watchdog] dump 失败: {exc}")
