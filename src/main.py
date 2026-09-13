"""claude-code-overlay — a frameless always-on-top token meter for Windows 11.

Run from source with ``python src/main.py``.  The window is a 220x65 rounded
panel anchored to the bottom-left of the work area, draggable anywhere, with a
right-click menu and an optional tray icon.

All painting happens in :meth:`OverlayWindow.paintEvent` rather than through
child widgets: at this size a stylesheet cascade costs more than it buys, and
custom painting keeps the rounded corners crisp on fractional-DPI displays.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Make the sibling modules importable whether this file is launched as a script
# (``python src/main.py``), as a module (``python -m src.main``) or from a
# PyInstaller one-file bundle.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from config import APP_NAME, APP_VERSION, Config, configure_logging
from parser import (
    LimitUsage,
    SessionUsage,
    UsageMonitor,
    build_tooltip,
    load_cached_limits,
)

log = logging.getLogger(__name__)


def resource_path(relative: str) -> Path:
    """Resolve a bundled asset both in development and inside a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return _HERE.parent / relative


def load_app_icon(theme_accent: str = "#d97757") -> QIcon:
    """Load ``assets/icon.ico``, falling back to a painted glyph if missing."""
    icon_path = resource_path("assets/icon.ico")
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QBrush(QColor("#151719")))
    painter.setPen(QPen(QColor(theme_accent), 4))
    painter.drawRoundedRect(QRectF(4, 4, 56, 56), 14, 14)
    painter.setPen(QPen(QColor(theme_accent)))
    font = QFont("Segoe UI", 26, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "C")
    painter.end()
    return QIcon(pixmap)


#: SetWindowPos arguments — see _reassert_topmost below.
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERZORDER = 0x0200

#: SetWinEventHook arguments — see _install_foreground_hook below.
_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002


def _user32():
    """user32 with the signatures we need declared explicitly.

    ctypes marshals an undeclared Python ``int`` as a 32-bit C ``int``. On x64
    that turns ``HWND_TOPMOST`` (-1) into ``0x00000000FFFFFFFF`` rather than a
    sign-extended ``0xFFFFFFFFFFFFFFFF``, so SetWindowPos rejects it and
    returns 0 — silently, if you do not check. Handles must therefore be passed
    as ``wintypes.HWND``, and pointer-sized return values must be declared or
    they are truncated.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    if getattr(user32, "_overlay_signatures_ready", False):
        return user32

    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWinEventHook.restype = ctypes.c_void_p
    user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
    user32.UnhookWinEvent.restype = wintypes.BOOL
    user32._overlay_signatures_ready = True
    return user32


def set_windows_app_id(app_id: str = "claude.code.overlay.1") -> None:
    """Give Windows a stable AppUserModelID so the tray icon groups correctly."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:  # pragma: no cover - cosmetic only
        log.debug("Could not set AppUserModelID", exc_info=True)


