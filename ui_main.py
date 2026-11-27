"""
ui_main.py - Interfaccia grafica principale di EasyLauncher (PySide6 / Qt6).

Layout a 3 colonne stile LaunchBox con QSplitter ridimensionabili:
  * Sinistra: QTreeWidget con "Tutte le Piattaforme" + nodi figli per console (icona + conteggio)
  * Centro:   QListView (IconMode) con delegate personalizzato e caricamento lazy via QThreadPool
              + slider zoom nella barra inferiore
  * Destra:   QSplitter verticale -> in alto anteprima media HD, in basso area scorrevole con
              Titolo, pulsante grande "AVVIA GIOCO", metadati completi e descrizione/trama.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import OrderedDict
from typing import Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygon,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QSplitterHandle,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from database import Config, Database, app_root
from dialogs import (
    AppearanceDialog,
    ChooseFavoriteFolderDialog,
    ExtraScreenshotsDialog,
    LayoutLockDialog,
    BackupRestoreDialog,
    EmulatorManagerDialog,
    GameEditDialog,
    PlatformDialog,
    PlatformManagerDialog,
    ScrapeBatchDialog,
)
from i18n import LANGUAGES, get_lang, set_lang, t
from launcher import launch_game
from bigscreen import BigScreenWindow

# --------------------------------------------------------------------------
# Tema Dinamico QSS (Scuro / Bianco + colore linee/accento personalizzabile)
# --------------------------------------------------------------------------

def _contrast_on_accent(hex_color: str) -> str:
    c = QColor(hex_color)
    if not c.isValid():
        return "#1c1000"
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return "#14161b" if lum > 140 else "#ffffff"


def build_qss(
    theme: str = "dark",
    accent: str = "#f58a1f",
    divider: str = "",
    scrollbar: str = "",
) -> str:
    ac = QColor(accent)
    if not ac.isValid():
        ac = QColor("#f58a1f")
    acc = ac.name()
    acc_hover = ac.lighter(118).name()
    acc_r, acc_g, acc_b = ac.red(), ac.green(), ac.blue()
    on_acc = _contrast_on_accent(acc)

    if theme == "light":
        bg_main = "#f3f5f8"
        bg_panel = "#ffffff"
        bg_bar = "#e9edf3"
        bg_input = "#ffffff"
        bg_btn = "#e3e7ef"
        bg_btn_hover = "#d4dae6"
        bg_hover_menu = "#e4e9f2"
        bg_splitter = "#dce1ea"
        border = "#c8d0de"
        border_dark = "#b8c2d2"
        border_handle = "#b4becf"
        text_main = "#1a1d24"
        text_title = "#11141a"
        text_muted = "#5b6475"
        text_disabled = "rgba(26, 29, 36, 95)"
        bg_disabled = "rgba(210, 216, 228, 120)"
        bg_status = "#e4e8f0"
        scroll_handle = "#b2bac9"
        tab_unselected = "#e4e8f0"
        tab_selected = "#ffffff"
        chk_border = "#8a94a6"
    else:
        bg_main = "#14161b"
        bg_panel = "#181b22"
        bg_bar = "#1b1e25"
        bg_input = "#0f1116"
        bg_btn = "#232733"
        bg_btn_hover = "#2f3647"
        bg_hover_menu = "#2f3647"
        bg_splitter = "#1f2430"
        border = "#2c313e"
        border_dark = "#0b0c0f"
        border_handle = "#2d3342"
        text_main = "#e6e8ee"
        text_title = "#ffffff"
        text_muted = "#8b92a3"
        text_disabled = "rgba(230, 232, 238, 85)"
        bg_disabled = "rgba(35, 39, 51, 95)"
        bg_status = "#0e1014"
        scroll_handle = "#363c4b"
        tab_unselected = "#1b1e25"
        tab_selected = "#232733"
        chk_border = "#4a5468"

    border_input = border
    div_transp = divider == "transparent"
    div_c = QColor(divider) if (divider and not div_transp) else QColor()
    if div_transp:
        bg_splitter = "transparent"
        border_handle = "transparent"
    elif div_c.isValid():
        bg_splitter = div_c.name()
        border_handle = div_c.name()
        border = div_c.name()

    scroll_handle = acc
    scroll_hover = acc_hover

    arrow_name = "arrow_down_dark.png" if theme == "light" else "arrow_down_light.png"
    arrow_url = ""
    for base in (app_root(), os.path.dirname(os.path.abspath(__file__))):
        cand = os.path.join(base, "icons", arrow_name)
        if os.path.isfile(cand):
            arrow_url = cand.replace("\\", "/")
            break
    arrow_rule = (
        f'QComboBox::down-arrow {{ image: url("{arrow_url}"); width: 12px; height: 12px; }}'
        if arrow_url
        else ""
    )

    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: {bg_main};
    color: {text_main};
    font-family: "Segoe UI", sans-serif;
    font-size: 10pt;
}}
QMenuBar {{
    background-color: {bg_bar};
    border-bottom: 1px solid {border_dark};
    padding: 2px 6px;
}}
QMenuBar::item {{
    padding: 5px 10px;
    border-radius: 4px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background-color: {bg_hover_menu};
    color: {text_title};
}}
QMenu {{
    background-color: {bg_bar};
    border: 1px solid {border};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 22px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {acc};
    color: {on_acc};
}}
QMenu::separator {{
    height: 1px;
    background-color: {border};
    margin: 5px 10px;
}}
QSplitter::handle {{
    background-color: {bg_splitter};
    border: none;
}}
QSplitter::handle:horizontal {{
    width: 3px;
    border: none;
}}
QSplitter::handle:vertical {{
    height: 3px;
    border: none;
}}
QSplitter::handle:hover {{
    background-color: {acc};
    border: none;
}}
QTreeWidget, QListView, QListWidget, QTableWidget, QPlainTextEdit {{
    background-color: {bg_panel};
    border: 1px solid {border};
    outline: 0;
}}
QTreeWidget, QListView#RomDropGrid {{
    border: none;
}}
QTreeWidget {{
    show-decoration-selected: 0;
}}
QTreeWidget::branch, QTreeWidget::branch:selected, QTreeWidget::branch:hover {{
    background-color: transparent;
}}
QTreeWidget::item {{
    padding: 5px 8px;
    border: none;
}}
QTreeWidget::item:hover,
QTreeWidget::item:selected,
QTreeWidget::item:selected:!active {{
    background-color: transparent;
    border: none;
    color: {text_title};
}}
QLineEdit, QComboBox, QDoubleSpinBox {{
    background-color: {bg_input};
    border: 1px solid {border_input};
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: {acc};
    selection-color: {on_acc};
}}
QComboBox {{
    padding-right: 26px;
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    border: none;
    width: 24px;
    background: transparent;
}}
{arrow_rule}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{
    border-color: {acc};
}}
QComboBox QAbstractItemView {{
    background-color: {bg_panel};
    border: 1px solid {border_input};
    border-radius: 6px;
    padding: 3px;
    selection-background-color: {acc};
    selection-color: {on_acc};
    outline: 0;
}}
QPushButton {{
    background-color: {bg_btn};
    border: 1px solid {border_input};
    border-radius: 5px;
    padding: 6px 12px;
    color: {text_main};
}}
QPushButton:hover {{
    background-color: {bg_btn_hover};
}}
QPushButton:disabled {{
    background-color: {bg_disabled};
    border-color: {border};
    color: {text_disabled};
}}
QPushButton#AccentButton, QPushButton#PlayButton {{
    background-color: {acc};
    color: {on_acc};
    border: none;
    font-weight: bold;
}}
QPushButton#AccentButton:hover, QPushButton#PlayButton:hover {{
    background-color: {acc_hover};
}}
QPushButton#AccentButton:disabled, QPushButton#PlayButton:disabled {{
    background-color: rgba({acc_r}, {acc_g}, {acc_b}, 85);
    color: rgba(28, 16, 0, 110);
}}
QPushButton#PlayButton {{
    font-size: 12pt;
    padding: 11px 16px;
    border-radius: 7px;
}}
QLabel#MutedHint {{
    color: {text_muted};
    font-size: 9pt;
    background-color: transparent;
}}
QLabel#HeaderTitle {{
    font-size: 14pt;
    font-weight: 600;
    color: {text_title};
    background-color: transparent;
}}
QScrollArea {{
    border: none;
    background-color: transparent;
}}
QStatusBar {{
    background-color: {bg_status};
    color: {text_muted};
    border-top: 1px solid {border};
}}
QSlider::groove:horizontal {{
    height: 5px;
    background: {bg_btn};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {acc};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QProgressBar {{
    background-color: {bg_input};
    border: 1px solid {border};
    border-radius: 5px;
    text-align: center;
    height: 18px;
}}
QProgressBar::chunk {{
    background-color: {acc};
    border-radius: 4px;
}}
QAbstractScrollArea > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
}}
QScrollBar::handle:vertical {{
    background: {scroll_handle};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {scroll_hover};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: {scroll_handle};
    border-radius: 5px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {scroll_hover};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 18px;
    height: 18px;
    border: 1px solid {chk_border};
    background-color: {bg_input};
}}
QCheckBox::indicator {{
    border-radius: 4px;
}}
QRadioButton::indicator {{
    border-radius: 9px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {acc};
}}
QCheckBox::indicator:checked {{
    background-color: {acc};
    border-color: {acc};
    image: url("");
}}
QRadioButton::indicator:checked {{
    background-color: {acc};
    border-color: {acc};
}}
QTabWidget::pane {{
    border: 1px solid {border};
    background-color: {bg_main};
    border-radius: 4px;
}}
QTabBar::tab {{
    background-color: {tab_unselected};
    color: {text_muted};
    padding: 8px 16px;
    border: 1px solid {border};
    border-bottom: none;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    margin-right: 3px;
}}
QTabBar::tab:selected {{
    background-color: {tab_selected};
    color: {text_title};
    border-top: 2px solid {acc};
}}
QTabBar::tab:hover:!selected {{
    background-color: {bg_btn_hover};
    color: {text_main};
}}
"""


def apply_window_titlebar(widget: QWidget, theme: Optional[str] = None) -> None:
    """Applica la barra del titolo nativa Windows 10/11 (scura o chiara) in tinta col tema."""
    if sys.platform != "win32":
        return
    flags = widget.windowFlags()
    if (flags & Qt.Popup) == Qt.Popup:
        try:
            hwnd = int(widget.winId())
            if hwnd:
                corner = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        except Exception:
            pass
        return
    if flags & Qt.FramelessWindowHint:
        return
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return
        if theme is None:
            app = QApplication.instance()
            theme = str(app.property("ui_theme") or "dark") if app is not None else "dark"
        is_light = theme == "light"
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(0 if is_light else 1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(dark), ctypes.sizeof(dark))
        if is_light:
            # #ffffff -> COLORREF 0x00FFFFFF, #1a1d24 -> COLORREF 0x00241D1A
            caption_col = ctypes.c_int(0x00FFFFFF)
            text_col = ctypes.c_int(0x00241D1A)
        else:
            # #1b1e25 -> COLORREF 0x00251E1B, #e6e8ee -> COLORREF 0x00EEE8E6
            caption_col = ctypes.c_int(0x00251E1B)
            text_col = ctypes.c_int(0x00EEE8E6)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_col), ctypes.sizeof(caption_col))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_col), ctypes.sizeof(text_col))
    except Exception:
        pass


def apply_app_theme(
    app: Optional[QApplication],
    theme: str = "dark",
    accent: str = "#f58a1f",
    divider: str = "",
    scrollbar: str = "",
) -> None:
    if app is None:
        return
    t_norm = "light" if theme == "light" else "dark"
    ac = QColor(accent)
    if not ac.isValid():
        ac = QColor("#f58a1f")
    app.setProperty("ui_theme", t_norm)
    app.setProperty("ui_accent", ac.name())
    pal = app.palette()
    if t_norm == "light":
        pal.setColor(QPalette.Window, QColor("#f3f5f8"))
        pal.setColor(QPalette.WindowText, QColor("#1a1d24"))
        pal.setColor(QPalette.Base, QColor("#ffffff"))
        pal.setColor(QPalette.AlternateBase, QColor("#e9edf3"))
        pal.setColor(QPalette.Text, QColor("#1a1d24"))
        pal.setColor(QPalette.Button, QColor("#e3e7ef"))
        pal.setColor(QPalette.ButtonText, QColor("#1a1d24"))
    else:
        pal.setColor(QPalette.Window, QColor("#181b22"))
        pal.setColor(QPalette.WindowText, QColor("#e6e8ee"))
        pal.setColor(QPalette.Base, QColor("#181b22"))
        pal.setColor(QPalette.AlternateBase, QColor("#1b1e25"))
        pal.setColor(QPalette.Text, QColor("#e6e8ee"))
        pal.setColor(QPalette.Button, QColor("#232733"))
        pal.setColor(QPalette.ButtonText, QColor("#e6e8ee"))
    pal.setColor(QPalette.Highlight, ac)
    pal.setColor(QPalette.HighlightedText, QColor(_contrast_on_accent(ac.name())))
    app.setPalette(pal)
    app.setStyleSheet(build_qss(t_norm, ac.name(), divider=divider, scrollbar=scrollbar))
    for w in app.topLevelWidgets():
        if w.isWindow() and w.isVisible():
            apply_window_titlebar(w, t_norm)


DARK_QSS = build_qss("dark", "#f58a1f")


# --------------------------------------------------------------------------
# Caricamento asincrono e cache LRU miniature (QThreadPool)
# --------------------------------------------------------------------------

class _ImageSignals(QObject):
    loaded = Signal(str, int, QImage)


