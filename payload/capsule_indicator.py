"""胶囊式听写指示器（第二种风格，可经 INDICATOR_STYLE=capsule 选用）。

生命周期（与状态机一致，接口同 ListeningIndicator）：
  录音中   -> show_recording()  底部弹出一条小胶囊，里面三个点依次脉动（"正在听"）
  转录中   -> show_processing() 胶囊"收起来"——横向收成一个小圆 + 旋转弧
  转录完成 -> complete()        小圆变绿、轻轻一胀再淡出
  其它->空闲 -> hide()
背景透明、点击穿透；所有 UI 操作经 AppHelper.callAfter 投递到主线程。
"""

from __future__ import annotations

import threading
from typing import List, Optional

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
        CACurrentMediaTime,
        CALayer,
        CAMediaTimingFunction,
        CAShapeLayer,
        CGPathCreateWithEllipseInRect,
    )
    _CA_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    logger.warning(f"[CapsuleIndicator] Core Animation 不可用，指示器禁用: {exc}")
    _CA_AVAILABLE = False

# 几何
_W = 240.0            # 浮窗宽（透明、点击穿透；留足胶囊 + 阴影余量）
_H = 60.0             # 浮窗高
_CX = _W / 2.0
_CY = _H / 2.0
_PILL_H = 34.0        # 胶囊高
_PILL_WIDE = 150.0    # 录音时胶囊宽
_PILL_COMPACT = 34.0  # 收起后（圆形）
_DOT_R = 3.0          # 听写点半径
_DOT_GAP = 13.0       # 点间距
_SPIN_R = 11.0        # 旋转弧半径
_BOTTOM_MARGIN = 64.0
_INF = float("inf")


def _cg(r: float, g: float, b: float, a: float):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).CGColor()


_DARK = (0.12, 0.12, 0.13, 0.92)
_GREEN = (0.20, 0.72, 0.40, 0.96)


