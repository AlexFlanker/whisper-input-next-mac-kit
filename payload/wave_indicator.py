"""流光波形听写指示器（第三种风格，INDICATOR_STYLE=wave）。

和 ring / capsule 不同：它跟着你**真实的说话音量**动——录音器每个音频块算一次音量（RMS），
经 set_level() 传进来；三条半透明正弦波（青 / 薰衣草 / 珊瑚）的振幅随音量起伏，
不说话时只剩一条轻轻呼吸的细线，一眼就知道麦克风有没有在听。

背景是 macOS 原生毛玻璃（NSVisualEffectView，模糊身后内容），没有固定边框：遮罩每帧重画成一个
会"呼吸"的有机轮廓——两端圆润、顶部不平，上下沿各自随音量起伏，外圈 _FEATHER 宽度逐层淡出融进背景。

生命周期（接口同 ListeningIndicator / CapsuleIndicator，多一个 set_level）：
  录音中   -> show_recording()  底部浮现毛玻璃光团，波形与外轮廓随音量起伏
  转录中   -> show_processing() 光团收窄，波形变成低振幅、快速流动的"流光"
  转录完成 -> complete()        收成绿色小圆 + 对勾，轻轻一胀再淡出
  其它->空闲 -> hide()
背景透明；中心可长按 ~1.8s 强制重启（hold_restart）。所有 UI 操作都在主线程；
set_level() 可从音频线程调用（只写一个 float）。任何异常都静默降级，绝不影响听写。
"""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

from src.utils.logger import logger

from AppKit import (
    NSAppearance,
    NSBezierPath,
    NSImage,
    NSMakeRect,
    NSMakeSize,
    NSPanel,
    NSView,
    NSVisualEffectView,
    NSPopUpMenuWindowLevel,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Cocoa import NSColor, NSScreen
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

from src.ui.hold_restart import attach_hold_to_restart

try:
    from Quartz import (
        CABasicAnimation,
        CALayer,
        CAMediaTimingFunction,
        CAShapeLayer,
        CATransaction,
        CGPathAddLineToPoint,
        CGPathCreateMutable,
        CGPathMoveToPoint,
    )
    _CA_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    logger.warning(f"[WaveIndicator] Core Animation 不可用，指示器禁用: {exc}")
    _CA_AVAILABLE = False

# ---- 几何 ----
_W, _H = 260.0, 108.0         # 浮窗（透明；留足"呼吸"外轮廓 + 羽化的上下余量）
_CX, _CY = _W / 2.0, _H / 2.0
_PILL_H = 40.0
_PILL_WIDE = 200.0            # 录音时
_PILL_NARROW = 132.0          # 转录中
_PILL_DONE = 40.0             # 完成（圆）
_WAVE_PAD = 18.0              # 波形离胶囊两端的留白
_STEP = 4.0                   # 波形采样步长（px）
_FPS = 30.0
_BOTTOM_MARGIN = 64.0

# ---- 毛玻璃背景：真实模糊身后内容 + 边缘羽化淡出，没有硬边框 ----
_FEATHER = 12.0               # 羽化宽度（px）：越大边缘越"化开"
_GLASS_ALPHA = 0.88           # 毛玻璃整体不透明度：越小越通透
_GLASS_MATERIAL = 13          # NSVisualEffectMaterialHUDWindow（深色 HUD 毛玻璃）
_BLEND_BEHIND_WINDOW = 0      # NSVisualEffectBlendingModeBehindWindow：模糊窗口后面的内容
_STATE_ACTIVE = 1             # NSVisualEffectStateActive：不随窗口失焦变灰

# ---- 颜色 ----
_DARK = (0.12, 0.12, 0.13, 0.50)   # 仅在毛玻璃不可用时兜底（半透明、无阴影）
_GREEN = (0.11, 0.62, 0.46, 0.96)
_WAVES = (                    # (r, g, b, a), 频率系数, 速度, 相位, 振幅系数
    ((0.36, 0.79, 0.65, 0.95), 1.00, 5.0, 0.0, 1.00),   # 青
    ((0.69, 0.66, 0.93, 0.80), 1.35, 7.0, 2.0, 0.80),   # 薰衣草
    ((0.94, 0.60, 0.48, 0.70), 1.70, 9.0, 4.0, 0.62),   # 珊瑚
)
_BASE_FREQ = 0.065            # 弧度 / px
_MAX_AMP = 13.0               # 满音量时的振幅（px）
_IDLE_AMP = 1.3               # 静音时的"呼吸"振幅

# 音量映射：RMS → dBFS → 0..1（-55 dB 以下算静音，-15 dB 以上算满格）
_DB_FLOOR, _DB_CEIL = -55.0, -15.0


def rms_to_level(rms: float) -> float:
    if rms <= 0:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db - _DB_FLOOR) / (_DB_CEIL - _DB_FLOOR)))


