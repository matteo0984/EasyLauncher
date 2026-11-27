"""
bigscreen.py - Modalita' Schermo Intero semplificata stile Console / Pad (XInput + Tastiera).
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import time
from typing import Callable, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPointF,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from database import Config, Database
from dialogs import ChooseFavoriteFolderDialog
from i18n import t
from launcher import LaunchResult, launch_game


def _make_abxy_icon(active: str, size: int = 24) -> QIcon:
    """Disegna i 4 tasti a rombo (Y alto, X sx, B dx, A basso) riempiendo di bianco solo quello attivo."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    cx, cy = size / 2.0, size / 2.0
    offset = size * 0.27
    r = size * 0.175
    positions = {
        "y": (cx, cy - offset),
        "x": (cx - offset, cy),
        "b": (cx + offset, cy),
        "a": (cx, cy + offset),
    }
    act = active.lower()
    for k, (px, py) in positions.items():
        if k == act:
            p.setBrush(QColor("#ffffff"))
            p.setPen(QPen(QColor(16, 20, 30, 180), max(1.0, size * 0.05)))
        else:
            p.setBrush(QColor(22, 27, 38, 220))
            p.setPen(QPen(QColor(195, 205, 220, 190), max(1.0, size * 0.05)))
        p.drawEllipse(QPointF(px, py), r, r)
    p.end()
    return QIcon(pm)


def _build_hints_bar(
    items: list[tuple[Optional[str], str]],
    *,
    framed: bool = True,
    font_size: int = 12,
    icon_size: int = 22,
) -> QFrame:
    """Crea una barra di suggerimenti con le icone dei 4 tasti colorate."""
    bar = QFrame()
    if framed:
        bar.setStyleSheet(
            "QFrame { background-color: rgba(14, 17, 24, 195); "
            "border: 1px solid rgba(255, 255, 255, 20); border-radius: 10px; }"
        )
    else:
        bar.setStyleSheet("QFrame { background: transparent; border: none; }")
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(14 if framed else 4, 6 if framed else 2, 14 if framed else 4, 6 if framed else 2)
    lay.setSpacing(8)
    lay.addStretch(1)
    for idx, (btn_key, text) in enumerate(items):
        if idx > 0:
            sep = QLabel("•")
            sep.setStyleSheet(f"color: #64748b; font-size: {font_size}px; border: none; background: transparent;")
            lay.addWidget(sep)
        if btn_key:
            ic_lbl = QLabel()
            ic_lbl.setPixmap(_make_abxy_icon(btn_key, icon_size).pixmap(icon_size, icon_size))
            ic_lbl.setStyleSheet("border: none; background: transparent;")
            lay.addWidget(ic_lbl)
        txt_lbl = QLabel(text)
        txt_lbl.setStyleSheet(
            f"color: #cbd5e1; font-size: {font_size}px; font-weight: 600; border: none; background: transparent;"
        )
        lay.addWidget(txt_lbl)
    lay.addStretch(1)
    return bar


# ---------------------------------------------------------------------------
# XInput Gamepad Poller (Windows, zero dipendenze esterne)
# ---------------------------------------------------------------------------

class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint32),
        ("Gamepad", _XInputGamepad),
    ]


class XInputPoller(QObject):
    """Legge i controller Xbox/XInput ogni 30ms ed emette eventi di navigazione e tasti."""

    connected_changed = Signal(bool)
    nav = Signal(str)       # "up", "down", "left", "right"
    action = Signal(str)    # "a", "b", "x", "y", "lb", "rb", "start", "back"
    b_hold_changed = Signal(float)

    _DEADZONE = 8500
    _BTN_MAP = (
        (0x1000, "a"),
        (0x2000, "b"),
        (0x4000, "x"),
        (0x8000, "y"),
        (0x0100, "lb"),
        (0x0200, "rb"),
        (0x0010, "start"),
        (0x0020, "back"),
    )
    _DPAD_MAP = (
        (0x0001, "up"),
        (0x0002, "down"),
        (0x0004, "left"),
        (0x0008, "right"),
    )

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._xinput = self._load_xinput()
        self._connected = False
        self._prev_buttons = 0
        self._hold_dir: Optional[str] = None
        self._hold_next_at: float = 0.0
        self._paused = False
        self._sync_on_resume = True
        self._b_hold_start: Optional[float] = None
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)

    @staticmethod
    def _load_xinput():
        for dll in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
            try:
                lib = ctypes.WinDLL(dll)
                lib.XInputGetState.argtypes = [ctypes.c_uint32, ctypes.POINTER(_XInputState)]
                lib.XInputGetState.restype = ctypes.c_uint32
                return lib
            except OSError:
                continue
        return None

    def start(self) -> None:
        if self._xinput is not None:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._hold_dir = None
            if self._b_hold_start is not None:
                self._b_hold_start = None
                self.b_hold_changed.emit(0.0)
        else:
            self._sync_on_resume = True

    def _poll(self) -> None:
        if self._xinput is None or self._paused:
            return
        state = _XInputState()
        found = False
        for idx in range(4):
            if self._xinput.XInputGetState(idx, ctypes.byref(state)) == 0:
                found = True
                break
        if found != self._connected:
            self._connected = found
            self.connected_changed.emit(found)
        if not found:
            self._prev_buttons = 0
            self._hold_dir = None
            if self._b_hold_start is not None:
                self._b_hold_start = None
                self.b_hold_changed.emit(0.0)
            return

        gp = state.Gamepad
        btns = int(gp.wButtons)
        if self._sync_on_resume:
            self._prev_buttons = btns
            self._sync_on_resume = False
            self._hold_dir = None
            return
        pressed = btns & ~self._prev_buttons
        self._prev_buttons = btns

        for mask, act in self._BTN_MAP:
            if pressed & mask:
                self.action.emit(act)

        now_b = time.monotonic()
        if btns & 0x2000:
            if self._b_hold_start is None:
                self._b_hold_start = now_b
            self.b_hold_changed.emit(now_b - self._b_hold_start)
        elif self._b_hold_start is not None:
            self._b_hold_start = None
            self.b_hold_changed.emit(0.0)

        cur_dir: Optional[str] = None
        for mask, d in self._DPAD_MAP:
            if btns & mask:
                cur_dir = d
                break
        if cur_dir is None:
            lx, ly = int(gp.sThumbLX), int(gp.sThumbLY)
            if abs(lx) > abs(ly):
                if lx > self._DEADZONE:
                    cur_dir = "right"
                elif lx < -self._DEADZONE:
                    cur_dir = "left"
            else:
                if ly > self._DEADZONE:
                    cur_dir = "up"
                elif ly < -self._DEADZONE:
                    cur_dir = "down"

        now = time.monotonic()
        if cur_dir is None:
            self._hold_dir = None
        elif cur_dir != self._hold_dir:
            self._hold_dir = cur_dir
            self._hold_next_at = now + 0.30
            self.nav.emit(cur_dir)
        elif now >= self._hold_next_at:
            self._hold_next_at = now + 0.105
            self.nav.emit(cur_dir)


# ---------------------------------------------------------------------------
# Delegate Card Piattaforme per Modalita' Schermo Intero
# ---------------------------------------------------------------------------