class OverlayWindow(QWidget):
    """The overlay panel itself."""

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._drag_offset: Optional[QPoint] = None
        self._dragging = False
        self._menu: Optional[QMenu] = None
        self.tray: Optional[QSystemTrayIcon] = None
        self._topmost_failed = False

        self.session = SessionUsage(status="Scanning...")
        self.limits = load_cached_limits()

        self._configure_window()
        self._build_fonts()
        self._build_actions()
        self._build_tray()
        self._build_monitor()
        self.apply_position(reset=False)

    # ------------------------------------------------------------ window

    def _window_flags(self) -> Qt.WindowType:
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        if self.config.window.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        return flags

    def _configure_window(self) -> None:
        win = self.config.window
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(self._window_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(win.width, win.height)
        self.setWindowOpacity(max(0.2, min(1.0, win.opacity)))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        self.setWindowIcon(load_app_icon(self.config.theme.accent))

    def _build_fonts(self) -> None:
        theme = self.config.theme
        self.font_primary = QFont(theme.font_family, theme.font_size_primary, QFont.Weight.DemiBold)
        self.font_secondary = QFont(theme.font_family, theme.font_size_secondary)
        self.font_badge = QFont(theme.font_family, theme.font_size_primary - 1, QFont.Weight.DemiBold)

    # ------------------------------------------------------------ actions

    def _build_actions(self) -> None:
        self.action_refresh = QAction("Refresh Now", self)
        self.action_refresh.triggered.connect(self.refresh_now)

        self.action_on_top = QAction("Always on Top", self)
        self.action_on_top.setCheckable(True)
        self.action_on_top.setChecked(self.config.window.always_on_top)
        self.action_on_top.triggered.connect(self.toggle_always_on_top)

        self.action_reset = QAction("Reset Position", self)
        self.action_reset.triggered.connect(lambda: self.apply_position(reset=True))

        self.action_layout_rows = QAction("Rows", self)
        self.action_layout_rows.setCheckable(True)
        self.action_layout_rows.triggered.connect(lambda: self.set_layout("rows"))

        self.action_layout_compact = QAction("Compact", self)
        self.action_layout_compact.setCheckable(True)
        self.action_layout_compact.triggered.connect(lambda: self.set_layout("compact"))
        self._sync_layout_actions()

        self.action_cache = QAction("Count Cache Tokens", self)
        self.action_cache.setCheckable(True)
        self.action_cache.setChecked(self.config.parser.include_cache_tokens)
        self.action_cache.triggered.connect(self.toggle_cache_tokens)

        self.action_hide = QAction("Hide Overlay", self)
        self.action_hide.triggered.connect(self.toggle_visible)

        self.action_exit = QAction("Exit", self)
        self.action_exit.triggered.connect(self.quit_app)

    def _context_menu(self) -> QMenu:
        # Built once and reused: a fresh QMenu(self) per right-click would
        # accumulate children for the lifetime of the window.
        if getattr(self, "_menu", None) is not None:
            return self._menu
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_stylesheet())
        menu.addAction(self.action_refresh)
        menu.addAction(self.action_on_top)
        menu.addAction(self.action_reset)
        menu.addSeparator()
        layout_menu = menu.addMenu("Layout")
        layout_menu.setStyleSheet(self._menu_stylesheet())
        layout_menu.addAction(self.action_layout_rows)
        layout_menu.addAction(self.action_layout_compact)
        menu.addAction(self.action_cache)
        if self.tray is not None:
            # Without a tray icon there would be no way to bring it back.
            menu.addAction(self.action_hide)
        menu.addSeparator()
        menu.addAction(self.action_exit)
        self._menu = menu
        return menu

    def _menu_stylesheet(self) -> str:
        theme = self.config.theme
        return f"""
            QMenu {{
                background-color: #1b1e21;
                color: {theme.text_primary};
                border: 1px solid #2e3338;
                padding: 4px;
                font-family: "{theme.font_family}";
                font-size: {theme.font_size_secondary + 1}pt;
            }}
            QMenu::item {{ padding: 5px 20px 5px 18px; border-radius: 4px; }}
            QMenu::item:selected {{ background-color: {theme.accent}; color: #ffffff; }}
            QMenu::separator {{ height: 1px; background: #2e3338; margin: 4px 6px; }}
            QMenu::indicator {{ width: 12px; height: 12px; left: 4px; }}
        """

    def _show_context_menu(self, pos: QPoint) -> None:
        self.action_hide.setText("Hide Overlay" if self.isVisible() else "Show Overlay")
        self._context_menu().exec(self.mapToGlobal(pos))

    # --------------------------------------------------------------- tray

    def _build_tray(self) -> None:
        if not self.config.window.show_tray_icon:
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("System tray unavailable; running without a tray icon")
            return

        self.tray = QSystemTrayIcon(load_app_icon(self.config.theme.accent), self)
        self.tray.setToolTip(f"{APP_NAME} {APP_VERSION}")
        menu = QMenu()
        menu.setStyleSheet(self._menu_stylesheet())
        self._tray_menu = menu  # setContextMenu does not take ownership
        self.action_tray_toggle = QAction("Show / Hide Overlay", menu)
        self.action_tray_toggle.triggered.connect(self.toggle_visible)
        menu.addAction(self.action_tray_toggle)
        menu.addAction(self.action_refresh)
        menu.addAction(self.action_on_top)
        menu.addAction(self.action_reset)
        menu.addSeparator()
        menu.addAction(self.action_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_visible()

    # ------------------------------------------------------------ polling

    def _reassert_topmost(self) -> None:
        """Push the overlay back to the top of the topmost band.

        The taskbar (``Shell_TrayWnd``) is a topmost window too, and topmost
        windows are ordered among themselves by activation — so clicking the
        taskbar raises it above the overlay, which then looks like it has
        vanished. Qt's WindowStaysOnTopHint only sets the style at creation; it
        does not defend the position. SWP_NOACTIVATE keeps focus where it is,
        so re-asserting never steals the click the user just made.
        """
        if os.name != "nt" or not self.config.window.always_on_top:
            return
        if not self.isVisible():
            return
        try:
            from ctypes import wintypes

            user32 = _user32()
            ok = user32.SetWindowPos(
                wintypes.HWND(int(self.winId())),
                wintypes.HWND(_HWND_TOPMOST),
                0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER,
            )
            if not ok and not self._topmost_failed:
                # Log once rather than every second.
                self._topmost_failed = True
                log.warning("SetWindowPos(HWND_TOPMOST) failed; overlay may fall behind")
        except Exception:  # pragma: no cover - cosmetic, never fatal
            log.debug("Could not re-assert topmost", exc_info=True)

    def _install_foreground_hook(self) -> None:
        """Re-assert topmost the instant another window takes the foreground.

        The polling timer alone leaves up to one interval during which the
        taskbar covers the overlay, which is exactly the flicker the timer is
        supposed to prevent. EVENT_SYSTEM_FOREGROUND fires the moment the user
        clicks the taskbar, so the correction lands in the same frame.

        Delivery is WINEVENT_OUTOFCONTEXT, so the callback arrives on this
        thread through the normal message queue and it is safe to touch the
        window from it.
        """
        self._win_event_hook = None
        self._win_event_proc = None
        if os.name != "nt" or self.config.window.topmost_interval_ms <= 0:
            return
        try:
            import ctypes
            from ctypes import wintypes

            prototype = ctypes.WINFUNCTYPE(
                None,
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.HWND,
                wintypes.LONG,
                wintypes.LONG,
                wintypes.DWORD,
                wintypes.DWORD,
            )

            def on_foreground(*_args) -> None:
                try:
                    self._reassert_topmost()
                except Exception:  # pragma: no cover - must never raise into Win32
                    pass

            # The callback must outlive the hook: ctypes does not keep a
            # reference, and a collected thunk means a crash when it fires.
            self._win_event_proc = prototype(on_foreground)
            self._win_event_hook = _user32().SetWinEventHook(
                _EVENT_SYSTEM_FOREGROUND,
                _EVENT_SYSTEM_FOREGROUND,
                None,
                self._win_event_proc,
                0,
                0,
                _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS,
            )
            if not self._win_event_hook:
                log.debug("SetWinEventHook failed; falling back to the timer alone")
        except Exception:  # pragma: no cover - cosmetic, never fatal
            log.debug("Could not install foreground hook", exc_info=True)

    def _remove_foreground_hook(self) -> None:
        if not getattr(self, "_win_event_hook", None):
            return
        try:
            _user32().UnhookWinEvent(self._win_event_hook)
        except Exception:  # pragma: no cover
            pass
        self._win_event_hook = None
        self._win_event_proc = None

    def _build_monitor(self) -> None:
        self.monitor = UsageMonitor(self.config, self)
        self.monitor.session_ready.connect(self._on_session)
        self.monitor.limits_ready.connect(self._on_limits)

        polling = self.config.polling
        self.session_timer = QTimer(self)
        self.session_timer.setInterval(max(250, polling.session_interval_ms))
        self.session_timer.timeout.connect(self.monitor.refresh_session)

        self.cli_timer = QTimer(self)
        self.cli_timer.setInterval(max(10_000, polling.cli_interval_ms))
        self.cli_timer.timeout.connect(self.monitor.refresh_limits)

        self.topmost_timer = QTimer(self)
        self.topmost_timer.setInterval(max(250, self.config.window.topmost_interval_ms))
        self.topmost_timer.timeout.connect(self._reassert_topmost)

    def start(self) -> None:
        self.session_timer.start()
        self.cli_timer.start()
        if self.config.window.topmost_interval_ms > 0:
            self.topmost_timer.start()
            self._install_foreground_hook()
            self._reassert_topmost()
        QTimer.singleShot(0, self.monitor.refresh_session)
        QTimer.singleShot(400, self.monitor.refresh_limits)

    def refresh_now(self) -> None:
        self.monitor.refresh_all()

    def _on_session(self, usage: SessionUsage) -> None:
        self.session = usage
        self._refresh_chrome()

    def _on_limits(self, limits: LimitUsage) -> None:
        self.limits = limits
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        tooltip = build_tooltip(self.session, self.limits, self.config)
        self.setToolTip(tooltip)
        if self.tray is not None:
            limits = self.limits
            if limits.session_percent is None and limits.weekly_percent is None:
                summary = "plan usage unavailable"
            else:
                summary = (
                    f"session {self._percent_text(limits.session_percent)} · "
                    f"week {self._percent_text(limits.weekly_percent)}"
                )
            self.tray.setToolTip(f"{APP_NAME} — {summary}")
        self.update()

    # ----------------------------------------------------------- commands

    def toggle_always_on_top(self) -> None:
        self.config.window.always_on_top = not self.config.window.always_on_top
        self.action_on_top.setChecked(self.config.window.always_on_top)
        was_visible = self.isVisible()
        # Changing window flags recreates the native window, so restore state.
        self.setWindowFlags(self._window_flags())
        if was_visible:
            self.show()
        if self.config.window.always_on_top:
            self._reassert_topmost()
        self.config.save()

    def _sync_layout_actions(self) -> None:
        current = self.config.window.layout
        self.action_layout_rows.setChecked(current == "rows")
        self.action_layout_compact.setChecked(current == "compact")

    def set_layout(self, name: str) -> None:
        self.config.window.layout = name
        self._sync_layout_actions()
        self.config.save()
        self.update()

    def toggle_cache_tokens(self) -> None:
        self.config.parser.include_cache_tokens = not self.config.parser.include_cache_tokens
        self.action_cache.setChecked(self.config.parser.include_cache_tokens)
        self.config.save()
        self._refresh_chrome()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self._reassert_topmost()

    def apply_position(self, reset: bool = False) -> None:
        """Move to the stored custom position, or re-anchor to bottom-left."""
        win = self.config.window
        if reset:
            win.pos_x = None
            win.pos_y = None

        target = self._anchored_position()
        if win.pos_x is not None and win.pos_y is not None:
            candidate = QPoint(int(win.pos_x), int(win.pos_y))
            if self._is_on_screen(candidate):
                target = candidate
            else:
                log.info("Stored position is off-screen; re-anchoring")
        self.move(target)
        if reset:
            self.config.save()

    def _anchored_position(self) -> QPoint:
        win = self.config.window
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return QPoint(win.margin_x, win.margin_y)

        available = screen.availableGeometry()
        full = screen.geometry()

        if win.anchor_over_taskbar:
            # Sit inside the taskbar strip: measure from the physical screen
            # edge, not from the work area, which stops above the taskbar.
            left = full.left()
            bottom = full.bottom()
        else:
            left = available.left()
            # availableGeometry already excludes the taskbar; if the platform
            # reports nothing useful, fall back to a fixed allowance.
            bottom = (
                full.bottom() - win.taskbar_fallback
                if available.height() >= full.height()
                else available.bottom()
            )

        x = left + win.margin_x
        y = bottom - win.height - win.margin_y
        return QPoint(int(x), int(y))

    def _is_on_screen(self, point: QPoint) -> bool:
        rect = self.rect().translated(point)
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(rect):
                return True
        return False

    def quit_app(self) -> None:
        self.close()
        QApplication.instance().quit()

    # ------------------------------------------------------------- events

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._dragging = True
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._drag_offset = None
            position = self.pos()
            self.config.window.pos_x = position.x()
            self.config.window.pos_y = position.y()
            self.config.save()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.refresh_now()
            event.accept()

    def wheelEvent(self, event) -> None:
        """Scroll over the overlay to fade it in and out."""
        delta = 0.04 if event.angleDelta().y() > 0 else -0.04
        opacity = max(0.25, min(1.0, self.windowOpacity() + delta))
        self.setWindowOpacity(opacity)
        self.config.window.opacity = round(opacity, 3)
        event.accept()

    def closeEvent(self, event) -> None:
        self.session_timer.stop()
        self.cli_timer.stop()
        self.topmost_timer.stop()
        self._remove_foreground_hook()
        self.monitor.shutdown()
        if self.tray is not None:
            self.tray.hide()
        self.config.save()
        super().closeEvent(event)

    # ------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:  # noqa: ARG002 - Qt signature
        theme = self.config.theme
        win = self.config.window

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        inset = win.border_width / 2.0
        body = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        shell = QPainterPath()
        shell.addRoundedRect(body, float(win.corner_radius), float(win.corner_radius))
        painter.fillPath(shell, QColor(theme.background))
        if win.border_width > 0:
            painter.setPen(QPen(QColor(theme.border), float(win.border_width)))
            painter.drawPath(shell)

        painters = {"rows": self._paint_rows, "compact": self._paint_compact}
        painters.get(win.layout, self._paint_rows)(painter)
        painter.end()

    # -- shared helpers --------------------------------------------------

    def _metrics(self) -> list:
        """The two limits, in display order: ``(label, percent, countdown)``."""
        limits = self.limits
        return [
            ("SESSION", limits.session_percent, limits.session_countdown()),
            ("WEEK", limits.weekly_percent, limits.weekly_countdown()),
        ]

    def _fraction(self, percent: Optional[float]) -> float:
        if percent is None:
            return 0.0
        return max(0.0, min(1.0, percent / 100.0))

    def _percent_text(self, percent: Optional[float]) -> str:
        return "--" if percent is None else f"{percent:.0f}%"

    def _countdown_text(self, countdown: str, percent: Optional[float]) -> str:
        """Reset countdown, or a placeholder when the CLI told us nothing."""
        if percent is None:
            return "--"
        return countdown or "--"

    def _draw_bar(
        self, painter: QPainter, rect: QRectF, fraction: float, colour: QColor
    ) -> None:
        """A rounded track with a rounded fill, clipped so the cap never bleeds."""
        radius = rect.height() / 2.0
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(self.config.theme.track))
        if fraction <= 0.0:
            return
        fill = QRectF(rect)
        fill.setWidth(max(rect.height(), rect.width() * fraction))
        path = QPainterPath()
        path.addRoundedRect(fill, radius, radius)
        painter.save()
        painter.setClipPath(track)
        painter.fillPath(path, colour)
        painter.restore()

    # -- layout: rows ----------------------------------------------------

    def _paint_rows(self, painter: QPainter) -> None:
        """Label, percentage, inline bar and countdown on one line each."""
        theme = self.config.theme
        pad_x, pad_y = 10.0, 5.0
        row_h = (self.height() - pad_y * 2) / 2.0
        content = self.width() - pad_x * 2

        label_w, value_w, time_w, gap = 44.0, 28.0, 40.0, 8.0
        bar_w = content - label_w - value_w - time_w - gap * 2

        font_label = QFont(theme.font_family, theme.font_size_label, QFont.Weight.Bold)
        font_label.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        font_value = QFont(theme.font_family, theme.font_size_value, QFont.Weight.DemiBold)
        font_time = QFont(theme.font_family, theme.font_size_label)

        left_align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        right_align = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for index, (label, percent, countdown) in enumerate(self._metrics()):
            top = pad_y + index * row_h
            row = QRectF(pad_x, top, content, row_h)
            colour = QColor(theme.color_for(percent))

            painter.setFont(font_label)
            painter.setPen(QPen(QColor(theme.text_secondary)))
            painter.drawText(QRectF(row.left(), row.top(), label_w, row_h), left_align, label)

            painter.setFont(font_value)
            painter.setPen(QPen(colour))
            painter.drawText(
                QRectF(row.left() + label_w, row.top(), value_w, row_h),
                right_align,
                self._percent_text(percent),
            )

            bar_x = row.left() + label_w + value_w + gap
            self._draw_bar(
                painter,
                QRectF(bar_x, row.center().y() - 1.5, bar_w, 3.0),
                self._fraction(percent),
                colour,
            )


            painter.setFont(font_time)
            painter.setPen(QPen(QColor(theme.text_secondary)))
            painter.drawText(
                QRectF(row.right() - time_w, row.top(), time_w, row_h),
                right_align,
                self._countdown_text(countdown, percent),
            )

    # -- layout: compact -------------------------------------------------

    def _paint_compact(self, painter: QPainter) -> None:
        """Text line with a full-width bar underneath it, twice."""
        theme = self.config.theme
        pad_x, pad_y = 10.0, 6.0
        gap = 4.0
        block_h = (self.height() - pad_y * 2 - gap) / 2.0
        content = self.width() - pad_x * 2
        bar_h = 3.0

        font_label = QFont(theme.font_family, theme.font_size_label, QFont.Weight.Bold)
        font_label.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        font_value = QFont(theme.font_family, theme.font_size_value, QFont.Weight.DemiBold)
        font_time = QFont(theme.font_family, theme.font_size_label)

        left_align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        right_align = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for index, (label, percent, countdown) in enumerate(self._metrics()):
            top = pad_y + index * (block_h + gap)
            colour = QColor(theme.color_for(percent))
            text_row = QRectF(pad_x, top, content, block_h - bar_h - 2.0)

            painter.setFont(font_label)
            painter.setPen(QPen(QColor(theme.text_secondary)))
            painter.drawText(text_row, left_align, label)

            painter.setFont(font_value)
            painter.setPen(QPen(colour))
            painter.drawText(
                QRectF(text_row.left() + 48.0, text_row.top(), 40.0, text_row.height()),
                left_align,
                self._percent_text(percent),
            )

            painter.setFont(font_time)
            painter.setPen(QPen(QColor(theme.text_secondary)))
            painter.drawText(text_row, right_align, self._countdown_text(countdown, percent))

            self._draw_bar(
                painter,
                QRectF(pad_x, top + block_h - bar_h, content, bar_h),
                self._fraction(percent),
                colour,
            )


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude-code-overlay",
        description="Live Claude Code token-usage overlay for Windows 11.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    parser.add_argument("--no-tray", action="store_true", help="do not create a tray icon")
    parser.add_argument("--reset-position", action="store_true", help="ignore the saved position")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    set_windows_app_id()

    config = Config.load()
    if args.no_tray:
        config.window.show_tray_icon = False
    if args.reset_position:
        config.window.pos_x = None
        config.window.pos_y = None

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)  # tray keeps the app alive when hidden

    window = OverlayWindow(config)
    start_hidden = args.minimized or config.window.start_hidden
    if start_hidden and window.tray is not None:
        log.info("Starting minimized to tray")
    else:
        window.show()
    window.start()

    log.info("%s %s started", APP_NAME, APP_VERSION)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
