"""底部居中的「听写指示器」浮窗：录音呼吸光环 → 转录旋转弧 → 完成扩散淡出。

背景全透明（无黑框），只剩一个带柔光的小圈，便于融入任意背景。
同 FloatingPreviewWindow 用无边框、不抢焦点、跨 Space 的 NSPanel；点击穿透；
所有 UI 操作经 AppHelper.callAfter 投递到主线程（状态机回调来自别的线程）。

由状态机驱动（main.py 的 _on_state_change）：
  录音中      -> show_recording()  呼吸光环
  转录中      -> show_processing() 暗轨 + 旋转弧
  转录完成    -> complete()        绿色小圈向外扩散淡出（"完成"那一下）
  其它 -> 空闲 -> hide()
环境变量 SHOW_INDICATOR=false 关闭（在 main.py 判断）。
"""

from __future__ import annotations

import math
import threading
from typing import Optional

from src.utils.logger import logger

from AppKit import (
    NSMakeRect,
    NSMakeSize,
    NSPanel,
    NSPopUpMenuWindowLevel,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Cocoa import NSColor, NSScreen
from PyObjCTools import AppHelper

try:
    from Quartz import (
        CABasicAnimation,
        CAMediaTimingFunction,
        CAShapeLayer,
        CGPathCreateWithEllipseInRect,
    )
    _CA_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 — Core Animation 不可用时降级为不显示
    logger.warning(f"[ListeningIndicator] Core Animation 不可用，指示器禁用: {exc}")
    _CA_AVAILABLE = False

# 几何（背景透明，留足完成动画向外扩散的余量）
_SIZE = 100.0          # 浮窗边长（透明、点击穿透）
_RING_BOX = 44.0       # 呼吸圈外接方形边长
_LINE_WIDTH = 2.5
_BOTTOM_MARGIN = 64.0  # 浮窗底距可视区域底部

_INF = float("inf")


def _cg(r: float, g: float, b: float, a: float):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).CGColor()