class _ImageLoadTask(QRunnable):
    """Carica e ridimensiona una copertina su un thread del QThreadPool."""

    def __init__(self, path: str, width: int, height: int, signals: _ImageSignals):
        super().__init__()
        self.path = path
        self.width = width
        self.height = height
        self.signals = signals

    def run(self) -> None:
        img = QImage(self.path)
        if img.isNull():
            self.signals.loaded.emit(self.path, self.width, QImage())
            return
        # Sovracampionamento 2x per schermi HiDPI / scaling Windows nitido
        scaled = img.scaled(self.width * 2, self.height * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.signals.loaded.emit(self.path, self.width, scaled)


class ThumbnailCache:
    """Cache LRU in memoria + lazy loading asincrono tramite QThreadPool."""

    def __init__(self, max_items: int = 600):
        self.max_items = max_items
        self._cache: OrderedDict[tuple[str, int], QPixmap] = OrderedDict()
        self._pending: set[tuple[str, int]] = set()
        self.pool = QThreadPool.globalInstance()
        self.signals = _ImageSignals()

    def clear(self) -> None:
        self._cache.clear()
        self._pending.clear()

    def get_or_request(self, abs_path: str, width: int, height: int) -> Optional[QPixmap]:
        if not abs_path or not os.path.isfile(abs_path):
            return None
        key = (abs_path, width)
        pix = self._cache.get(key)
        if pix is not None:
            self._cache.move_to_end(key)
            return pix
        if key not in self._pending:
            self._pending.add(key)
            self.pool.start(_ImageLoadTask(abs_path, width, height, self.signals))
        return None

    def store(self, abs_path: str, width: int, image: QImage) -> None:
        key = (abs_path, width)
        self._pending.discard(key)
        if image.isNull():
            return
        if len(self._cache) >= self.max_items:
            self._cache.popitem(last=False)
        pix = QPixmap.fromImage(image)
        pix.setDevicePixelRatio(2.0)
        self._cache[key] = pix


# --------------------------------------------------------------------------
# Modello e Delegate per QListView (griglia copertine ad alte prestazioni)
# --------------------------------------------------------------------------

ROLE_GAME_ID = Qt.UserRole + 1
ROLE_BOXART = Qt.UserRole + 2
ROLE_FAVORITE = Qt.UserRole + 3
ROLE_META = Qt.UserRole + 4
ROLE_SECTION = Qt.UserRole + 5


def _title_section_letter(title: str) -> str:
    import unicodedata
    s = (title or "").strip()
    if not s:
        return "#"
    ch = unicodedata.normalize("NFKD", s[0])
    base = ch[0] if ch else s[0]
    return base.upper() if base.isalpha() else "#"


class GameListModel(QAbstractListModel):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.games: list[sqlite3.Row] = []
        self._entries: list[tuple[bool, object]] = []
        self._id_to_row: dict[int, int] = {}
        self._section_to_row: dict[str, int] = {}
        self._group_by_letter: bool = False

    def _rebuild_entries(self) -> None:
        self._entries = []
        self._id_to_row = {}
        self._section_to_row = {}
        if self._group_by_letter:
            last_letter = None
            for g in self.games:
                letter = _title_section_letter(g["title"])
                if letter != last_letter:
                    self._section_to_row[letter] = len(self._entries)
                    self._entries.append((True, letter))
                    last_letter = letter
                self._id_to_row[g["id"]] = len(self._entries)
                self._entries.append((False, g))
        else:
            for g in self.games:
                self._id_to_row[g["id"]] = len(self._entries)
                self._entries.append((False, g))

    def set_group_by_letter(self, enabled: bool) -> None:
        if self._group_by_letter == enabled:
            return
        self.beginResetModel()
        self._group_by_letter = enabled
        self._rebuild_entries()
        self.endResetModel()

    def set_games(self, games: list[sqlite3.Row]) -> None:
        self.beginResetModel()
        self.games = games
        self._rebuild_entries()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags
        is_hdr, _ = self._entries[index.row()]
        if is_hdr:
            return Qt.NoItemFlags
        return super().flags(index)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        is_hdr, item = self._entries[index.row()]
        if is_hdr:
            if role in (ROLE_SECTION, Qt.DisplayRole):
                return item
            return None
        g = item
        if role == Qt.DisplayRole:
            return g["title"]
        if role == ROLE_GAME_ID:
            return g["id"]
        if role == ROLE_BOXART:
            return self.db.resolve(g["boxart_path"]) if g["boxart_path"] else ""
        if role == ROLE_FAVORITE:
            return bool(g["favorite"])
        if role == ROLE_META:
            parts = [
                str(g["release_year"]) if g["release_year"] else "",
                str(g["genre"] or ""),
                str(g["developer"] or ""),
            ]
            return "  •  ".join(p for p in parts if p)
        return None

    def game_at(self, row: int) -> Optional[sqlite3.Row]:
        if 0 <= row < len(self._entries):
            is_hdr, item = self._entries[row]
            return None if is_hdr else item
        return None

    def row_of(self, game_id: int) -> Optional[int]:
        return self._id_to_row.get(game_id)

    def row_of_section(self, letter: str) -> Optional[int]:
        return self._section_to_row.get(letter)

    def available_sections(self) -> set[str]:
        return set(self._section_to_row.keys())


def make_star_icon(accent_color: str = "#f58a1f", size: int = 96) -> QIcon:
    """Genera l'icona stella per la cartella Tutti i Preferiti."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pts = []
    cx, cy = size * 0.5, size * 0.52
    r_out, r_in = size * 0.415, size * 0.175
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        r = r_out if i % 2 == 0 else r_in
        pts.append(QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang)))
    ac = QColor(accent_color)
    if not ac.isValid():
        ac = QColor("#f58a1f")
    p.setBrush(ac)
    p.setPen(QPen(QColor("#ffffff"), max(1.5, size * 0.022)))
    p.drawPolygon(QPolygonF(pts))
    p.end()
    return QIcon(pm)


def make_white_gamepad_icon(size: int = 24) -> QIcon:
    """Genera un'icona pad bianca vettoriale nitida a qualsiasi dimensione."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 20.0
    p.setBrush(QColor("#ffffff"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(3 * s, 4 * s, 14 * s, 9 * s), 4 * s, 4 * s)
    p.drawEllipse(QRectF(1 * s, 7 * s, 6 * s, 9 * s))
    p.drawEllipse(QRectF(13 * s, 7 * s, 6 * s, 9 * s))
    p.setBrush(QColor("#191e29"))
    p.drawRect(QRectF(5 * s, 8 * s, 4 * s, 1.6 * s))
    p.drawRect(QRectF(6.2 * s, 6.8 * s, 1.6 * s, 4 * s))
    p.drawEllipse(QRectF(12.8 * s, 7.2 * s, 1.8 * s, 1.8 * s))
    p.drawEllipse(QRectF(14.8 * s, 8.8 * s, 1.8 * s, 1.8 * s))
    p.end()
    return QIcon(pm)


class _GripSplitterHandle(QSplitterHandle):
    """Maniglia splitter pulita senza puntini."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)


class _GripSplitter(QSplitter):
    def __init__(self, orientation: Qt.Orientation, parent: Optional[QWidget] = None):
        super().__init__(orientation, parent)
        self.setHandleWidth(3)

    def createHandle(self) -> QSplitterHandle:
        return _GripSplitterHandle(self.orientation(), self)


class PlatformTreeDelegate(QStyledItemDelegate):
    """Disegna la pillola di selezione stile LaunchBox senza oscurare icona o testo."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.light_theme = False
        self.accent_color = "#f58a1f"

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        opt = QStyleOptionViewItem(option)
        opt.font.setBold(True)
        return super().sizeHint(opt, index)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected or hovered:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            pill = option.rect.adjusted(4, 2, -6, -2)
            if self.light_theme:
                painter.setBrush(QColor("#d5dde9") if selected else QColor("#e6ebf3"))
            else:
                painter.setBrush(QColor("#2c3342") if selected else QColor("#222733"))
            painter.setPen(QPen(QColor(self.accent_color), 1.5) if selected else Qt.NoPen)
            painter.drawRoundedRect(pill, 12, 12)
            painter.restore()

        # Rimuove State_Selected e State_HasFocus per impedire al tema Fusion di coprire icona/testo
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        opt.font.setBold(True)
        if self.light_theme:
            opt.palette.setColor(opt.palette.ColorRole.Text, QColor("#11141a" if selected else "#1e2430"))
        else:
            opt.palette.setColor(opt.palette.ColorRole.Text, QColor("#ffffff" if selected else "#eef1f7"))
        super().paint(painter, opt, index)


def _make_blurred_backdrop(path: str, radius: int) -> Optional[QPixmap]:
    """Genera uno sfondo sfocato stile LaunchBox/Playnite da copertina o screenshot."""
    if not path or not os.path.isfile(path):
        return None
    img = QImage(path)
    if img.isNull():
        return None
    # Scala verso il basso per velocita' (<3 ms) e sfocatura uniforme senza artefatti
    if img.width() < img.height():
        img = img.scaledToWidth(280, Qt.SmoothTransformation)
    else:
        img = img.scaledToHeight(280, Qt.SmoothTransformation)
    w, h = img.width(), img.height()
    if w < 16 or h < 16:
        return None
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(img))
    blur = QGraphicsBlurEffect()
    blur.setBlurRadius(float(max(4, min(90, radius))))
    blur.setBlurHints(QGraphicsBlurEffect.PerformanceHint)
    item.setGraphicsEffect(blur)
    scene.addItem(item)
    # Ritaglia il bordo sfocato esterno cosi' non compaiono aloni scuri ai margini
    pad = min(w // 6, h // 6, max(8, int(radius * 0.45)))
    out_w = max(8, w - 2 * pad)
    out_h = max(8, h - 2 * pad)
    out = QPixmap(out_w, out_h)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    scene.render(painter, QRectF(0, 0, out_w, out_h), QRectF(pad, pad, out_w, out_h))
    painter.end()
    return out


class _BgSectionWidget(QWidget):
    """Contenitore di sezione (Sinistra/Centro/Destra) con supporto sfondo immagine o tinta unita."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._bg_pixmap: Optional[QPixmap] = None
        self._bg_color: Optional[QColor] = None
        self._light_theme: bool = False
        self._apply_scrim: bool = True
        self._scrim_alpha: Optional[int] = None
        self._canvas_widget: Optional[QWidget] = None

    def set_background(
        self,
        path: str,
        light_theme: bool,
        apply_scrim: bool = True,
        solid_color: str = "",
        canvas_widget: Optional[QWidget] = None,
        pixmap: Optional[QPixmap] = None,
        scrim_alpha: Optional[int] = None,
    ) -> None:
        self._light_theme = light_theme
        self._apply_scrim = apply_scrim
        self._scrim_alpha = scrim_alpha
        self._canvas_widget = canvas_widget
        if pixmap is not None and not pixmap.isNull():
            self._bg_pixmap = pixmap
            self._bg_color = None
        elif path and os.path.isfile(path):
            pm = QPixmap(path)
            self._bg_pixmap = pm if not pm.isNull() else None
            self._bg_color = None
        elif solid_color and QColor(solid_color).isValid():
            self._bg_pixmap = None
            self._bg_color = QColor(solid_color)
        else:
            self._bg_pixmap = None
            self._bg_color = None
        self.update()

    def has_background(self) -> bool:
        return self._bg_pixmap is not None or self._bg_color is not None

    def update_dynamic_live(self, pixmap: Optional[QPixmap], scrim_alpha: int) -> None:
        """Aggiorna al volo sfondo sfocato e opacita' senza ricostruire i fogli di stile."""
        self._bg_pixmap = pixmap
        self._bg_color = None
        self._scrim_alpha = scrim_alpha
        self.update()

    def paintEvent(self, event) -> None:
        if self._bg_pixmap is None and self._bg_color is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        if self._bg_pixmap is not None:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if self._canvas_widget is not None and self._canvas_widget.width() > 0 and self._canvas_widget.height() > 0:
                canvas_size = self._canvas_widget.size()
                offset = self.mapTo(self._canvas_widget, QPoint(0, 0))
            else:
                canvas_size = self.size()
                offset = QPoint(0, 0)
            scaled = self._bg_pixmap.scaled(
                canvas_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            x = (canvas_size.width() - scaled.width()) // 2 - offset.x()
            y = (canvas_size.height() - scaled.height()) // 2 - offset.y()
            painter.drawPixmap(x, y, scaled)
            if self._apply_scrim:
                alpha = self._scrim_alpha if self._scrim_alpha is not None else (170 if self._light_theme else 155)
                scrim = QColor(248, 249, 252, alpha) if self._light_theme else QColor(16, 18, 23, alpha)
                painter.fillRect(self.rect(), scrim)
        elif self._bg_color is not None:
            painter.fillRect(self.rect(), self._bg_color)
        painter.end()


class _CirclePlusButton(QPushButton):
    """Pulsante circolare trasparente disegnato con QPainter: + perfettamente centrato e bordo liscio."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("", parent)
        self.accent_color = "#f58a1f"
        self.setFixedSize(30, 30)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        hovered = self.underMouse()
        pressed = self.isDown()
        ac = QColor(self.accent_color)
        if pressed:
            painter.setBrush(QColor(ac.red(), ac.green(), ac.blue(), 195))
            pen_col = ac
        elif hovered:
            painter.setBrush(QColor(ac.red(), ac.green(), ac.blue(), 135))
            pen_col = ac
        else:
            painter.setBrush(Qt.NoBrush)
            pen_col = QColor(255, 255, 255, 210)
        painter.setPen(QPen(pen_col, 1.8))
        painter.drawEllipse(r)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        arm = 5.5
        plus_pen = QPen(QColor("#ffffff"), 2.2, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(plus_pen)
        painter.drawLine(QRectF(cx - arm, cy, arm * 2.0, 0).topLeft(), QRectF(cx + arm, cy, 0, 0).topLeft())
        painter.drawLine(QRectF(cx, cy - arm, 0, arm * 2.0).topLeft(), QRectF(cx, cy + arm, 0, 0).topLeft())
        painter.end()


class _PlatformTree(QTreeWidget):
    """Albero piattaforme con frecce espandi/comprimi e riordinamento drag-and-drop."""

    platforms_reordered = Signal(list)  # [(platform_id, sort_order), ...]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.show_drag_hint = True
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def set_drag_reorder_enabled(self, enabled: bool) -> None:
        self.show_drag_hint = bool(enabled)
        self.setDragEnabled(bool(enabled))
        self.setAcceptDrops(bool(enabled))
        self.setDropIndicatorShown(bool(enabled))
        self.viewport().update()

    def _is_valid_drop(self, pos: QPoint) -> bool:
        dragged = self.currentItem()
        if dragged is None or dragged.data(0, Qt.UserRole) is None:
            return False
        ind = self.dropIndicatorPosition()
        if ind not in (QAbstractItemView.AboveItem, QAbstractItemView.BelowItem):
            return False
        target = self.itemAt(pos)
        if target is None:
            return False
        if dragged.parent() is None:
            if target.parent() is not None:
                return False
            if target.data(0, Qt.UserRole) is None and ind == QAbstractItemView.AboveItem:
                return False
            return True
        return target.parent() is dragged.parent()

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)
        if self._is_valid_drop(event.position().toPoint()):
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if not self._is_valid_drop(event.position().toPoint()):
            event.ignore()
            return
        dragged = self.currentItem()
        was_expanded = dragged.isExpanded() if dragged is not None else True
        super().dropEvent(event)
        if dragged is not None and dragged.childCount() > 0:
            dragged.setExpanded(was_expanded)

        order: list[tuple[int, int]] = []

        def _collect_children(parent: Optional[QTreeWidgetItem]) -> None:
            count = self.topLevelItemCount() if parent is None else parent.childCount()
            idx = 1
            for i in range(count):
                it = self.topLevelItem(i) if parent is None else parent.child(i)
                pid = it.data(0, Qt.UserRole)
                if pid is not None:
                    order.append((int(pid), idx))
                    idx += 1
                if it.childCount() > 0:
                    _collect_children(it)

        _collect_children(None)
        self.platforms_reordered.emit(order)

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex) -> None:
        if not self.model() or not self.model().hasChildren(index):
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        cx = rect.center().x()
        cy = rect.center().y()
        painter.setBrush(QColor("#d5dce8"))
        painter.setPen(Qt.NoPen)
        if self.isExpanded(index):
            poly = QPolygon([QPoint(cx - 7, cy - 4), QPoint(cx + 7, cy - 4), QPoint(cx, cy + 5)])
        else:
            poly = QPolygon([QPoint(cx - 4, cy - 7), QPoint(cx + 6, cy), QPoint(cx - 4, cy + 7)])
        painter.drawPolygon(poly)
        painter.restore()

    def _last_item_bottom(self) -> int:
        n = self.topLevelItemCount()
        if n == 0:
            return 0
        it = self.topLevelItem(n - 1)
        while it is not None and it.isExpanded() and it.childCount() > 0:
            it = it.child(it.childCount() - 1)
        if it is None:
            return 0
        r = self.visualItemRect(it)
        return r.bottom() if r.isValid() else 0

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not getattr(self, "show_drag_hint", True):
            return
        last_b = self._last_item_bottom()
        vp = self.viewport()
        vh = vp.height()
        vw = vp.width()
        if vw > 120 and vh - last_b > 56:
            p = QPainter(vp)
            p.setRenderHint(QPainter.Antialiasing)
            is_light = self.palette().color(QPalette.WindowText).lightness() < 128
            p.setPen(QColor(15, 23, 42, 90) if is_light else QColor(255, 255, 255, 72))
            f = QFont("Segoe UI", 10)
            f.setItalic(True)
            p.setFont(f)
            y_top = max(last_b + 20, int(vh * 0.42))
            if y_top + 44 > vh:
                y_top = last_b + 12
            rect = QRect(14, y_top, vw - 28, min(64, vh - y_top - 8))
            p.drawText(
                rect,
                Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                t("tree_drag_hint"),
            )
            p.end()


class _RomDropGrid(QListView):
    """Griglia giochi che accetta il drag-and-drop di file ROM coerenti con la piattaforma."""

    def __init__(self, validator, on_drop, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._validator = validator
        self._on_drop = on_drop
        self.setObjectName("RomDropGrid")
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._cur_h_margins: tuple[int, int] = (0, 0)

    def _update_center_margins(self) -> None:
        d = self.itemDelegate()
        if getattr(d, "list_mode", False):
            sp = 6
            sb = self.verticalScrollBar()
            sb_w = sb.width() if (sb is not None and sb.isVisible()) else 14
            avail_w = max(240, self.contentsRect().width() - sb_w - 4)
            cols = 3
            while cols > 1 and (avail_w - (cols + 1) * sp) // cols < 160:
                cols -= 1
            col_w = max(140, (avail_w - (cols + 1) * sp) // cols)
            d.list_col_width = col_w
            d.list_full_width = max(col_w, avail_w - 2 * sp - 2)
            if self.spacing() != sp:
                self.setSpacing(sp)
            target = (2, 2)
            if target != self._cur_h_margins:
                self._cur_h_margins = target
                self.setViewportMargins(2, 4, 2, 4)
            return
        item_w = (d.cell_width + 8) if hasattr(d, "cell_width") else 168
        min_sp = 10
        sb = self.verticalScrollBar()
        sb_w = sb.width() if (sb is not None and sb.isVisible()) else 0
        avail_w = max(0, self.contentsRect().width() - sb_w)
        if avail_w <= item_w + 2 * min_sp:
            sp = min_sp
            target = (0, 0)
        else:
            cols = max(1, (avail_w - min_sp) // (item_w + min_sp))
            sp = max(min_sp, min(24, (avail_w - cols * item_w) // (cols + 1)))
            used_w = cols * item_w + (cols + 1) * sp
            extra = max(0, avail_w - used_w)
            left_pad = extra // 2
            right_pad = max(0, extra - left_pad - 2)
            target = (left_pad, right_pad)
        if self.spacing() != sp:
            self.setSpacing(sp)
        if target != self._cur_h_margins:
            self._cur_h_margins = target
            self.setViewportMargins(target[0], 0, target[1], 0)

    def resizeEvent(self, event) -> None:
        self._update_center_margins()
        super().resizeEvent(event)

    def _set_hover(self, active: bool) -> None:
        acc = getattr(self, "accent_color", "#f58a1f")
        self.setStyleSheet(f"QListView#RomDropGrid {{ border: 2px dashed {acc}; }}" if active else "")

    def dragEnterEvent(self, event) -> None:
        if self._validator(event.mimeData()):
            event.acceptProposedAction()
            self._set_hover(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._validator(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_hover(False)
        files = self._validator(event.mimeData())
        if files:
            self._on_drop(files)
            event.acceptProposedAction()
        else:
            event.ignore()


class BoxArtDelegate(QStyledItemDelegate):
    """Disegna ogni cella: box art centrata nel rettangolo, stellina preferito e titolo sotto."""

    def __init__(self, cache: ThumbnailCache, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cache = cache
        self.cell_width = 160
        self.accent_color = "#f58a1f"
        self.light_theme = False
        self.card_color = ""
        self.list_mode = False
        self.list_col_width = 280
        self.list_full_width = 600
        self.cover_radius = 6

    @property
    def art_height(self) -> int:
        return int(self.cell_width * 1.32)

    @property
    def list_thumb_size(self) -> tuple[int, int]:
        h = max(34, int(self.cell_width * 0.30))
        w = max(28, int(h * 0.82))
        return w, h

    @property
    def list_row_height(self) -> int:
        return self.list_thumb_size[1] + 12

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        if self.list_mode:
            if index.data(ROLE_SECTION):
                return QSize(max(240, self.list_full_width), 30)
            return QSize(max(160, self.list_col_width), self.list_row_height)
        return QSize(self.cell_width + 8, self.art_height + 42)

    def _paint_list_header(self, painter: QPainter, option: QStyleOptionViewItem, letter: str) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect: QRect = option.rect.adjusted(2, 4, -2, -2)
        f = QFont(option.font)
        f.setPointSize(12)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QColor(self.accent_color))
        fm = QFontMetrics(f)
        lbl_w = max(22, fm.horizontalAdvance(letter) + 8)
        painter.drawText(QRect(rect.left() + 4, rect.top(), lbl_w, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, letter)
        line_y = rect.center().y()
        line_x1 = rect.left() + lbl_w + 8
        line_x2 = rect.right() - 4
        if line_x2 > line_x1:
            line_col = QColor("#cbd5e1" if self.light_theme else "#2d3446")
            painter.setPen(QPen(line_col, 1))
            painter.drawLine(line_x1, line_y, line_x2, line_y)
        painter.restore()

    def _paint_list_row(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect: QRect = option.rect.adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.setBrush(QColor("#d5dde9" if self.light_theme else "#2f3647"))
            painter.setPen(QPen(QColor(self.accent_color), 2))
            painter.drawRoundedRect(rect, 7, 7)
        elif hovered:
            painter.setBrush(QColor("#e6ebf3" if self.light_theme else "#232733"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 7, 7)
        else:
            painter.setBrush(QColor("#f4f6fa" if self.light_theme else "#1a1e28"))
            painter.setPen(QPen(QColor("#dde3ee" if self.light_theme else "#262b38"), 1))
            painter.drawRoundedRect(rect, 7, 7)

        tw, th = self.list_thumb_size
        thumb_rect = QRect(rect.left() + 8, rect.top() + (rect.height() - th) // 2, tw, th)
        box_path: str = index.data(ROLE_BOXART) or ""
        title: str = index.data(Qt.DisplayRole) or ""
        meta: str = index.data(ROLE_META) or ""
        fav: bool = bool(index.data(ROLE_FAVORITE))

        pix = self.cache.get_or_request(box_path, thumb_rect.width(), thumb_rect.height())
        if pix is not None and not pix.isNull():
            dpr = pix.devicePixelRatio() or 1.0
            lw = pix.width() / dpr
            lh = pix.height() / dpr
            px = thumb_rect.left() + int((thumb_rect.width() - lw) // 2)
            py = thumb_rect.top() + int((thumb_rect.height() - lh) // 2)
            if self.cover_radius > 0:
                r_clip = min(float(self.cover_radius), lw * 0.45, lh * 0.45)
                clip_path = QPainterPath()
                clip_path.addRoundedRect(QRectF(px, py, lw, lh), r_clip, r_clip)
                painter.save()
                painter.setClipPath(clip_path)
                painter.drawPixmap(px, py, pix)
                painter.restore()
            else:
                painter.drawPixmap(px, py, pix)
        else:
            r_ph = max(2, min(self.cover_radius, 12)) if self.cover_radius > 0 else 0
            painter.setBrush(QColor("#e2e8f0" if self.light_theme else "#252b39"))
            painter.setPen(QPen(QColor("#cbd5e1" if self.light_theme else "#343c4e"), 1))
            painter.drawRoundedRect(thumb_rect, r_ph, r_ph)

        star_space = 28 if fav else 10
        text_left = thumb_rect.right() + 12
        text_w = max(40, rect.right() - text_left - star_space)

        title_col = QColor("#11141a" if selected else "#1a1d24") if self.light_theme else QColor("#ffffff" if selected else "#eef1f7")
        painter.setPen(title_col)
        f_title = QFont(option.font)
        f_title.setPointSize(10)
        f_title.setBold(True)
        painter.setFont(f_title)
        fm_t = QFontMetrics(f_title)
        if meta:
            block_top = rect.top() + max(4, (rect.height() - 40) // 2)
            t_rect = QRect(text_left, block_top, text_w, 22)
            painter.drawText(t_rect, Qt.AlignLeft | Qt.AlignVCenter, fm_t.elidedText(title, Qt.ElideRight, text_w))
            f_meta = QFont(option.font)
            f_meta.setPointSize(9)
            painter.setFont(f_meta)
            fm_m = QFontMetrics(f_meta)
            painter.setPen(QColor("#64748b" if self.light_theme else "#94a3b8"))
            m_rect = QRect(text_left, block_top + 22, text_w, 18)
            painter.drawText(m_rect, Qt.AlignLeft | Qt.AlignVCenter, fm_m.elidedText(meta, Qt.ElideRight, text_w))
        else:
            t_rect = QRect(text_left, rect.top() + 4, text_w, rect.height() - 8)
            painter.drawText(t_rect, Qt.AlignLeft | Qt.AlignVCenter, fm_t.elidedText(title, Qt.ElideRight, text_w))

        if fav:
            painter.setPen(QColor(self.accent_color))
            f_star = QFont(option.font)
            f_star.setPointSize(13)
            painter.setFont(f_star)
            painter.drawText(QRect(rect.right() - 28, rect.top(), 22, rect.height()), Qt.AlignCenter, "★")

        painter.restore()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if self.list_mode:
            sec = index.data(ROLE_SECTION)
            if sec:
                self._paint_list_header(painter, option, str(sec))
                return
            self._paint_list_row(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect: QRect = option.rect.adjusted(1, 1, -1, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.setBrush(QColor("#d5dde9" if self.light_theme else "#2f3647"))
            painter.setPen(QPen(QColor(self.accent_color), 2))
            painter.drawRoundedRect(rect, 7, 7)
        elif hovered:
            painter.setBrush(QColor("#e6ebf3" if self.light_theme else "#232733"))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 7, 7)

        art_rect = QRect(rect.left() + 3, rect.top() + 3, rect.width() - 6, self.art_height - 6)
        box_path: str = index.data(ROLE_BOXART) or ""
        title: str = index.data(Qt.DisplayRole) or ""
        fav: bool = bool(index.data(ROLE_FAVORITE))

        pix = self.cache.get_or_request(box_path, art_rect.width(), art_rect.height())
        if pix is not None and not pix.isNull():
            dpr = pix.devicePixelRatio() or 1.0
            lw = pix.width() / dpr
            lh = pix.height() / dpr
            x = art_rect.left() + int((art_rect.width() - lw) // 2)
            y = art_rect.top() + int((art_rect.height() - lh) // 2)
            if self.cover_radius > 0:
                r_clip = min(float(self.cover_radius), lw * 0.45, lh * 0.45)
                clip_path = QPainterPath()
                clip_path.addRoundedRect(QRectF(x, y, lw, lh), r_clip, r_clip)
                painter.save()
                painter.setClipPath(clip_path)
                painter.drawPixmap(x, y, pix)
                painter.restore()
            else:
                painter.drawPixmap(x, y, pix)
        else:
            card_bg = QColor(self.card_color) if (self.card_color and QColor(self.card_color).isValid()) else QColor("#e7ebf2" if self.light_theme else "#202430")
            painter.setBrush(card_bg)
            painter.setPen(QPen(QColor("#c8d0de" if self.light_theme else "#2c313e"), 1))
            r_ph = max(0, min(self.cover_radius, 24))
            painter.drawRoundedRect(art_rect, r_ph, r_ph)
            painter.setPen(QColor("#5b6475" if self.light_theme else "#8b92a3"))
            painter.drawText(art_rect.adjusted(8, 8, -8, -8), Qt.AlignCenter | Qt.TextWordWrap, title)

        text_rect = QRect(rect.left() + 4, rect.top() + self.art_height + 3, rect.width() - 8, 34)
        title_col = QColor("#11141a" if selected else "#1a1d24") if self.light_theme else QColor("#ffffff" if selected else "#e6e8ee")
        title_font = QFont("Segoe UI", 11, QFont.DemiBold if selected else QFont.Medium)
        painter.setFont(title_font)
        fm = QFontMetrics(title_font)
        if fav:
            star_txt = "★ "
            star_w = fm.horizontalAdvance(star_txt)
            elided = fm.elidedText(title, Qt.ElideRight, max(20, text_rect.width() - star_w))
            total_w = star_w + fm.horizontalAdvance(elided)
            start_x = text_rect.left() + max(0, (text_rect.width() - total_w) // 2)
            painter.setPen(QColor(self.accent_color))
            painter.drawText(QRect(start_x, text_rect.top(), star_w, text_rect.height()), Qt.AlignLeft | Qt.AlignTop, star_txt)
            painter.setPen(title_col)
            painter.drawText(QRect(start_x + star_w, text_rect.top(), text_rect.right() - (start_x + star_w) + 1, text_rect.height()), Qt.AlignLeft | Qt.AlignTop, elided)
        else:
            painter.setPen(title_col)
            elided = fm.elidedText(title, Qt.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, elided)

        painter.restore()


# --------------------------------------------------------------------------
# Finestra Principale
# --------------------------------------------------------------------------

class _TranslateSignals(QObject):
    done = Signal(int, str, str)  # game_id, orig, translated
    failed = Signal(int, str)     # game_id, error


class _TranslateTask(QRunnable):
    """Traduce la descrizione di un gioco nella lingua selezionata tramite Google Traduttore."""

    def __init__(self, game_id: int, text: str, signals: _TranslateSignals, target_lang: str = "it"):
        super().__init__()
        self.game_id = game_id
        self.text = text
        self.signals = signals
        self.target_lang = target_lang or "it"

    def run(self) -> None:
        import re
        import requests

        translated = ""
        last_err = ""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }

        # 1. Endpoint primario (clients5 dict-chrome-ex, supporta testi lunghi via POST)
        try:
            r = requests.post(
                "https://clients5.google.com/translate_a/t",
                params={"client": "dict-chrome-ex", "sl": "auto", "tl": self.target_lang},
                data={"q": self.text},
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                parts = []
                for item in data:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, list) and item and isinstance(item[0], str):
                        parts.append(item[0])
                translated = " ".join(p.strip() for p in parts if p.strip()).strip()
            elif isinstance(data, dict) and data.get("sentences"):
                translated = "".join(s.get("trans", "") for s in data["sentences"]).strip()
        except Exception as exc:
            last_err = str(exc)

        # 2. Fallback automatico su MyMemory (a blocchi <= 450 caratteri)
        if not translated:
            try:
                chunks: list[str] = []
                cur = ""
                for sent in re.split(r"(?<=[.!?])\s+", self.text.strip()):
                    if len(cur) + len(sent) + 1 <= 450:
                        cur = f"{cur} {sent}".strip() if cur else sent
                    else:
                        if cur:
                            chunks.append(cur)
                        while len(sent) > 450:
                            cut = sent.rfind(" ", 0, 450)
                            if cut <= 0:
                                cut = 450
                            chunks.append(sent[:cut].strip())
                            sent = sent[cut:].strip()
                        cur = sent
                if cur:
                    chunks.append(cur)

                out_parts: list[str] = []
                for ch in chunks:
                    rm = requests.get(
                        "https://api.mymemory.translated.net/get",
                        params={"q": ch, "langpair": f"en|{self.target_lang}"},
                        headers=headers,
                        timeout=10,
                    )
                    rm.raise_for_status()
                    t_part = ((rm.json().get("responseData") or {}).get("translatedText") or "").strip()
                    if t_part:
                        out_parts.append(t_part)
                translated = " ".join(out_parts).strip()
            except Exception as exc:
                last_err = str(exc)

        if translated:
            self.signals.done.emit(self.game_id, self.text, translated)
        else:
            self.signals.failed.emit(self.game_id, last_err or "Risposta vuota")


class _UnifiedTitleBar(QFrame):
    """Barra superiore unificata: icona + menu File/Strumenti/Interfaccia + controlli finestra."""

    def __init__(self, win: QMainWindow, root_dir: str):
        super().__init__(win)
        self._win = win
        self.setObjectName("UnifiedTitleBar")
        self.setFixedHeight(50)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 0, 0)
        lay.setSpacing(8)

        self.icon_lbl = QLabel()
        self.icon_lbl.setFixedSize(32, 32)
        self.icon_lbl.setStyleSheet("background: transparent; border: none;")
        icon_p = os.path.join(root_dir, "icons", "app_icon.png")
        if os.path.isfile(icon_p):
            dpr = max(float(win.devicePixelRatioF() or 1.0), 2.0)
            px_sz = round(32 * dpr)
            pm = QPixmap(icon_p).scaled(px_sz, px_sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
            self.icon_lbl.setPixmap(pm)
        lay.addWidget(self.icon_lbl, 0, Qt.AlignVCenter)

        self.menu_bar = QMenuBar(self)
        self.menu_bar.setStyleSheet(
            "QMenuBar { background: transparent; border: none; border-bottom: none; padding: 0px; }"
        )
        lay.addWidget(self.menu_bar, 0, Qt.AlignVCenter)

        self._tools_slot = QHBoxLayout()
        self._tools_slot.setContentsMargins(6, 0, 0, 0)
        self._tools_slot.setSpacing(6)
        lay.addLayout(self._tools_slot)

        self._drag_spacer = QWidget(self)
        self._drag_spacer.setStyleSheet("background: transparent; border: none;")
        self._drag_spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self._drag_spacer, 1)

        self._zoom_slot = QHBoxLayout()
        self._zoom_slot.setContentsMargins(0, 0, 0, 0)
        self._zoom_slot.setSpacing(6)
        lay.addLayout(self._zoom_slot)

        self._right_spacer = QWidget(self)
        self._right_spacer.setStyleSheet("background: transparent; border: none;")
        self._right_spacer.setFixedWidth(0)
        self._right_spacer.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        lay.addWidget(self._right_spacer)

        self.min_btn = QPushButton("─")
        self.max_btn = QPushButton("□")
        self.close_btn = QPushButton("✕")
        for btn in (self.min_btn, self.max_btn, self.close_btn):
            btn.setFixedSize(44, 50)
            btn.setFocusPolicy(Qt.NoFocus)
            lay.addWidget(btn)

        self.min_btn.clicked.connect(self._win.showMinimized)
        self.max_btn.clicked.connect(self._toggle_max)
        self.close_btn.clicked.connect(self._win.close)
        self.apply_theme(False)

    def _toggle_max(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()
        self.sync_max_icon()

    def sync_max_icon(self) -> None:
        self.max_btn.setText("❐" if self._win.isMaximized() else "□")

    def apply_theme(self, light: bool, border_col: str = "", bg_col: str = "") -> None:
        bg = bg_col if (bg_col and QColor(bg_col).isValid()) else ("#e9edf3" if light else "#1b1e25")
        if bg_col and QColor(bg_col).isValid():
            lum = 0.299 * QColor(bg).red() + 0.587 * QColor(bg).green() + 0.114 * QColor(bg).blue()
            fg = "#1a1d24" if lum > 140 else "#d8dce6"
            hov = QColor(bg).darker(112).name() if lum > 140 else QColor(bg).lighter(125).name()
        else:
            fg = "#1a1d24" if light else "#d8dce6"
            hov = "#d4dae6" if light else "#2f3647"
        self.setStyleSheet(
            f"QFrame#UnifiedTitleBar {{ background-color: {bg}; border: none; }}"
        )
        self.menu_bar.setStyleSheet(
            f"QMenuBar {{ background: transparent; border: none; border-bottom: none; padding: 0px; color: {fg}; }} "
            f"QMenuBar::item {{ color: {fg}; }} "
            f"QMenuBar::item:selected {{ background-color: {hov}; color: {fg}; }}"
        )
        btn_css = (
            f"QPushButton {{ background: transparent; border: none; border-radius: 0px; color: {fg}; font-size: 11pt; padding: 0px; }} "
            f"QPushButton:hover {{ background-color: {hov}; }}"
        )
        self.min_btn.setStyleSheet(btn_css)
        self.max_btn.setStyleSheet(btn_css)
        self.close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 0px; color: {fg}; font-size: 11pt; padding: 0px; }} "
            "QPushButton:hover { background-color: #e81123; color: #ffffff; }"
        )

    def set_right_align_width(self, right_col_width: int) -> None:
        # 3 pulsanti finestra da 44px = 132px
        w = max(0, int(right_col_width) - 132)
        self._right_spacer.setFixedWidth(w)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.position().toPoint())
            if child in (None, self._drag_spacer, self._right_spacer, self.icon_lbl):
                wh = self._win.windowHandle()
                if wh is not None:
                    wh.startSystemMove()
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.position().toPoint())
            if child in (None, self._drag_spacer, self._right_spacer, self.icon_lbl):
                self._toggle_max()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


class _FramelessResizeFilter(QObject):
    """Abilita il ridimensionamento sui bordi della finestra principale frameless."""

    MARGIN = 5

    def __init__(self, win: QMainWindow):
        super().__init__(win)
        self._win = win

    def _edges_at(self, global_pos: QPoint) -> Qt.Edges:
        if self._win.isMaximized():
            return Qt.Edges()
        r = self._win.frameGeometry()
        m = self.MARGIN
        edges = Qt.Edges()
        if abs(global_pos.x() - r.left()) <= m:
            edges |= Qt.LeftEdge
        elif abs(global_pos.x() - r.right()) <= m:
            edges |= Qt.RightEdge
        if abs(global_pos.y() - r.top()) <= m:
            edges |= Qt.TopEdge
        elif abs(global_pos.y() - r.bottom()) <= m:
            edges |= Qt.BottomEdge
        return edges

    def eventFilter(self, obj, event) -> bool:
        if not self._win.isVisible() or self._win.isMinimized():
            return False
        t = event.type()
        if t in (QEvent.MouseMove, QEvent.MouseButtonPress) and isinstance(obj, QWidget):
            if obj.window() is not self._win:
                return False
            gpos = event.globalPosition().toPoint()
            edges = self._edges_at(gpos)
            if t == QEvent.MouseMove:
                if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
                    self._win.setCursor(Qt.SizeFDiagCursor)
                elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
                    self._win.setCursor(Qt.SizeBDiagCursor)
                elif edges in (Qt.LeftEdge, Qt.RightEdge):
                    self._win.setCursor(Qt.SizeHorCursor)
                elif edges in (Qt.TopEdge, Qt.BottomEdge):
                    self._win.setCursor(Qt.SizeVerCursor)
                else:
                    self._win.unsetCursor()
            elif t == QEvent.MouseButtonPress and event.button() == Qt.LeftButton and edges:
                wh = self._win.windowHandle()
                if wh is not None:
                    wh.startSystemResize(edges)
                    return True
        return False


class MainWindow(QMainWindow):
    def __init__(self, db: Database, config: Config):
        super().__init__()
        self.db = db
        self.config = config
        set_lang(self.config.get("ui_language", self.config.get("metadata_language", "en")))
        self.setProperty("app_root", db.root)
        self.setWindowTitle("EasyLauncher")
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint | Qt.WindowSystemMenuHint
        )

        self.current_platform_id: Optional[int] = None
        self.current_game_id: Optional[int] = None
        self._banner_source: Optional[QPixmap] = None
        self._preview_source: Optional[QPixmap] = None
        self._preview_shots: list[QPixmap] = []
        self._preview_shot_idx: int = 0
        self._dynamic_bg_path: str = ""
        self._dynamic_bg_radius: int = 0
        self._dynamic_bg_pixmap: Optional[QPixmap] = None
        self._dynamic_blur_cache: OrderedDict[tuple[str, int], QPixmap] = OrderedDict()
        self._dynamic_blur_timer = QTimer(self)
        self._dynamic_blur_timer.setSingleShot(True)
        self._dynamic_blur_timer.setInterval(45)
        self._dynamic_blur_timer.timeout.connect(self._refresh_dynamic_blur)
        self._bg_scrape_worker = None
        self._bg_scrape_last: tuple[int, int, str] = (0, 0, "")

        self.thumb_cache = ThumbnailCache()
        self.thumb_cache.signals.loaded.connect(self._on_thumb_loaded)

        self.model = GameListModel(self.db)
        self.delegate = BoxArtDelegate(self.thumb_cache, self)
        self._translate_signals = _TranslateSignals()
        self._translate_signals.done.connect(self._on_desc_translated)
        self._translate_signals.failed.connect(self._on_desc_translate_failed)
        self._translated_cache: dict[int, tuple[str, str]] = {}
        self._showing_translated = False
        self._active_sessions: list[tuple[int, subprocess.Popen, float]] = []
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(1000)
        self._session_timer.timeout.connect(self._poll_active_sessions)

        self._build_menu()
        self._build_ui()
        self._restore_window_state()
        self._apply_appearance()

        self.reload_platforms()
        self.reload_games()

        app = QApplication.instance()
        if app is not None:
            self._resize_filter = _FramelessResizeFilter(self)
            app.installEventFilter(self._resize_filter)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and hasattr(self, "_title_bar"):
            self._title_bar.sync_max_icon()

    # ----- Menu superiore -----
    def _build_menu(self) -> None:
        self._title_bar = _UnifiedTitleBar(self, self.db.root)
        mb = self._title_bar.menu_bar

        self.file_m = mb.addMenu(t("menu_file"))
        self.act_new_plat = QAction(t("act_new_plat"), self)
        self.act_new_plat.triggered.connect(self._quick_new_platform)
        self.act_add_rom = QAction(t("act_add_rom"), self)
        self.act_add_rom.triggered.connect(self._add_roms_manually)
        self.act_scan_all = QAction(t("act_scan_all"), self)
        self.act_scan_all.triggered.connect(self._scan_all_platforms)
        self.act_quit = QAction(t("act_quit"), self, shortcut=QKeySequence.Quit)
        self.act_quit.triggered.connect(self.close)
        self.file_m.addActions([self.act_new_plat, self.act_add_rom, self.act_scan_all])
        self.file_m.addSeparator()
        acc = self.config.get("accent_color", "#f58a1f") or "#f58a1f"
        self.act_bigscreen = QWidgetAction(self)
        self.act_bigscreen.setShortcut(QKeySequence("F11"))
        self.act_bigscreen.triggered.connect(self._open_bigscreen)
        bs_btn = QPushButton(t("act_bigscreen_menu"))
        self._bs_menu_btn = bs_btn
        bs_btn.setIcon(make_white_gamepad_icon(20))
        bs_btn.setIconSize(QSize(20, 20))
        bs_btn.setCursor(Qt.PointingHandCursor)
        bs_btn.setStyleSheet(
            "QPushButton { text-align: left; color: #eef1f7; font-size: 13px; "
            "background: transparent; border: none; border-radius: 4px; padding: 6px 14px 6px 10px; } "
            f"QPushButton:hover {{ background-color: {acc}; color: #10131a; }}"
        )
        bs_btn.clicked.connect(lambda: (self.file_m.close(), self._open_bigscreen()))
        self.act_bigscreen.setDefaultWidget(bs_btn)
        self.file_m.addAction(self.act_bigscreen)

        self.act_start_fullscreen = QWidgetAction(self)
        sf_wrap = QWidget()
        sf_wrap.setStyleSheet(
            "QWidget { background: transparent; border-radius: 4px; } "
            "QWidget:hover { background-color: rgba(245, 138, 31, 45); }"
        )
        sf_lay = QHBoxLayout(sf_wrap)
        sf_lay.setContentsMargins(13, 6, 14, 6)
        sf_lay.setSpacing(8)
        self.start_fullscreen_cb = QCheckBox(t("act_start_fullscreen"))
        self.start_fullscreen_cb.setCursor(Qt.PointingHandCursor)
        self.start_fullscreen_cb.setChecked(bool(self.config.get("start_fullscreen", False)))
        self.start_fullscreen_cb.setStyleSheet(
            "QCheckBox { color: #eef1f7; font-size: 13px; spacing: 11px; background: transparent; } "
            "QCheckBox::indicator { width: 14px; height: 14px; border-radius: 3px; "
            "border: 2px solid #94a3b8; background-color: #141822; } "
            f"QCheckBox::indicator:hover {{ border-color: {acc}; }} "
            f"QCheckBox::indicator:checked {{ background-color: {acc}; border-color: {acc}; }}"
        )
        self.start_fullscreen_cb.toggled.connect(self._toggle_start_fullscreen)
        sf_lay.addWidget(self.start_fullscreen_cb)
        self.act_start_fullscreen.setDefaultWidget(sf_wrap)
        self.file_m.addAction(self.act_start_fullscreen)
        self.file_m.addSeparator()
        self.file_m.addAction(self.act_quit)

        self.tools_m = mb.addMenu(t("menu_tools"))
        self.act_plats = QAction(t("act_plats"), self)
        self.act_plats.triggered.connect(self._open_platforms)
        self.act_emus = QAction(t("act_emus"), self)
        self.act_emus.triggered.connect(self._open_emulators)
        self.act_scrape = QAction(t("act_scrape"), self)
        self.act_scrape.triggered.connect(self._open_batch_scrape)
        self.act_backup = QAction(t("act_backup"), self)
        self.act_backup.triggered.connect(self._open_backup_restore)
        self.tools_m.addActions([self.act_plats, self.act_emus, self.act_scrape])
        self.tools_m.addSeparator()
        self.tools_m.addAction(self.act_backup)

        self.iface_m = mb.addMenu(t("menu_interface"))
        self.lang_m = self.iface_m.addMenu(t("menu_select_language"))
        self._lang_actions: dict[str, QAction] = {}
        cur_lang = get_lang()
        for code, label in LANGUAGES:
            act = QAction(label, self, checkable=True)
            act.setChecked(code == cur_lang)
            act.triggered.connect(lambda _chk=False, c=code: self._set_ui_language(c))
            self.lang_m.addAction(act)
            self._lang_actions[code] = act
        self.iface_m.addSeparator()
        self.act_appearance = QAction(t("act_appearance"), self)
        self.act_appearance.triggered.connect(self._open_appearance)
        self.iface_m.addAction(self.act_appearance)
        self.act_corner_radius = QAction(t("act_corner_radius"), self)
        self.act_corner_radius.triggered.connect(self._open_corner_radius_dialog)
        self.iface_m.addAction(self.act_corner_radius)
        self.act_lock_layout = QAction(t("act_lock_layout"), self)
        self.act_lock_layout.triggered.connect(self._open_lock_layout)
        self.iface_m.addAction(self.act_lock_layout)

        self.info_m = mb.addMenu(t("menu_info"))
        self.act_guide = QAction(t("act_guide"), self)
        self.act_guide.triggered.connect(self._open_user_guide)
        self.info_m.addAction(self.act_guide)
        self.act_version = QAction(t("act_version"), self)
        self.act_version.triggered.connect(self._open_about_dialog)
        self.info_m.addAction(self.act_version)

    def _set_ui_language(self, lang: str) -> None:
        code = set_lang(lang)
        self.config.data["ui_language"] = code
        self.config.data["metadata_language"] = code
        self.config.save()
        self._translated_cache.clear()
        self._showing_translated = False
        for c, act in self._lang_actions.items():
            act.setChecked(c == code)
        self._retranslate_ui()
        self.reload_platforms()
        self.reload_games()
        lang_name = dict(LANGUAGES).get(code, code.upper())
        self.statusBar().showMessage(t("lang_changed_status", name=lang_name), 4000)

    def _retranslate_ui(self) -> None:
        self.file_m.setTitle(t("menu_file"))
        self.act_new_plat.setText(t("act_new_plat"))
        self.act_add_rom.setText(t("act_add_rom"))
        self.act_scan_all.setText(t("act_scan_all"))
        self.act_quit.setText(t("act_quit"))

        self.tools_m.setTitle(t("menu_tools"))
        self.act_plats.setText(t("act_plats"))
        self.act_emus.setText(t("act_emus"))
        self.act_scrape.setText(t("act_scrape"))
        self.act_backup.setText(t("act_backup"))

        self.iface_m.setTitle(t("menu_interface"))
        self.lang_m.setTitle(t("menu_select_language"))
        if hasattr(self, "act_appearance"):
            self.act_appearance.setText(t("act_appearance"))
        if hasattr(self, "act_corner_radius"):
            self.act_corner_radius.setText(t("act_corner_radius"))
        if hasattr(self, "act_lock_layout"):
            self.act_lock_layout.setText(t("act_lock_layout"))
        if hasattr(self, "info_m"):
            self.info_m.setTitle(t("menu_info"))
        if hasattr(self, "act_guide"):
            self.act_guide.setText(t("act_guide"))
        if hasattr(self, "act_version"):
            self.act_version.setText(t("act_version"))
        if hasattr(self, "_bs_menu_btn"):
            self._bs_menu_btn.setText(t("act_bigscreen_menu"))
        if hasattr(self, "start_fullscreen_cb"):
            self.start_fullscreen_cb.setText(t("act_start_fullscreen"))
        if hasattr(self, "bigscreen_btn"):
            self.bigscreen_btn.setToolTip(t("bigscreen_btn_tip"))
        if hasattr(self, "list_view_btn"):
            self.list_view_btn.setText(t("view_list_btn"))
            self.list_view_btn.setToolTip(t("view_list_tip"))
        if hasattr(self, "grid_view_btn"):
            self.grid_view_btn.setText(t("view_grid_btn"))
            self.grid_view_btn.setToolTip(t("view_grid_tip"))
        if hasattr(self, "plat_sort_alpha_btn"):
            self.plat_sort_alpha_btn.setText(t("plat_sort_alpha"))
        if hasattr(self, "plat_sort_custom_btn"):
            self.plat_sort_custom_btn.setText(t("plat_sort_custom"))

        if hasattr(self, "search_ed"):
            self.search_ed.setPlaceholderText(t("search_placeholder"))
        if hasattr(self, "fav_filter"):
            self.fav_filter.setText(t("fav_only"))
        if hasattr(self, "sort_cb"):
            prefix = t("sort_label")
            self.sort_cb.setItemText(0, f"{prefix} {t('sort_title')}")
            self.sort_cb.setItemText(1, f"{prefix} {t('sort_year')}")
            self.sort_cb.setItemText(2, f"{prefix} {t('lbl_developer').rstrip(':：')}")
            self.sort_cb.setItemText(3, f"{prefix} {t('sort_recent')}")
            self.sort_cb.setItemText(4, f"{prefix} {t('sort_rating')}")
        if hasattr(self, "btn_new_plat"):
            self.btn_new_plat.setToolTip(t("btn_new_plat_tip"))
        if hasattr(self, "btn_scan"):
            self.btn_scan.setText(t("btn_sync"))
            self.btn_scan.setToolTip(t("btn_sync_tip"))
        if hasattr(self, "zoom_lbl"):
            self.zoom_lbl.setText(t("zoom_label"))
        if hasattr(self, "play_btn"):
            self.play_btn.setText(t("btn_play"))
        if hasattr(self, "fav_btn"):
            if self.current_game_id is not None:
                cur_g = self.db.get_game(self.current_game_id)
                self.fav_btn.setText(t("btn_fav_remove") if (cur_g and cur_g["favorite"]) else t("btn_fav"))
            else:
                self.fav_btn.setText(t("btn_fav"))
        if hasattr(self, "edit_btn"):
            self.edit_btn.setText(t("btn_edit_meta"))
        if hasattr(self, "folder_btn"):
            self.folder_btn.setToolTip(t("btn_open_folder_tip"))
        if hasattr(self, "_meta_key_labels"):
            key_map = {
                "platform": "lbl_platform",
                "genre": "lbl_genre",
                "year": "lbl_year",
                "developer": "lbl_developer",
                "publisher": "lbl_publisher",
                "players": "lbl_players",
                "rating": "lbl_rating",
                "plays": "lbl_plays",
                "play_time": "lbl_playtime",
            }
            for k, lbl in self._meta_key_labels.items():
                if k in key_map:
                    txt = t(key_map[k])
                    lbl.setText(txt.rstrip(":：") if k in ("plays", "play_time") else txt)
        if hasattr(self, "left_tab_plat_btn"):
            self.left_tab_plat_btn.setText(t("tab_platforms"))
        if hasattr(self, "left_tab_fav_btn"):
            self.left_tab_fav_btn.setText(f"{t('tab_favorites')} ★")
        if hasattr(self, "fav_new_btn"):
            self.fav_new_btn.setText(t("fav_new_folder"))
        if hasattr(self, "fav_tree"):
            self.reload_favorites()
        if hasattr(self, "detail_tab_details_btn"):
            self.detail_tab_details_btn.setText(t("lbl_details_hdr").title())
        if hasattr(self, "detail_tab_desc_btn"):
            self.detail_tab_desc_btn.setText(t("lbl_desc"))
        if hasattr(self, "desc_hdr"):
            self.desc_hdr.setText(t("lbl_desc").upper())
        if hasattr(self, "translate_btn"):
            self.translate_btn.setText(t("btn_original") if self._showing_translated else t("btn_translate"))
            self.translate_btn.setToolTip(t("btn_translate_tip"))
        self.statusBar().showMessage(t("portable_status", root=self.db.root))

    # ----- Costruzione interfaccia a 3 colonne -----
    def _build_ui(self) -> None:
        root_w = QWidget()
        self.setCentralWidget(root_w)
        root_lay = QVBoxLayout(root_w)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)
        root_lay.addWidget(self._title_bar)

        # Popola ricerca, ordinamento e zoom nella barra superiore unificata
        self.search_ed = QLineEdit()
        self.search_ed.setPlaceholderText(t("search_placeholder"))
        self.search_ed.setClearButtonEnabled(True)
        self.search_ed.setFixedHeight(34)
        self.search_ed.setMinimumWidth(200)
        self.search_ed.setMaximumWidth(280)
        self.search_ed.textChanged.connect(lambda _: self.reload_games())
        self._title_bar._tools_slot.addWidget(self.search_ed)

        self.sort_cb = QComboBox()
        self.sort_cb.setFixedHeight(34)
        self.sort_cb.setMinimumWidth(135)
        self.sort_cb.setMaximumWidth(185)
        self.sort_cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.sort_cb.setMinimumContentsLength(8)
        self.sort_cb.addItem("Ordina: Titolo", "title")
        self.sort_cb.addItem("Ordina: Anno", "year")
        self.sort_cb.addItem("Ordina: Sviluppatore", "developer")
        self.sort_cb.addItem("Ordina: Più giocati", "played")
        self.sort_cb.addItem("Ordina: Aggiunti di recente", "added")
        self.sort_cb.currentIndexChanged.connect(lambda _: self.reload_games())
        self._title_bar._tools_slot.addWidget(self.sort_cb)

        self.count_lbl = QLabel(t("games_count", n=0))
        self.count_lbl.setObjectName("MutedHint")
        self.count_lbl.setStyleSheet("background: transparent; padding-left: 6px;")
        self._title_bar._tools_slot.addWidget(self.count_lbl)

        self.zoom_lbl = QLabel(t("zoom_label"))
        self.zoom_lbl.setStyleSheet("background: transparent;")
        self._title_bar._zoom_slot.addWidget(self.zoom_lbl)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(100, 360)
        self.zoom_slider.setFixedWidth(140)
        self.zoom_slider.setStyleSheet("background: transparent;")
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        self.zoom_slider.sliderReleased.connect(self._save_zoom)
        self._title_bar._zoom_slot.addWidget(self.zoom_slider)

        view_btn_qss = (
            "QPushButton { padding: 2px 10px; font-size: 12px; font-weight: 600; "
            "border-radius: 6px; border: 1px solid rgba(255, 255, 255, 0.16); }"
            "QPushButton:hover { border: 1px solid rgba(255, 255, 255, 0.35); }"
            "QPushButton:checked { border: 2px solid #f58a1f; "
            "background-color: rgba(245, 138, 31, 0.14); }"
        )
        self.list_view_btn = QPushButton(t("view_list_btn"))
        self.list_view_btn.setCheckable(True)
        self.list_view_btn.setFixedHeight(28)
        self.list_view_btn.setCursor(Qt.PointingHandCursor)
        self.list_view_btn.setToolTip(t("view_list_tip"))
        self.list_view_btn.setStyleSheet(view_btn_qss)
        self.list_view_btn.clicked.connect(lambda: self._set_view_mode("list"))

        self.grid_view_btn = QPushButton(t("view_grid_btn"))
        self.grid_view_btn.setCheckable(True)
        self.grid_view_btn.setFixedHeight(28)
        self.grid_view_btn.setCursor(Qt.PointingHandCursor)
        self.grid_view_btn.setToolTip(t("view_grid_tip"))
        self.grid_view_btn.setStyleSheet(view_btn_qss)
        self.grid_view_btn.clicked.connect(lambda: self._set_view_mode("grid"))

        self._title_bar._zoom_slot.addSpacing(8)
        self._title_bar._zoom_slot.addWidget(self.list_view_btn)
        self._title_bar._zoom_slot.addSpacing(4)
        self._title_bar._zoom_slot.addWidget(self.grid_view_btn)

        self.bigscreen_btn = QPushButton("")
        self.bigscreen_btn.setIcon(make_white_gamepad_icon(26))
        self.bigscreen_btn.setIconSize(QSize(26, 26))
        self.bigscreen_btn.setFixedSize(38, 30)
        self.bigscreen_btn.setCursor(Qt.PointingHandCursor)
        self.bigscreen_btn.setToolTip(t("bigscreen_btn_tip"))
        self.bigscreen_btn.setStyleSheet("padding: 0px;")
        self.bigscreen_btn.clicked.connect(self._open_bigscreen)
        self._title_bar._zoom_slot.addSpacing(8)
        self._title_bar._zoom_slot.addWidget(self.bigscreen_btn)

        # Splitter orizzontale a 3 colonne
        self.main_splitter = _GripSplitter(Qt.Horizontal)

        # 1) Colonna Sinistra: Schede Piattaforme / Preferiti
        self.current_fav_folder_id: Optional[int] = None
        self.left_w = _BgSectionWidget()
        self.left_w.setObjectName("LeftPanel")
        left_lay = QVBoxLayout(self.left_w)
        left_lay.setContentsMargins(0, 8, 0, 0)
        left_lay.setSpacing(6)

        left_tab_row = QHBoxLayout()
        left_tab_row.setContentsMargins(8, 2, 8, 2)
        left_tab_row.setSpacing(8)
        self.left_tab_plat_btn = QPushButton(t("tab_platforms"))
        self.left_tab_plat_btn.setObjectName("LeftTabBtn")
        self.left_tab_plat_btn.setCheckable(True)
        self.left_tab_plat_btn.setChecked(True)
        self.left_tab_plat_btn.setCursor(Qt.PointingHandCursor)
        self.left_tab_fav_btn = QPushButton(f"{t('tab_favorites')} ★")
        self.left_tab_fav_btn.setObjectName("LeftTabBtn")
        self.left_tab_fav_btn.setCheckable(True)
        self.left_tab_fav_btn.setCursor(Qt.PointingHandCursor)
        self._left_tab_group = QButtonGroup(self)
        self._left_tab_group.setExclusive(True)
        self._left_tab_group.addButton(self.left_tab_plat_btn, 0)
        self._left_tab_group.addButton(self.left_tab_fav_btn, 1)
        left_tab_row.addWidget(self.left_tab_plat_btn, 1)
        left_tab_row.addWidget(self.left_tab_fav_btn, 1)
        left_lay.addLayout(left_tab_row)

        self.left_stack = QStackedWidget()
        self.left_stack.setObjectName("LeftStack")
        self.left_stack.setAutoFillBackground(False)

        self.tree = _PlatformTree()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(26)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setIconSize(QSize(36, 36))
        self.tree_delegate = PlatformTreeDelegate(self.tree)
        self.tree.setItemDelegate(self.tree_delegate)
        self.tree.setMouseTracking(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.currentItemChanged.connect(self._on_tree_selected)
        self.tree.customContextMenuRequested.connect(self._on_tree_context)
        self.tree.platforms_reordered.connect(self._on_platforms_reordered)

        plat_page = QWidget()
        plat_page.setObjectName("LeftPlatPage")
        plat_lay = QVBoxLayout(plat_page)
        plat_lay.setContentsMargins(0, 0, 0, 0)
        plat_lay.setSpacing(4)
        plat_lay.addWidget(self.tree, 1)

        plat_sort_row = QHBoxLayout()
        plat_sort_row.setContentsMargins(8, 2, 8, 8)
        plat_sort_row.setSpacing(6)
        sort_btn_qss = (
            "QPushButton { padding: 4px 6px; font-size: 11px; font-weight: 600; "
            "border-radius: 6px; border: 1px solid rgba(255, 255, 255, 0.16); }"
            "QPushButton:hover { border: 1px solid rgba(255, 255, 255, 0.35); }"
            "QPushButton:checked { border: 2px solid #f58a1f; "
            "background-color: rgba(245, 138, 31, 0.14); }"
        )
        self.plat_sort_alpha_btn = QPushButton(t("plat_sort_alpha"))
        self.plat_sort_alpha_btn.setCheckable(True)
        self.plat_sort_alpha_btn.setCursor(Qt.PointingHandCursor)
        self.plat_sort_alpha_btn.setStyleSheet(sort_btn_qss)
        self.plat_sort_custom_btn = QPushButton(t("plat_sort_custom"))
        self.plat_sort_custom_btn.setCheckable(True)
        self.plat_sort_custom_btn.setCursor(Qt.PointingHandCursor)
        self.plat_sort_custom_btn.setStyleSheet(sort_btn_qss)
        self._plat_sort_group = QButtonGroup(self)
        self._plat_sort_group.setExclusive(True)
        self._plat_sort_group.addButton(self.plat_sort_alpha_btn, 0)
        self._plat_sort_group.addButton(self.plat_sort_custom_btn, 1)
        self.plat_sort_alpha_btn.clicked.connect(lambda: self._set_platform_sort_mode("alpha"))
        self.plat_sort_custom_btn.clicked.connect(lambda: self._set_platform_sort_mode("custom"))
        plat_sort_row.addWidget(self.plat_sort_alpha_btn, 1)
        plat_sort_row.addWidget(self.plat_sort_custom_btn, 1)
        plat_lay.addLayout(plat_sort_row)

        self.left_stack.addWidget(plat_page)
        self._sync_platform_sort_ui()

        fav_page = QWidget()
        fav_page.setObjectName("LeftFavPage")
        fav_lay = QVBoxLayout(fav_page)
        fav_lay.setContentsMargins(6, 8, 6, 4)
        fav_lay.setSpacing(6)
        self.fav_new_btn = QPushButton(t("fav_new_folder"))
        self.fav_new_btn.setStyleSheet("padding: 5px 8px;")
        self.fav_new_btn.clicked.connect(self._create_favorite_folder)
        fav_lay.addWidget(self.fav_new_btn)

        self.fav_tree = QTreeWidget()
        self.fav_tree.setHeaderHidden(True)
        self.fav_tree.setIndentation(16)
        self.fav_tree.setIconSize(QSize(36, 36))
        self.fav_tree.setItemDelegate(self.tree_delegate)
        self.fav_tree.setMouseTracking(True)
        self.fav_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.fav_tree.currentItemChanged.connect(self._on_fav_tree_selected)
        self.fav_tree.customContextMenuRequested.connect(self._on_fav_tree_context)
        fav_lay.addWidget(self.fav_tree, 1)
        self.left_stack.addWidget(fav_page)

        def _on_left_tab_clicked(idx: int) -> None:
            self.left_stack.setCurrentIndex(idx)
            self.reload_games()

        self._left_tab_group.idClicked.connect(_on_left_tab_clicked)
        left_lay.addWidget(self.left_stack, 1)
        self.main_splitter.addWidget(self.left_w)

        # 2) Colonna Centrale: Griglia Copertine + barra inferiore con Zoom
        self.center_w = _BgSectionWidget()
        center_lay = QVBoxLayout(self.center_w)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        self.header_bar = QWidget()
        self.header_bar.setObjectName("CenterHeaderBar")
        header_lay = QHBoxLayout(self.header_bar)
        header_lay.setContentsMargins(12, 8, 12, 6)
        header_lay.setSpacing(12)
        self.center_header = QLabel("")
        self.center_header.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.center_header.setVisible(False)
        self.center_title_lbl = QLabel("Tutte le Piattaforme")
        self.center_title_lbl.setObjectName("HeaderTitle")
        self.center_title_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header_lay.addWidget(self.center_header, 0, Qt.AlignLeft | Qt.AlignVCenter)
        header_lay.addStretch(1)
        header_lay.addWidget(self.center_title_lbl, 0, Qt.AlignRight | Qt.AlignVCenter)
        center_lay.addWidget(self.header_bar)

        self.alpha_bar = QWidget()
        self.alpha_bar.setObjectName("AlphaBar")
        self.alpha_bar.setStyleSheet("QWidget#AlphaBar { background: transparent; }")
        alpha_wrap_lay = QHBoxLayout(self.alpha_bar)
        alpha_wrap_lay.setContentsMargins(8, 2, 8, 4)
        alpha_wrap_lay.setSpacing(0)

        alpha_pill = QFrame()
        alpha_pill.setObjectName("AlphaPill")
        alpha_pill.setAttribute(Qt.WA_StyledBackground, True)
        alpha_pill.setStyleSheet(
            "QFrame#AlphaPill { background-color: #1e2330; "
            "border: 1px solid #2d3446; border-radius: 8px; }"
        )
        alpha_lay = QHBoxLayout(alpha_pill)
        alpha_lay.setContentsMargins(6, 3, 6, 3)
        alpha_lay.setSpacing(3)
        self._alpha_buttons: dict[str, QPushButton] = {}
        alpha_btn_qss = (
            "QPushButton { padding: 0px; font-size: 14px; font-weight: 700; "
            "border-radius: 5px; border: 1px solid transparent; background: transparent; }"
            "QPushButton:hover:enabled { border: 1px solid #f58a1f; "
            "background-color: rgba(245, 138, 31, 0.16); color: #f58a1f; }"
            "QPushButton:pressed:enabled { background-color: rgba(245, 138, 31, 0.30); }"
            "QPushButton:disabled { color: rgba(148, 163, 184, 0.28); }"
        )
        for ch in ["#"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]:
            btn = QPushButton(ch)
            btn.setFixedHeight(28)
            btn.setMinimumWidth(20)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(alpha_btn_qss)
            btn.clicked.connect(lambda _checked=False, letter=ch: self._jump_to_letter(letter))
            alpha_lay.addWidget(btn, 1)
            self._alpha_buttons[ch] = btn
        alpha_wrap_lay.addWidget(alpha_pill)
        center_lay.addWidget(self.alpha_bar)

        self.setAcceptDrops(True)
        self.grid = _RomDropGrid(self._valid_dropped_roms, self._on_roms_dropped)
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(6)
        self.grid.setMouseTracking(True)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(self.delegate)
        self.grid.selectionModel().currentChanged.connect(self._on_grid_selected)
        self.grid.doubleClicked.connect(lambda _: self._launch_selected())
        self.grid.activated.connect(lambda _: self._launch_selected())
        self.grid.customContextMenuRequested.connect(self._on_grid_context)
        center_lay.addWidget(self.grid, 1)
        self._set_view_mode(self.config.get("view_mode", "list"))

        self.main_splitter.addWidget(self.center_w)

        # 3) Colonna Destra: Splitter verticale (Locandina/Logo + Gameplay sopra, Dettagli scorrevoli sotto)
        self.right_w = _BgSectionWidget()
        right_lay = QVBoxLayout(self.right_w)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)
        self.right_splitter = _GripSplitter(Qt.Vertical)
        self.right_splitter.setObjectName("RightSplitter")
        right_lay.addWidget(self.right_splitter)

        self.preview_box = QWidget()
        self.preview_box.setObjectName("PreviewBox")
        self.preview_box.setStyleSheet("QWidget#PreviewBox { background-color: #101217; }")
        pv_lay = QVBoxLayout(self.preview_box)
        pv_lay.setContentsMargins(8, 8, 8, 8)
        pv_lay.setSpacing(0)

        self.preview_splitter = _GripSplitter(Qt.Vertical)
        self.banner_lbl = QLabel("")
        self.banner_lbl.setAlignment(Qt.AlignCenter)
        self.banner_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.banner_lbl.setMinimumHeight(60)
        self.banner_lbl.setVisible(False)
        self.preview_splitter.addWidget(self.banner_lbl)

        self.preview_lbl = QLabel("Nessun gioco selezionato")
        self.preview_lbl.setAlignment(Qt.AlignCenter)
        self.preview_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.preview_lbl.setMinimumHeight(80)
        self.preview_splitter.addWidget(self.preview_lbl)

        _nav_css = (
            "QPushButton { background-color: rgba(18, 21, 28, 185); color: #eef1f7; "
            "border: 1px solid #3c4354; border-radius: 14px; font-weight: bold; padding: 0px; } "
            "QPushButton:hover { background-color: rgba(245, 138, 31, 220); color: #111318; border-color: #f58a1f; }"
        )
        self.shot_prev_btn = QPushButton("◀", self.preview_lbl)
        self.shot_prev_btn.setFixedSize(28, 28)
        self.shot_prev_btn.setCursor(Qt.PointingHandCursor)
        self.shot_prev_btn.setStyleSheet(_nav_css)
        self.shot_prev_btn.clicked.connect(lambda: self._step_preview_shot(-1))
        self.shot_prev_btn.setVisible(False)

        self.shot_next_btn = QPushButton("▶", self.preview_lbl)
        self.shot_next_btn.setFixedSize(28, 28)
        self.shot_next_btn.setCursor(Qt.PointingHandCursor)
        self.shot_next_btn.setStyleSheet(_nav_css)
        self.shot_next_btn.clicked.connect(lambda: self._step_preview_shot(1))
        self.shot_next_btn.setVisible(False)

        self.shot_idx_lbl = QLabel("", self.preview_lbl)
        self.shot_idx_lbl.setAlignment(Qt.AlignCenter)
        self.shot_idx_lbl.setStyleSheet(
            "background-color: rgba(18, 21, 28, 185); color: #eef1f7; "
            "border: 1px solid #3c4354; border-radius: 9px; font-size: 11px; padding: 1px 8px;"
        )
        self.shot_idx_lbl.setVisible(False)

        self.shot_add_btn = _CirclePlusButton(self.preview_lbl)
        self.shot_add_btn.setToolTip("Aggiungi / gestisci foto gameplay extra")
        self.shot_add_btn.clicked.connect(self._open_extra_screenshots_direct)
        self.shot_add_btn.setVisible(False)

        self.preview_splitter.setStretchFactor(0, 2)
        self.preview_splitter.setStretchFactor(1, 3)
        self.preview_splitter.setSizes([140, 220])
        self.preview_splitter.splitterMoved.connect(self._on_preview_splitter_moved)
        pv_lay.addWidget(self.preview_splitter, 1)

        self.right_splitter.addWidget(self.preview_box)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.detail_inner = QWidget()
        self.detail_inner.setObjectName("DetailInner")
        dl = QVBoxLayout(self.detail_inner)
        dl.setContentsMargins(12, 8, 12, 8)
        dl.setSpacing(6)

        self.d_title = QLabel("Seleziona un gioco")
        self.d_title.setObjectName("DetailGameTitle")
        self.d_title.setWordWrap(True)
        dl.addWidget(self.d_title)

        self.play_btn = QPushButton("▶   AVVIA GIOCO")
        self.play_btn.setObjectName("PlayButton")
        self.play_btn.setMinimumHeight(36)
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._launch_selected)
        dl.addWidget(self.play_btn)

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self.fav_btn = QPushButton("☆ Preferito")
        self.fav_btn.setObjectName("DetailActionBtn")
        self.fav_btn.setEnabled(False)
        self.fav_btn.clicked.connect(self._toggle_favorite)
        self.edit_btn = QPushButton("✎ Modifica / Metadati")
        self.edit_btn.setObjectName("DetailActionBtn")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._edit_selected)
        self.folder_btn = QPushButton()
        self.folder_btn.setObjectName("DetailActionBtn")
        self.folder_btn.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.folder_btn.setFixedWidth(36)
        self.folder_btn.setToolTip("Apri cartella del gioco")
        self.folder_btn.setEnabled(False)
        self.folder_btn.clicked.connect(self._open_selected_rom_folder)
        action_row.addWidget(self.fav_btn, 1)
        action_row.addWidget(self.edit_btn, 1)
        action_row.addWidget(self.folder_btn)
        dl.addLayout(action_row)

        self._meta_labels: dict[str, QLabel] = {}
        self._meta_key_labels: dict[str, QLabel] = {}

        tab_bar_row = QHBoxLayout()
        tab_bar_row.setContentsMargins(0, 2, 0, 0)
        tab_bar_row.setSpacing(4)
        self.detail_tab_details_btn = QPushButton(t("lbl_details_hdr").title())
        self.detail_tab_details_btn.setObjectName("DetailTabBtn")
        self.detail_tab_details_btn.setCheckable(True)
        self.detail_tab_details_btn.setChecked(True)
        self.detail_tab_details_btn.setCursor(Qt.PointingHandCursor)
        self.detail_tab_desc_btn = QPushButton(t("lbl_desc"))
        self.detail_tab_desc_btn.setObjectName("DetailTabBtn")
        self.detail_tab_desc_btn.setCheckable(True)
        self.detail_tab_desc_btn.setCursor(Qt.PointingHandCursor)
        self._detail_tab_group = QButtonGroup(self)
        self._detail_tab_group.setExclusive(True)
        self._detail_tab_group.addButton(self.detail_tab_details_btn, 0)
        self._detail_tab_group.addButton(self.detail_tab_desc_btn, 1)
        tab_bar_row.addWidget(self.detail_tab_details_btn)
        tab_bar_row.addWidget(self.detail_tab_desc_btn)
        tab_bar_row.addStretch(1)
        dl.addLayout(tab_bar_row)

        self.detail_stack = QStackedWidget()
        self.detail_stack.setObjectName("DetailStack")
        self.detail_stack.setAutoFillBackground(False)
        self._detail_tab_group.idClicked.connect(self.detail_stack.setCurrentIndex)

        # Tab 1: Statistiche compatte + Metadati
        tab_details = QWidget()
        tab_details.setObjectName("DetailTabPage")
        td_lay = QVBoxLayout(tab_details)
        td_lay.setContentsMargins(0, 6, 0, 0)
        td_lay.setSpacing(6)

        self.stats_card = QFrame()
        self.stats_card.setObjectName("DetailCard")
        stats_cols = QHBoxLayout(self.stats_card)
        stats_cols.setContentsMargins(8, 5, 8, 5)
        stats_cols.setSpacing(8)
        for s_key, s_cap in (("plays", t("lbl_plays")), ("play_time", t("lbl_playtime"))):
            box = QFrame()
            box.setObjectName("DetailStatBox")
            bl = QHBoxLayout(box)
            bl.setContentsMargins(8, 4, 8, 4)
            bl.setSpacing(6)
            k_lbl = QLabel(s_cap.rstrip(":："))
            k_lbl.setObjectName("DetailMetaKey")
            v_lbl = QLabel("—")
            v_lbl.setObjectName("DetailStatVal")
            v_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            bl.addWidget(k_lbl)
            bl.addStretch(1)
            bl.addWidget(v_lbl)
            stats_cols.addWidget(box, 1)
            self._meta_key_labels[s_key] = k_lbl
            self._meta_labels[s_key] = v_lbl
        td_lay.addWidget(self.stats_card)

        self.meta_card = QFrame()
        self.meta_card.setObjectName("DetailCard")
        meta_lay = QVBoxLayout(self.meta_card)
        meta_lay.setContentsMargins(12, 6, 12, 6)
        meta_lay.setSpacing(0)
        meta_fields = (
            ("platform", t("lbl_platform")),
            ("genre", t("lbl_genre")),
            ("year", t("lbl_year")),
            ("developer", t("lbl_developer")),
            ("publisher", t("lbl_publisher")),
            ("players", t("lbl_players")),
            ("rating", t("lbl_rating")),
        )
        for row_idx, (key, caption) in enumerate(meta_fields):
            row_f = QFrame()
            row_f.setObjectName("DetailMetaRowLast" if row_idx == len(meta_fields) - 1 else "DetailMetaRow")
            rl = QHBoxLayout(row_f)
            rl.setContentsMargins(0, 5, 0, 5)
            rl.setSpacing(10)
            k_lbl = QLabel(caption)
            k_lbl.setObjectName("DetailMetaKey")
            v_lbl = QLabel("—")
            v_lbl.setObjectName("DetailMetaVal")
            v_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v_lbl.setWordWrap(True)
            rl.addWidget(k_lbl, 0, Qt.AlignTop)
            rl.addWidget(v_lbl, 1, Qt.AlignTop)
            meta_lay.addWidget(row_f)
            self._meta_labels[key] = v_lbl
            self._meta_key_labels[key] = k_lbl
        td_lay.addWidget(self.meta_card)
        td_lay.addStretch(1)
        self.detail_stack.addWidget(tab_details)

        # Tab 2: Descrizione / Trama completa
        tab_desc = QWidget()
        tab_desc.setObjectName("DetailTabPage")
        tdesc_lay = QVBoxLayout(tab_desc)
        tdesc_lay.setContentsMargins(0, 6, 0, 0)
        tdesc_lay.setSpacing(0)

        self.desc_card = QFrame()
        self.desc_card.setObjectName("DetailCard")
        desc_lay = QVBoxLayout(self.desc_card)
        desc_lay.setContentsMargins(10, 8, 10, 8)
        desc_lay.setSpacing(6)
        desc_row = QHBoxLayout()
        self.desc_hdr = QLabel(t("lbl_desc").upper())
        self.desc_hdr.setObjectName("DetailCardHdr")
        self.translate_btn = QPushButton(t("btn_translate"))
        self.translate_btn.setToolTip(t("btn_translate_tip"))
        self.translate_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.translate_btn.setEnabled(False)
        self.translate_btn.clicked.connect(self._toggle_or_translate_desc)
        desc_row.addWidget(self.desc_hdr)
        desc_row.addStretch(1)
        desc_row.addWidget(self.translate_btn)
        desc_lay.addLayout(desc_row)

        desc_scroll = QScrollArea()
        desc_scroll.setWidgetResizable(True)
        desc_scroll.setFrameShape(QFrame.NoFrame)
        desc_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        self.d_desc = QLabel(t("no_desc"))
        self.d_desc.setObjectName("DetailDescText")
        self.d_desc.setWordWrap(True)
        self.d_desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.d_desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        desc_scroll.setWidget(self.d_desc)
        desc_lay.addWidget(desc_scroll, 1)
        tdesc_lay.addWidget(self.desc_card, 1)
        self.detail_stack.addWidget(tab_desc)

        dl.addWidget(self.detail_stack, 1)

        self.scroll_area.setWidget(self.detail_inner)
        self.right_splitter.addWidget(self.scroll_area)
        self.right_splitter.setSizes([310, 470])
        self.right_splitter.splitterMoved.connect(self._on_right_splitter_moved)

        self.main_splitter.addWidget(self.right_w)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.splitterMoved.connect(self._on_splitter_moved)

        root_lay.addWidget(self.main_splitter, 1)

        sb = QStatusBar()
        sb.setMinimumHeight(30)
        self.setStatusBar(sb)
        sb.showMessage(t("portable_status", root=self.db.root))
        self._retranslate_ui()

        self.bg_bar_w = QWidget()
        bg_lay = QHBoxLayout(self.bg_bar_w)
        bg_lay.setContentsMargins(4, 2, 8, 2)
        bg_lay.setSpacing(8)

        self.bg_scrape_lbl = QLabel("")
        self.bg_scrape_lbl.setStyleSheet("color: #f58a1f;")
        self.bg_scrape_pbar = QProgressBar()
        self.bg_scrape_pbar.setFixedWidth(180)
        self.bg_scrape_pbar.setFixedHeight(16)
        self.bg_scrape_pbar.setTextVisible(True)
        self.bg_scrape_details_btn = QPushButton("Dettagli")
        self.bg_scrape_details_btn.setFixedSize(78, 22)
        self.bg_scrape_details_btn.setStyleSheet("padding: 2px 8px; font-size: 9pt;")
        self.bg_scrape_details_btn.clicked.connect(lambda: self._open_batch_scrape())

        self.bg_scrape_stop_btn = QPushButton("Interrompi")
        self.bg_scrape_stop_btn.setFixedSize(90, 22)
        self.bg_scrape_stop_btn.setStyleSheet("padding: 2px 8px; font-size: 9pt;")
        self.bg_scrape_stop_btn.clicked.connect(self._stop_bg_scrape)

        bg_lay.addWidget(self.bg_scrape_lbl, 0, Qt.AlignVCenter)
        bg_lay.addWidget(self.bg_scrape_pbar, 0, Qt.AlignVCenter)
        bg_lay.addWidget(self.bg_scrape_details_btn, 0, Qt.AlignVCenter)
        bg_lay.addWidget(self.bg_scrape_stop_btn, 0, Qt.AlignVCenter)
        self.bg_bar_w.setVisible(False)
        sb.addPermanentWidget(self.bg_bar_w)
        sb.setVisible(False)

    # ----- Stato finestra & Config -----
    def _restore_window_state(self) -> None:
        win = self.config.get("window", {})
        self.resize(int(win.get("width", 1380)), int(win.get("height", 840)))
        self._start_maximized = bool(win.get("maximized"))
        sizes = win.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 3:
            self.main_splitter.setSizes([int(x) for x in sizes])
        else:
            self.main_splitter.setSizes([302, 1120, 498])

        r_sizes = win.get("right_splitter")
        if isinstance(r_sizes, list) and len(r_sizes) == 2:
            self.right_splitter.setSizes([int(x) for x in r_sizes])
        else:
            self.right_splitter.setSizes([520, 462])

        pv_sizes = win.get("preview_splitter")
        if isinstance(pv_sizes, list) and len(pv_sizes) == 2:
            self.preview_splitter.setSizes([int(x) for x in pv_sizes])
        else:
            self.preview_splitter.setSizes([195, 325])

        zoom_key = "list_zoom" if self.delegate.list_mode else "grid_zoom"
        default_zoom = 207 if self.delegate.list_mode else 193
        zoom = int(self.config.get(zoom_key, default_zoom))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(zoom)
        self.zoom_slider.blockSignals(False)
        self.delegate.cell_width = zoom
        self.delegate.cover_radius = int(self.config.get("cover_radius", 6))
        self.logo_radius = int(self.config.get("logo_radius", 6))
        self.screenshot_radius = int(self.config.get("screenshot_radius", 6))
        self._apply_layout_lock()

    def _on_preview_splitter_moved(self, _pos: int, _index: int) -> None:
        win_cfg = self.config.data.setdefault("window", {})
        win_cfg["preview_splitter"] = self.preview_splitter.sizes()
        self.config.save()
        self._update_preview_pixmap()

    def _save_zoom(self) -> None:
        key = "list_zoom" if self.delegate.list_mode else "grid_zoom"
        self.config.data[key] = self.zoom_slider.value()
        self.config.save()

    def _sync_topbar_zoom_align(self) -> None:
        if hasattr(self, "_title_bar") and hasattr(self, "right_w"):
            self._title_bar.set_right_align_width(self.right_w.width())

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        win_cfg = self.config.data.setdefault("window", {})
        win_cfg["splitter"] = self.main_splitter.sizes()
        self.config.save()
        self._update_preview_pixmap()
        self._sync_topbar_zoom_align()

    def closeEvent(self, event) -> None:
        if self._session_timer.isActive():
            self._session_timer.stop()
        if self._active_sessions:
            now = time.monotonic()
            for gid, _proc, started in self._active_sessions:
                self.db.add_play_time(gid, max(1, int(now - started)))
            self._active_sessions.clear()
        if self._bg_scrape_worker and self._bg_scrape_worker.isRunning():
            self._bg_scrape_worker.stop()
            self._bg_scrape_worker.wait(3000)
        self.config.data["grid_zoom"] = self.zoom_slider.value()
        self.config.data["last_platform_id"] = self.current_platform_id
        self.config.data["window"] = {
            "width": self.width(),
            "height": self.height(),
            "maximized": self.isMaximized(),
            "splitter": self.main_splitter.sizes(),
            "right_splitter": self.right_splitter.sizes(),
            "preview_splitter": self.preview_splitter.sizes(),
        }
        self.config.save()
        self.db.close()
        app = QApplication.instance()
        if app is not None and hasattr(self, "_resize_filter"):
            app.removeEventFilter(self._resize_filter)
        super().closeEvent(event)

    # ----- Albero Piattaforme -----
    def _sync_platform_sort_ui(self) -> None:
        is_alpha = (self.config.get("platform_sort_mode", "custom") == "alpha")
        self.plat_sort_alpha_btn.setChecked(is_alpha)
        self.plat_sort_custom_btn.setChecked(not is_alpha)
        self.tree.set_drag_reorder_enabled(not is_alpha)

    def _set_platform_sort_mode(self, mode: str) -> None:
        norm = "alpha" if mode == "alpha" else "custom"
        if self.config.get("platform_sort_mode", "custom") != norm:
            self.config.data["platform_sort_mode"] = norm
            self.config.save()
        self._sync_platform_sort_ui()
        self.reload_platforms()

    def reload_platforms(self) -> None:
        alpha_mode = (self.config.get("platform_sort_mode", "custom") == "alpha")
        expanded_pids: set[int] = set()
        if self.tree.topLevelItemCount() > 0:
            def _collect_expanded(it: QTreeWidgetItem) -> None:
                pid_val = it.data(0, Qt.UserRole)
                if pid_val is not None and it.childCount() > 0 and it.isExpanded():
                    expanded_pids.add(pid_val)
                for i in range(it.childCount()):
                    _collect_expanded(it.child(i))
            for i in range(self.tree.topLevelItemCount()):
                _collect_expanded(self.tree.topLevelItem(i))

        self.tree.blockSignals(True)
        self.tree.clear()

        all_item = QTreeWidgetItem(self.tree, [t("all_platforms")])
        all_item.setData(0, Qt.UserRole, None)
        all_item.setFlags((all_item.flags() & ~Qt.ItemIsDragEnabled) | Qt.ItemIsDropEnabled)
        all_icon = os.path.join(self.db.root, "icons", "all_platforms.png")
        if not os.path.isfile(all_icon):
            all_icon = os.path.join(self.db.root, "icons", "app_icon.png")
        if os.path.isfile(all_icon):
            all_item.setIcon(0, QIcon(all_icon))
        else:
            all_item.setIcon(0, self.style().standardIcon(QStyle.SP_DirHomeIcon))

        selected_item = all_item

        def build_branch(
            parent_item: Optional[QTreeWidgetItem],
            parent_id: Optional[int],
            parent_icon_path: str = "",
        ) -> None:
            nonlocal selected_item
            if parent_id is None:
                plats = self.db.list_platforms(root_only=True)
            else:
                plats = self.db.list_subplatforms(parent_id)
            if alpha_mode:
                plats = sorted(plats, key=lambda r: (r["name"] or "").casefold())

            for p in plats:
                label = p["name"]

                child = QTreeWidgetItem(parent_item if parent_item is not None else self.tree, [label])
                child.setData(0, Qt.UserRole, p["id"])
                raw_icon = p["icon_path"] or parent_icon_path
                icon_abs = self.db.resolve(raw_icon) if raw_icon else ""
                if icon_abs and os.path.isfile(icon_abs):
                    child.setIcon(0, QIcon(icon_abs))
                else:
                    child.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))

                if self.current_platform_id == p["id"]:
                    selected_item = child

                child.setExpanded(p["id"] in expanded_pids)
                build_branch(child, p["id"], raw_icon)

        build_branch(None, None)

        if self.current_platform_id is not None and selected_item is all_item:
            self.current_platform_id = None

        self.tree.setCurrentItem(selected_item)
        self.tree.blockSignals(False)
        if hasattr(self, "fav_tree"):
            self.reload_favorites()

    def reload_favorites(self) -> None:
        self.fav_tree.blockSignals(True)
        self.fav_tree.clear()
        all_fav = QTreeWidgetItem(self.fav_tree, [t("fav_all")])
        all_fav.setData(0, Qt.UserRole, None)
        acc = self.config.get("accent_color", "#f58a1f") or "#f58a1f"
        all_fav.setIcon(0, make_star_icon(acc))
        selected_item = all_fav
        for f in self.db.list_favorite_folders():
            it = QTreeWidgetItem(self.fav_tree, [f["name"]])
            it.setData(0, Qt.UserRole, int(f["id"]))
            raw_ic = f["icon_path"] if "icon_path" in f.keys() else ""
            ic_abs = self.db.resolve(raw_ic) if raw_ic else ""
            if ic_abs and os.path.isfile(ic_abs):
                it.setIcon(0, QIcon(ic_abs))
            else:
                it.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))
            if self.current_fav_folder_id == int(f["id"]):
                selected_item = it
        if self.current_fav_folder_id is not None and selected_item is all_fav:
            self.current_fav_folder_id = None
        self.fav_tree.setCurrentItem(selected_item)
        self.fav_tree.blockSignals(False)

    def _on_fav_tree_selected(self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        if current is None:
            return
        self.current_fav_folder_id = current.data(0, Qt.UserRole)
        if self.left_stack.currentIndex() == 1:
            self.reload_games()

    def _on_fav_tree_context(self, pos) -> None:
        item = self.fav_tree.itemAt(pos)
        menu = QMenu(self)
        menu.addAction(t("fav_new_folder"), self._create_favorite_folder)
        if item is not None:
            fid = item.data(0, Qt.UserRole)
            if fid is not None:
                menu.addSeparator()
                menu.addAction(t("fav_change_icon"), lambda: self._change_favorite_folder_icon(int(fid)))
                menu.addAction(t("fav_rename_folder"), lambda: self._rename_favorite_folder(int(fid)))
                menu.addAction(t("fav_delete_folder"), lambda: self._delete_favorite_folder(int(fid)))
        menu.exec(self.fav_tree.viewport().mapToGlobal(pos))

    def _change_favorite_folder_icon(self, folder_id: int) -> None:
        icons_dir = os.path.join(self.db.root, "icons")
        start_dir = icons_dir if os.path.isdir(icons_dir) else self.db.root
        fp, _ = QFileDialog.getOpenFileName(
            self, t("fav_change_icon"), start_dir, "Images (*.png *.ico *.jpg *.jpeg *.webp *.bmp)"
        )
        if not fp or not os.path.isfile(fp):
            return
        root_abs = os.path.abspath(self.db.root)
        fp_abs = os.path.abspath(fp)
        try:
            inside = os.path.commonpath([root_abs, fp_abs]) == root_abs
        except ValueError:
            inside = False
        if inside:
            stored = self.db.store(fp_abs)
        else:
            os.makedirs(icons_dir, exist_ok=True)
            base_name = os.path.basename(fp_abs)
            dest = os.path.join(icons_dir, base_name)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(base_name)
                dest = os.path.join(icons_dir, f"fav_{folder_id}_{stem}{ext or '.png'}")
            shutil.copy2(fp_abs, dest)
            stored = self.db.store(dest)
        self.db.update_favorite_folder_icon(folder_id, stored)
        self.reload_favorites()

    def _create_favorite_folder(self) -> Optional[int]:
        name, ok = QInputDialog.getText(self, t("fav_new_folder"), t("fav_folder_prompt"))
        if not ok or not name.strip():
            return None
        fid = self.db.create_favorite_folder(name.strip())
        self.current_fav_folder_id = fid
        self.reload_favorites()
        if self.left_stack.currentIndex() == 1:
            self.reload_games()
        return fid

    def _rename_favorite_folder(self, folder_id: int) -> None:
        f = self.db.get_favorite_folder(folder_id)
        if f is None:
            return
        name, ok = QInputDialog.getText(self, t("fav_rename_folder"), t("fav_folder_prompt"), text=f["name"])
        if not ok or not name.strip():
            return
        self.db.rename_favorite_folder(folder_id, name.strip())
        self.reload_favorites()
        if self.left_stack.currentIndex() == 1:
            self.reload_games()

    def _delete_favorite_folder(self, folder_id: int) -> None:
        f = self.db.get_favorite_folder(folder_id)
        if f is None:
            return
        if QMessageBox.question(self, t("fav_delete_folder"), t("fav_delete_confirm", name=f["name"])) == QMessageBox.Yes:
            self.db.delete_favorite_folder(folder_id)
            if self.current_fav_folder_id == folder_id:
                self.current_fav_folder_id = None
            self.reload_favorites()
            self.reload_games()

    def _on_platforms_reordered(self, order: list) -> None:
        if order:
            self.db.reorder_platforms(order)

    def _on_tree_selected(self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        if current is None:
            return
        self.current_platform_id = current.data(0, Qt.UserRole)
        self.reload_games()

    def _on_tree_context(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is not None:
            self.tree.setCurrentItem(item)
        pid = item.data(0, Qt.UserRole) if item else None

        menu = QMenu(self)
        if pid is not None:
            plat = self.db.get_platform(pid)
            menu.addAction(t("ctx_new_sub"), lambda: self._quick_new_platform(parent_id=pid))
            menu.addSeparator()
            label_edit = t("ctx_edit_sub") if (plat and plat["parent_id"]) else t("ctx_edit_plat")
            label_del = t("ctx_del_sub") if (plat and plat["parent_id"]) else t("ctx_del_plat")
            menu.addAction(label_edit, lambda: self._edit_platform(pid))
            menu.addAction(t("ctx_scan_rom"), lambda: self._scan_platform(pid))
            menu.addAction(t("ctx_add_rom"), self._add_roms_manually)
            menu.addAction(t("ctx_scrape"), lambda: self._open_batch_scrape(pid))
            menu.addSeparator()
            menu.addAction(label_del, lambda: self._delete_platform(pid))
        else:
            menu.addAction(t("ctx_new_plat"), self._quick_new_platform)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ----- Griglia Giochi -----
    def reload_games(self) -> None:
        in_fav_tab = hasattr(self, "left_stack") and self.left_stack.currentIndex() == 1
        if in_fav_tab:
            games = self.db.list_games(
                platform_id=None,
                search=self.search_ed.text(),
                favorites_only=(self.current_fav_folder_id is None),
                favorite_folder_id=self.current_fav_folder_id,
                order=self.sort_cb.currentData() or "title",
            )
        else:
            games = self.db.list_games(
                platform_id=self.current_platform_id,
                search=self.search_ed.text(),
                favorites_only=False,
                order=self.sort_cb.currentData() or "title",
            )
        self.model.set_games(games)
        self._update_alpha_bar()

        logo_pix: Optional[QPixmap] = None
        if in_fav_tab:
            if self.current_fav_folder_id is None:
                plat_title = t("fav_all")
            else:
                f = self.db.get_favorite_folder(self.current_fav_folder_id)
                plat_title = f["name"] if f else t("fav_all")
        elif self.current_platform_id is None:
            plat_title = t("all_platforms")
        else:
            p = self.db.get_platform(self.current_platform_id)
            plat_title = p["name"] if p else "Piattaforma"
            raw_logo = ""
            cur_p = p
            seen_pids: set[int] = set()
            while cur_p is not None and cur_p["id"] not in seen_pids:
                seen_pids.add(cur_p["id"])
                if "logo_path" in cur_p.keys() and cur_p["logo_path"]:
                    raw_logo = cur_p["logo_path"]
                    break
                pid_up = cur_p["parent_id"] if "parent_id" in cur_p.keys() else None
                cur_p = self.db.get_platform(pid_up) if pid_up else None
            if raw_logo:
                lp = self.db.resolve(raw_logo)
                if lp and os.path.isfile(lp):
                    im = QImage(lp)
                    if not im.isNull():
                        if max(im.width(), im.height()) > 600:
                            im = im.scaled(600, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        im = im.convertToFormat(QImage.Format_ARGB32)
                        w, h = im.width(), im.height()
                        buf = memoryview(im.bits())
                        min_x, min_y, max_x, max_y = w, h, -1, -1
                        try:
                            for y in range(h):
                                row_off = y * w * 4
                                for x in range(w):
                                    if buf[row_off + x * 4 + 3] > 8:
                                        if x < min_x:
                                            min_x = x
                                        if x > max_x:
                                            max_x = x
                                        if y < min_y:
                                            min_y = y
                                        if y > max_y:
                                            max_y = y
                        finally:
                            buf.release()
                        if max_x >= min_x and max_y >= min_y:
                            im = im.copy(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
                        logo_pix = QPixmap.fromImage(im).scaled(
                            420, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation
                        )
        self.center_title_lbl.setText(plat_title)
        if logo_pix is not None:
            self.center_header.clear()
            self.center_header.setMinimumHeight(56)
            self.center_header.setPixmap(logo_pix)
            self.center_header.setVisible(True)
        else:
            self.center_header.clear()
            self.center_header.setMinimumHeight(0)
            self.center_header.setVisible(False)
        self.center_header.adjustSize()
        self.center_header.updateGeometry()
        self.center_header.repaint()
        self.count_lbl.setText(t("games_count", n=len(games)))
        QTimer.singleShot(0, self.grid._update_center_margins)

        # Ripristina selezione se ancora presente, altrimenti azzera pannello dettagli
        if self.current_game_id is not None:
            row = self.model.row_of(self.current_game_id)
            if row is not None:
                idx = self.model.index(row, 0)
                self.grid.setCurrentIndex(idx)
                self._show_game(self.current_game_id)
                return
        self._show_game(None)

    def _on_zoom_changed(self, value: int) -> None:
        self.delegate.cell_width = value
        key = "list_zoom" if self.delegate.list_mode else "grid_zoom"
        self.config.data[key] = value
        self.thumb_cache.clear()
        self.grid._update_center_margins()
        self.grid.doItemsLayout()

    def _jump_to_letter(self, letter: str) -> None:
        row = self.model.row_of_section(letter)
        if row is not None:
            idx = self.model.index(row, 0)
            self.grid.scrollTo(idx, QAbstractItemView.PositionAtTop)

    def _update_alpha_bar(self) -> None:
        if not hasattr(self, "_alpha_buttons"):
            return
        avail = self.model.available_sections()
        for ch, btn in self._alpha_buttons.items():
            btn.setEnabled(ch in avail)

    def _set_view_mode(self, mode: str) -> None:
        is_list = (mode == "list")
        sel_id = self.current_game_id
        self.delegate.list_mode = is_list
        zoom = int(self.config.get("list_zoom" if is_list else "grid_zoom", 160 if is_list else 260))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(zoom)
        self.zoom_slider.blockSignals(False)
        self.delegate.cell_width = zoom
        self.thumb_cache.clear()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setUniformItemSizes(not is_list)
        self.grid.setWordWrap(not is_list)
        self.zoom_lbl.setVisible(True)
        self.zoom_slider.setVisible(True)
        self.alpha_bar.setVisible(is_list)
        self.list_view_btn.setChecked(is_list)
        self.grid_view_btn.setChecked(not is_list)
        self.model.set_group_by_letter(is_list)
        self._update_alpha_bar()
        if sel_id is not None:
            row = self.model.row_of(sel_id)
            if row is not None:
                self.grid.setCurrentIndex(self.model.index(row, 0))
        self.grid._update_center_margins()
        self.grid.doItemsLayout()
        self.grid.viewport().update()
        if self.config.get("view_mode") != ("list" if is_list else "grid"):
            self.config.data["view_mode"] = "list" if is_list else "grid"
            self.config.save()

    def _on_thumb_loaded(self, abs_path: str, width: int, image: QImage) -> None:
        self.thumb_cache.store(abs_path, width, image)
        self.grid.viewport().update()

    def _on_grid_selected(self, current: QModelIndex, _prev: QModelIndex) -> None:
        gid = current.data(ROLE_GAME_ID) if current.isValid() else None
        self._show_game(gid)

    def _on_grid_context(self, pos) -> None:
        idx = self.grid.indexAt(pos)
        if not idx.isValid():
            return
        self.grid.setCurrentIndex(idx)
        gid = idx.data(ROLE_GAME_ID)
        game = self.db.get_game(gid)
        if game is None:
            return

        menu = QMenu(self)
        menu.addAction(f"▶  {t('ctx_play')}", self._launch_selected)

        emus = self.db.platform_emulators(game["platform_id"])
        if len(emus) > 1:
            sub = menu.addMenu(t("ctx_play_with"))
            for e in emus:
                sub.addAction(e["name"], lambda eid=e["id"]: self._launch_selected(eid))

        menu.addSeparator()
        menu.addAction(t("ctx_edit_game"), self._edit_selected)
        
        # Sottomenu rapido Sposta nella sotto-sezione
        cur_plat = self.db.get_platform(game["platform_id"])
        base_plat_id = cur_plat["parent_id"] if (cur_plat and cur_plat["parent_id"]) else game["platform_id"]
        base_plat = self.db.get_platform(base_plat_id)
        if base_plat:
            subs = self.db.list_subplatforms(base_plat_id)
            if subs:
                move_menu = menu.addMenu(t("ctx_move_folder"))
                act_root = move_menu.addAction(
                    t("ctx_root", name=base_plat["name"]),
                    lambda: self._move_game_to_plat(gid, base_plat_id),
                )
                if game["platform_id"] == base_plat_id:
                    act_root.setEnabled(False)
                for s in subs:
                    act_sub = move_menu.addAction(s["name"], lambda target_id=s["id"]: self._move_game_to_plat(gid, target_id))
                    if game["platform_id"] == s["id"]:
                        act_sub.setEnabled(False)

        menu.addAction(
            t("ctx_fav_remove") if game["favorite"] else t("ctx_fav_add"),
            self._toggle_favorite,
        )
        menu.addAction(t("ctx_open_rom_dir"), lambda: self._open_rom_folder(game))
        menu.addSeparator()
        menu.addAction(t("ctx_remove_game"), self._remove_selected_game)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    # ----- Pannello Dettagli (Colonna Destra) -----
    def _show_game(self, game_id: Optional[int]) -> None:
        self.current_game_id = game_id
        if game_id is None:
            self._banner_source = None
            self._preview_source = None
            self._preview_shots = []
            self._preview_shot_idx = 0
            self.banner_lbl.setPixmap(QPixmap())
            self.banner_lbl.setVisible(False)
            self.preview_lbl.setPixmap(QPixmap())
            self.preview_lbl.setVisible(True)
            self.preview_lbl.setText(t("no_game_selected"))
            self._update_shot_nav()
            self.d_title.setText(t("no_game_title"))
            self.play_btn.setEnabled(False)
            self.fav_btn.setEnabled(False)
            self.edit_btn.setEnabled(False)
            self.folder_btn.setEnabled(False)
            for lbl in self._meta_labels.values():
                lbl.setText("—")
            self._showing_translated = False
            self.translate_btn.setText(t("btn_translate"))
            self.translate_btn.setEnabled(False)
            self.d_desc.setText(t("no_desc"))
            self._schedule_dynamic_blur("")
            return

        g = self.db.get_game(game_id)
        if g is None:
            self._show_game(None)
            return

        plat = self.db.get_platform(g["platform_id"])
        self.d_title.setText(g["title"])
        self.play_btn.setEnabled(True)
        self.fav_btn.setEnabled(True)
        self.edit_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
        self.fav_btn.setText(t("btn_fav_remove") if g["favorite"] else t("btn_fav"))

        rating_txt = "—"
        if g["rating"]:
            stars = int(round(float(g["rating"])))
            rating_txt = f"{'★' * stars}{'☆' * (5 - stars)}  ({g['rating']:.1f}/5)"

        self._meta_labels["platform"].setText(plat["name"] if plat else "—")
        self._meta_labels["genre"].setText(g["genre"] or "—")
        self._meta_labels["year"].setText(str(g["release_year"]) if g["release_year"] else "—")
        self._meta_labels["developer"].setText(g["developer"] or "—")
        self._meta_labels["publisher"].setText(g["publisher"] or "—")
        self._meta_labels["players"].setText(g["players"] or "—")
        self._meta_labels["rating"].setText(rating_txt)
        self._meta_labels["plays"].setText(str(g["play_count"] or 0))
        pt_sec = int(g["play_time"]) if "play_time" in g.keys() and g["play_time"] else 0
        self._meta_labels["play_time"].setText(self._format_play_time(pt_sec))
        desc_txt = (g["description"] or "").strip()
        self._showing_translated = False
        self.translate_btn.setText(t("btn_translate"))
        self.translate_btn.setEnabled(bool(desc_txt))
        self.d_desc.setText(desc_txt or t("no_desc"))

        # Anteprima HD: preferisce lo screenshot, altrimenti la copertina
        def _load_pix_path(rel_or_abs: str) -> Optional[QPixmap]:
            p = self.db.resolve(rel_or_abs) if rel_or_abs else ""
            if p and os.path.isfile(p):
                cand = QPixmap(p)
                if not cand.isNull():
                    return cand
            return None

        self._banner_source = _load_pix_path(g["logo_path"]) or _load_pix_path(g["boxart_path"])
        try:
            ox = float(g["logo_offset_x"]) if ("logo_offset_x" in g.keys() and g["logo_offset_x"] is not None) else 0.5
        except (TypeError, ValueError):
            ox = 0.5
        try:
            oy = float(g["logo_offset_y"]) if ("logo_offset_y" in g.keys() and g["logo_offset_y"] is not None) else 0.5
        except (TypeError, ValueError):
            oy = 0.5
        self._banner_offset = (max(0.0, min(1.0, ox)), max(0.0, min(1.0, oy)))
        shots: list[QPixmap] = []
        main_shot = _load_pix_path(g["screenshot_path"])
        if main_shot is not None:
            shots.append(main_shot)
        raw_extra = g["extra_screenshots"] if "extra_screenshots" in g.keys() else "[]"
        try:
            extra_list = json.loads(raw_extra or "[]")
        except ValueError:
            extra_list = []
        for ep in extra_list:
            px = _load_pix_path(ep)
            if px is not None:
                shots.append(px)
        self._preview_shots = shots
        self._preview_shot_idx = 0
        self._preview_source = shots[0] if shots else None
        self._update_preview_pixmap()

        # Sfondo sfocato dinamico: preferisce screenshot/fanart, altrimenti copertina
        bg_cand = ""
        for rel_p in (g["screenshot_path"], g["boxart_path"]):
            if rel_p:
                cand_abs = self.db.resolve(rel_p)
                if cand_abs and os.path.isfile(cand_abs):
                    bg_cand = cand_abs
                    break
        self._schedule_dynamic_blur(bg_cand)

    def _toggle_or_translate_desc(self) -> None:
        if self.current_game_id is None:
            return
        gid = self.current_game_id
        if gid in self._translated_cache:
            orig, trans = self._translated_cache[gid]
            if self._showing_translated:
                self.d_desc.setText(orig)
                self._showing_translated = False
                self.translate_btn.setText(t("btn_translate"))
            else:
                self.d_desc.setText(trans)
                self._showing_translated = True
                self.translate_btn.setText(t("btn_original"))
            return
        orig = self.d_desc.text().strip()
        if not orig or orig == t("no_desc"):
            return
        self.translate_btn.setEnabled(False)
        self.translate_btn.setText(t("btn_translating"))
        QThreadPool.globalInstance().start(
            _TranslateTask(gid, orig, self._translate_signals, target_lang=get_lang())
        )

    def _on_desc_translated(self, gid: int, orig: str, trans: str) -> None:
        self._translated_cache[gid] = (orig, trans)
        if gid == self.current_game_id:
            self.d_desc.setText(trans)
            self._showing_translated = True
            self.translate_btn.setText(t("btn_original"))
            self.translate_btn.setEnabled(True)

    def _on_desc_translate_failed(self, gid: int, err: str) -> None:
        if gid == self.current_game_id:
            self.translate_btn.setText(t("btn_translate"))
            self.translate_btn.setEnabled(True)
            self.statusBar().showMessage(f"Errore traduzione: {err}", 5000)

    def _clip_pixmap_rounded(self, pix: QPixmap, radius: int, dpr: float) -> QPixmap:
        if radius <= 0 or pix.isNull():
            return pix
        lw = pix.width() / dpr
        lh = pix.height() / dpr
        r_clip = min(float(radius), lw * 0.45, lh * 0.45)
        out = QPixmap(pix.size())
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        clip_path = QPainterPath()
        clip_path.addRoundedRect(QRectF(0, 0, lw, lh), r_clip, r_clip)
        p.setClipPath(clip_path)
        p.drawPixmap(0, 0, pix)
        p.end()
        return out

    def _apply_scaled_pixmap(self, lbl: QLabel, src: QPixmap) -> None:
        avail = lbl.size() - QSize(8, 8)
        if avail.width() < 20 or avail.height() < 20:
            return
        dpr = self.devicePixelRatioF() or 1.0
        target = QSize(int(avail.width() * dpr), int(avail.height() * dpr))
        src_w, src_h = src.width(), src.height()
        mode = Qt.FastTransformation if (src_w <= 320 and src_h <= 240 and target.width() > src_w) else Qt.SmoothTransformation
        scaled = src.scaled(target, Qt.KeepAspectRatio, mode)
        scaled.setDevicePixelRatio(dpr)
        radius = getattr(self, "screenshot_radius", int(self.config.get("screenshot_radius", 6)))
        scaled = self._clip_pixmap_rounded(scaled, radius, dpr)
        lbl.setText("")
        lbl.setPixmap(scaled)

    def _open_corner_radius_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QGridLayout, QSpinBox
        orig_cover = int(self.config.get("cover_radius", 6))
        orig_logo = int(self.config.get("logo_radius", 6))
        orig_shot = int(self.config.get("screenshot_radius", 6))

        dlg = QDialog(self)
        dlg.setWindowTitle(t("corner_dlg_title"))
        dlg.setMinimumWidth(440)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(14)

        grid_lay = QGridLayout()
        grid_lay.setHorizontalSpacing(12)
        grid_lay.setVerticalSpacing(10)
        grid_lay.setColumnStretch(1, 1)

        def _make_row(row_idx: int, label_txt: str, init_val: int):
            lbl = QLabel(label_txt)
            sld = QSlider(Qt.Horizontal)
            sld.setRange(0, 28)
            sld.setValue(init_val)
            spn = QSpinBox()
            spn.setRange(0, 28)
            spn.setSuffix(" px")
            spn.setFixedWidth(74)
            spn.setValue(init_val)
            sld.valueChanged.connect(spn.setValue)
            spn.valueChanged.connect(sld.setValue)
            grid_lay.addWidget(lbl, row_idx, 0)
            grid_lay.addWidget(sld, row_idx, 1)
            grid_lay.addWidget(spn, row_idx, 2)
            return sld

        sld_cover = _make_row(0, t("corner_covers_lbl"), orig_cover)
        sld_logo = _make_row(1, t("corner_logo_lbl"), orig_logo)
        sld_shot = _make_row(2, t("corner_shot_lbl"), orig_shot)
        lay.addLayout(grid_lay)

        def _apply_preview() -> None:
            self.delegate.cover_radius = sld_cover.value()
            self.logo_radius = sld_logo.value()
            self.screenshot_radius = sld_shot.value()
            self.grid.viewport().update()
            self._update_preview_pixmap()

        sld_cover.valueChanged.connect(lambda _v: _apply_preview())
        sld_logo.valueChanged.connect(lambda _v: _apply_preview())
        sld_shot.valueChanged.connect(lambda _v: _apply_preview())

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        reset_btn = btns.addButton(t("corner_reset_btn"), QDialogButtonBox.ResetRole)
        reset_btn.clicked.connect(lambda: (sld_cover.setValue(6), sld_logo.setValue(6), sld_shot.setValue(6)))
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() == QDialog.Accepted:
            self.config.data["cover_radius"] = sld_cover.value()
            self.config.data["logo_radius"] = sld_logo.value()
            self.config.data["screenshot_radius"] = sld_shot.value()
            self.config.save()
        else:
            self.delegate.cover_radius = orig_cover
            self.logo_radius = orig_logo
            self.screenshot_radius = orig_shot
            self.grid.viewport().update()
            self._update_preview_pixmap()

    def _step_preview_shot(self, delta: int) -> None:
        if len(self._preview_shots) <= 1:
            return
        self._preview_shot_idx = (self._preview_shot_idx + delta) % len(self._preview_shots)
        self._preview_source = self._preview_shots[self._preview_shot_idx]
        self._update_preview_pixmap()

    def _update_shot_nav(self) -> None:
        w, h = self.preview_lbl.width(), self.preview_lbl.height()
        can_add = self.current_game_id is not None and self.preview_lbl.isVisible()
        self.shot_add_btn.setVisible(can_add)
        if can_add:
            self.shot_add_btn.move(
                max(8, w - self.shot_add_btn.width() - 8),
                max(8, h - self.shot_add_btn.height() - 8),
            )
            self.shot_add_btn.raise_()

        multi = len(self._preview_shots) > 1 and self.preview_lbl.isVisible()
        self.shot_prev_btn.setVisible(multi)
        self.shot_next_btn.setVisible(multi)
        self.shot_idx_lbl.setVisible(multi)
        if not multi:
            return
        cy = max(6, (h - self.shot_prev_btn.height()) // 2)
        self.shot_prev_btn.move(8, cy)
        self.shot_next_btn.move(max(8, w - self.shot_next_btn.width() - 8), cy)
        self.shot_idx_lbl.setText(f"{self._preview_shot_idx + 1} / {len(self._preview_shots)}")
        self.shot_idx_lbl.adjustSize()
        self.shot_idx_lbl.move(
            max(8, (w - self.shot_idx_lbl.width()) // 2),
            max(6, h - self.shot_idx_lbl.height() - 8),
        )
        self.shot_prev_btn.raise_()
        self.shot_next_btn.raise_()
        self.shot_idx_lbl.raise_()

    def _open_extra_screenshots_direct(self) -> None:
        if self.current_game_id is None:
            return
        g = self.db.get_game(self.current_game_id)
        if g is None:
            return
        plat = self.db.get_platform(g["platform_id"])
        plat_name = plat["name"] if plat else ""
        shots_dir = os.path.dirname(self.db.media_file(plat, "screenshots", g["rom_file"], ".png")) if plat else ""
        stem = os.path.splitext(g["rom_file"])[0]
        raw_extra = g["extra_screenshots"] if "extra_screenshots" in g.keys() else "[]"
        try:
            extra_list = json.loads(raw_extra or "[]")
        except ValueError:
            extra_list = []
        initial_shots = [
            self.db.resolve(p) for p in extra_list
            if p and os.path.isfile(self.db.resolve(p))
        ]
        dlg = ExtraScreenshotsDialog(
            self,
            self.db,
            g["id"],
            g["title"],
            plat_name,
            shots_dir,
            stem,
            initial_shots,
        )
        if dlg.exec() != ExtraScreenshotsDialog.Accepted:
            return
        extra_stored: list[str] = []
        if shots_dir:
            os.makedirs(shots_dir, exist_ok=True)
            shots_dir_abs = os.path.abspath(shots_dir)
            for idx, src in enumerate(dlg.shots, start=1):
                if not src or not os.path.isfile(src):
                    continue
                src_abs = os.path.abspath(src)
                if os.path.commonpath([src_abs, shots_dir_abs]) == shots_dir_abs:
                    extra_stored.append(self.db.store(src_abs))
                    continue
                ext = os.path.splitext(src)[1].lower() or ".png"
                dst = os.path.join(shots_dir, f"{stem}_extra_{idx}{ext}")
                n_try = idx
                while os.path.exists(dst) and os.path.abspath(dst) != src_abs:
                    n_try += 1
                    dst = os.path.join(shots_dir, f"{stem}_extra_{n_try}{ext}")
                shutil.copy2(src_abs, dst)
                extra_stored.append(self.db.store(dst))
        self.db.update_game(g["id"], extra_screenshots=json.dumps(extra_stored))
        self._show_game(g["id"])

    def _apply_banner_pixmap(self) -> None:
        src = self._banner_source
        if src is None or src.isNull():
            return
        avail = self.banner_lbl.size()
        if avail.width() < 20 or avail.height() < 20:
            return
        dpr = self.devicePixelRatioF() or 1.0
        target = QSize(int(avail.width() * dpr), int(avail.height() * dpr))
        src_w, src_h = src.width(), src.height()
        mode = Qt.FastTransformation if (src_w <= 320 and src_h <= 240 and target.width() > src_w) else Qt.SmoothTransformation
        scaled = src.scaledToWidth(target.width(), mode)
        if scaled.height() > target.height():
            oy = getattr(self, "_banner_offset", (0.5, 0.5))[1]
            crop_y = max(0, int(round((scaled.height() - target.height()) * oy)))
            out = scaled.copy(0, crop_y, target.width(), target.height())
        else:
            out = scaled
        out.setDevicePixelRatio(dpr)
        logo_r = getattr(self, "logo_radius", int(self.config.get("logo_radius", 6)))
        out = self._clip_pixmap_rounded(out, logo_r, dpr)
        self.banner_lbl.setText("")
        self.banner_lbl.setPixmap(out)

    def _rescale_previews(self) -> None:
        if self.banner_lbl.isVisible() and self._banner_source is not None and not self._banner_source.isNull():
            self._apply_banner_pixmap()
        if self.preview_lbl.isVisible() and self._preview_source is not None and not self._preview_source.isNull():
            self._apply_scaled_pixmap(self.preview_lbl, self._preview_source)
        self._update_shot_nav()

    def _update_preview_pixmap(self) -> None:
        has_banner = self._banner_source is not None and not self._banner_source.isNull()
        has_shot = self._preview_source is not None and not self._preview_source.isNull()
        visibility_changed = (has_banner != self.banner_lbl.isVisible()) or (has_shot != self.preview_lbl.isVisible())

        if has_banner:
            self.banner_lbl.setVisible(True)
            self._apply_banner_pixmap()
        else:
            self.banner_lbl.setPixmap(QPixmap())
            self.banner_lbl.setVisible(False)

        if has_shot:
            self.preview_lbl.setVisible(True)
            self._apply_scaled_pixmap(self.preview_lbl, self._preview_source)
        elif has_banner:
            self.preview_lbl.setPixmap(QPixmap())
            self.preview_lbl.setVisible(False)
        else:
            self.preview_lbl.setVisible(True)
            self.preview_lbl.setPixmap(QPixmap())
            if self.current_game_id is not None:
                self.preview_lbl.setText("Nessuna immagine disponibile")

        if has_banner and has_shot and visibility_changed:
            pv_saved = self.config.get("window", {}).get("preview_splitter")
            if isinstance(pv_saved, list) and len(pv_saved) == 2 and pv_saved[0] > 0 and pv_saved[1] > 0:
                self.preview_splitter.setSizes([int(x) for x in pv_saved])
            elif self.preview_splitter.height() > 120:
                tot = self.preview_splitter.height()
                self.preview_splitter.setSizes([int(tot * 0.42), int(tot * 0.58)])

        self._update_shot_nav()
        if has_banner or has_shot:
            QTimer.singleShot(0, self._rescale_previews)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_preview_pixmap()
        QTimer.singleShot(0, self._sync_topbar_zoom_align)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_topbar_zoom_align)

    # ----- Azioni Utente -----
    @staticmethod
    def _format_play_time(seconds: int) -> str:
        if seconds <= 0:
            return "—"
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60:02d}s"
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"

    def _poll_active_sessions(self) -> None:
        still_running: list[tuple[int, subprocess.Popen, float]] = []
        now = time.monotonic()
        for gid, proc, started in self._active_sessions:
            if proc.poll() is None:
                still_running.append((gid, proc, started))
            else:
                elapsed = max(1, int(now - started))
                self.db.add_play_time(gid, elapsed)
                if gid == self.current_game_id:
                    g = self.db.get_game(gid)
                    pt_sec = int(g["play_time"]) if g and g["play_time"] else elapsed
                    self._meta_labels["play_time"].setText(self._format_play_time(pt_sec))
                self.statusBar().showMessage(f"Sessione terminata: +{self._format_play_time(elapsed)}", 5000)
        self._active_sessions = still_running
        if not self._active_sessions:
            self._session_timer.stop()

    def _launch_selected(self, emulator_override_id: Optional[int] = None) -> None:
        if isinstance(emulator_override_id, bool):
            emulator_override_id = None
        if self.current_game_id is None:
            return
        gid = self.current_game_id
        res = launch_game(self.db, gid, emulator_override_id)
        if not res.ok:
            QMessageBox.warning(self, "Avvio non riuscito", res.message)
            return
        if res.process is not None:
            self._active_sessions.append((gid, res.process, time.monotonic()))
            if not self._session_timer.isActive():
                self._session_timer.start()
        self.statusBar().showMessage(res.message, 5000)
        self._show_game(gid)

    def _toggle_favorite(self) -> None:
        if self.current_game_id is None:
            return
        gid = self.current_game_id
        g = self.db.get_game(gid)
        if g is None:
            return
        folders = self.db.list_favorite_folders()
        if not folders:
            if g["favorite"]:
                self.db.remove_game_from_all_favorites(gid)
            else:
                name, ok = QInputDialog.getText(self, t("fav_new_folder"), t("fav_no_folders_msg"))
                if not ok or not name.strip():
                    return
                fid = self.db.create_favorite_folder(name.strip())
                self.db.set_game_favorite_folders(gid, [fid])
        else:
            dlg = ChooseFavoriteFolderDialog(self, self.db, gid, g["title"])
            if dlg.exec() != ChooseFavoriteFolderDialog.Accepted:
                return
            self.db.set_game_favorite_folders(gid, dlg.selected_folder_ids)
        if hasattr(self, "fav_tree"):
            self.reload_favorites()
        self.reload_games()

    def _edit_selected(self) -> None:
        if self.current_game_id is None:
            return
        dlg = GameEditDialog(self, self.db, self.current_game_id)
        if dlg.exec() == GameEditDialog.Accepted or getattr(dlg, "metadata_cleared", False):
            self.thumb_cache.clear()
            self.reload_games()

    def _remove_selected_game(self) -> None:
        if self.current_game_id is None:
            return
        g = self.db.get_game(self.current_game_id)
        if g and QMessageBox.question(
            self, t("del_game_title"), t("del_game_confirm", title=g["title"])
        ) == QMessageBox.Yes:
            self.db.delete_games([self.current_game_id])
            self.current_game_id = None
            self.reload_platforms()
            self.reload_games()
            QTimer.singleShot(0, self.reload_games)

    def _open_selected_rom_folder(self) -> None:
        if self.current_game_id is None:
            return
        g = self.db.get_game(self.current_game_id)
        if g:
            self._open_rom_folder(g)

    def _open_rom_folder(self, game: sqlite3.Row) -> None:
        folder = os.path.dirname(self.db.resolve(game["rom_path"]))
        if os.path.isdir(folder):
            os.startfile(folder)

    def _move_game_to_plat(self, game_id: int, target_plat_id: int) -> None:
        self.db.update_game(game_id, platform_id=target_plat_id)
        self.reload_platforms()
        self.reload_games()
        self.statusBar().showMessage("Gioco spostato nella sotto-sezione.", 3000)

    # ----- Azioni Menu & Piattaforme -----
    def _quick_new_platform(self, parent_id: Optional[int] = None) -> None:
        # I segnali Qt clicked/triggered passano bool (checked=False) come primo argomento
        if isinstance(parent_id, bool):
            parent_id = None
        dlg = PlatformDialog(self, self.db, parent_id=parent_id)
        if dlg.exec() == PlatformDialog.Accepted and dlg.saved_id is not None:
            self.current_platform_id = dlg.saved_id
            plat = self.db.get_platform(dlg.saved_id)
            if plat and plat["rom_dir"]:
                if QMessageBox.question(
                    self, t("scan_plat_title"), t("scan_plat_confirm", name=plat["name"])
                ) == QMessageBox.Yes:
                    self.db.scan_platform(dlg.saved_id)
            self.reload_platforms()
            self.reload_games()
            QTimer.singleShot(0, self.reload_games)

    def _edit_platform(self, plat_id: int) -> None:
        plat = self.db.get_platform(plat_id)
        if plat and PlatformDialog(self, self.db, plat).exec() == PlatformDialog.Accepted:
            new_plat = self.db.get_platform(plat_id)
            if new_plat and new_plat["rom_dir"]:
                changed_scan_cfg = (
                    new_plat["rom_dir"] != plat["rom_dir"]
                    or new_plat["extensions"] != plat["extensions"]
                    or new_plat["scan_subdirs"] != plat["scan_subdirs"]
                    or new_plat["keep_structure"] != plat["keep_structure"]
                    or not self.db.list_games(plat_id, include_subplatforms=False)
                )
                if changed_scan_cfg:
                    res = self.db.scan_platform(plat_id)
                    self.statusBar().showMessage(
                        f"{new_plat['name']}: +{res['added']} aggiunte, -{res['removed']} rimosse.",
                        5000,
                    )
            self.reload_platforms()
            self.reload_games()
            QTimer.singleShot(0, self.reload_games)

    def _delete_platform(self, plat_id: int) -> None:
        plat = self.db.get_platform(plat_id)
        if plat and QMessageBox.question(
            self,
            t("del_plat_title"),
            t("del_plat_confirm", name=plat["name"]),
        ) == QMessageBox.Yes:
            self.db.delete_platform(plat_id)
            if self.current_platform_id == plat_id:
                self.current_platform_id = None
            self.reload_platforms()
            self.reload_games()
            QTimer.singleShot(0, self.reload_games)

    def _open_platforms(self) -> None:
        dlg = PlatformManagerDialog(self, self.db)
        dlg.platforms_changed.connect(lambda: (self.reload_platforms(), self.reload_games()))
        dlg.exec()
        if dlg.changed:
            self.reload_platforms()
            self.reload_games()
            QTimer.singleShot(0, self.reload_games)

    def _open_emulators(self) -> None:
        EmulatorManagerDialog(self, self.db).exec()

    def _open_backup_restore(self) -> None:
        dlg = BackupRestoreDialog(self, self.db, self.config)
        dlg.exec()
        if dlg.restored:
            self.thumb_cache.clear()
            self.current_platform_id = self.config.get("last_platform_id")
            lang = self.config.get("ui_language", self.config.get("metadata_language", "en"))
            set_lang(lang)
            for c, act in self._lang_actions.items():
                act.setChecked(c == get_lang())
            self._retranslate_ui()
            self._apply_appearance()
            self._restore_window_state()
            self.reload_platforms()
            self.reload_games()
            QTimer.singleShot(0, self.reload_games)
            self.statusBar().showMessage("Backup ripristinato correttamente.", 5000)

    def _toggle_dynamic_blur(self, checked: bool) -> None:
        self.config.data["bg_dynamic_blur"] = bool(checked)
        self.config.save()
        if checked:
            self._refresh_dynamic_blur()
        else:
            self._dynamic_bg_pixmap = None
            self._apply_appearance()

    def _schedule_dynamic_blur(self, path: str) -> None:
        self._dynamic_bg_path = path or ""
        if not self.config.get("bg_dynamic_blur", True):
            if self._dynamic_bg_pixmap is not None:
                self._dynamic_bg_pixmap = None
                self._apply_appearance()
            return
        if not self._dynamic_bg_path:
            self._dynamic_blur_timer.stop()
            if self._dynamic_bg_pixmap is not None:
                self._dynamic_bg_pixmap = None
                self._apply_appearance()
            return
        # Se gia' in cache, applica subito senza attesa
        radius = int(self.config.get("bg_dynamic_blur_radius", 36) or 36)
        key = (self._dynamic_bg_path, radius)
        if key in self._dynamic_blur_cache:
            self._dynamic_blur_timer.stop()
            self._dynamic_blur_cache.move_to_end(key)
            self._dynamic_bg_pixmap = self._dynamic_blur_cache[key]
            self._dynamic_bg_radius = radius
            self._apply_appearance()
            return
        self._dynamic_blur_timer.start()

    def _refresh_dynamic_blur(self) -> None:
        if not self.config.get("bg_dynamic_blur", True) or not self._dynamic_bg_path:
            self._dynamic_bg_pixmap = None
            self._apply_appearance()
            return
        radius = int(self.config.get("bg_dynamic_blur_radius", 36) or 36)
        key = (self._dynamic_bg_path, radius)
        pm = self._dynamic_blur_cache.get(key)
        if pm is None:
            pm = _make_blurred_backdrop(self._dynamic_bg_path, radius)
            if pm is not None:
                self._dynamic_blur_cache[key] = pm
                if len(self._dynamic_blur_cache) > 32:
                    self._dynamic_blur_cache.popitem(last=False)
        else:
            self._dynamic_blur_cache.move_to_end(key)
        self._dynamic_bg_pixmap = pm
        self._dynamic_bg_radius = radius
        self._apply_appearance()

    def _apply_dynamic_blur_live(self, recompute_blur: bool = True) -> None:
        """Aggiornamento leggero in tempo reale per gli slider di sfocatura e intensita'."""
        if not self.config.get("bg_dynamic_blur", True):
            return
        dyn_l = bool(self.config.get("bg_dynamic_left", False))
        dyn_c = bool(self.config.get("bg_dynamic_center", True))
        dyn_r = bool(self.config.get("bg_dynamic_right", True))
        if not (dyn_l or dyn_c or dyn_r):
            return
        radius = int(self.config.get("bg_dynamic_blur_radius", 36) or 36)
        if recompute_blur and self._dynamic_bg_path and (
            self._dynamic_bg_pixmap is None or self._dynamic_bg_radius != radius
        ):
            key = (self._dynamic_bg_path, radius)
            pm = self._dynamic_blur_cache.get(key)
            if pm is None:
                pm = _make_blurred_backdrop(self._dynamic_bg_path, radius)
                if pm is not None:
                    self._dynamic_blur_cache[key] = pm
                    if len(self._dynamic_blur_cache) > 32:
                        self._dynamic_blur_cache.popitem(last=False)
            else:
                self._dynamic_blur_cache.move_to_end(key)
            self._dynamic_bg_pixmap = pm
            self._dynamic_bg_radius = radius

        dyn_pm = self._dynamic_bg_pixmap
        if dyn_pm is None:
            return
        dyn_int = max(10, min(80, int(self.config.get("bg_dynamic_blur_intensity", 50) or 50)))
        dyn_scrim = int(round(235 - dyn_int * 2.0))
        for w, enabled in (
            (getattr(self, "left_w", None), dyn_l),
            (getattr(self, "center_w", None), dyn_c),
            (getattr(self, "right_w", None), dyn_r),
        ):
            if w is not None and enabled:
                w.update_dynamic_live(dyn_pm, dyn_scrim)

    def _apply_appearance(self) -> None:
        theme = self.config.get("theme", "dark")
        accent = self.config.get("accent_color", "#f58a1f")
        divider = self.config.get("divider_color", "") or ""
        div_transp = bool(self.config.get("divider_transparent", False))
        eff_divider = "transparent" if div_transp else divider
        menubar_col = self.config.get("menubar_color", "") or ""
        card_col = self.config.get("card_color", "") or ""
        light = theme == "light"
        ac = QColor(accent)
        if not ac.isValid():
            ac = QColor("#f58a1f")
        acc = ac.name()
        on_acc = _contrast_on_accent(acc)

        apply_app_theme(QApplication.instance(), theme, acc, divider=eff_divider)
        hw = 0 if (div_transp and bool(self.config.get("lock_layout", True))) else 3
        for sp in (
            getattr(self, "main_splitter", None),
            getattr(self, "right_splitter", None),
            getattr(self, "preview_splitter", None),
        ):
            if sp is not None:
                sp.setHandleWidth(hw)

        if hasattr(self, "tree_delegate"):
            self.tree_delegate.light_theme = light
            self.tree_delegate.accent_color = acc
        self.delegate.light_theme = light
        self.delegate.accent_color = acc
        self.delegate.card_color = card_col
        self.grid.accent_color = acc

        if hasattr(self, "bg_scrape_lbl"):
            self.bg_scrape_lbl.setStyleSheet(f"color: {acc};")

        nav_css = (
            f"QPushButton {{ background-color: rgba(18, 21, 28, 185); color: #eef1f7; "
            f"border: 1px solid #3c4354; border-radius: 14px; font-weight: bold; padding: 0px; }} "
            f"QPushButton:hover {{ background-color: rgba({ac.red()}, {ac.green()}, {ac.blue()}, 220); color: {on_acc}; border-color: {acc}; }}"
        )
        if hasattr(self, "shot_prev_btn"):
            self.shot_prev_btn.setStyleSheet(nav_css)
        if hasattr(self, "shot_next_btn"):
            self.shot_next_btn.setStyleSheet(nav_css)
        if hasattr(self, "shot_add_btn"):
            self.shot_add_btn.accent_color = acc
            self.shot_add_btn.update()

        unified = bool(self.config.get("bg_unified_enabled", False))
        dyn_blur = bool(self.config.get("bg_dynamic_blur", True))
        dyn_l = dyn_blur and bool(self.config.get("bg_dynamic_left", False))
        dyn_c = dyn_blur and bool(self.config.get("bg_dynamic_center", True))
        dyn_r = dyn_blur and bool(self.config.get("bg_dynamic_right", True))
        any_dyn = dyn_l or dyn_c or dyn_r
        radius = int(self.config.get("bg_dynamic_blur_radius", 36) or 36)
        if any_dyn and self._dynamic_bg_path and (self._dynamic_bg_pixmap is None or self._dynamic_bg_radius != radius):
            key = (self._dynamic_bg_path, radius)
            pm = self._dynamic_blur_cache.get(key)
            if pm is None:
                pm = _make_blurred_backdrop(self._dynamic_bg_path, radius)
                if pm is not None:
                    self._dynamic_blur_cache[key] = pm
                    if len(self._dynamic_blur_cache) > 32:
                        self._dynamic_blur_cache.popitem(last=False)
            self._dynamic_bg_pixmap = pm
            self._dynamic_bg_radius = radius
        dyn_pm = self._dynamic_bg_pixmap if any_dyn else None
        dyn_int = max(10, min(80, int(self.config.get("bg_dynamic_blur_intensity", 50) or 50)))
        dyn_scrim = int(round(235 - dyn_int * 2.0))
        splitter_w = self.main_splitter if hasattr(self, "main_splitter") else None
        canvas_w = splitter_w if ((unified and not any_dyn) or dyn_pm is not None) else None
        if any_dyn:
            bg_left = "" if dyn_l else self.db.resolve(self.config.get("bg_left", ""))
            bg_center = "" if dyn_c else self.db.resolve(self.config.get("bg_center", ""))
            bg_right = "" if dyn_r else self.db.resolve(self.config.get("bg_right", ""))
            col_left = "" if dyn_l else (self.config.get("bg_color_left", "") or "")
            col_center = "" if dyn_c else (self.config.get("bg_color_center", "") or "")
            col_right = "" if dyn_r else (self.config.get("bg_color_right", "") or "")
            pm_left = dyn_pm if dyn_l else None
            pm_center = dyn_pm if dyn_c else None
            pm_right = dyn_pm if dyn_r else None
            scrim_left = (dyn_scrim if dyn_pm is not None else None) if dyn_l else None
            scrim_center = (dyn_scrim if dyn_pm is not None else None) if dyn_c else None
            scrim_right = (dyn_scrim if dyn_pm is not None else None) if dyn_r else None
        elif unified:
            u_img = self.db.resolve(self.config.get("bg_unified", ""))
            u_col = self.config.get("bg_color_unified", "") or ""
            bg_left = bg_center = bg_right = u_img
            col_left = col_center = col_right = u_col
            pm_left = pm_center = pm_right = None
            scrim_left = scrim_center = scrim_right = None
        else:
            bg_left = self.db.resolve(self.config.get("bg_left", ""))
            bg_center = self.db.resolve(self.config.get("bg_center", ""))
            bg_right = self.db.resolve(self.config.get("bg_right", ""))
            col_left = self.config.get("bg_color_left", "") or ""
            col_center = self.config.get("bg_color_center", "") or ""
            col_right = self.config.get("bg_color_right", "") or ""
            pm_left = pm_center = pm_right = None
            scrim_left = scrim_center = scrim_right = None

        bar_bg = "#e9edf3" if light else "#181b22"
        bar_border = divider if (divider and QColor(divider).isValid()) else ("#c8d0de" if light else "#232733")
        prev_bg = "#e9edf3" if light else "#101217"

        if hasattr(self, "_title_bar"):
            self._title_bar.apply_theme(
                light,
                border_col="transparent" if div_transp else (bar_border if (divider and QColor(divider).isValid()) else ""),
                bg_col=menubar_col,
            )

        def _sync_scroll_bg(w: QWidget, transparent: bool) -> None:
            for sb_fn in ("verticalScrollBar", "horizontalScrollBar"):
                sb = getattr(w, sb_fn, None)
                if callable(sb):
                    bar = sb()
                    if bar is not None:
                        bar.setAutoFillBackground(False)
                        p = bar.parentWidget()
                        if p is not None and p is not w:
                            p.setAutoFillBackground(False)
                            p.setStyleSheet("background: transparent;" if transparent else "")

        if hasattr(self, "left_w"):
            self.left_w.set_background(
                bg_left, light, apply_scrim=True, solid_color=col_left,
                canvas_widget=canvas_w, pixmap=pm_left, scrim_alpha=scrim_left,
            )
            if light:
                tab_unsel = "background-color: rgba(15, 23, 42, 12); border: 1px solid rgba(15, 23, 42, 24); color: #64748b;"
                tab_hov = "background-color: rgba(15, 23, 42, 20); border: 1px solid rgba(15, 23, 42, 42); color: #1e293b;"
                tab_press = f"background-color: rgba({ac.red()}, {ac.green()}, {ac.blue()}, 45); border: 1.5px solid {acc}; color: #0f172a; padding-top: 7px; padding-bottom: 5px;"
                tab_sel = f"background-color: rgba(255, 255, 255, 215); border: 1.5px solid {acc}; color: #0f172a;"
            else:
                tab_unsel = "background-color: rgba(15, 18, 26, 150); border: 1px solid rgba(255, 255, 255, 22); color: #8c96ab;"
                tab_hov = "background-color: rgba(28, 33, 46, 190); border: 1px solid rgba(255, 255, 255, 45); color: #dce2ee;"
                tab_press = f"background-color: rgba({ac.red()}, {ac.green()}, {ac.blue()}, 75); border: 1.5px solid {acc}; color: #ffffff; padding-top: 7px; padding-bottom: 5px;"
                tab_sel = f"background-color: rgba(28, 33, 46, 210); border: 1.5px solid {acc}; color: #ffffff;"
            left_tab_css = (
                "QWidget#LeftFavPage, QStackedWidget#LeftStack { background: transparent; border: none; } "
                f"QPushButton#LeftTabBtn {{ {tab_unsel} border-radius: 14px; font-size: 12.5px; font-weight: bold; padding: 6px 14px; }} "
                f"QPushButton#LeftTabBtn:hover {{ {tab_hov} }} "
                f"QPushButton#LeftTabBtn:checked {{ {tab_sel} }} "
                f"QPushButton#LeftTabBtn:pressed {{ {tab_press} }}"
            )
            if self.left_w.has_background():
                self.left_w.setStyleSheet(left_tab_css)
                self.tree.setAttribute(Qt.WA_StyledBackground, False)
                self.tree.viewport().setAutoFillBackground(False)
                self.tree.setStyleSheet("QTreeWidget { background: transparent; border: none; }")
                self.tree.viewport().setStyleSheet("background: transparent;")
                self.fav_tree.setAttribute(Qt.WA_StyledBackground, False)
                self.fav_tree.viewport().setAutoFillBackground(False)
                self.fav_tree.setStyleSheet("QTreeWidget { background: transparent; border: none; }")
                self.fav_tree.viewport().setStyleSheet("background: transparent;")
                _sync_scroll_bg(self.tree, True)
                _sync_scroll_bg(self.fav_tree, True)
            else:
                self.left_w.setStyleSheet(f"QWidget#LeftPanel {{ background-color: {'#ffffff' if light else '#181b22'}; }} {left_tab_css}")
                self.tree.setAttribute(Qt.WA_StyledBackground, True)
                self.tree.viewport().setAutoFillBackground(True)
                self.fav_tree.setAttribute(Qt.WA_StyledBackground, True)
                self.fav_tree.viewport().setAutoFillBackground(True)
                self.tree.setStyleSheet("")
                self.tree.viewport().setStyleSheet("")
                self.fav_tree.setStyleSheet("")
                self.fav_tree.viewport().setStyleSheet("")
                _sync_scroll_bg(self.tree, False)
                _sync_scroll_bg(self.fav_tree, False)

        if hasattr(self, "center_w"):
            self.center_w.set_background(
                bg_center, light, apply_scrim=True, solid_color=col_center,
                canvas_widget=canvas_w, pixmap=pm_center, scrim_alpha=scrim_center,
            )
            self.header_bar.setStyleSheet(
                "QWidget#CenterHeaderBar, QWidget#CenterHeaderBar QLabel { background-color: transparent; }"
            )
            if self.center_w.has_background():
                self.center_w.setStyleSheet("")
                self.grid.setStyleSheet("QListView#RomDropGrid { background-color: transparent; border: none; }")
                self.grid.viewport().setStyleSheet("background-color: transparent;")
                _sync_scroll_bg(self.grid, True)
            else:
                self.center_w.setStyleSheet(f"background-color: {'#ffffff' if light else '#181b22'};")
                self.grid.setStyleSheet("")
                self.grid.viewport().setStyleSheet("")
                _sync_scroll_bg(self.grid, False)

        if hasattr(self, "right_w"):
            self.right_w.set_background(
                bg_right, light, apply_scrim=True, solid_color=col_right,
                canvas_widget=canvas_w, pixmap=pm_right, scrim_alpha=scrim_right,
            )
            acc_hover = ac.lighter(112).name()
            acc_pressed = ac.darker(118).name()
            acc_edge = ac.lighter(125).name()
            play_css = (
                f"QPushButton#PlayButton {{ background-color: {acc}; color: {on_acc}; border: 1px solid {acc_edge}; border-radius: 7px; font-size: 12pt; font-weight: bold; padding: 11px 16px; }} "
                f"QPushButton#PlayButton:hover {{ background-color: {acc_hover}; border-color: {acc_edge}; }} "
                f"QPushButton#PlayButton:pressed {{ background-color: {acc_pressed}; border-color: {acc}; padding-top: 12px; padding-bottom: 10px; }} "
                f"QPushButton#PlayButton:disabled {{ background-color: rgba({ac.red()}, {ac.green()}, {ac.blue()}, 85); color: rgba(28, 16, 0, 110); border: 1px solid rgba({ac.red()}, {ac.green()}, {ac.blue()}, 60); }} "
            )
            act_btn_css = (
                f"QPushButton#DetailActionBtn {{ background-color: rgba(255, 255, 255, 210); color: #0f172a; border: 1px solid rgba(15, 23, 42, 34); border-radius: 6px; padding: 5px 10px; }} "
                f"QPushButton#DetailActionBtn:hover {{ background-color: #ffffff; border: 1px solid {acc}; color: #0f172a; }} "
                f"QPushButton#DetailActionBtn:pressed {{ background-color: rgba({ac.red()}, {ac.green()}, {ac.blue()}, 45); border: 1.5px solid {acc}; color: #0f172a; padding-top: 6px; padding-bottom: 4px; }} "
                "QPushButton#DetailActionBtn:disabled { background-color: rgba(241, 245, 249, 150); color: rgba(26, 29, 36, 95); border: 1px solid rgba(15, 23, 42, 18); } "
            ) if light else (
                f"QPushButton#DetailActionBtn {{ background-color: rgba(28, 33, 46, 195); color: #eef1f7; border: 1px solid rgba(255, 255, 255, 36); border-radius: 6px; padding: 5px 10px; }} "
                f"QPushButton#DetailActionBtn:hover {{ background-color: rgba(40, 47, 65, 220); border: 1px solid {acc}; color: #ffffff; }} "
                f"QPushButton#DetailActionBtn:pressed {{ background-color: rgba({ac.red()}, {ac.green()}, {ac.blue()}, 75); border: 1.5px solid {acc}; color: #ffffff; padding-top: 6px; padding-bottom: 4px; }} "
                "QPushButton#DetailActionBtn:disabled { background-color: rgba(20, 24, 34, 130); color: rgba(230, 232, 238, 85); border: 1px solid rgba(255, 255, 255, 16); } "
            )
            card_css = play_css + act_btn_css + (
                "QLabel#DetailGameTitle { font-size: 19px; font-weight: bold; color: #0f172a; background: transparent; } "
                "QWidget#DetailTabPage { background: transparent; } "
                "QStackedWidget#DetailStack { border: none; background: transparent; } "
                f"QPushButton#DetailTabBtn {{ background: transparent; color: #64748b; font-size: 12.5px; font-weight: bold; padding: 6px 14px; border: none; border-bottom: 2px solid transparent; border-radius: 0px; }} "
                "QPushButton#DetailTabBtn:hover { color: #0f172a; } "
                f"QPushButton#DetailTabBtn:checked {{ color: #0f172a; border-bottom: 2px solid {acc}; }} "
                "QFrame#DetailCard { background-color: rgba(255, 255, 255, 195); border: 1px solid rgba(15, 23, 42, 26); border-radius: 8px; } "
                "QFrame#DetailStatBox { background-color: rgba(15, 23, 42, 10); border: 1px solid rgba(15, 23, 42, 18); border-radius: 6px; } "
                "QFrame#DetailMetaRow { background: transparent; border: none; border-bottom: 1px solid rgba(15, 23, 42, 18); } "
                "QFrame#DetailMetaRowLast { background: transparent; border: none; } "
                "QLabel#DetailCardHdr { font-size: 11px; font-weight: bold; letter-spacing: 0.8px; color: #475569; background: transparent; } "
                "QLabel#DetailMetaKey { font-size: 13px; color: #64748b; background: transparent; } "
                "QLabel#DetailMetaVal { font-size: 13px; font-weight: 600; color: #0f172a; background: transparent; } "
                "QLabel#DetailStatVal { font-size: 13.5px; font-weight: bold; color: #0f172a; background: transparent; } "
                "QLabel#DetailDescText { font-size: 13px; color: #1e293b; background: transparent; }"
            ) if light else (
                "QLabel#DetailGameTitle { font-size: 19px; font-weight: bold; color: #ffffff; background: transparent; } "
                "QWidget#DetailTabPage { background: transparent; } "
                "QStackedWidget#DetailStack { border: none; background: transparent; } "
                f"QPushButton#DetailTabBtn {{ background: transparent; color: #8c96ab; font-size: 12.5px; font-weight: bold; padding: 6px 14px; border: none; border-bottom: 2px solid transparent; border-radius: 0px; }} "
                "QPushButton#DetailTabBtn:hover { color: #ffffff; } "
                f"QPushButton#DetailTabBtn:checked {{ color: #ffffff; border-bottom: 2px solid {acc}; }} "
                "QFrame#DetailCard { background-color: rgba(28, 33, 46, 175); border: 1px solid rgba(255, 255, 255, 24); border-radius: 8px; } "
                "QFrame#DetailStatBox { background-color: rgba(15, 18, 26, 150); border: 1px solid rgba(255, 255, 255, 18); border-radius: 6px; } "
                "QFrame#DetailMetaRow { background: transparent; border: none; border-bottom: 1px solid rgba(255, 255, 255, 16); } "
                "QFrame#DetailMetaRowLast { background: transparent; border: none; } "
                "QLabel#DetailCardHdr { font-size: 11px; font-weight: bold; letter-spacing: 0.8px; color: #9aa4b8; background: transparent; } "
                "QLabel#DetailMetaKey { font-size: 13px; color: #8f99ad; background: transparent; } "
                "QLabel#DetailMetaVal { font-size: 13px; font-weight: 600; color: #f3f5fa; background: transparent; } "
                "QLabel#DetailStatVal { font-size: 13.5px; font-weight: bold; color: #ffffff; background: transparent; } "
                "QLabel#DetailDescText { font-size: 13px; color: #dce2ee; background: transparent; }"
            )
            if self.right_w.has_background():
                self.right_splitter.setStyleSheet("QSplitter#RightSplitter { background-color: transparent; }")
                self.preview_box.setStyleSheet("QWidget#PreviewBox, QWidget#PreviewBox QLabel { background-color: transparent; }")
                self.preview_splitter.setStyleSheet("background-color: transparent;")
                self.banner_lbl.setStyleSheet("background-color: transparent;")
                self.preview_lbl.setStyleSheet("background-color: transparent;")
                self.scroll_area.setAutoFillBackground(False)
                self.scroll_area.viewport().setAutoFillBackground(False)
                self.detail_inner.setAutoFillBackground(False)
                self.detail_stack.setAutoFillBackground(False)
                self.scroll_area.setStyleSheet("QScrollArea { background-color: transparent; border: none; }")
                self.scroll_area.viewport().setObjectName("DetailScrollViewport")
                self.scroll_area.viewport().setStyleSheet("QWidget#DetailScrollViewport { background-color: transparent; }")
                self.detail_inner.setStyleSheet(f"QWidget#DetailInner {{ background-color: transparent; }} {card_css}")
                _sync_scroll_bg(self.scroll_area, True)
            else:
                self.scroll_area.setAutoFillBackground(True)
                self.scroll_area.viewport().setAutoFillBackground(True)
                self.detail_inner.setAutoFillBackground(False)
                self.right_splitter.setStyleSheet("")
                self.preview_box.setStyleSheet(f"QWidget#PreviewBox {{ background-color: {prev_bg}; }}")
                self.preview_splitter.setStyleSheet("")
                self.banner_lbl.setStyleSheet("")
                self.preview_lbl.setStyleSheet("")
                self.scroll_area.setStyleSheet("")
                self.scroll_area.viewport().setStyleSheet("")
                self.detail_inner.setStyleSheet(card_css)
                _sync_scroll_bg(self.scroll_area, False)

        self.tree.viewport().update()
        self.grid.viewport().update()
        if hasattr(self, "fav_tree") and self.fav_tree.topLevelItemCount() > 0:
            self.fav_tree.topLevelItem(0).setIcon(0, make_star_icon(acc))

    def _on_right_splitter_moved(self, _pos: int, _index: int) -> None:
        win_cfg = self.config.data.setdefault("window", {})
        win_cfg["right_splitter"] = self.right_splitter.sizes()
        self.config.save()
        self._update_preview_pixmap()

    def _apply_layout_lock(self) -> None:
        locked = bool(self.config.get("lock_layout", True))
        for sp in (
            getattr(self, "main_splitter", None),
            getattr(self, "right_splitter", None),
            getattr(self, "preview_splitter", None),
        ):
            if sp is None:
                continue
            cur = Qt.ArrowCursor if locked else (Qt.SplitHCursor if sp.orientation() == Qt.Horizontal else Qt.SplitVCursor)
            for i in range(1, sp.count()):
                h = sp.handle(i)
                if h is not None:
                    h.setEnabled(not locked)
                    h.setCursor(cur)

    def _reset_layout_defaults(self) -> None:
        def_main = [302, 1120, 498]
        def_right = [520, 462]
        def_pv = [195, 325]
        self.main_splitter.setSizes(def_main)
        self.right_splitter.setSizes(def_right)
        self.preview_splitter.setSizes(def_pv)
        win_cfg = self.config.data.setdefault("window", {})
        win_cfg["splitter"] = def_main
        win_cfg["right_splitter"] = def_right
        win_cfg["preview_splitter"] = def_pv
        self.config.data["lock_layout"] = True
        self.config.save()
        self._apply_layout_lock()
        self.grid._update_center_margins()
        self.grid.doItemsLayout()
        self._update_preview_pixmap()
        self._sync_topbar_zoom_align()

    def _open_lock_layout(self) -> None:
        dlg = getattr(self, "_lock_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        dlg = LayoutLockDialog(
            self,
            self.config,
            on_apply=self._apply_layout_lock,
            on_reset=self._reset_layout_defaults,
        )
        dlg.finished.connect(lambda _r=0: setattr(self, "_lock_dlg", None))
        self._lock_dlg = dlg
        dlg.show()

    def _open_appearance(self) -> None:
        dlg = AppearanceDialog(
            self, self.db, self.config,
            on_apply=self._apply_appearance,
            on_blur_live=self._apply_dynamic_blur_live,
        )
        dlg.exec()
        self._apply_appearance()

    def _open_user_guide(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        lang = get_lang()
        candidates = [f"manual_{lang}.html"] if lang != "it" else []
        candidates.append("manual.html")
        base_dirs = [
            os.path.join(self.db.root, "docs"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs"),
        ]
        for bdir in base_dirs:
            for name in candidates:
                fp = os.path.join(bdir, name)
                if os.path.isfile(fp):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(fp)))
                    return
        QMessageBox.information(self, t("menu_info"), "Manuale non trovato (docs/manual.html).")

    def _open_about_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog
        dlg = QDialog(self)
        dlg.setWindowTitle(t("about_title"))
        dlg.setFixedWidth(360)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(10)

        logo_lbl = QLabel()
        logo_lbl.setAlignment(Qt.AlignCenter)
        icon_path = os.path.join(self.db.root, "icons", "app_icon.png")
        if not os.path.isfile(icon_path):
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "app_icon.png")
        if os.path.isfile(icon_path):
            pm = QPixmap(icon_path).scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo_lbl.setPixmap(pm)
        lay.addWidget(logo_lbl)

        title_lbl = QLabel("EasyLauncher")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setStyleSheet("font-size: 20px; font-weight: 700;")
        lay.addWidget(title_lbl)

        acc = self.config.get("accent_color", "#f58a1f") or "#f58a1f"
        ver_lbl = QLabel(t("about_version", ver="1.0"))
        ver_lbl.setAlignment(Qt.AlignCenter)
        ver_lbl.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {acc};")
        lay.addWidget(ver_lbl)

        sub_lbl = QLabel(t("about_subtitle"))
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setObjectName("MutedHint")
        sub_lbl.setWordWrap(True)
        lay.addWidget(sub_lbl)

        lay.addSpacing(8)
        close_btn = QPushButton(t("dlg_btn_close"))
        close_btn.setObjectName("AccentButton")
        close_btn.setMinimumHeight(34)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn)

        dlg.exec()

    def _toggle_start_fullscreen(self, checked: bool) -> None:
        self.config.data["start_fullscreen"] = bool(checked)
        self.config.save()

    def _on_bigscreen_game_launched(self, gid: int, proc) -> None:
        if proc is not None:
            self._active_sessions.append((gid, proc, time.monotonic()))
            if not self._session_timer.isActive():
                self._session_timer.start()

    def _open_bigscreen(self) -> None:
        if getattr(self, "_bigscreen_win", None) is not None:
            self._bigscreen_win.raise_()
            self._bigscreen_win.activateWindow()
            return
        from bigscreen import BigScreenWindow

        win = BigScreenWindow(
            self.db,
            self.config,
            GameListModel(self.db),
            self.thumb_cache,
            _make_blurred_backdrop,
            on_game_launched=self._on_bigscreen_game_launched,
        )
        self._bigscreen_win = win

        def _on_closed() -> None:
            self._bigscreen_win = None
            self.reload_favorites()
            self.reload_games()

        win.destroyed.connect(_on_closed)
        win.showFullScreen()

    def _open_batch_scrape(self, plat_id: Optional[int] = None) -> None:
        if isinstance(plat_id, bool):
            plat_id = None
        target = plat_id if isinstance(plat_id, int) else self.current_platform_id
        self.config.load()
        dlg = ScrapeBatchDialog(self, self.db, target, config=self.config, initial_tab=0)

        if self._bg_scrape_worker and self._bg_scrape_worker.isRunning():
            worker = self._bg_scrape_worker
            self._bg_scrape_worker = None
            try:
                worker.progress.disconnect(self._on_bg_scrape_progress)
            except (TypeError, RuntimeError):
                pass
            try:
                worker.completed.disconnect(self._on_bg_scrape_completed)
            except (TypeError, RuntimeError):
                pass
            self.bg_bar_w.setVisible(False)
            self.statusBar().setVisible(False)
            idx, total, msg = self._bg_scrape_last
            dlg.attach_running_worker(worker, idx, total, msg)

        dlg.exec()
        if getattr(dlg, "continued_in_background", False) and dlg.worker is not None:
            self._attach_bg_scrape(dlg.worker)
        elif dlg.updated_any or (self._bg_scrape_last[0] > 0):
            self.thumb_cache.clear()
            self.reload_games()

    def _attach_bg_scrape(self, worker) -> None:
        self._bg_scrape_worker = worker
        worker.setParent(self)
        worker.progress.connect(self._on_bg_scrape_progress)
        worker.completed.connect(self._on_bg_scrape_completed)
        idx, total, _ = self._bg_scrape_last
        if total > 0:
            self.bg_scrape_pbar.setMaximum(total)
            self.bg_scrape_pbar.setValue(idx)
            self.bg_scrape_lbl.setText(f"Metadati: {idx}/{total}")
        else:
            self.bg_scrape_lbl.setText("Download metadati in background…")
            self.bg_scrape_pbar.setValue(0)
        self.bg_scrape_stop_btn.setEnabled(True)
        self.bg_bar_w.setVisible(True)
        self.statusBar().setVisible(True)

    def _on_bg_scrape_progress(self, idx: int, total: int, msg: str) -> None:
        self._bg_scrape_last = (idx, total, msg)
        self.bg_scrape_pbar.setMaximum(max(total, 1))
        self.bg_scrape_pbar.setValue(idx)
        self.bg_scrape_lbl.setText(f"Metadati: {idx}/{total}")

    def _stop_bg_scrape(self) -> None:
        if self._bg_scrape_worker and self._bg_scrape_worker.isRunning():
            self._bg_scrape_worker.stop()
            self.bg_scrape_stop_btn.setEnabled(False)
            self.bg_scrape_lbl.setText("Interruzione in corso…")

    def _on_bg_scrape_completed(self, stats: dict) -> None:
        self.bg_bar_w.setVisible(False)
        self.statusBar().setVisible(False)
        self._bg_scrape_worker = None
        self.thumb_cache.clear()
        self.reload_games()
        self.statusBar().showMessage(
            f"Download metadati completato — Analizzati: {stats.get('total', 0)}, "
            f"Aggiornati: {stats.get('updated', 0)}, Non trovati: {stats.get('missed', 0)}",
            8000,
        )

    def _open_settings(self) -> None:
        self.config.load()
        dlg = ScrapeBatchDialog(self, self.db, self.current_platform_id, config=self.config, initial_tab=1)
        dlg.exec()
        if dlg.updated_any:
            self.thumb_cache.clear()
            self.reload_games()

    def _scan_platform(self, plat_id: int) -> None:
        res = self.db.scan_platform(plat_id)
        self.reload_platforms()
        self.reload_games()
        self.statusBar().showMessage(
            f"Scansione completata: +{res['added']} aggiunti, -{res['removed']} rimossi.", 5000
        )

    def _scan_current_or_all(self) -> None:
        if self.current_platform_id is not None:
            self._scan_platform(self.current_platform_id)
        else:
            self._scan_all_platforms()

    def _scan_all_platforms(self) -> None:
        added, removed = 0, 0
        for p in self.db.list_platforms():
            res = self.db.scan_platform(p["id"])
            added += res["added"]
            removed += res["removed"]
        self.reload_platforms()
        self.reload_games()
        QMessageBox.information(
            self, "Scansione ROM", f"Scansione di tutte le piattaforme completata.\nAggiunti: {added}\nRimossi: {removed}"
        )

    def _add_roms_manually(self) -> None:
        if self.current_platform_id is None:
            QMessageBox.information(
                self, "Seleziona piattaforma", "Seleziona prima una piattaforma nell'albero a sinistra."
            )
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Seleziona file ROM", self.db.root)
        if not files:
            return
        added = self.db.add_games(self.current_platform_id, files)
        self.db.link_existing_media(self.current_platform_id)
        self.reload_platforms()
        self.reload_games()
        self.statusBar().showMessage(f"Aggiunti {added} giochi.", 4000)

    def _valid_dropped_roms(self, mime_data) -> list[str]:
        if self.current_platform_id is None or mime_data is None or not mime_data.hasUrls():
            return []
        plat = self.db.get_platform(self.current_platform_id)
        if plat is None:
            return []
        exts: set[str] = set()
        for raw_ext in (plat["extensions"] or "").split(","):
            e = raw_ext.strip().lower()
            if e:
                exts.add(e if e.startswith(".") else "." + e)
        valid: list[str] = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = os.path.normpath(url.toLocalFile())
            if not path or not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if not exts or ext in exts:
                valid.append(path)
        return valid

    def _on_roms_dropped(self, files: list[str]) -> None:
        if self.current_platform_id is None or not files:
            return
        plat = self.db.get_platform(self.current_platform_id)
        added = self.db.add_games(self.current_platform_id, files)
        self.db.link_existing_media(self.current_platform_id)
        self.reload_platforms()
        self.reload_games()
        pname = plat["name"] if plat else "piattaforma"
        self.statusBar().showMessage(f"Aggiunti {added} giochi a {pname}.", 4000)

    def dragEnterEvent(self, event) -> None:
        if self._valid_dropped_roms(event.mimeData()):
            event.acceptProposedAction()
            self.grid._set_hover(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._valid_dropped_roms(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.grid._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self.grid._set_hover(False)
        files = self._valid_dropped_roms(event.mimeData())
        if files:
            self._on_roms_dropped(files)
            event.acceptProposedAction()
        else:
            event.ignore()
