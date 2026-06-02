"""长按指示器图标 → 强制重启的覆盖层（卡死时的逃生口）。

在指示器浮窗上盖一个透明、可接收鼠标的小视图：**按住约 1.8s**，一圈红环填满 →
强制重启（os._exit(0)，launchd KeepAlive 会立刻把服务拉回来，~1s）。**中途松手 = 取消**。
ring / capsule 两种指示器共用。失败时静默降级，绝不影响主流程。
"""

from __future__ import annotations

import faulthandler
import os
import threading
import time

from AppKit import NSView, NSMakeRect
from Cocoa import NSColor
from PyObjCTools import AppHelper

from src.utils.logger import logger

try:
    from Quartz import CABasicAnimation, CAShapeLayer, CGPathCreateWithEllipseInRect
    _CA = True
except Exception as exc:  # noqa: BLE001
    logger.warning(f"[hold-restart] Core Animation 不可用，长按重启禁用: {exc}")
    _CA = False


def _default_restart() -> None:
    logger.warning("[hold-restart] 用户长按触发强制重启 → 先抓现场再 os._exit(0)（launchd 将自动拉起）")
    # 退出前把全线程栈快照下来：即使用户没等看门狗的 45s 阈值就手动重启，也留下卡死现场
    try:
        app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        log_path = os.path.join(app_root, "logs", "freeze_diagnostics.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n===== MANUAL FORCE-RESTART @ {time.strftime('%Y-%m-%d %H:%M:%S')} (user hold) =====\n")
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.write("===== end =====\n")
            f.flush()
    except Exception:  # noqa: BLE001 — 抓现场失败也要照常重启
        pass
    os._exit(0)


class _HoldRestartView(NSView):
    """透明视图：按住计时 + 红环填充；满了触发重启，松手取消。"""

    def acceptsFirstMouse_(self, event):  # 非激活面板也能接住第一次点击
        return True

    def mouseDown_(self, event):
        try:
            self._begin_hold()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[hold-restart] mouseDown 异常: {exc}")

    def mouseDragged_(self, event):
        try:
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
            b = self.bounds()
            inside = (b.origin.x <= p.x <= b.origin.x + b.size.width
                      and b.origin.y <= p.y <= b.origin.y + b.size.height)
            if not inside:
                self._cancel_hold()
        except Exception:  # noqa: BLE001
            pass

    def mouseUp_(self, event):
        self._cancel_hold()

    # ---- python helpers ----
    def _begin_hold(self):
        if getattr(self, "_fired", False):
            return
        secs = getattr(self, "_seconds", 1.8)
        ring = getattr(self, "_ring", None)
        if ring is not None:
            ring.setHidden_(False)
            ring.removeAllAnimations()
            ring.setStrokeEnd_(0.0)
            anim = CABasicAnimation.animationWithKeyPath_("strokeEnd")
            anim.setFromValue_(0.0)
            anim.setToValue_(1.0)
            anim.setDuration_(secs)
            anim.setRemovedOnCompletion_(False)
            anim.setFillMode_("forwards")
            ring.addAnimation_forKey_(anim, "fill")
        t = threading.Timer(secs, self._fire)
        t.daemon = True
        self._timer = t
        t.start()

    def _cancel_hold(self):
        if getattr(self, "_fired", False):
            return
        t = getattr(self, "_timer", None)
        if t is not None:
            t.cancel()
        self._timer = None
        ring = getattr(self, "_ring", None)
        if ring is not None:
            ring.removeAllAnimations()
            ring.setStrokeEnd_(0.0)
            ring.setHidden_(True)

    def _fire(self):  # 计时线程里调用
        self._fired = True
        AppHelper.callAfter(getattr(self, "_on_trigger", _default_restart))


def attach_hold_to_restart(panel, hit_w: float, hit_h: float,
                           seconds: float = 1.8, on_trigger=None):
    """给指示器浮窗加"长按重启"：在中心放一个 hit_w×hit_h 的可点区域。"""
    if not _CA or panel is None:
        return None
    try:
        panel.setIgnoresMouseEvents_(False)  # 取消点击穿透，才能接住长按
        content = panel.contentView()
        cb = content.bounds()
        x = (cb.size.width - hit_w) / 2.0
        y = (cb.size.height - hit_h) / 2.0
        view = _HoldRestartView.alloc().initWithFrame_(NSMakeRect(x, y, hit_w, hit_h))
        view.setWantsLayer_(True)

        # 红色填充进度环，居中
        d = min(hit_w, hit_h) - 6.0
        ring = CAShapeLayer.layer()
        ring.setFrame_(NSMakeRect(0, 0, hit_w, hit_h))
        ring.setPath_(CGPathCreateWithEllipseInRect(
            NSMakeRect((hit_w - d) / 2.0, (hit_h - d) / 2.0, d, d), None))
        ring.setFillColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0, 0, 0, 0).CGColor())
        ring.setStrokeColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.30, 0.25, 0.95).CGColor())
        ring.setLineWidth_(3.0)
        ring.setLineCap_("round")
        ring.setStrokeEnd_(0.0)
        ring.setHidden_(True)
        view.layer().addSublayer_(ring)

        # 配置挂到实例上
        view._ring = ring
        view._seconds = float(seconds)
        view._on_trigger = on_trigger or _default_restart
        view._timer = None
        view._fired = False

        content.addSubview_(view)
        return view
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[hold-restart] 安装失败: {exc}")
        return None
