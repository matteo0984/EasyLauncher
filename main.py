"""
main.py - Entry point di EasyLauncher (100% portable).

Alla prima esecuzione crea automaticamente nella cartella radice:
  data/library.db, data/config.json, emulators/, roms/, media/
Applica il tema Dark Mode QSS e avvia MainWindow.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from database import Config, Database, app_root, ensure_tree
from ui_main import MainWindow, apply_app_theme, apply_window_titlebar


class _DarkTitleBarFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Show, QEvent.WinIdChange) and isinstance(obj, QWidget) and obj.isWindow():
            apply_window_titlebar(obj)
        return super().eventFilter(obj, event)


def main() -> int:
    root = app_root()
    ensure_tree(root)

    db = Database(root)
    config = Config(root)

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EasyLauncher.Portable.Frontend")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("EasyLauncher")
    for icon_name in ("app_icon.ico", "app_icon.png"):
        icon_file = os.path.join(root, "icons", icon_name)
        if os.path.isfile(icon_file):
            app.setWindowIcon(QIcon(icon_file))
            break
    app.setStyle("Fusion")
    config.load()
    apply_app_theme(
        app,
        config.get("theme", "dark"),
        config.get("accent_color", "#f58a1f"),
        divider=config.get("divider_color", ""),
    )

    dark_filter = _DarkTitleBarFilter(app)
    app.installEventFilter(dark_filter)

    splash: QLabel | None = None
    t_splash_start = time.monotonic()
    splash_file = os.path.join(root, "icons", "splash_logo.png")
    if os.path.isfile(splash_file):
        pm = QPixmap(splash_file)
        if not pm.isNull():
            splash = QLabel()
            splash.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            splash.setStyleSheet("border: none; margin: 0px; padding: 0px;")
            splash.setPixmap(pm)
            splash.resize(pm.size())
            screen = app.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                splash.move(geo.center() - splash.rect().center())
            splash.show()
            app.processEvents()

    win = MainWindow(db, config)

    if splash is not None:
        elapsed_ms = int((time.monotonic() - t_splash_start) * 1000)
        remaining_ms = max(0, 3000 - elapsed_ms)

        def _finish_splash() -> None:
            splash.close()
            app.processEvents()
            if getattr(win, "_start_maximized", False):
                win.showMaximized()
            else:
                win.show()
            if config.get("start_fullscreen", False):
                QTimer.singleShot(0, win._open_bigscreen)

        QTimer.singleShot(remaining_ms, _finish_splash)
    else:
        if getattr(win, "_start_maximized", False):
            win.showMaximized()
        else:
            win.show()
        if config.get("start_fullscreen", False):
            QTimer.singleShot(0, win._open_bigscreen)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