def wave_points(half_w: float, t: float, level: float, k: int, processing: bool) -> list[tuple]:
    """第 k 条波在相对胶囊中心的坐标（纯函数，便于测试）。"""
    _, fmul, speed, phase, amul = _WAVES[k]
    if processing:
        amp, speed = 2.2 * amul, speed * 2.2
    else:
        amp = max(_IDLE_AMP, level * _MAX_AMP * amul)
    pts = []
    n = max(2, int(2 * half_w / _STEP) + 1)
    for i in range(n):
        x = -half_w + i * (2 * half_w / (n - 1))
        env = 1.0 - (x / half_w) ** 2               # 两端收拢
        y = math.sin(x * _BASE_FREQ * fmul + t * speed + phase) * amp * env
        pts.append((x, y))
    return pts


def _cg(r: float, g: float, b: float, a: float):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).CGColor()


def feather_alphas(steps: int = 12) -> list[float]:
    """由外到内每一圈的叠加不透明度：最外圈几乎透明，越往里越实（纯函数，便于测试）。"""
    return [0.06 + 0.30 * (i + 1) / steps for i in range(steps)]


def blob_outline(half_w: float, t: float, level: float, processing: bool, n: int = 28) -> list[tuple]:
    """会"呼吸"的外轮廓：返回 [(x, top, bottom)]，相对中心（纯函数，便于测试）。

    基础形状是两端圆润、顶部不平的椭圆状；上下沿各自叠加随时间流动的低频起伏，
    起伏幅度跟着音量走——不说话时只轻轻呼吸，说话越大声边缘涌动越明显。
    """
    base = _PILL_H / 2.0
    lv = 0.0 if processing else level
    breathe = 1.0 + 0.05 * math.sin(t * 1.7)
    pts = []
    for i in range(n + 1):
        u = -1.0 + 2.0 * i / n
        prof = max(0.0, 1.0 - abs(u) ** 2.6) ** 0.5
        wt = lv * (0.30 * math.sin(u * 3.1 + t * 3.2) + 0.18 * math.sin(u * 7.3 - t * 4.6)) \
            + 0.06 * math.sin(u * 2.0 + t * 1.3)
        wb = lv * (0.30 * math.sin(u * 2.7 - t * 2.9 + 1.7) + 0.18 * math.sin(u * 6.1 + t * 5.1 + 0.6)) \
            + 0.06 * math.sin(u * 2.3 - t * 1.1 + 2.0)
        if processing:  # 转录中：慢慢流动的微光
            wt += 0.08 * math.sin(u * 4.0 + t * 6.0)
            wb += 0.08 * math.sin(u * 4.0 - t * 6.0 + 1.0)
        top = base * prof * breathe * min(1.5, max(0.55, 1.0 + wt))
        bot = -base * prof * breathe * min(1.5, max(0.55, 1.0 + wb))
        pts.append((u * half_w, top, bot))
    return pts