class _BigPlatformDelegate(QStyledItemDelegate):
    def __init__(self, accent_color: str = "#f58a1f", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.accent_color = accent_color

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(250, 220)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = option.rect.adjusted(6, 6, -6, -6)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.setBrush(QColor(34, 41, 58, 225))
            painter.setPen(QPen(QColor(self.accent_color), 2.5))
        elif hovered:
            painter.setBrush(QColor(28, 33, 46, 200))
            painter.setPen(QPen(QColor(255, 255, 255, 55), 1.5))
        else:
            painter.setBrush(QColor(20, 24, 34, 185))
            painter.setPen(QPen(QColor(255, 255, 255, 24), 1))
        painter.drawRoundedRect(rect, 14, 14)

        icon: QIcon = index.data(Qt.DecorationRole)
        icon_rect = QRect(rect.left() + 16, rect.top() + 14, rect.width() - 32, 118)
        if isinstance(icon, QIcon) and not icon.isNull():
            pm = icon.pixmap(QSize(116, 116))
            if not pm.isNull():
                pm = pm.scaled(icon_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pm.setDevicePixelRatio(1.0)
                lw, lh = pm.width(), pm.height()
                x = icon_rect.left() + int((icon_rect.width() - lw) // 2)
                y = icon_rect.top() + int((icon_rect.height() - lh) // 2)
                painter.drawPixmap(x, y, pm)

        title = str(index.data(Qt.DisplayRole) or "")
        count_txt = str(index.data(Qt.UserRole + 2) or "")

        painter.setPen(QColor("#ffffff") if selected else QColor("#e6e8ee"))
        painter.setFont(QFont("Segoe UI", 12, QFont.Bold))
        t_rect = QRect(rect.left() + 12, rect.top() + 138, rect.width() - 24, 32)
        elided = QFontMetrics(painter.font()).elidedText(title, Qt.ElideRight, t_rect.width())
        painter.drawText(t_rect, Qt.AlignHCenter | Qt.AlignVCenter, elided)

        if count_txt:
            painter.setPen(QColor(self.accent_color) if selected else QColor("#8f99ad"))
            painter.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
            c_rect = QRect(rect.left() + 12, rect.top() + 170, rect.width() - 24, 24)
            painter.drawText(c_rect, Qt.AlignHCenter | Qt.AlignVCenter, count_txt)

        painter.restore()


# ---------------------------------------------------------------------------
# Modello e Delegate misto (Sottocartelle prima in alto a SX + Giochi)
# ---------------------------------------------------------------------------

ROLE_ENTRY_KIND = Qt.UserRole + 10
ROLE_FOLDER_COUNT = Qt.UserRole + 11


class _BigGridModel(QAbstractListModel):
    """Lista mista: prima eventuali sottocartelle della piattaforma, poi i giochi."""

    def __init__(self, db: Database, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.db = db
        self._entries: list[dict] = []
        self._id_to_row: dict[int, int] = {}

    def set_entries(self, entries: list[dict]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self._id_to_row = {
            e["game"]["id"]: idx
            for idx, e in enumerate(self._entries)
            if e.get("kind") == "game"
        }
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        ent = self._entries[index.row()]
        kind = ent.get("kind")
        if role == ROLE_ENTRY_KIND:
            return kind
        if kind == "folder":
            p = ent["platform"]
            if role == Qt.DisplayRole:
                return p["name"]
            if role == Qt.DecorationRole:
                return ent.get("icon")
            if role == ROLE_FOLDER_COUNT:
                return ent.get("count", 0)
            return None
        g = ent["game"]
        from ui_main import ROLE_BOXART, ROLE_FAVORITE
        if role == Qt.DisplayRole:
            return g["title"]
        if role == ROLE_BOXART:
            return self.db.resolve(g["boxart_path"]) if g["boxart_path"] else ""
        if role == ROLE_FAVORITE:
            return bool(g["favorite"])
        return None

    def entry_at(self, row: int) -> Optional[dict]:
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def game_at(self, row: int):
        ent = self.entry_at(row)
        return ent["game"] if (ent and ent.get("kind") == "game") else None

    def row_of(self, game_id: int) -> Optional[int]:
        return self._id_to_row.get(game_id)


class _BigGridDelegate(QStyledItemDelegate):
    """Disegna sia le sottocartelle (in alto a SX) sia le copertine dei giochi."""

    def __init__(self, box_delegate, accent_color: str = "#f58a1f", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.box_delegate = box_delegate
        self.accent_color = accent_color

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return self.box_delegate.sizeHint(option, index)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if index.data(ROLE_ENTRY_KIND) != "folder":
            self.box_delegate.paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = option.rect.adjusted(1, 1, -1, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            painter.setBrush(QColor(47, 54, 71, 235))
            painter.setPen(QPen(QColor(self.accent_color), 2.2))
            painter.drawRoundedRect(rect, 8, 8)
        elif hovered:
            painter.setBrush(QColor(35, 39, 51, 215))
            painter.setPen(QPen(QColor(255, 255, 255, 45), 1))
            painter.drawRoundedRect(rect, 8, 8)

        art_rect = QRect(rect.left() + 3, rect.top() + 3, rect.width() - 6, self.box_delegate.art_height - 6)
        painter.setBrush(QColor(24, 29, 42, 210))
        painter.setPen(QPen(QColor(self.accent_color if selected else "#343b4d"), 1.2))
        painter.drawRoundedRect(art_rect, 7, 7)

        icon: QIcon = index.data(Qt.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            ic_side = max(48, int(min(art_rect.width(), art_rect.height()) * 0.62))
            pm = icon.pixmap(QSize(ic_side, ic_side))
            if not pm.isNull():
                pm = pm.scaled(QSize(ic_side, ic_side), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pm.setDevicePixelRatio(1.0)
                lw, lh = pm.width(), pm.height()
                x = art_rect.left() + int((art_rect.width() - lw) // 2)
                y = art_rect.top() + int((art_rect.height() - lh) // 2) - 10
                painter.drawPixmap(x, y, pm)

        cnt = int(index.data(ROLE_FOLDER_COUNT) or 0)
        painter.setPen(QColor(self.accent_color) if selected else QColor("#9aa4b8"))
        painter.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        painter.drawText(art_rect.adjusted(6, 0, -6, -12), Qt.AlignBottom | Qt.AlignHCenter, f"{cnt} giochi")

        painter.setPen(QColor(self.accent_color))
        painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
        painter.drawText(art_rect.adjusted(0, 6, -8, 0), Qt.AlignTop | Qt.AlignRight, "📁")

        title = str(index.data(Qt.DisplayRole) or "")
        text_h = self.box_delegate.title_box_height
        pt = self.box_delegate.title_font_pt
        text_rect = QRect(rect.left() + 4, rect.top() + self.box_delegate.art_height + 3, rect.width() - 8, text_h)
        painter.setPen(QColor("#ffffff") if selected else QColor("#e6e8ee"))
        painter.setFont(QFont("Segoe UI", pt, QFont.DemiBold if selected else QFont.Medium))
        elided = QFontMetrics(painter.font()).elidedText(title, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, elided)

        painter.restore()


# ---------------------------------------------------------------------------
# Tastiera Virtuale per Ricerca col Pad / Tastiera
# ---------------------------------------------------------------------------

class _VirtualKeyboardSearchDialog(QDialog):
    """Campo di ricerca con tastiera virtuale navigabile da Pad (D-Pad/Stick + A/B/X/Y/Start)."""

    _ROWS = [
        ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
        ["Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"],
        ["A", "S", "D", "F", "G", "H", "J", "K", "L", "-"],
        ["Z", "X", "C", "V", "B", "N", "M", "⌫", "Spazio", "Svuota"],
        ["Annulla (B)", "✓ CERCA (Start)"],
    ]

    def __init__(
        self,
        initial_text: str = "",
        accent_color: str = "#f58a1f",
        include_subfolders: bool = True,
        has_subfolders: bool = True,
        title: str = "Cerca Gioco",
        placeholder: str = "Scrivi con la tastiera o usa il pad...",
        confirm_text: str = "✓ CERCA (Start)",
        show_subfolders_option: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._accent = accent_color
        self.search_text = initial_text
        self.include_subfolders = bool(include_subfolders and has_subfolders)
        self._has_subfolders = bool(has_subfolders)
        self._cur_r = 1
        self._cur_c = 0
        self._btns: list[list[QPushButton]] = []

        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(860, 505 if show_subfolders_option else 470)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: rgba(16, 20, 30, 245); "
            f"border: 2px solid {self._accent}; border-radius: 16px; }}"
        )
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(14)

        hdr_text = t("bs_search_title") if show_subfolders_option else f"📁 {title.upper()}"
        hdr = QLabel(hdr_text)
        hdr.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffffff; border: none; background: transparent;")
        lay.addWidget(hdr)

        self.edit = QLineEdit(initial_text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumHeight(48)
        self.edit.setStyleSheet(
            f"QLineEdit {{ background-color: #0e1118; color: #ffffff; border: 1.5px solid #3c4354; "
            f"border-radius: 10px; padding: 8px 14px; font-size: 20px; font-weight: bold; }} "
            f"QLineEdit:focus {{ border-color: {self._accent}; }}"
        )
        lay.addWidget(self.edit)

        self.sub_cb = QCheckBox(t("bs_search_subfolders"))
        self.sub_cb.setFocusPolicy(Qt.NoFocus)
        self.sub_cb.setCursor(Qt.PointingHandCursor if self._has_subfolders else Qt.ArrowCursor)
        self.sub_cb.setChecked(self.include_subfolders)
        self.sub_cb.setEnabled(self._has_subfolders)
        self.sub_cb.setStyleSheet(
            f"QCheckBox {{ color: #eef1f7; font-size: 14px; font-weight: bold; spacing: 8px; background: transparent; border: none; }} "
            f"QCheckBox:disabled {{ color: #4b5263; }} "
            f"QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 2px solid #7a8499; background-color: #12151c; }} "
            f"QCheckBox::indicator:checked {{ background-color: {self._accent}; border-color: {self._accent}; }} "
            f"QCheckBox::indicator:disabled {{ background-color: #10131a; border-color: #2b303d; }}"
        )
        if not self._has_subfolders:
            eff = QGraphicsOpacityEffect(self.sub_cb)
            eff.setOpacity(0.32)
            self.sub_cb.setGraphicsEffect(eff)
        if not show_subfolders_option:
            self.sub_cb.setVisible(False)
        lay.addWidget(self.sub_cb)

        grid = QGridLayout()
        grid.setSpacing(8)
        for r_idx, row_keys in enumerate(self._ROWS):
            row_btns: list[QPushButton] = []
            if r_idx < 4:
                for c_idx, key_lbl in enumerate(row_keys):
                    btn = QPushButton(key_lbl)
                    btn.setFocusPolicy(Qt.NoFocus)
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setMinimumHeight(48)
                    btn.clicked.connect(lambda _c=False, k=key_lbl, rr=r_idx, cc=c_idx: self._on_key_clicked(k, rr, cc))
                    grid.addWidget(btn, r_idx, c_idx)
                    row_btns.append(btn)
            else:
                cancel_btn = QPushButton(t("dlg_btn_cancel"))
                cancel_btn.setIcon(_make_abxy_icon("b", 22))
                cancel_btn.setIconSize(QSize(22, 22))
                cancel_btn.setFocusPolicy(Qt.NoFocus)
                cancel_btn.setCursor(Qt.PointingHandCursor)
                cancel_btn.setMinimumHeight(50)
                cancel_btn.clicked.connect(self.reject)
                ok_btn = QPushButton(confirm_text)
                ok_btn.setFocusPolicy(Qt.NoFocus)
                ok_btn.setCursor(Qt.PointingHandCursor)
                ok_btn.setMinimumHeight(50)
                ok_btn.clicked.connect(self._accept_search)
                grid.addWidget(cancel_btn, r_idx, 0, 1, 4)
                grid.addWidget(ok_btn, r_idx, 4, 1, 6)
                row_btns = [cancel_btn, ok_btn]
            self._btns.append(row_btns)
        lay.addLayout(grid)

        hint = _build_hints_bar(
            [
                (None, t("bs_hint_move")),
                ("a", "OK"),
                ("x", "⌫"),
                ("y", "␣"),
                (None, "[Start] OK"),
                ("b", t("dlg_btn_close")),
            ],
            framed=False,
            font_size=12,
            icon_size=20,
        )
        lay.addWidget(hint)

        self._refresh_focus_styles()

        self._poller = XInputPoller(self)
        self._poller.nav.connect(self._on_pad_nav)
        self._poller.action.connect(self._on_pad_action)
        self._poller.start()

    def _refresh_focus_styles(self) -> None:
        for r_idx, row_btns in enumerate(self._btns):
            for c_idx, btn in enumerate(row_btns):
                is_cur = (r_idx == self._cur_r and c_idx == self._cur_c)
                is_ok = (r_idx == 4 and c_idx == 1)
                if is_cur:
                    btn.setStyleSheet(
                        f"QPushButton {{ background-color: {self._accent}; color: #111318; "
                        f"border: 2px solid #ffffff; border-radius: 10px; font-size: 16px; font-weight: 900; }}"
                    )
                elif is_ok:
                    btn.setStyleSheet(
                        f"QPushButton {{ background-color: rgba(40, 48, 66, 230); color: {self._accent}; "
                        f"border: 1.5px solid {self._accent}; border-radius: 10px; font-size: 16px; font-weight: bold; }}"
                    )
                else:
                    btn.setStyleSheet(
                        "QPushButton { background-color: rgba(28, 34, 48, 220); color: #eef1f7; "
                        "border: 1px solid rgba(255, 255, 255, 34); border-radius: 10px; font-size: 15px; font-weight: bold; } "
                        f"QPushButton:hover {{ border-color: {self._accent}; }}"
                    )

    def _on_key_clicked(self, key: str, r_idx: int, c_idx: int) -> None:
        self._cur_r, self._cur_c = r_idx, c_idx
        self._refresh_focus_styles()
        self._press_key(key)

    def _press_key(self, key: str) -> None:
        if key == "⌫":
            self.edit.backspace()
        elif key == "Spazio":
            self.edit.insert(" ")
        elif key == "Svuota":
            self.edit.clear()
        elif key.startswith("Annulla"):
            self.reject()
        elif key.startswith("✓"):
            self._accept_search()
        else:
            self.edit.insert(key.lower())

    def _accept_search(self) -> None:
        self.search_text = self.edit.text().strip()
        self.include_subfolders = self.sub_cb.isChecked() if self._has_subfolders else False
        self.accept()

    def _on_pad_nav(self, direction: str) -> None:
        if direction == "up":
            self._cur_r = (self._cur_r - 1) % len(self._btns)
        elif direction == "down":
            self._cur_r = (self._cur_r + 1) % len(self._btns)
        elif direction == "left":
            self._cur_c = (self._cur_c - 1) % len(self._btns[self._cur_r])
        elif direction == "right":
            self._cur_c = (self._cur_c + 1) % len(self._btns[self._cur_r])
        self._cur_c = min(self._cur_c, len(self._btns[self._cur_r]) - 1)
        self._refresh_focus_styles()

    def _on_pad_action(self, act: str) -> None:
        if act == "a":
            key = self._ROWS[self._cur_r][self._cur_c]
            self._press_key(key)
        elif act == "b":
            self.reject()
        elif act == "x":
            self.edit.backspace()
        elif act == "y":
            self.edit.insert(" ")
        elif act in ("lb", "rb") and self._has_subfolders:
            self.sub_cb.setChecked(not self.sub_cb.isChecked())
        elif act == "start":
            self._accept_search()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        k = event.key()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self._accept_search()
            return
        if k == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._poller.stop()
        super().closeEvent(event)


class _BigConfirmDialog(QDialog):
    """Finestra di conferma frameless navigabile con Pad (SX/DX, A, B) e Tastiera."""

    def __init__(
        self,
        parent: Optional[QWidget],
        title_text: str,
        body_text: str,
        confirm_label: str = "🗑 Elimina",
        accent_color: str = "#f58a1f",
    ):
        super().__init__(parent)
        self._accent = accent_color
        self._sel = 0  # 0=Annulla, 1=Conferma

        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.resize(560, 250)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: rgba(16, 20, 30, 250); "
            f"border: 2px solid {accent_color}; border-radius: 16px; }}"
        )
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(14)

        t_lbl = QLabel(title_text)
        t_lbl.setWordWrap(True)
        t_lbl.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffffff; border: none; background: transparent;")
        lay.addWidget(t_lbl)

        b_lbl = QLabel(body_text)
        b_lbl.setWordWrap(True)
        b_lbl.setStyleSheet("font-size: 15px; color: #b5bfd4; border: none; background: transparent;")
        lay.addWidget(b_lbl)

        lay.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(14)
        self.cancel_btn = QPushButton("Annulla")
        self.cancel_btn.setIcon(_make_abxy_icon("b", 22))
        self.cancel_btn.setIconSize(QSize(22, 22))
        self.cancel_btn.setFocusPolicy(Qt.NoFocus)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)

        self.ok_btn = QPushButton(confirm_label)
        self.ok_btn.setIcon(_make_abxy_icon("a", 22))
        self.ok_btn.setIconSize(QSize(22, 22))
        self.ok_btn.setFocusPolicy(Qt.NoFocus)
        self.ok_btn.setCursor(Qt.PointingHandCursor)
        self.ok_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.cancel_btn, 1)
        btn_row.addWidget(self.ok_btn, 1)
        lay.addLayout(btn_row)

        hint = _build_hints_bar(
            [
                (None, "[SX / DX] Scegli"),
                ("a", "Conferma"),
                ("b", "Annulla"),
            ],
            framed=False,
            font_size=12,
            icon_size=20,
        )
        lay.addWidget(hint)

        self._refresh_styles()

        self._poller = XInputPoller(self)
        self._poller.nav.connect(self._on_pad_nav)
        self._poller.action.connect(self._on_pad_action)
        self._poller.start()

    def _refresh_styles(self) -> None:
        self.cancel_btn.setStyleSheet(
            f"QPushButton {{ background-color: rgba(245, 138, 31, 45); color: #ffffff; border: 2px solid {self._accent}; "
            f"border-radius: 10px; padding: 12px 18px; font-size: 15px; font-weight: bold; }}"
            if self._sel == 0 else
            "QPushButton { background-color: #232938; color: #dce2ee; border: 1.5px solid rgba(255,255,255,35); "
            "border-radius: 10px; padding: 12px 18px; font-size: 15px; font-weight: bold; }"
        )
        self.ok_btn.setStyleSheet(
            f"QPushButton {{ background-color: #e05252; color: #ffffff; border: 2px solid {self._accent}; "
            f"border-radius: 10px; padding: 12px 18px; font-size: 15px; font-weight: bold; }}"
            if self._sel == 1 else
            "QPushButton { background-color: rgba(56, 30, 36, 220); color: #f5b5b5; border: 1.5px solid rgba(224,82,82,110); "
            "border-radius: 10px; padding: 12px 18px; font-size: 15px; font-weight: bold; }"
        )

    def _on_pad_nav(self, direction: str) -> None:
        if direction in ("left", "right"):
            self._sel = 1 - self._sel
            self._refresh_styles()

    def _on_pad_action(self, act: str) -> None:
        if act == "a":
            if self._sel == 1:
                self.accept()
            else:
                self.reject()
        elif act == "b":
            self.reject()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        k = event.key()
        if k in (Qt.Key_Left, Qt.Key_Right):
            self._sel = 1 - self._sel
            self._refresh_styles()
            return
        if k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._sel == 1:
                self.accept()
            else:
                self.reject()
            return
        if k == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._poller.stop()
        super().closeEvent(event)


class _BigFavoriteFolderDialog(QDialog):
    """Dialog cartelle preferiti navigabile con Pad (Su/Giù/SX/DX, A, B, X, Y) e Tastiera."""

    def __init__(
        self,
        parent: Optional[QWidget],
        db: Database,
        game_id: int,
        game_title: str,
        accent_color: str = "#f58a1f",
    ):
        super().__init__(parent)
        self.db = db
        self.game_id = game_id
        self._accent = accent_color
        self._checked_ids: set[int] = db.get_game_favorite_folder_ids(game_id)
        self.selected_folder_ids: list[int] = []
        self._focus_col: int = 0  # 0=spunta cartella, 1=rinomina, 2=elimina
        self._row_widgets: list[tuple[int, str, QFrame, QCheckBox, QPushButton, QPushButton]] = []

        self.setWindowTitle(t("tab_favorites"))
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.resize(860, 580)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: rgba(16, 20, 30, 248); "
            f"border: 2px solid {accent_color}; border-radius: 16px; }}"
        )
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(16)

        lbl = QLabel(t("bs_fav_choose", game_title=game_title))
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffffff; border: none; background: transparent;")
        lay.addWidget(lbl)

        self.list_w = QListWidget()
        self.list_w.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_w.setIconSize(QSize(36, 36))
        self.list_w.setStyleSheet(
            "QListWidget { background-color: #191e29; border: 1px solid rgba(255,255,255,30); border-radius: 12px; padding: 8px; outline: 0; }"
            "QListWidget::item { background: transparent; border: none; padding: 3px 0px; }"
            "QListWidget::item:selected { background: transparent; border: none; }"
        )
        self.list_w.currentRowChanged.connect(self._on_row_changed)
        self.list_w.installEventFilter(self)
        lay.addWidget(self.list_w, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_css = (
            f"QPushButton {{ background-color: #232938; color: #eef1f7; border: 1.5px solid rgba(255,255,255,38); "
            f"border-radius: 10px; padding: 10px 16px; font-size: 14px; font-weight: bold; }} "
            f"QPushButton:hover {{ border-color: {accent_color}; }}"
        )
        new_btn = QPushButton(t("fav_new_folder"))
        new_btn.setIcon(_make_abxy_icon("y", 22))
        new_btn.setIconSize(QSize(22, 22))
        new_btn.setFocusPolicy(Qt.NoFocus)
        new_btn.setStyleSheet(btn_css)
        new_btn.clicked.connect(self._create_new_folder)
        btn_row.addWidget(new_btn)

        rem_btn = QPushButton(t("bs_fav_remove_all"))
        rem_btn.setIcon(_make_abxy_icon("x", 22))
        rem_btn.setIconSize(QSize(22, 22))
        rem_btn.setFocusPolicy(Qt.NoFocus)
        rem_btn.setStyleSheet(btn_css)
        rem_btn.clicked.connect(self._remove_all)
        btn_row.addWidget(rem_btn)

        btn_row.addStretch(1)
        close_btn = QPushButton(t("dlg_btn_close"))
        close_btn.setIcon(_make_abxy_icon("b", 22))
        close_btn.setIconSize(QSize(22, 22))
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setStyleSheet(
            f"QPushButton {{ background-color: {accent_color}; color: #10131a; border: none; "
            f"border-radius: 10px; padding: 10px 24px; font-size: 15px; font-weight: bold; }}"
        )
        close_btn.clicked.connect(self._confirm)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        hint = _build_hints_bar(
            [
                (None, "[Su / Giù] Cartella"),
                (None, "[DX / SX] Rinomina / Elimina"),
                ("a", "Seleziona"),
                ("y", "Nuova Cartella"),
                ("x", "Rimuovi tutti"),
                ("b", "Chiudi"),
            ],
            framed=False,
            font_size=13,
            icon_size=22,
        )
        lay.addWidget(hint)

        self._reload()

        self._poller = XInputPoller(self)
        self._poller.nav.connect(self._on_pad_nav)
        self._poller.action.connect(self._on_pad_action)
        self._poller.start()

    def _on_folder_toggled(self, fid: int, checked: bool, row_idx: int) -> None:
        if checked:
            self._checked_ids.add(int(fid))
        else:
            self._checked_ids.discard(int(fid))
        self.db.set_game_favorite_folders(self.game_id, sorted(self._checked_ids))
        if self.list_w.currentRow() != row_idx:
            self.list_w.setCurrentRow(row_idx)
        else:
            self._refresh_row_buttons()

    def _on_row_changed(self, _row: int) -> None:
        self._focus_col = 0
        self._refresh_row_buttons()

    def _refresh_row_buttons(self) -> None:
        cur_row = self.list_w.currentRow()
        for r_idx, (_fid, _fname, folder_box, _cb, ren_btn, del_btn) in enumerate(self._row_widgets):
            is_cur = (r_idx == cur_row)
            folder_active = is_cur and self._focus_col == 0
            ren_active = is_cur and self._focus_col == 1
            del_active = is_cur and self._focus_col == 2

            folder_box.setStyleSheet(
                f"QFrame {{ background-color: rgba(245, 138, 31, 40); border: 2px solid {self._accent}; border-radius: 8px; }}"
                if folder_active else
                "QFrame { background: transparent; border: 2px solid transparent; border-radius: 8px; }"
            )
            ren_btn.setStyleSheet(
                f"QPushButton {{ background-color: rgba(245, 138, 31, 55); color: #ffffff; border: 2px solid {self._accent}; "
                f"border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: bold; }}"
                if ren_active else
                "QPushButton { background-color: rgba(35, 41, 56, 200); color: #dce2ee; "
                "border: 1px solid rgba(255,255,255,30); border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 600; } "
                f"QPushButton:hover {{ border-color: {self._accent}; }}"
            )
            del_btn.setStyleSheet(
                f"QPushButton {{ background-color: rgba(224, 82, 82, 110); color: #ffffff; border: 2px solid {self._accent}; "
                f"border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: bold; }}"
                if del_active else
                "QPushButton { background-color: rgba(56, 30, 36, 200); color: #f5b5b5; "
                "border: 1px solid rgba(224,82,82,90); border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 600; } "
                "QPushButton:hover { border-color: #e05252; }"
            )

    def _reload(self, select_row: int = 0) -> None:
        self.list_w.blockSignals(True)
        self.list_w.clear()
        self._row_widgets.clear()
        cb_css = (
            f"QCheckBox {{ color: #eef1f7; font-size: 18px; font-weight: bold; spacing: 14px; background: transparent; border: none; }}"
            f"QCheckBox::indicator {{ width: 26px; height: 26px; border-radius: 6px; border: 2px solid #7a8499; background-color: #10131a; }}"
            f"QCheckBox::indicator:checked {{ background-color: {self._accent}; border-color: {self._accent}; }}"
        )
        for r_idx, f in enumerate(self.db.list_favorite_folders()):
            fid = int(f["id"])
            fname = str(f["name"])
            it = QListWidgetItem("")
            raw_ic = f["icon_path"] if "icon_path" in f.keys() else ""
            ic_abs = self.db.resolve(raw_ic) if raw_ic else ""
            ic = QIcon(ic_abs) if (ic_abs and os.path.isfile(ic_abs)) else self.style().standardIcon(QStyle.SP_DirIcon)
            it.setData(Qt.UserRole, fid)
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            it.setSizeHint(QSize(0, 62))
            self.list_w.addItem(it)

            row_w = QWidget()
            row_w.setStyleSheet("background: transparent;")
            rw_lay = QHBoxLayout(row_w)
            rw_lay.setContentsMargins(2, 3, 4, 3)
            rw_lay.setSpacing(10)

            folder_box = QFrame()
            fb_lay = QHBoxLayout(folder_box)
            fb_lay.setContentsMargins(14, 6, 14, 6)
            fb_lay.setSpacing(10)
            folder_cb = QCheckBox(fname)
            folder_cb.setIcon(ic)
            folder_cb.setIconSize(QSize(32, 32))
            folder_cb.setFocusPolicy(Qt.NoFocus)
            folder_cb.setStyleSheet(cb_css)
            folder_cb.setChecked(fid in self._checked_ids)
            folder_cb.toggled.connect(lambda chk, f_id=fid, ri=r_idx: self._on_folder_toggled(f_id, chk, ri))
            fb_lay.addWidget(folder_cb, 1)
            rw_lay.addWidget(folder_box, 1)

            ren_btn = QPushButton("✏ Rinomina")
            ren_btn.setFocusPolicy(Qt.NoFocus)
            ren_btn.setCursor(Qt.PointingHandCursor)
            ren_btn.clicked.connect(lambda _c=False, f_id=fid, fn=fname: self._rename_folder(f_id, fn))
            rw_lay.addWidget(ren_btn)

            del_btn = QPushButton("🗑 Elimina")
            del_btn.setFocusPolicy(Qt.NoFocus)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.clicked.connect(lambda _c=False, f_id=fid, fn=fname: self._delete_folder(f_id, fn))
            rw_lay.addWidget(del_btn)

            self.list_w.setItemWidget(it, row_w)
            self._row_widgets.append((fid, fname, folder_box, folder_cb, ren_btn, del_btn))

        self.list_w.blockSignals(False)
        self._focus_col = 0
        if self.list_w.count() > 0:
            self.list_w.setCurrentRow(min(max(0, select_row), self.list_w.count() - 1))
        self._refresh_row_buttons()
        self.list_w.setFocus()

    def _toggle_current_row(self) -> None:
        row = self.list_w.currentRow()
        if not (0 <= row < len(self._row_widgets)):
            return
        folder_cb = self._row_widgets[row][3]
        folder_cb.setChecked(not folder_cb.isChecked())

    def _rename_folder(self, folder_id: int, current_name: str) -> None:
        cur_row = max(0, self.list_w.currentRow())
        self._poller.set_paused(True)
        try:
            vk = _VirtualKeyboardSearchDialog(
                current_name,
                self._accent,
                include_subfolders=False,
                has_subfolders=False,
                title="Rinomina Cartella Preferiti",
                placeholder="Nuovo nome cartella...",
                confirm_text="✓ SALVA (Start)",
                show_subfolders_option=False,
                parent=self,
            )
            if vk.exec() != _VirtualKeyboardSearchDialog.Accepted or not vk.search_text.strip():
                return
            self.db.rename_favorite_folder(folder_id, vk.search_text.strip())
            self._reload(select_row=cur_row)
        finally:
            self._poller.set_paused(False)

    def _delete_folder(self, folder_id: int, folder_name: str = "") -> None:
        cur_row = max(0, self.list_w.currentRow())
        self._poller.set_paused(True)
        try:
            dlg = _BigConfirmDialog(
                self,
                t("fav_delete_folder"),
                t("fav_delete_confirm", name=folder_name),
                confirm_label="🗑 Elimina",
                accent_color=self._accent,
            )
            if dlg.exec() != _BigConfirmDialog.Accepted:
                return
        finally:
            self._poller.set_paused(False)
        self.db.delete_favorite_folder(folder_id)
        self._checked_ids.discard(folder_id)
        self._reload(select_row=cur_row)

    def _create_new_folder(self) -> None:
        self._poller.set_paused(True)
        try:
            vk = _VirtualKeyboardSearchDialog(
                "",
                self._accent,
                include_subfolders=False,
                has_subfolders=False,
                title="Nuova Cartella Preferiti",
                placeholder="Digita il nome della cartella...",
                confirm_text="✓ CREA (Start)",
                show_subfolders_option=False,
                parent=self,
            )
            if vk.exec() != _VirtualKeyboardSearchDialog.Accepted or not vk.search_text.strip():
                return
            fid = self.db.create_favorite_folder(vk.search_text.strip())
            self._checked_ids.add(fid)
            self.db.set_game_favorite_folders(self.game_id, sorted(self._checked_ids))
            self._reload(select_row=self.list_w.count())
        finally:
            self._poller.set_paused(False)

    def _remove_all(self) -> None:
        self._checked_ids.clear()
        self.selected_folder_ids = []
        self.db.set_game_favorite_folders(self.game_id, [])
        self._reload(select_row=max(0, self.list_w.currentRow()))

    def _confirm(self) -> None:
        self.selected_folder_ids = sorted(self._checked_ids)
        self.accept()

    def _activate_focused(self) -> None:
        row = self.list_w.currentRow()
        if not (0 <= row < len(self._row_widgets)):
            return
        fid, fname, _fb, _cb, _ren, _del = self._row_widgets[row]
        if self._focus_col == 0:
            self._toggle_current_row()
        elif self._focus_col == 1:
            self._rename_folder(fid, fname)
        elif self._focus_col == 2:
            self._delete_folder(fid, fname)

    def _on_pad_nav(self, direction: str) -> None:
        cnt = self.list_w.count()
        if cnt <= 0:
            return
        row = self.list_w.currentRow()
        if direction == "up":
            self._focus_col = 0
            self.list_w.setCurrentRow((row - 1) % cnt)
            self._refresh_row_buttons()
        elif direction == "down":
            self._focus_col = 0
            self.list_w.setCurrentRow((row + 1) % cnt)
            self._refresh_row_buttons()
        elif direction == "right":
            self._focus_col = min(2, self._focus_col + 1)
            self._refresh_row_buttons()
        elif direction == "left":
            self._focus_col = max(0, self._focus_col - 1)
            self._refresh_row_buttons()

    def _on_pad_action(self, act: str) -> None:
        if act == "a":
            self._activate_focused()
        elif act == "b":
            self._confirm()
        elif act == "x":
            self._remove_all()
        elif act == "y":
            self._create_new_folder()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.list_w and event.type() == QEvent.KeyPress:
            k = event.key()
            if k == Qt.Key_Up:
                self._on_pad_nav("up")
                return True
            if k == Qt.Key_Down:
                self._on_pad_nav("down")
                return True
            if k == Qt.Key_Right:
                self._on_pad_nav("right")
                return True
            if k == Qt.Key_Left:
                self._on_pad_nav("left")
                return True
            if k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
                self._activate_focused()
                return True
            if k == Qt.Key_Escape:
                self._confirm()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        k = event.key()
        if k == Qt.Key_Escape:
            self._confirm()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._poller.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Finestra Schermo Intero Semplificata (Pad / Tastiera)
# ---------------------------------------------------------------------------

class BigScreenWindow(QWidget):
    """Modalita' Console a schermo intero per scegliere piattaforme e giochi col pad."""

    def __init__(
        self,
        db: Database,
        config: Config,
        game_model,
        thumb_cache,
        blur_fn: Callable[[str, int], Optional[QPixmap]],
        on_game_launched: Optional[Callable[[int, object], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.db = db
        self.config = config
        self.game_model = _BigGridModel(self.db, self)
        self.thumb_cache = thumb_cache
        self._blur_fn = blur_fn
        self._on_game_launched = on_game_launched

        self._accent = self.config.get("accent_color", "#f58a1f") or "#f58a1f"
        if not QColor(self._accent).isValid():
            self._accent = "#f58a1f"

        self._bg_pixmap: Optional[QPixmap] = None
        self._bg_cache: dict[str, QPixmap] = {}
        self._platforms: list[dict] = []  # {"id": Optional[int], "fav": bool, "name": str, "count": int, "icon": QIcon}
        self._cur_plat_idx: int = 0
        self._cur_section_id: Optional[int] = None
        self._cur_section_title: str = ""
        self._cur_section_fav: bool = False
        self._search_query: str = ""
        self._search_include_subfolders: bool = True
        self._folder_history: list[tuple[Optional[int], str, bool]] = []
        self._last_folder_open_ts: float = 0.0
        self._detail_game_id: Optional[int] = None
        self._detail_logo_pm: Optional[QPixmap] = None
        self._detail_photos: list[QPixmap] = []
        self._detail_photo_idx: int = 0
        self._active_procs: list = []
        saved_cols = self.config.get("bigscreen_cols", None)
        self._grid_cols: Optional[int] = int(saved_cols) if isinstance(saved_cols, int) and saved_cols >= 2 else None

        self.setWindowTitle(t("bs_title"))
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._build_ui()
        self.thumb_cache.signals.loaded.connect(self._on_thumb_loaded)
        self._load_platforms()

        self._poller = XInputPoller(self)
        self._poller.connected_changed.connect(self._on_pad_status)
        self._poller.nav.connect(self._on_pad_nav)
        self._poller.action.connect(self._on_pad_action)
        self._poller.b_hold_changed.connect(self._on_b_hold_changed)
        self._poller.start()

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._update_clock_and_procs)
        self._clock_timer.start()
        self._update_clock_and_procs()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(36, 26, 36, 22)
        root.setSpacing(16)

        # Barra superiore
        top = QHBoxLayout()
        top.setSpacing(14)
        self.left_title_lbl = QLabel(t("bs_platforms_hdr"))
        self.left_title_lbl.setStyleSheet(
            "font-size: 26px; font-weight: bold; color: #ffffff; background: transparent;"
        )
        self.left_sub_lbl = QLabel("")
        self.left_sub_lbl.setStyleSheet("font-size: 14px; color: #b5bfd4; background: transparent;")
        top.addWidget(self.left_title_lbl)
        top.addWidget(self.left_sub_lbl)
        self.plat_logo_lbl = QLabel("")
        self.plat_logo_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.plat_logo_lbl.setStyleSheet("background: transparent;")
        self.plat_logo_lbl.setVisible(False)
        top.addWidget(self.plat_logo_lbl)
        top.addStretch(1)

        self.header_lbl = QLabel("")
        self.header_lbl.setStyleSheet(
            "font-size: 26px; font-weight: bold; color: #ffffff; background: transparent;"
        )
        self.header_lbl.setVisible(False)
        self.sub_lbl = QLabel("")
        self.sub_lbl.setStyleSheet("font-size: 14px; color: #b5bfd4; background: transparent;")
        self.sub_lbl.setVisible(False)
        top.addWidget(self.header_lbl)
        top.addWidget(self.sub_lbl)

        top_pill_bg = "#181d29"
        top_pill_border = "#5c667e"
        top_btn_css = (
            f"QPushButton {{ background-color: {top_pill_bg}; color: #ffffff; "
            f"border: 3px solid {top_pill_border}; border-radius: 12px; "
            f"padding: 0px 16px; font-size: 14px; font-weight: bold; }} "
            f"QPushButton:hover {{ border-color: {self._accent}; background-color: #282f41; }}"
        )

        pad_pill = QFrame()
        pad_pill.setFixedHeight(42)
        pad_pill.setStyleSheet(
            f"QFrame {{ background-color: {top_pill_bg}; "
            f"border: 3px solid {top_pill_border}; border-radius: 12px; }}"
        )
        pp_lay = QHBoxLayout(pad_pill)
        pp_lay.setContentsMargins(14, 0, 16, 0)
        pp_lay.setSpacing(9)
        self.pad_icon_lbl = QLabel("⌨")
        self.pad_icon_lbl.setStyleSheet("color: #ffffff; font-size: 16px; border: none; background: transparent;")
        self.pad_lbl = QLabel(t("bs_pad_ready"))
        self.pad_lbl.setStyleSheet("color: #ffffff; font-size: 14px; font-weight: bold; border: none; background: transparent;")
        pp_lay.addWidget(self.pad_icon_lbl)
        pp_lay.addWidget(self.pad_lbl)
        top.addWidget(pad_pill)

        self.clock_lbl = QLabel("")
        self.clock_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #eef1f7; background: transparent;")
        top.addWidget(self.clock_lbl)

        self.search_btn = QPushButton(t("bs_btn_search"))
        self.search_btn.setFixedHeight(42)
        self.search_btn.setIcon(_make_abxy_icon("y", 24))
        self.search_btn.setIconSize(QSize(24, 24))
        self.search_btn.setFocusPolicy(Qt.NoFocus)
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.setStyleSheet(top_btn_css)
        self.search_btn.clicked.connect(self._open_search_keyboard)
        top.addWidget(self.search_btn)

        self.exit_btn = QPushButton(t("bs_btn_exit"))
        self.exit_btn.setFixedHeight(42)
        self.exit_btn.setIcon(_make_abxy_icon("b", 24))
        self.exit_btn.setIconSize(QSize(24, 24))
        self.exit_btn.setFocusPolicy(Qt.NoFocus)
        self.exit_btn.setCursor(Qt.PointingHandCursor)
        self.exit_btn.setStyleSheet(top_btn_css)
        self.exit_btn.clicked.connect(self.close)
        top.addWidget(self.exit_btn)
        root.addLayout(top)

        # Area centrale a 2 pagine: Piattaforme (0) / Giochi (1)
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent; border: none;")

        self.plat_list = QListWidget()
        self.plat_list.setViewMode(QListView.IconMode)
        self.plat_list.setResizeMode(QListView.Adjust)
        self.plat_list.setMovement(QListView.Static)
        self.plat_list.setWrapping(True)
        self.plat_list.setSpacing(14)
        self.plat_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.plat_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.plat_list.setFrameShape(QFrame.NoFrame)
        self.plat_list.setStyleSheet("QListWidget { background: transparent; border: none; outline: 0; }")
        self.plat_list.viewport().setAutoFillBackground(False)
        self.plat_list.viewport().setStyleSheet("background: transparent;")
        self.plat_delegate = _BigPlatformDelegate(self._accent, self.plat_list)
        self.plat_list.setItemDelegate(self.plat_delegate)
        self.plat_list.itemActivated.connect(lambda _it: self._enter_selected_platform())
        self.plat_list.itemClicked.connect(lambda _it: self._enter_selected_platform())
        self.stack.addWidget(self.plat_list)

        # Pagina Giochi
        games_page = QWidget()
        games_page.setStyleSheet("background: transparent;")
        gp_lay = QVBoxLayout(games_page)
        gp_lay.setContentsMargins(0, 0, 0, 0)
        gp_lay.setSpacing(12)

        from ui_main import BoxArtDelegate

        self.game_grid = QListView()
        self.game_grid.setModel(self.game_model)
        self.game_grid.setViewMode(QListView.IconMode)
        self.game_grid.setResizeMode(QListView.Adjust)
        self.game_grid.setMovement(QListView.Static)
        self.game_grid.setUniformItemSizes(True)
        self.game_grid.setSpacing(16)
        self.game_grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.game_grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.game_grid.setFrameShape(QFrame.NoFrame)
        self.game_grid.setStyleSheet("QListView { background: transparent; border: none; outline: 0; }")
        self.game_grid.viewport().setAutoFillBackground(False)
        self.game_grid.viewport().setStyleSheet("background: transparent;")
        self.game_delegate = BoxArtDelegate(self.thumb_cache, self.game_grid)
        self.game_delegate.cell_width = 210
        self.game_delegate.accent_color = self._accent
        self.game_delegate.light_theme = False
        self.game_delegate.scale_title = True
        self.grid_delegate = _BigGridDelegate(self.game_delegate, self._accent, self.game_grid)
        self.game_grid.setItemDelegate(self.grid_delegate)
        self.game_grid.selectionModel().currentChanged.connect(self._on_game_changed)
        self.game_grid.doubleClicked.connect(lambda _idx: self._activate_current_grid_item())
        gp_lay.addWidget(self.game_grid, 1)

        # Barra dettagli gioco selezionato
        self.info_bar = QFrame()
        self.info_bar.setStyleSheet(
            "QFrame { background-color: rgba(18, 22, 32, 205); "
            "border: 1px solid rgba(255, 255, 255, 28); border-radius: 12px; }"
        )
        ib_lay = QHBoxLayout(self.info_bar)
        ib_lay.setContentsMargins(18, 10, 18, 10)
        ib_lay.setSpacing(16)
        self.game_title_lbl = QLabel("—")
        self.game_title_lbl.setStyleSheet("font-size: 19px; font-weight: bold; color: #ffffff; border: none; background: transparent;")
        self.game_meta_lbl = QLabel("")
        self.game_meta_lbl.setStyleSheet("font-size: 13px; color: #b5bfd4; border: none; background: transparent;")
        ib_lay.addWidget(self.game_title_lbl)
        ib_lay.addWidget(self.game_meta_lbl, 1)

        self.launch_btn = QPushButton(t("bs_btn_select_game"))
        self.launch_btn.setIcon(_make_abxy_icon("a", 22))
        self.launch_btn.setIconSize(QSize(22, 22))
        self.launch_btn.setFocusPolicy(Qt.NoFocus)
        self.launch_btn.setCursor(Qt.PointingHandCursor)
        self.launch_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._accent}; color: #111318; "
            "border: none; border-radius: 8px; padding: 8px 18px; font-size: 14px; font-weight: bold; }}"
        )
        self.launch_btn.clicked.connect(self._activate_current_grid_item)
        ib_lay.addWidget(self.launch_btn)
        gp_lay.addWidget(self.info_bar)

        self.stack.addWidget(games_page)

        # Pagina 2: Schermata Dettaglio Gioco (SX: titolo, descrizione + pulsante gigante GIOCA; DX: logo in alto + gameplay sotto)
        detail_page = QWidget()
        detail_page.setStyleSheet("background: transparent;")
        det_lay = QHBoxLayout(detail_page)
        det_lay.setContentsMargins(8, 8, 8, 8)
        det_lay.setSpacing(26)

        # Colonna Sinistra: Titolo, Metadati, Descrizione e Pulsante gigante GIOCA
        det_left = QFrame()
        det_left.setStyleSheet(
            "QFrame { background-color: rgba(16, 20, 30, 195); "
            "border: 1px solid rgba(255, 255, 255, 28); border-radius: 14px; }"
        )
        dl_lay = QVBoxLayout(det_left)
        dl_lay.setContentsMargins(26, 22, 26, 22)
        dl_lay.setSpacing(14)

        self.detail_title_lbl = QLabel("—")
        self.detail_title_lbl.setWordWrap(True)
        self.detail_title_lbl.setStyleSheet("font-size: 28px; font-weight: bold; color: #ffffff; border: none; background: transparent;")
        dl_lay.addWidget(self.detail_title_lbl)

        self.detail_meta_lbl = QLabel("")
        self.detail_meta_lbl.setWordWrap(True)
        self.detail_meta_lbl.setStyleSheet(f"font-size: 14.5px; font-weight: 600; color: {self._accent}; border: none; background: transparent;")
        dl_lay.addWidget(self.detail_meta_lbl)

        desc_hdr = QLabel(t("bs_desc_hdr"))
        desc_hdr.setStyleSheet("font-size: 12px; font-weight: bold; letter-spacing: 1px; color: #9aa4b8; border: none; background: transparent;")
        dl_lay.addWidget(desc_hdr)

        self.detail_desc_scroll = QScrollArea()
        self.detail_desc_scroll.setWidgetResizable(True)
        self.detail_desc_scroll.setFrameShape(QFrame.NoFrame)
        self.detail_desc_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        self.detail_desc_lbl = QLabel("")
        self.detail_desc_lbl.setWordWrap(True)
        self.detail_desc_lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail_desc_lbl.setStyleSheet("font-size: 16px; line-height: 1.5; color: #e2e8f0; border: none; background: transparent;")
        self.detail_desc_scroll.setWidget(self.detail_desc_lbl)
        dl_lay.addWidget(self.detail_desc_scroll, 1)

        self.detail_play_btn = QPushButton(t("bs_btn_play"))
        self.detail_play_btn.setFocusPolicy(Qt.NoFocus)
        self.detail_play_btn.setCursor(Qt.PointingHandCursor)
        self.detail_play_btn.setMinimumHeight(74)
        self.detail_play_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._accent}; color: #111318; "
            "border: none; border-radius: 14px; font-size: 24px; font-weight: 900; letter-spacing: 1px; } "
            "QPushButton:hover { background-color: #ff9f3f; } "
            "QPushButton:pressed { padding-top: 4px; }"
        )
        self.detail_play_btn.clicked.connect(self._launch_current_game)
        dl_lay.addWidget(self.detail_play_btn)

        det_btn_row = QHBoxLayout()
        det_btn_row.setSpacing(10)
        self.detail_fav_btn = QPushButton(t("btn_fav"))
        self.detail_fav_btn.setIcon(_make_abxy_icon("x", 22))
        self.detail_fav_btn.setIconSize(QSize(22, 22))
        self.detail_fav_btn.setFocusPolicy(Qt.NoFocus)
        self.detail_fav_btn.setCursor(Qt.PointingHandCursor)
        self.detail_fav_btn.setMinimumHeight(42)
        self.detail_fav_btn.setStyleSheet(
            "QPushButton { background-color: rgba(24, 29, 42, 210); color: #eef1f7; "
            "border: 1.5px solid rgba(255, 255, 255, 38); border-radius: 10px; font-size: 14px; font-weight: bold; } "
            f"QPushButton:hover {{ border-color: {self._accent}; }}"
        )
        self.detail_fav_btn.clicked.connect(self._toggle_current_favorite)
        self.detail_back_btn = QPushButton(t("bs_btn_back"))
        self.detail_back_btn.setIcon(_make_abxy_icon("b", 22))
        self.detail_back_btn.setIconSize(QSize(22, 22))
        self.detail_back_btn.setFocusPolicy(Qt.NoFocus)
        self.detail_back_btn.setCursor(Qt.PointingHandCursor)
        self.detail_back_btn.setMinimumHeight(42)
        self.detail_back_btn.setStyleSheet(
            "QPushButton { background-color: rgba(24, 29, 42, 210); color: #eef1f7; "
            "border: 1.5px solid rgba(255, 255, 255, 38); border-radius: 10px; font-size: 14px; font-weight: bold; } "
            f"QPushButton:hover {{ border-color: {self._accent}; }}"
        )
        self.detail_back_btn.clicked.connect(self._back_from_detail)
        det_btn_row.addWidget(self.detail_fav_btn, 1)
        det_btn_row.addWidget(self.detail_back_btn, 1)
        dl_lay.addLayout(det_btn_row)

        det_lay.addWidget(det_left, 54)

        # Colonna Destra: Logo in alto + Foto Gameplay scorribili sotto
        det_right = QWidget()
        det_right.setStyleSheet("background: transparent;")
        dr_lay = QVBoxLayout(det_right)
        dr_lay.setContentsMargins(0, 0, 0, 0)
        dr_lay.setSpacing(16)

        self.detail_logo_frame = QFrame()
        self.detail_logo_frame.setStyleSheet(
            "QFrame { background-color: rgba(14, 18, 28, 185); "
            "border: 1px solid rgba(255, 255, 255, 28); border-radius: 14px; }"
        )
        dlf_lay = QVBoxLayout(self.detail_logo_frame)
        dlf_lay.setContentsMargins(12, 12, 12, 12)
        self.detail_logo_lbl = QLabel("")
        self.detail_logo_lbl.setAlignment(Qt.AlignCenter)
        self.detail_logo_lbl.setMinimumHeight(140)
        self.detail_logo_lbl.setStyleSheet("background: transparent; border: none; color: #8f99ad; font-size: 14px;")
        dlf_lay.addWidget(self.detail_logo_lbl, 1)
        dr_lay.addWidget(self.detail_logo_frame, 38)

        self.detail_photo_frame = QFrame()
        self.detail_photo_frame.setStyleSheet(
            "QFrame { background-color: rgba(14, 18, 28, 195); "
            "border: 1px solid rgba(255, 255, 255, 30); border-radius: 14px; }"
        )
        dpf_lay = QVBoxLayout(self.detail_photo_frame)
        dpf_lay.setContentsMargins(10, 10, 10, 10)
        self.detail_photo_lbl = QLabel(t("bs_no_gameplay"))
        self.detail_photo_lbl.setAlignment(Qt.AlignCenter)
        self.detail_photo_lbl.setMinimumHeight(220)
        self.detail_photo_lbl.setStyleSheet("background: transparent; border: none; color: #8f99ad; font-size: 14px;")
        dpf_lay.addWidget(self.detail_photo_lbl, 1)

        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(4, 0, 4, 0)
        _dnav_css = (
            "QPushButton { background-color: rgba(28, 33, 46, 205); color: #eef1f7; "
            "border: 1px solid rgba(255, 255, 255, 40); border-radius: 14px; font-weight: bold; padding: 4px 12px; } "
            f"QPushButton:hover {{ background-color: {self._accent}; color: #111318; border-color: {self._accent}; }}"
        )
        self.detail_prev_btn = QPushButton(t("bs_photo_prev"))
        self.detail_prev_btn.setFocusPolicy(Qt.NoFocus)
        self.detail_prev_btn.setCursor(Qt.PointingHandCursor)
        self.detail_prev_btn.setStyleSheet(_dnav_css)
        self.detail_prev_btn.clicked.connect(lambda: self._step_detail_photo(-1))
        self.detail_photo_idx_lbl = QLabel("")
        self.detail_photo_idx_lbl.setAlignment(Qt.AlignCenter)
        self.detail_photo_idx_lbl.setStyleSheet("color: #cbd5e1; font-size: 13px; font-weight: bold; border: none; background: transparent;")
        self.detail_next_btn = QPushButton(t("bs_photo_next"))
        self.detail_next_btn.setFocusPolicy(Qt.NoFocus)
        self.detail_next_btn.setCursor(Qt.PointingHandCursor)
        self.detail_next_btn.setStyleSheet(_dnav_css)
        self.detail_next_btn.clicked.connect(lambda: self._step_detail_photo(1))
        nav_row.addWidget(self.detail_prev_btn)
        nav_row.addStretch(1)
        nav_row.addWidget(self.detail_photo_idx_lbl)
        nav_row.addStretch(1)
        nav_row.addWidget(self.detail_next_btn)
        dpf_lay.addLayout(nav_row)
        dr_lay.addWidget(self.detail_photo_frame, 62)

        det_lay.addWidget(det_right, 46)
        self.stack.addWidget(detail_page)

        root.addWidget(self.stack, 1)

        # Barra legende comandi Pad / Tastiera in basso
        self.hints_lbl = _build_hints_bar(
            [
                (None, t("bs_hint_move")),
                ("a", t("bs_hint_select_play")),
                ("b", t("bs_hint_back_exit")),
                ("y", t("bs_hint_search")),
                ("x", t("bs_hint_fav")),
                (None, "LB/RB Zoom"),
            ],
            framed=True,
            font_size=15,
            icon_size=28,
        )
        root.addWidget(self.hints_lbl)

    def _load_platforms(self) -> None:
        self._platforms.clear()
        self.plat_list.clear()

        total_games = len(self.db.list_games(None, include_subplatforms=True))
        fav_games = len(self.db.list_games(None, favorites_only=True))

        all_icon_path = os.path.join(self.db.root, "icons", "all_platforms.png")
        if not os.path.isfile(all_icon_path):
            all_icon_path = os.path.join(self.db.root, "icons", "app_icon.png")
        all_icon = QIcon(all_icon_path) if os.path.isfile(all_icon_path) else self.style().standardIcon(QStyle.SP_ComputerIcon)

        from ui_main import make_star_icon

        fav_icon = make_star_icon(self._accent)

        entries = [
            {"id": None, "fav": False, "name": t("all_platforms"), "count": total_games, "icon": all_icon},
            {"id": None, "fav": True, "name": t("tab_favorites"), "count": fav_games, "icon": fav_icon},
        ]
        for p in self.db.list_platforms(root_only=True):
            ic_path = self.db.resolve(p["icon_path"])
            ic = QIcon(ic_path) if ic_path else self.style().standardIcon(QStyle.SP_DirIcon)
            cnt = len(self.db.list_games(p["id"], include_subplatforms=True))
            entries.append({"id": p["id"], "fav": False, "name": p["name"], "count": cnt, "icon": ic})

        self._platforms = entries
        for ent in entries:
            it = QListWidgetItem(ent["icon"], ent["name"])
            it.setData(Qt.UserRole + 2, t("bs_games_count", n=ent["count"]))
            self.plat_list.addItem(it)

        if self._platforms:
            self.plat_list.setCurrentRow(0)
        self.left_title_lbl.setText(t("bs_platforms_hdr"))
        self.left_sub_lbl.setText(f"({t('bs_sections_count', n=len(self._platforms))})")
        self.left_title_lbl.setVisible(True)
        self.left_sub_lbl.setVisible(True)
        self.plat_logo_lbl.clear()
        self.plat_logo_lbl.setVisible(False)
        self.header_lbl.setVisible(False)
        self.sub_lbl.setVisible(False)
        self.search_btn.setVisible(False)
        self.stack.setCurrentIndex(0)
        self.plat_list.setFocus()

    def _load_platform_logo(self, platform_id: Optional[int]) -> Optional[QPixmap]:
        if platform_id is None:
            return None
        raw_logo = ""
        cur_p = self.db.get_platform(platform_id)
        seen_pids: set[int] = set()
        while cur_p is not None and cur_p["id"] not in seen_pids:
            seen_pids.add(cur_p["id"])
            if "logo_path" in cur_p.keys() and cur_p["logo_path"]:
                raw_logo = cur_p["logo_path"]
                break
            pid_up = cur_p["parent_id"] if "parent_id" in cur_p.keys() else None
            cur_p = self.db.get_platform(pid_up) if pid_up else None
        if not raw_logo:
            return None
        lp = self.db.resolve(raw_logo)
        if not lp or not os.path.isfile(lp):
            return None
        im = QImage(lp)
        if im.isNull():
            return None
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
        return QPixmap.fromImage(im).scaled(260, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _enter_selected_platform(self) -> None:
        row = self.plat_list.currentRow()
        if row < 0 or row >= len(self._platforms):
            return
        self._open_platform_index(row)

    def _open_platform_index(self, idx: int) -> None:
        if not self._platforms:
            return
        idx = idx % len(self._platforms)
        self._cur_plat_idx = idx
        self._search_query = ""
        self._folder_history.clear()
        ent = self._platforms[idx]
        self.plat_list.setCurrentRow(idx)
        self._load_section(ent["id"], ent["name"], fav=bool(ent["fav"]))

    def _load_section(self, platform_id: Optional[int], title: str, fav: bool = False) -> None:
        self._cur_section_id = platform_id
        self._cur_section_title = title
        self._cur_section_fav = fav
        q = self._search_query.strip()

        entries: list[dict] = []
        if fav:
            if platform_id is not None:
                games = self.db.list_games(None, search=q, favorite_folder_id=platform_id, order="title")
                for g in games:
                    entries.append({"kind": "game", "game": g})
            elif not q:
                fav_folders = self.db.list_favorite_folders()
                if fav_folders:
                    for ff in fav_folders:
                        raw_ic = ff["icon_path"] if "icon_path" in ff.keys() else ""
                        ic_abs = self.db.resolve(raw_ic) if raw_ic else ""
                        if ic_abs and os.path.isfile(ic_abs):
                            ff_ic = QIcon(ic_abs)
                        else:
                            ff_ic = self.style().standardIcon(QStyle.SP_DirIcon)
                        entries.append({
                            "kind": "folder",
                            "platform": {"id": ff["id"], "name": ff["name"]},
                            "fav_folder": True,
                            "icon": ff_ic,
                            "count": ff["game_count"],
                        })
                else:
                    games = self.db.list_games(None, favorites_only=True, order="title")
                    for g in games:
                        entries.append({"kind": "game", "game": g})
            else:
                games = self.db.list_games(None, search=q, favorites_only=True, order="title")
                for g in games:
                    entries.append({"kind": "game", "game": g})
        elif platform_id is None:
            games = self.db.list_games(None, search=q, order="title", include_subplatforms=True)
            for g in games:
                entries.append({"kind": "game", "game": g})
        else:
            if not q:
                all_plats = self.db.list_platforms()
                sub_plats = [p for p in all_plats if p["parent_id"] == platform_id]
                for sp in sub_plats:
                    ic_path = self.db.resolve(sp["icon_path"])
                    ic = QIcon(ic_path) if ic_path else self.style().standardIcon(QStyle.SP_DirIcon)
                    cnt = len(self.db.list_games(sp["id"], include_subplatforms=True))
                    entries.append({"kind": "folder", "platform": sp, "icon": ic, "count": cnt})
                direct_games = self.db.list_games(platform_id, order="title", include_subplatforms=False)
            else:
                inc_sub = self._search_include_subfolders
                direct_games = self.db.list_games(platform_id, search=q, order="title", include_subplatforms=inc_sub)
            for g in direct_games:
                entries.append({"kind": "game", "game": g})

        self.game_model.set_entries(entries)

        self.left_title_lbl.setVisible(False)
        self.left_sub_lbl.setVisible(False)
        if fav:
            from ui_main import make_star_icon

            logo_pm = make_star_icon(self._accent).pixmap(54, 54)
        elif platform_id is None:
            all_ic = os.path.join(self.db.root, "icons", "all_platforms.png")
            if not os.path.isfile(all_ic):
                all_ic = os.path.join(self.db.root, "icons", "app_icon.png")
            logo_pm = None
            if os.path.isfile(all_ic):
                pm = QPixmap(all_ic)
                if not pm.isNull():
                    logo_pm = pm.scaled(58, 58, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            logo_pm = self._load_platform_logo(platform_id)
        if logo_pm is not None:
            self.plat_logo_lbl.setPixmap(logo_pm)
            self.plat_logo_lbl.setVisible(True)
        else:
            self.plat_logo_lbl.clear()
            self.plat_logo_lbl.setVisible(False)

        self.header_lbl.setText(title.upper())
        self.header_lbl.setVisible(True)
        n_folders = sum(1 for e in entries if e["kind"] == "folder")
        n_games = len(entries) - n_folders
        q_note = f'  •  Filtro: "{q}"' if q else ""
        if n_folders:
            self.sub_lbl.setText(f"({n_folders} cartelle, {n_games} giochi){q_note}")
        else:
            self.sub_lbl.setText(f"({n_games} giochi){q_note}")
        self.sub_lbl.setVisible(True)
        self.search_btn.setVisible(True)
        self.stack.setCurrentIndex(1)
        self._fit_grid_columns()
        QTimer.singleShot(0, self._fit_grid_columns)
        self.game_grid.setFocus()
        if entries:
            first = self.game_model.index(0, 0)
            self.game_grid.setCurrentIndex(first)
            self.game_grid.scrollTo(first)
            self._update_selected_game_ui(0)
        else:
            self.game_title_lbl.setText("Nessun contenuto in questa sezione")
            self.game_meta_lbl.setText("")
            self._set_dynamic_bg("")

    def _back_to_platforms(self) -> None:
        self._search_query = ""
        self._folder_history.clear()
        self.left_title_lbl.setText("PIATTAFORME")
        self.left_sub_lbl.setText(f"({len(self._platforms)} sezioni)")
        self.left_title_lbl.setVisible(True)
        self.left_sub_lbl.setVisible(True)
        self.plat_logo_lbl.clear()
        self.plat_logo_lbl.setVisible(False)
        self.header_lbl.setVisible(False)
        self.sub_lbl.setVisible(False)
        self.search_btn.setVisible(False)
        self.stack.setCurrentIndex(0)
        self.plat_list.setFocus()
        self._set_dynamic_bg("")

    def _on_game_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if current.isValid():
            self._update_selected_game_ui(current.row())

    def _update_selected_game_ui(self, row: int) -> None:
        ent = self.game_model.entry_at(row)
        if ent is None:
            return
        if ent["kind"] == "folder":
            sp = ent["platform"]
            cnt = ent.get("count", 0)
            self.game_title_lbl.setText(f"📁 {sp['name']}")
            kind_lbl = "Cartella preferiti" if ent.get("fav_folder") else "Sottocartella"
            self.game_meta_lbl.setText(f"{kind_lbl}  •  {cnt} giochi")
            self.launch_btn.setText("▶  APRI CARTELLA")
            self._set_dynamic_bg("")
            return

        g = ent["game"]
        self.launch_btn.setText(t("bs_btn_select_game"))
        fav_star = "★ " if g["favorite"] else ""
        self.game_title_lbl.setText(f"{fav_star}{g['title']}")
        plat = self.db.get_platform(g["platform_id"])
        parts = [p for p in (
            plat["name"] if plat else "",
            str(g["release_year"]) if g["release_year"] else "",
            g["genre"] or "",
        ) if p]
        self.game_meta_lbl.setText("  •  ".join(parts))

        bg_path = ""
        for rel in (g["screenshot_path"], g["boxart_path"]):
            if rel:
                cand = self.db.resolve(rel)
                if cand:
                    bg_path = cand
                    break
        self._set_dynamic_bg(bg_path)

    def _activate_current_grid_item(self) -> None:
        if time.monotonic() - self._last_folder_open_ts < 0.25:
            return
        idx = self.game_grid.currentIndex()
        if not idx.isValid():
            return
        ent = self.game_model.entry_at(idx.row())
        if ent is None:
            return
        if ent["kind"] == "folder":
            self._last_folder_open_ts = time.monotonic()
            sp = ent["platform"]
            self._folder_history.append((self._cur_section_id, self._cur_section_title, self._cur_section_fav))
            self._load_section(sp["id"], sp["name"], fav=bool(ent.get("fav_folder")))
        else:
            self._open_game_detail(ent["game"])

    def _fit_grid_columns(self) -> None:
        if not hasattr(self, "game_grid") or not hasattr(self, "game_delegate"):
            return
        vw = self.game_grid.viewport().width() - 4
        if vw < 240:
            return
        spacing = 14
        max_cols = max(2, (vw + spacing) // (110 + spacing))
        if self._grid_cols is None:
            cols = max(2, round((vw + spacing) / (215 + spacing)))
            self._grid_cols = cols
        else:
            cols = max(2, min(max_cols, self._grid_cols))
        avail_per_col = (vw - spacing * (cols + 1)) // cols
        cell_w = max(95, avail_per_col - 8)
        rem = max(0, vw - (spacing * (cols + 1) + (cell_w + 8) * cols))
        pad_l = rem // 2
        self.game_grid.setViewportMargins(pad_l, 0, rem - pad_l, 0)
        if self.game_grid.spacing() != spacing:
            self.game_grid.setSpacing(spacing)
        if self.game_delegate.cell_width != cell_w:
            self.game_delegate.cell_width = cell_w
            self.game_grid.setUniformItemSizes(False)
            self.game_grid.doItemsLayout()
            self.game_grid.setUniformItemSizes(True)
            self.game_grid.viewport().update()

    def _zoom_grid(self, delta_cols: int) -> None:
        if not hasattr(self, "game_grid"):
            return
        vw = self.game_grid.viewport().width() - 4
        if vw < 240:
            return
        spacing = 14
        max_cols = max(2, (vw + spacing) // (110 + spacing))
        cur = self._grid_cols if self._grid_cols is not None else max(2, round((vw + spacing) / (215 + spacing)))
        nxt = max(2, min(max_cols, cur + delta_cols))
        if nxt != cur:
            self._grid_cols = nxt
            self.config.data["bigscreen_cols"] = nxt
            self.config.save()
            self._fit_grid_columns()
            idx = self.game_grid.currentIndex()
            if idx.isValid():
                self.game_grid.scrollTo(idx)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_grid_columns)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_grid_columns()
        if self.stack.currentIndex() == 2:
            self._update_detail_photo()

    def _open_game_detail(self, g) -> None:
        self._detail_game_id = int(g["id"])
        fav_star = "★ " if g["favorite"] else ""
        self.detail_title_lbl.setText(f"{fav_star}{g['title']}")
        plat = self.db.get_platform(g["platform_id"])
        meta_parts = [p for p in (
            plat["name"] if plat else "",
            str(g["release_year"]) if g["release_year"] else "",
            g["genre"] or "",
            g["developer"] or "",
            g["publisher"] or "",
            f"{g['players']} gioc." if g["players"] else "",
        ) if p]
        self.detail_meta_lbl.setText("  •  ".join(meta_parts))
        desc = (g["description"] or "").strip()
        self.detail_desc_lbl.setText(desc or t("no_desc"))
        self.detail_desc_scroll.verticalScrollBar().setValue(0)
        self.detail_fav_btn.setText(t("btn_fav_remove") if g["favorite"] else t("btn_fav"))

        # Logo in alto a destra (logo_path, fallback boxart_path)
        self._detail_logo_pm = None
        for rel in (g["logo_path"], g["boxart_path"]):
            if rel:
                p = self.db.resolve(rel)
                if p and os.path.isfile(p):
                    pm = QPixmap(p)
                    if not pm.isNull():
                        self._detail_logo_pm = pm
                        break

        # Foto gameplay scorribili sotto (screenshot_path + extra_screenshots)
        photos: list[QPixmap] = []
        raw_extra = g["extra_screenshots"] if "extra_screenshots" in g.keys() else "[]"
        try:
            extra_list = json.loads(raw_extra or "[]")
        except ValueError:
            extra_list = []
        for rel in [g["screenshot_path"], *extra_list]:
            if rel:
                p = self.db.resolve(rel)
                if p and os.path.isfile(p):
                    pm = QPixmap(p)
                    if not pm.isNull():
                        photos.append(pm)
        if not photos and self._detail_logo_pm is not None:
            photos.append(self._detail_logo_pm)
        self._detail_photos = photos
        self._detail_photo_idx = 0
        self.stack.setCurrentIndex(2)
        QTimer.singleShot(0, self._update_detail_photo)

    def _step_detail_photo(self, delta: int) -> None:
        if len(self._detail_photos) <= 1:
            return
        self._detail_photo_idx = (self._detail_photo_idx + delta) % len(self._detail_photos)
        self._update_detail_photo()

    def _update_detail_photo(self) -> None:
        if self._detail_logo_pm is not None and not self._detail_logo_pm.isNull():
            lsz = self.detail_logo_lbl.size()
            if lsz.width() > 20 and lsz.height() > 20:
                self.detail_logo_lbl.setPixmap(self._detail_logo_pm.scaled(lsz, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.detail_logo_lbl.setPixmap(self._detail_logo_pm)
        else:
            self.detail_logo_lbl.setPixmap(QPixmap())
            self.detail_logo_lbl.setText("")

        n = len(self._detail_photos)
        multi = n > 1
        self.detail_prev_btn.setVisible(multi)
        self.detail_next_btn.setVisible(multi)
        self.detail_photo_idx_lbl.setVisible(multi)
        if n == 0:
            self.detail_photo_lbl.setPixmap(QPixmap())
            self.detail_photo_lbl.setText("Nessuna foto gameplay disponibile")
            return
        self.detail_photo_idx_lbl.setText(f"{self._detail_photo_idx + 1} / {n}")
        pm = self._detail_photos[self._detail_photo_idx]
        sz = self.detail_photo_lbl.size()
        if sz.width() > 20 and sz.height() > 20:
            scaled = pm.scaled(sz, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.detail_photo_lbl.setPixmap(scaled)
        else:
            self.detail_photo_lbl.setPixmap(pm)

    def _open_search_keyboard(self) -> None:
        if self.stack.currentIndex() == 0:
            return
        if self._cur_section_fav or self._cur_section_id is None:
            has_sub = False
        else:
            all_plats = self.db.list_platforms()
            has_sub = any(p["parent_id"] == self._cur_section_id for p in all_plats)
        self._poller.set_paused(True)
        try:
            dlg = _VirtualKeyboardSearchDialog(
                self._search_query,
                self._accent,
                include_subfolders=self._search_include_subfolders,
                has_subfolders=has_sub,
                parent=self,
            )
            if dlg.exec() != _VirtualKeyboardSearchDialog.Accepted:
                return
            self._search_query = dlg.search_text
            if has_sub:
                self._search_include_subfolders = dlg.include_subfolders
        finally:
            self._poller.set_paused(False)
        if self.stack.currentIndex() == 0:
            self._cur_plat_idx = 0
            self._folder_history.clear()
            self._load_section(None, "Tutte le Piattaforme", fav=False)
        else:
            self._load_section(self._cur_section_id, self._cur_section_title, fav=self._cur_section_fav)

    def _back_from_detail(self) -> None:
        self.stack.setCurrentIndex(1)
        self.game_grid.setFocus()

    def _set_dynamic_bg(self, path: str) -> None:
        if not path:
            self._bg_pixmap = None
            self.update()
            return
        pm = self._bg_cache.get(path)
        if pm is None:
            pm = self._blur_fn(path, 38)
            if pm is not None:
                if len(self._bg_cache) > 32:
                    self._bg_cache.pop(next(iter(self._bg_cache)))
                self._bg_cache[path] = pm
        self._bg_pixmap = pm
        self.update()

    def _launch_current_game(self) -> None:
        gid: Optional[int] = None
        if self.stack.currentIndex() == 2 and self._detail_game_id is not None:
            gid = self._detail_game_id
        else:
            idx = self.game_grid.currentIndex()
            if idx.isValid():
                g = self.game_model.game_at(idx.row())
                if g is not None:
                    gid = g["id"]
        if gid is None:
            return
        res: LaunchResult = launch_game(self.db, gid)
        if not res.ok:
            if self.stack.currentIndex() == 2:
                self.detail_meta_lbl.setText(f"Errore avvio: {res.message}")
            else:
                self.game_meta_lbl.setText(f"Errore avvio: {res.message}")
            return
        if res.process is not None:
            self._active_procs.append(res.process)
            self._poller.set_paused(True)
            if callable(self._on_game_launched):
                self._on_game_launched(gid, res.process)

    def _toggle_current_favorite(self) -> None:
        gid: Optional[int] = None
        if self.stack.currentIndex() == 2 and self._detail_game_id is not None:
            gid = self._detail_game_id
        else:
            idx = self.game_grid.currentIndex()
            if idx.isValid():
                g0 = self.game_model.game_at(idx.row())
                if g0 is not None:
                    gid = g0["id"]
        if gid is None:
            return
        g = self.db.get_game(gid)
        if g is None:
            return
        folders = self.db.list_favorite_folders()
        self._poller.set_paused(True)
        try:
            if not folders:
                if g["favorite"]:
                    self.db.remove_game_from_all_favorites(gid)
                else:
                    vk = _VirtualKeyboardSearchDialog(
                        "",
                        self._accent,
                        include_subfolders=False,
                        has_subfolders=False,
                        title=t("fav_new_folder"),
                        placeholder="Digita il nome della cartella...",
                        confirm_text="✓ CREA (Start)",
                        show_subfolders_option=False,
                        parent=self,
                    )
                    if vk.exec() != _VirtualKeyboardSearchDialog.Accepted or not vk.search_text.strip():
                        return
                    fid = self.db.create_favorite_folder(vk.search_text.strip())
                    self.db.set_game_favorite_folders(gid, [fid])
            else:
                dlg = _BigFavoriteFolderDialog(self, self.db, gid, g["title"], accent_color=self._accent)
                dlg.exec()
        finally:
            self._poller.set_paused(False)
        in_detail = self.stack.currentIndex() == 2
        self._load_section(self._cur_section_id, self._cur_section_title, fav=self._cur_section_fav)
        row = self.game_model.row_of(gid)
        if row is not None:
            mi = self.game_model.index(row, 0)
            self.game_grid.setCurrentIndex(mi)
            self._update_selected_game_ui(row)
        if in_detail:
            g_upd = self.db.get_game(gid)
            if g_upd is not None:
                self._open_game_detail(g_upd)

    def _on_pad_status(self, connected: bool) -> None:
        if connected:
            from ui_main import make_white_gamepad_icon

            self.pad_icon_lbl.setPixmap(make_white_gamepad_icon(26).pixmap(26, 26))
            self.pad_lbl.setText(t("bs_pad_connected"))
        else:
            self.pad_icon_lbl.setPixmap(QPixmap())
            self.pad_icon_lbl.setText("⌨")
            self.pad_lbl.setText(t("bs_pad_ready"))

    def _on_pad_nav(self, direction: str) -> None:
        cur = self.stack.currentIndex()
        if cur == 0:
            self._move_view_selection(self.plat_list, direction)
        elif cur == 1:
            self._move_view_selection(self.game_grid, direction)
        elif cur == 2:
            if direction == "left":
                self._step_detail_photo(-1)
            elif direction == "right":
                self._step_detail_photo(1)
            elif direction in ("up", "down"):
                sb = self.detail_desc_scroll.verticalScrollBar()
                sb.setValue(sb.value() + (-64 if direction == "up" else 64))

    @staticmethod
    def _move_view_selection(view: QAbstractItemView, direction: str) -> None:
        key_map = {
            "up": Qt.Key_Up,
            "down": Qt.Key_Down,
            "left": Qt.Key_Left,
            "right": Qt.Key_Right,
        }
        k = key_map.get(direction)
        if k is None:
            return
        ev = QKeyEvent(QKeyEvent.KeyPress, k, Qt.NoModifier)
        QApplication.sendEvent(view, ev)
        cur = view.currentIndex()
        if cur.isValid():
            view.scrollTo(cur)

    def _on_b_hold_changed(self, secs: float) -> None:
        if self.stack.currentIndex() != 0 or secs <= 0.0:
            self.exit_btn.setText("Esci (Esc / Tieni 3s)")
            return
        if secs >= 3.0:
            self.close()
            return
        rem = max(0.1, 3.0 - secs)
        self.exit_btn.setText(f"Uscita tra {rem:.1f}s...")

    def _on_pad_action(self, act: str) -> None:
        if act in ("start", "back"):
            self.close()
            return
        cur = self.stack.currentIndex()
        if act == "a":
            if cur == 0:
                self._enter_selected_platform()
            elif cur == 1:
                self._activate_current_grid_item()
            elif cur == 2:
                self._launch_current_game()
        elif act == "b":
            if cur == 2:
                self._back_from_detail()
            elif cur == 1:
                if self._folder_history:
                    prev_id, prev_title, prev_fav = self._folder_history.pop()
                    self._load_section(prev_id, prev_title, fav=prev_fav)
                else:
                    self._back_to_platforms()
            else:
                # Su schermata principale (Piattaforme) serve tenere premuto B per 3s per uscire
                pass
        elif act == "lb":
            if cur == 1:
                self._zoom_grid(1)
            elif cur == 2:
                self._step_detail_photo(-1)
        elif act == "rb":
            if cur == 1:
                self._zoom_grid(-1)
            elif cur == 2:
                self._step_detail_photo(1)
        elif act == "x":
            if cur in (1, 2):
                self._toggle_current_favorite()
        elif act == "y":
            self._open_search_keyboard()

    def _update_clock_and_procs(self) -> None:
        self.clock_lbl.setText(time.strftime("%H:%M"))
        alive = [p for p in self._active_procs if p.poll() is None]
        self._active_procs = alive
        self._poller.set_paused(bool(alive) or not self.isActiveWindow())

    def keyPressEvent(self, event: QKeyEvent) -> None:
        k = event.key()
        if k == Qt.Key_F11:
            self.close()
            return
        if k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._on_pad_action("a")
            return
        if k in (Qt.Key_Escape, Qt.Key_Backspace):
            self._on_pad_action("b")
            return
        if k in (Qt.Key_Q, Qt.Key_PageUp):
            self._on_pad_action("lb")
            return
        if k in (Qt.Key_E, Qt.Key_PageDown):
            self._on_pad_action("rb")
            return
        if k == Qt.Key_F:
            self._on_pad_action("x")
            return
        if k == Qt.Key_Slash or (k == Qt.Key_F and event.modifiers() & Qt.ControlModifier):
            self._open_search_keyboard()
            return
        wasd = {Qt.Key_W: "up", Qt.Key_S: "down", Qt.Key_A: "left", Qt.Key_D: "right"}
        if k in wasd:
            self._on_pad_nav(wasd[k])
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#10131a"))
        if self._bg_pixmap is not None and not self._bg_pixmap.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            scaled = self._bg_pixmap.scaled(
                self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.fillRect(self.rect(), QColor(12, 15, 22, 165))
        painter.end()

    def _on_thumb_loaded(self, abs_path: str, width: int, image: QImage) -> None:
        self.thumb_cache.store(abs_path, width, image)
        self.game_grid.viewport().update()

    def closeEvent(self, event) -> None:
        try:
            self.thumb_cache.signals.loaded.disconnect(self._on_thumb_loaded)
        except (TypeError, RuntimeError):
            pass
        self._poller.stop()
        self._clock_timer.stop()
        super().closeEvent(event)
