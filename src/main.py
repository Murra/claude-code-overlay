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
    QFontMetrics,
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
    format_io,
    format_percent,
    format_tokens,
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

    def start(self) -> None:
        self.session_timer.start()
        self.cli_timer.start()
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
            tokens = format_tokens(self.session, self.config.parser.include_cache_tokens)
            self.tray.setToolTip(f"{APP_NAME} — {tokens} this session")
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
        self.config.save()

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
        # availableGeometry already excludes the taskbar; if the platform gives
        # us nothing useful, fall back to a fixed taskbar allowance.
        if available.height() >= full.height():
            bottom = full.bottom() - win.taskbar_fallback
        else:
            bottom = available.bottom()
        x = available.left() + win.margin_x
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

        radius = float(win.corner_radius)
        inset = win.border_width / 2.0
        body = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)

        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        painter.fillPath(path, QColor(theme.background))
        if win.border_width > 0:
            painter.setPen(QPen(QColor(theme.border), float(win.border_width)))
            painter.drawPath(path)

        pad_x = 11
        content_width = self.width() - 2 * pad_x
        headline = self.limits.headline_percent
        headline_color = QColor(theme.color_for(headline))

        # --- accent dot ------------------------------------------------
        dot_color = QColor(theme.accent if self.session.ok else theme.text_secondary)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(dot_color))
        painter.drawEllipse(QRectF(pad_x, 15.0, 6.0, 6.0))

        # --- primary line ----------------------------------------------
        tokens = format_tokens(self.session, self.config.parser.include_cache_tokens)
        painter.setFont(self.font_primary)
        painter.setPen(QPen(QColor(theme.text_primary)))
        primary_rect = QRectF(pad_x + 13, 8, content_width - 13, 20)
        painter.drawText(
            primary_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{tokens} tokens",
        )

        # --- percentage badge (right aligned, same baseline) ------------
        painter.setFont(self.font_badge)
        painter.setPen(QPen(headline_color))
        painter.drawText(
            primary_rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            format_percent(headline),
        )

        # --- secondary line ---------------------------------------------
        painter.setFont(self.font_secondary)
        painter.setPen(QPen(QColor(theme.text_secondary)))
        secondary_rect = QRectF(pad_x, 28, content_width, 16)
        detail = format_io(self.session)
        metrics = QFontMetrics(self.font_secondary)
        label = self._limit_label()
        label_width = metrics.horizontalAdvance(label) + 8
        painter.drawText(
            QRectF(secondary_rect.left(), secondary_rect.top(),
                   secondary_rect.width() - label_width, secondary_rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(
                detail,
                Qt.TextElideMode.ElideRight,
                int(secondary_rect.width() - label_width),
            ),
        )
        painter.drawText(
            secondary_rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            label,
        )

        # --- progress bar -----------------------------------------------
        track = QRectF(pad_x, self.height() - 14.0, float(content_width), 4.0)
        track_path = QPainterPath()
        track_path.addRoundedRect(track, 2.0, 2.0)
        painter.fillPath(track_path, QColor("#2a2e33"))

        fraction = self._bar_fraction(headline)
        if fraction > 0:
            fill = QRectF(track)
            fill.setWidth(max(4.0, track.width() * fraction))
            fill_path = QPainterPath()
            fill_path.addRoundedRect(fill, 2.0, 2.0)
            painter.fillPath(fill_path, headline_color)

        painter.end()

    def _limit_label(self) -> str:
        """Caption for the percentage badge, reflecting where the number came from."""
        if self.limits.weekly_percent is not None:
            return "cached wk" if self.limits.from_cache else "weekly"
        if self.limits.session_percent is not None:
            return "cached" if self.limits.from_cache else "plan"
        if not self.session.ok:
            return self.session.status.lower()[:14]
        return "context"

    def _bar_fraction(self, headline: Optional[float]) -> float:
        """Bar tracks plan usage when known, otherwise local context fill."""
        if headline is not None:
            return max(0.0, min(1.0, headline / 100.0))
        context = self.session.context_percent(
            self.config.parser.context_window_tokens,
            self.config.parser.include_cache_tokens,
        )
        if context is None:
            return 0.0
        return max(0.0, min(1.0, context / 100.0))


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