def _blob_mask(half_w: float, t: float, level: float, processing: bool):
    """毛玻璃遮罩：实心的会呼吸轮廓 + 外圈 _FEATHER 宽度逐层淡出。每帧重画。"""
    outline = blob_outline(half_w, t, level, processing)
    base = _PILL_H / 2.0
    alphas = feather_alphas(8)

    def polygon(grow: float):
        sx = (half_w + grow) / half_w if half_w > 0 else 1.0
        sy = (base + grow) / base
        top = [(_CX + x * sx, _CY + tp * sy) for x, tp, _ in outline]
        bottom = [(_CX + x * sx, _CY + bt * sy) for x, _, bt in reversed(outline)]
        path = NSBezierPath.bezierPath()
        pts = top + bottom
        path.moveToPoint_(pts[0])
        for p in pts[1:]:
            path.lineToPoint_(p)
        path.closePath()
        return path

    def draw(_rect):
        n = len(alphas)
        for i, a in enumerate(alphas):          # 由外向内，越往里越实
            NSColor.colorWithWhite_alpha_(1.0, a).set()
            polygon(_FEATHER * (1.0 - i / n)).fill()
        NSColor.colorWithWhite_alpha_(1.0, 1.0).set()
        polygon(0.0).fill()
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(_W, _H), False, draw)