class CapsuleIndicator:
    """胶囊式指示器。失败时静默降级，绝不影响听写。"""

    def __init__(self) -> None:
        self._panel: Optional[NSPanel] = None
        self._pill = None                 # 胶囊背景（可变宽）
        self._dots: Optional[List] = None  # 三个脉动点
        self._spinner = None              # 旋转弧
        self._state: Optional[str] = None

    # ---- 公开 API ----------------------------------------------------------
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
            if state == self._state and (state is None or (self._panel and self._panel.isVisible())):
                return
            if state is None:
                if self._panel is not None:
                    self._panel.orderOut_(None)
                self._state = None
                return

            prev = self._state
            if self._panel is None:
                self._create_panel()
            self._position_bottom_center()

            if state == "recording":
                self._spinner.removeAnimationForKey_("spin")
                self._spinner.setHidden_(True)
                self._pill.removeAllAnimations()
                self._pill.setBackgroundColor_(_cg(*_DARK))
                self._pill.setOpacity_(1.0)
                self._set_pill_width(_PILL_WIDE, animated=False)
                self._dots_visible(True)
                self._start_dots()
            else:  # processing：把胶囊"收起来"成小圆 + 旋转弧
                self._dots_visible(False)
                self._stop_dots()
                self._set_pill_width(_PILL_COMPACT, animated=(prev == "recording"))
                self._spinner.setHidden_(False)
                self._start_spinner()

            self._panel.orderFrontRegardless()
            self._state = state
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CapsuleIndicator] 应用状态 {state} 失败: {exc}")

    def _play_completion(self) -> None:
        try:
            if self._panel is None or not self._panel.isVisible():
                self._state = None
                return
            self._spinner.removeAnimationForKey_("spin")
            self._spinner.setHidden_(True)
            self._dots_visible(False)
            self._set_pill_width(_PILL_COMPACT, animated=False)
            self._pill.removeAllAnimations()
            self._pill.setBackgroundColor_(_cg(*_GREEN))  # 成功绿

            dur = 0.42
            ease = CAMediaTimingFunction.functionWithName_("easeOut")
            scale = CABasicAnimation.animationWithKeyPath_("transform.scale")
            scale.setFromValue_(1.0)
            scale.setToValue_(1.18)
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
            self._pill.addAnimation_forKey_(scale, "doneScale")
            self._pill.addAnimation_forKey_(opacity, "doneOpacity")

            self._state = "complete"
            threading.Timer(dur + 0.05, lambda: AppHelper.callAfter(self._finish_hide)).start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[CapsuleIndicator] 完成动画失败: {exc}")
            if self._panel is not None:
                self._panel.orderOut_(None)
            self._state = None

    def _finish_hide(self) -> None:
        try:
            if self._panel is not None:
                self._panel.orderOut_(None)
            if self._pill is not None:
                self._pill.removeAllAnimations()
                self._pill.setBackgroundColor_(_cg(*_DARK))
                self._pill.setOpacity_(1.0)
        finally:
            self._state = None

    # ---- 内部动画 ----------------------------------------------------------
    def _set_pill_width(self, w: float, animated: bool, dur: float = 0.32) -> None:
        if self._pill is None:
            return
        if animated:
            cur = self._pill.bounds().size.width
            anim = CABasicAnimation.animationWithKeyPath_("bounds.size.width")
            anim.setFromValue_(cur)
            anim.setToValue_(w)
            anim.setDuration_(dur)
            anim.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeInEaseOut"))
            self._pill.addAnimation_forKey_(anim, "retract")
        self._pill.setBounds_(NSMakeRect(0, 0, w, _PILL_H))  # 模型值 = 目标宽

    def _start_dots(self) -> None:
        if not self._dots:
            return
        now = CACurrentMediaTime()
        for i, dot in enumerate(self._dots):
            dot.removeAllAnimations()
            a = CABasicAnimation.animationWithKeyPath_("opacity")
            a.setFromValue_(0.3)
            a.setToValue_(1.0)
            a.setDuration_(0.45)
            a.setAutoreverses_(True)
            a.setRepeatCount_(_INF)
            a.setBeginTime_(now + i * 0.16)  # 依次脉动
            a.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeInEaseOut"))
            dot.addAnimation_forKey_(a, "pulse")

    def _stop_dots(self) -> None:
        if self._dots:
            for dot in self._dots:
                dot.removeAnimationForKey_("pulse")

    def _dots_visible(self, visible: bool) -> None:
        if self._dots:
            for dot in self._dots:
                dot.setHidden_(not visible)

    def _start_spinner(self) -> None:
        spin = CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
        spin.setFromValue_(0.0)
        spin.setToValue_(6.283185307179586)
        spin.setDuration_(0.85)
        spin.setRepeatCount_(_INF)
        spin.setTimingFunction_(CAMediaTimingFunction.functionWithName_("linear"))
        self._spinner.addAnimation_forKey_(spin, "spin")

    # ---- 构造 --------------------------------------------------------------
    def _create_panel(self) -> None:
        screen = NSScreen.mainScreen()
        sf = screen.frame()
        x = (sf.size.width - _W) / 2.0
        y = sf.size.height - 200

        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, _W, _H), style, 2, False)
        panel.setLevel_(NSPopUpMenuWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setHidesOnDeactivate_(False)
        panel.setCanHide_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)

        content = panel.contentView()
        content.setWantsLayer_(True)
        root = content.layer()
        root.setMasksToBounds_(False)

        # 胶囊背景（居中、anchorPoint 在中心，便于横向收放与缩放）
        pill = CALayer.layer()
        pill.setBounds_(NSMakeRect(0, 0, _PILL_WIDE, _PILL_H))
        pill.setPosition_((_CX, _CY))
        pill.setCornerRadius_(_PILL_H / 2.0)
        pill.setBackgroundColor_(_cg(*_DARK))
        pill.setShadowColor_(NSColor.blackColor().CGColor())
        pill.setShadowRadius_(8.0)
        pill.setShadowOpacity_(0.35)
        pill.setShadowOffset_(NSMakeSize(0, -2))
        root.addSublayer_(pill)
        self._pill = pill

        # 三个脉动点（root 的子层，居中一排；不随胶囊收放而挤压）
        self._dots = []
        for i in (-1, 0, 1):
            dot = CALayer.layer()
            dot.setBounds_(NSMakeRect(0, 0, _DOT_R * 2, _DOT_R * 2))
            dot.setPosition_((_CX + i * _DOT_GAP, _CY))
            dot.setCornerRadius_(_DOT_R)
            dot.setBackgroundColor_(_cg(1, 1, 1, 0.95))
            dot.setHidden_(True)
            root.addSublayer_(dot)
            self._dots.append(dot)

        # 旋转弧（转录中）
        spinner = CAShapeLayer.layer()
        spinner.setFrame_(NSMakeRect(0, 0, _W, _H))  # anchorPoint 中心
        spinner.setPath_(CGPathCreateWithEllipseInRect(
            NSMakeRect(_CX - _SPIN_R, _CY - _SPIN_R, _SPIN_R * 2, _SPIN_R * 2), None))
        spinner.setFillColor_(_cg(0, 0, 0, 0))
        spinner.setStrokeColor_(_cg(0.35, 0.78, 1.0, 1.0))
        spinner.setLineWidth_(2.5)
        spinner.setLineCap_("round")
        spinner.setStrokeStart_(0.0)
        spinner.setStrokeEnd_(0.7)
        spinner.setHidden_(True)
        root.addSublayer_(spinner)
        self._spinner = spinner

        self._panel = panel

    def _position_bottom_center(self) -> None:
        if self._panel is None:
            return
        screen = NSScreen.mainScreen()
        vf = screen.visibleFrame()
        x = vf.origin.x + (vf.size.width - _W) / 2.0
        y = vf.origin.y + _BOTTOM_MARGIN
        self._panel.setFrame_display_(NSMakeRect(x, y, _W, _H), True)