class ListeningIndicator:
    """录音/转录状态指示器（透明小圈 + 柔光）。失败时静默降级，绝不影响听写。"""

    def __init__(self) -> None:
        self._panel: Optional[NSPanel] = None
        self._breathing = None   # 呼吸光环（录音）/ 完成扩散
        self._track = None       # 暗色轨道（转录中底圈）
        self._spinner = None     # 旋转弧（转录中）
        self._state: Optional[str] = None  # 'recording' | 'processing' | 'complete' | None

    # ---- 公开 API（线程安全：投递到主线程）---------------------------------
    def show_recording(self) -> None:
        if _CA_AVAILABLE:
            AppHelper.callAfter(self._apply, "recording")

    def show_processing(self) -> None:
        if _CA_AVAILABLE:
            AppHelper.callAfter(self._apply, "processing")

    def complete(self) -> None:
        """转录完成那一下：绿色小圈向外扩散并淡出。"""
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

            if state == self._state and (state is None or (self._panel and self._panel.isVisible())):
                return  # 同状态去重，避免动画重启抖动

            if state is None:
                if self._panel is not None:
                    self._panel.orderOut_(None)
                self._state = None
                return

            if self._panel is None:
                self._create_panel()
            self._position_bottom_center()

            if state == "recording":
                self._spinner.removeAnimationForKey_("spin")
                self._spinner.setHidden_(True)
                self._track.setHidden_(True)
                self._reset_breathing()
                self._breathing.setHidden_(False)
                self._start_breathing()
            else:  # processing
                self._breathing.removeAllAnimations()
                self._breathing.setHidden_(True)
                self._track.setHidden_(False)
                self._spinner.setHidden_(False)
                self._start_spinner()

            self._panel.orderFrontRegardless()
            self._state = state
        except Exception as exc:  # noqa: BLE001 — UI 故障绝不能拖垮听写
            logger.warning(f"[ListeningIndicator] 应用状态 {state} 失败: {exc}")

    def _play_completion(self) -> None:
        """绿色小圈向外扩散 + 淡出，结束后隐藏。"""
        try:
            if self._panel is None or not self._panel.isVisible():
                self._state = None
                return
            self._spinner.removeAnimationForKey_("spin")
            self._spinner.setHidden_(True)
            self._track.setHidden_(True)

            self._breathing.removeAllAnimations()
            self._breathing.setHidden_(False)
            self._breathing.setStrokeColor_(_cg(0.30, 0.85, 0.45, 1.0))  # 成功绿

            dur = 0.42
            ease = CAMediaTimingFunction.functionWithName_("easeOut")
            scale = CABasicAnimation.animationWithKeyPath_("transform.scale")
            scale.setFromValue_(1.0)
            scale.setToValue_(1.8)
            scale.setDuration_(dur)
            scale.setTimingFunction_(ease)
            scale.setRemovedOnCompletion_(False)
            scale.setFillMode_("forwards")
            opacity = CABasicAnimation.animationWithKeyPath_("opacity")
            opacity.setFromValue_(1.0)
            opacity.setToValue_(0.0)
            opacity.setDuration_(dur)
            opacity.setTimingFunction_(ease)
            opacity.setRemovedOnCompletion_(False)
            opacity.setFillMode_("forwards")
            self._breathing.addAnimation_forKey_(scale, "doneScale")
            self._breathing.addAnimation_forKey_(opacity, "doneOpacity")

            self._state = "complete"
            threading.Timer(dur + 0.05, lambda: AppHelper.callAfter(self._finish_hide)).start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ListeningIndicator] 完成动画失败: {exc}")
            if self._panel is not None:
                self._panel.orderOut_(None)
            self._state = None

    def _finish_hide(self) -> None:
        try:
            if self._panel is not None:
                self._panel.orderOut_(None)
        finally:
            self._state = None

    def _reset_breathing(self) -> None:
        """复位呼吸圈（完成动画可能改过颜色/动画）。"""
        self._breathing.removeAllAnimations()
        self._breathing.setStrokeColor_(_cg(1, 1, 1, 0.95))

    def _start_breathing(self) -> None:
        ease = CAMediaTimingFunction.functionWithName_("easeInEaseOut")
        scale = CABasicAnimation.animationWithKeyPath_("transform.scale")
        scale.setFromValue_(0.82)
        scale.setToValue_(1.0)
        scale.setDuration_(0.95)
        scale.setAutoreverses_(True)
        scale.setRepeatCount_(_INF)
        scale.setTimingFunction_(ease)
        self._breathing.addAnimation_forKey_(scale, "scale")

        opacity = CABasicAnimation.animationWithKeyPath_("opacity")
        opacity.setFromValue_(0.5)
        opacity.setToValue_(1.0)
        opacity.setDuration_(0.95)
        opacity.setAutoreverses_(True)
        opacity.setRepeatCount_(_INF)
        opacity.setTimingFunction_(ease)
        self._breathing.addAnimation_forKey_(opacity, "opacity")

    def _start_spinner(self) -> None:
        spin = CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
        spin.setFromValue_(0.0)
        spin.setToValue_(2.0 * math.pi)
        spin.setDuration_(0.85)
        spin.setRepeatCount_(_INF)
        spin.setTimingFunction_(CAMediaTimingFunction.functionWithName_("linear"))
        self._spinner.addAnimation_forKey_(spin, "spin")

    def _ring_layer(self, stroke, stroke_start: float = 0.0, stroke_end: float = 1.0):
        """居中、anchorPoint 在圆心、带柔光的环形 CAShapeLayer。"""
        layer = CAShapeLayer.layer()
        layer.setFrame_(NSMakeRect(0, 0, _SIZE, _SIZE))  # anchorPoint 默认 (0.5,0.5) = 圆心
        inset = (_SIZE - _RING_BOX) / 2.0
        layer.setPath_(CGPathCreateWithEllipseInRect(
            NSMakeRect(inset, inset, _RING_BOX, _RING_BOX), None))
        layer.setFillColor_(_cg(0, 0, 0, 0))
        layer.setStrokeColor_(stroke)
        layer.setLineWidth_(_LINE_WIDTH)
        layer.setLineCap_("round")
        layer.setStrokeStart_(stroke_start)
        layer.setStrokeEnd_(stroke_end)
        # 柔和暗色外发光：浅色/深色背景都看得清，又不需要黑框
        layer.setShadowColor_(NSColor.blackColor().CGColor())
        layer.setShadowRadius_(3.0)
        layer.setShadowOpacity_(0.55)
        layer.setShadowOffset_(NSMakeSize(0, 0))
        return layer

    def _create_panel(self) -> None:
        screen = NSScreen.mainScreen()
        sf = screen.frame()
        x = (sf.size.width - _SIZE) / 2.0
        y = sf.size.height - 200  # 临时，_position_bottom_center 会重定位

        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, _SIZE, _SIZE), style, 2, False)  # 2 = NSBackingStoreBuffered

        # 与 FloatingPreviewWindow 相同的「跨 Space / 顶层 / 不随失焦消失」配置
        panel.setLevel_(NSPopUpMenuWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setHidesOnDeactivate_(False)
        panel.setCanHide_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())  # 透明：去黑框，只留小圈
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)  # 点击穿透，纯展示

        content = panel.contentView()
        content.setWantsLayer_(True)
        root = content.layer()
        root.setMasksToBounds_(False)  # 不裁剪，让柔光/扩散动画溢出显示

        # 三个图层：暗色轨道（底）→ 旋转弧 → 呼吸/完成环（顶）
        self._track = self._ring_layer(_cg(1, 1, 1, 0.18))
        self._spinner = self._ring_layer(_cg(0.35, 0.78, 1.0, 1.0), 0.0, 0.7)  # 浅蓝 3/4 弧
        self._breathing = self._ring_layer(_cg(1, 1, 1, 0.95))
        self._track.setHidden_(True)
        self._spinner.setHidden_(True)
        self._breathing.setHidden_(True)
        for lyr in (self._track, self._spinner, self._breathing):
            root.addSublayer_(lyr)

        self._panel = panel

    def _position_bottom_center(self) -> None:
        if self._panel is None:
            return
        screen = NSScreen.mainScreen()
        vf = screen.visibleFrame()  # 排除菜单栏/Dock
        x = vf.origin.x + (vf.size.width - _SIZE) / 2.0
        y = vf.origin.y + _BOTTOM_MARGIN
        self._panel.setFrame_display_(NSMakeRect(x, y, _SIZE, _SIZE), True)