class WaveIndicator:
    """流光波形指示器。失败时静默降级，绝不影响听写。"""

    def __init__(self) -> None:
        self._panel: Optional[NSPanel] = None
        self._glass = None      # NSVisualEffectView（毛玻璃）；不可用时为 None，退回半透明 _pill
        self._pill = None       # 兜底背景 + 完成时的绿色小圆
        self._waves: list = []
        self._check = None
        self._timer = None
        self._state: Optional[str] = None
        self._target = 0.0      # 音频线程写入
        self._level = 0.0       # 主线程平滑后的值
        self._pill_w = _PILL_WIDE
        self._t0 = time.monotonic()

    # ---- 公开 API ----------------------------------------------------------
    def set_level(self, rms: float) -> None:
        """音频线程调用：传入这一块音频的 RMS（float，0..1 幅度）。"""
        self._target = rms_to_level(rms)

    def show_recording(self) -> None:
        if _CA_AVAILABLE:
            AppHelper.callAfter(self._apply, "recording")

    def show_processing(self) -> None:
        if _CA_AVAILABLE:
            AppHelper.callAfter(self._apply, "processing")

    def complete(self) -> None:
        if _CA_AVAILABLE:
            AppHelper.callAfter(self._apply, "complete")

    def hide(self) -> None:
        if _CA_AVAILABLE:
            AppHelper.callAfter(self._apply, None)

    # ---- 主线程实现 --------------------------------------------------------
    def _apply(self, state: Optional[str]) -> None:
        try:
            if state == "complete":
                self._play_completion()
                return
            if state is None:
                self._stop_timer()
                if self._panel is not None:
                    self._panel.orderOut_(None)
                self._state = None
                return
            if self._panel is None:
                self._create_panel()
            if self._state not in ("recording", "processing"):
                self._reset_visuals()
                self._position_bottom_center()
            self._state = state
            self._start_timer()
            self._panel.orderFrontRegardless()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[WaveIndicator] 应用状态 {state} 失败: {exc}")

    def _reset_visuals(self) -> None:
        self._pill.removeAllAnimations()
        # 有毛玻璃时胶囊本身透明，只在毛玻璃不可用时用半透明深色兜底
        self._pill.setBackgroundColor_(_cg(0, 0, 0, 0) if self._glass is not None else _cg(*_DARK))
        self._pill.setOpacity_(1.0)
        if self._glass is not None:
            self._glass.setHidden_(False)
            self._glass.setAlphaValue_(_GLASS_ALPHA)
        self._check.setHidden_(True)
        for w in self._waves:
            w.setHidden_(False)
        self._level = self._target = 0.0
        self._pill_w = _PILL_WIDE
        self._set_pill_width(_PILL_WIDE)

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = NSTimer.timerWithTimeInterval_repeats_block_(1.0 / _FPS, True, lambda _t: self._tick())
        NSRunLoop.mainRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _tick(self) -> None:
        try:
            if self._state not in ("recording", "processing"):
                return
            processing = self._state == "processing"
            target = 0.0 if processing else self._target
            # 起音快、落音慢：跟手但不抖
            self._level += (target - self._level) * (0.45 if target > self._level else 0.12)
            goal_w = _PILL_NARROW if processing else _PILL_WIDE
            self._pill_w += (goal_w - self._pill_w) * 0.22
            half_w = self._pill_w / 2.0 - _WAVE_PAD
            t = time.monotonic() - self._t0

            if self._glass is not None:  # 外轮廓跟着声音一起"呼吸"
                self._glass.setMaskImage_(_blob_mask(self._pill_w / 2.0, t, self._level, processing))

            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            self._set_pill_width(self._pill_w)
            for k, layer in enumerate(self._waves):
                path = CGPathCreateMutable()
                for i, (x, y) in enumerate(wave_points(half_w, t, self._level, k, processing)):
                    if i == 0:
                        CGPathMoveToPoint(path, None, _CX + x, _CY + y)
                    else:
                        CGPathAddLineToPoint(path, None, _CX + x, _CY + y)
                layer.setPath_(path)
                layer.setOpacity_(0.55 if processing else 1.0)
            CATransaction.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[WaveIndicator] 帧更新失败: {exc}")

    def _play_completion(self) -> None:
        try:
            self._stop_timer()
            if self._panel is None or not self._panel.isVisible():
                self._state = None
                return
            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            for w in self._waves:
                w.setHidden_(True)
            CATransaction.commit()
            if self._glass is not None:  # 毛玻璃淡出，只留绿色小圆
                self._glass.animator().setAlphaValue_(0.0)

            # 胶囊收成绿色小圆 + 对勾
            shrink = CABasicAnimation.animationWithKeyPath_("bounds.size.width")
            shrink.setFromValue_(self._pill_w)
            shrink.setToValue_(_PILL_DONE)
            shrink.setDuration_(0.18)
            shrink.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeOut"))
            self._pill.addAnimation_forKey_(shrink, "shrink")
            self._set_pill_width(_PILL_DONE)
            self._pill.setBackgroundColor_(_cg(*_GREEN))
            self._check.setHidden_(False)

            dur, delay = 0.42, 0.28
            ease = CAMediaTimingFunction.functionWithName_("easeOut")
            for layer in (self._pill, self._check):
                scale = CABasicAnimation.animationWithKeyPath_("transform.scale")
                scale.setFromValue_(1.0)
                scale.setToValue_(1.18)
                fade = CABasicAnimation.animationWithKeyPath_("opacity")
                fade.setFromValue_(1.0)
                fade.setToValue_(0.0)
                for a in (scale, fade):
                    a.setBeginTime_(_now() + delay)  # 先让绿色对勾亮一下，再胀开淡出
                    a.setDuration_(dur)
                    a.setTimingFunction_(ease)
                    a.setRemovedOnCompletion_(False)
                    a.setFillMode_("forwards")
                layer.addAnimation_forKey_(scale, "doneScale")
                layer.addAnimation_forKey_(fade, "doneOpacity")

            self._state = "complete"
            threading.Timer(delay + dur + 0.05, lambda: AppHelper.callAfter(self._finish_hide)).start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[WaveIndicator] 完成动画失败: {exc}")
            if self._panel is not None:
                self._panel.orderOut_(None)
            self._state = None

    def _finish_hide(self) -> None:
        try:
            if self._state != "complete":   # 期间又开始录音了，别把新浮窗藏掉
                return
            if self._panel is not None:
                self._panel.orderOut_(None)
            self._pill.removeAllAnimations()
            self._check.removeAllAnimations()
        finally:
            if self._state == "complete":
                self._state = None

    # ---- 构造 --------------------------------------------------------------
    def _set_pill_width(self, w: float) -> None:
        if self._pill is not None:
            self._pill.setBounds_(NSMakeRect(0, 0, w, _PILL_H))

    def _make_glass(self, content):
        """真·毛玻璃：模糊身后内容，遮罩边缘羽化。任何异常都退回半透明胶囊。"""
        try:
            glass = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, _W, _H))  # 铺满，形状全靠遮罩
            glass.setMaterial_(_GLASS_MATERIAL)
            glass.setBlendingMode_(_BLEND_BEHIND_WINDOW)
            glass.setState_(_STATE_ACTIVE)
            dark = NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark")
            if dark is not None:  # 波形配色按深色设计，不跟随系统浅色模式
                glass.setAppearance_(dark)
            glass.setMaskImage_(_blob_mask(_PILL_WIDE / 2.0, 0.0, 0.0, False))
            glass.setAlphaValue_(_GLASS_ALPHA)
            content.addSubview_(glass)
            return glass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[WaveIndicator] 毛玻璃不可用，改用半透明背景: {exc}")
            return None

    def _create_panel(self) -> None:
        sf = NSScreen.mainScreen().frame()
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect((sf.size.width - _W) / 2.0, 200, _W, _H), style, 2, False)
        panel.setLevel_(NSPopUpMenuWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setHidesOnDeactivate_(False)
        panel.setCanHide_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)

        content = panel.contentView()
        content.setWantsLayer_(True)
        # 视图层级（下→上）：毛玻璃 → 前景层（波形 / 绿色小圆 / 对勾）
        self._glass = self._make_glass(content)
        fg = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _W, _H))
        fg.setWantsLayer_(True)
        content.addSubview_(fg)
        root = fg.layer()
        root.setMasksToBounds_(False)

        pill = CALayer.layer()  # 无阴影：阴影会勾出一圈硬边
        pill.setBounds_(NSMakeRect(0, 0, _PILL_WIDE, _PILL_H))
        pill.setPosition_((_CX, _CY))
        pill.setCornerRadius_(_PILL_H / 2.0)
        pill.setBackgroundColor_(_cg(0, 0, 0, 0) if self._glass is not None else _cg(*_DARK))
        root.addSublayer_(pill)
        self._pill = pill

        self._waves = []
        for rgba, *_ in _WAVES:
            w = CAShapeLayer.layer()
            w.setFrame_(NSMakeRect(0, 0, _W, _H))
            w.setFillColor_(_cg(0, 0, 0, 0))
            w.setStrokeColor_(_cg(*rgba))
            w.setLineWidth_(2.2)
            w.setLineCap_("round")
            w.setLineJoin_("round")
            root.addSublayer_(w)
            self._waves.append(w)

        check = CAShapeLayer.layer()
        check.setFrame_(NSMakeRect(0, 0, _W, _H))   # anchorPoint 在中心 → 缩放围绕胶囊中心
        path = CGPathCreateMutable()
        CGPathMoveToPoint(path, None, _CX - 8, _CY)
        CGPathAddLineToPoint(path, None, _CX - 2, _CY - 6)
        CGPathAddLineToPoint(path, None, _CX + 9, _CY + 6)
        check.setPath_(path)
        check.setFillColor_(_cg(0, 0, 0, 0))
        check.setStrokeColor_(_cg(1, 1, 1, 1))
        check.setLineWidth_(3.0)
        check.setLineCap_("round")
        check.setLineJoin_("round")
        check.setHidden_(True)
        root.addSublayer_(check)
        self._check = check

        self._panel = panel
        attach_hold_to_restart(panel, _PILL_WIDE - 40.0, _PILL_H)  # 长按 ~1.8s 强制重启

    def _position_bottom_center(self) -> None:
        if self._panel is None:
            return
        vf = NSScreen.mainScreen().visibleFrame()
        x = vf.origin.x + (vf.size.width - _W) / 2.0
        y = vf.origin.y + _BOTTOM_MARGIN
        self._panel.setFrame_display_(NSMakeRect(x, y, _W, _H), True)


def _now() -> float:
    from Quartz import CACurrentMediaTime

    return CACurrentMediaTime()
