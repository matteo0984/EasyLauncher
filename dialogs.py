"""
dialogs.py - Finestre modali di EasyLauncher (PySide6).

Contiene:
  * EmulatorEditDialog / EmulatorManagerDialog
  * PlatformDialog / PlatformManagerDialog (con abbinamento multiplo emulatori)
  * GameEditDialog (modifica manuale + copia immagini + scraping singolo via QThread)
  * ScrapeBatchDialog (scraping in batch via QThread con barra di avanzamento e stop)
  * SettingsDialog (chiavi API in data/config.json e ordine scraper)
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
from typing import Optional

import requests

from PySide6.QtCore import QObject, QRect, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from database import (
    Config,
    Database,
    IMAGE_EXTS,
    create_backup,
    inspect_backup,
    restore_backup,
    safe_name,
)
from i18n import get_lang, t
from scraper import ScraperManager


def _browse_row(parent: QWidget, initial: str, choose_dir: bool = False,
                file_filter: str = "Tutti i file (*.*)", default_dir: str = "",
                prefer_default: bool = False) -> tuple[QWidget, QLineEdit]:
    """Riga con QLineEdit + pulsante Sfoglia..."""
    box = QWidget(parent)
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    edit = QLineEdit(initial, box)
    btn = QPushButton("Sfoglia…", box)

    def pick() -> None:
        cur = edit.text().strip()
        if prefer_default and default_dir and os.path.isdir(default_dir):
            start = default_dir
        elif cur:
            start = cur
        elif default_dir and os.path.isdir(default_dir):
            start = default_dir
        else:
            start = parent.window().property("app_root") or ""
        if choose_dir:
            res = QFileDialog.getExistingDirectory(parent, "Seleziona cartella", start)
        else:
            res, _ = QFileDialog.getOpenFileName(parent, "Seleziona file", start, file_filter)
        if res:
            edit.setText(os.path.normpath(res))

    btn.clicked.connect(pick)
    lay.addWidget(edit, 1)
    lay.addWidget(btn)
    return box, edit


def _has_droppable_image(mime) -> bool:
    if mime is None:
        return False
    if mime.hasImage() or mime.hasUrls() or mime.hasHtml():
        return True
    if mime.hasText() and mime.text().strip().startswith(("http://", "https://", "data:image/")):
        return True
    return False


def _save_drop_bytes(raw: bytes, ext: str = ".png") -> str:
    import tempfile
    import uuid
    img = QImage.fromData(raw)
    if img.isNull():
        return ""
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        ext = ".png"
    out = os.path.join(tempfile.gettempdir(), f"easylauncher_drop_{uuid.uuid4().hex[:10]}{ext}")
    with open(out, "wb") as f:
        f.write(raw)
    return out


def _extract_dropped_image(mime) -> str:
    """Estrae un'immagine da QMimeData (Chrome HTML/URL, file locale, data URI o QImage) e restituisce un percorso su disco."""
    import base64
    import hashlib
    import html as _html
    import tempfile
    import uuid
    from urllib.parse import parse_qs, urlparse

    if mime is None:
        return ""

    candidates: list[str] = []
    if mime.hasUrls():
        for u in mime.urls():
            if u.isLocalFile():
                lp = u.toLocalFile()
                if lp and os.path.isfile(lp) and not QImage(lp).isNull():
                    return lp
            s = u.toString()
            if s:
                if "imgurl=" in s:
                    qs = parse_qs(urlparse(s).query)
                    for iu in qs.get("imgurl", []):
                        if iu:
                            candidates.append(iu)
                candidates.append(s)

    if mime.hasHtml():
        raw_html = _html.unescape(mime.html())
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', raw_html, re.I):
            src = m.group(1).strip()
            if src:
                candidates.append(src)

    if mime.hasText():
        txt = mime.text().strip()
        if txt.startswith(("http://", "https://", "data:image/")):
            candidates.append(txt)

    # Priorità a URL http/https reali rispetto a data:image thumbnail
    http_cands = [c for c in candidates if c.startswith(("http://", "https://")) and "google.com/imgres" not in c]
    data_cands = [c for c in candidates if c.startswith("data:image/")]

    for url in http_cands:
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
                timeout=8,
            )
            if r.ok and r.content:
                ctype = (r.headers.get("Content-Type") or "").lower()
                ext = ".jpg" if "jpeg" in ctype or url.lower().endswith((".jpg", ".jpeg")) else (
                    ".webp" if "webp" in ctype or url.lower().endswith(".webp") else ".png"
                )
                saved = _save_drop_bytes(r.content, ext)
                if saved:
                    return saved
        except Exception:
            continue

    for d_uri in data_cands:
        try:
            header, b64 = d_uri.split(",", 1)
            raw = base64.b64decode(b64)
            ext = ".jpg" if "jpeg" in header.lower() else (".webp" if "webp" in header.lower() else ".png")
            saved = _save_drop_bytes(raw, ext)
            if saved:
                return saved
        except Exception:
            continue

    if mime.hasImage():
        try:
            img = QImage(mime.imageData())
            if not img.isNull():
                out = os.path.join(tempfile.gettempdir(), f"easylauncher_drop_{uuid.uuid4().hex[:10]}.png")
                if img.save(out, "PNG"):
                    return out
        except Exception:
            pass

    return ""


class _DropImageCard(QFrame):
    """QFrame che accetta drag-and-drop di immagini da Chrome o dal file system."""

    image_dropped = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, normal_style: str = "", hover_style: str = ""):
        super().__init__(parent)
        self._normal_style = normal_style
        self._hover_style = hover_style
        self.setAcceptDrops(True)
        if normal_style:
            self.setStyleSheet(normal_style)

    def dragEnterEvent(self, event) -> None:
        if _has_droppable_image(event.mimeData()):
            event.acceptProposedAction()
            if self._hover_style:
                self.setStyleSheet(self._hover_style)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        if self._normal_style:
            self.setStyleSheet(self._normal_style)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        if self._normal_style:
            self.setStyleSheet(self._normal_style)
        path = _extract_dropped_image(event.mimeData())
        if path:
            self.image_dropped.emit(path)
            event.acceptProposedAction()
        else:
            event.ignore()


def _close_side_by_side_browser(dialog: QWidget) -> None:
    """Chiude solo le finestre browser aperte da questo dialog e ripristina la geometria originale."""
    watch_t = getattr(dialog, "_browser_watch_timer", None)
    if watch_t is not None:
        try:
            watch_t.stop()
        except Exception:
            pass
    hwnds = getattr(dialog, "_opened_browser_hwnds", None)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            if hwnds:
                for hwnd in list(hwnds):
                    if user32.IsWindow(hwnd):
                        user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
                        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                hwnds.clear()
            if dialog.winId():
                user32.SetWindowPos(wintypes.HWND(int(dialog.winId())), wintypes.HWND(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass
    pre_geom = getattr(dialog, "_pre_browser_geom", None)
    if pre_geom is not None:
        try:
            dialog.setGeometry(pre_geom)
        except Exception:
            pass
        dialog._pre_browser_geom = None
    cb = getattr(dialog, "_browser_state_cb", None)
    if callable(cb):
        try:
            cb(False)
        except Exception:
            pass


def _open_browser_side_by_side(dialog: QWidget, query: str) -> None:
    """Apre Google Immagini nel browser predefinito (Chrome/Edge/Firefox/Brave/Opera) affiancato a sinistra e sposta EasyLauncher a destra."""
    import shlex
    import subprocess
    from urllib.parse import quote_plus
    from PySide6.QtWidgets import QApplication

    url = f"https://www.google.com/search?tbm=isch&q={quote_plus(query.strip())}"
    screen = QApplication.screenAt(dialog.frameGeometry().center()) or QApplication.primaryScreen()
    avail = screen.availableGeometry() if screen else None

    if not getattr(dialog, "_browser_cleanup_hooked", False) and hasattr(dialog, "finished"):
        dialog._browser_cleanup_hooked = True
        dialog.finished.connect(lambda _res: _close_side_by_side_browser(dialog))

    cb = getattr(dialog, "_browser_state_cb", None)
    if callable(cb):
        try:
            cb(True)
        except Exception:
            pass

    if avail is not None:
        if getattr(dialog, "_pre_browser_geom", None) is None:
            dialog._pre_browser_geom = dialog.geometry()
        half_w = avail.width() // 2
        frame_top = max(30, dialog.geometry().y() - dialog.frameGeometry().y())
        frame_side = max(0, dialog.geometry().x() - dialog.frameGeometry().x())
        right_x = avail.x() + half_w + frame_side
        right_y = avail.y() + frame_top
        right_w = max(480, avail.width() - half_w - 2 * frame_side)
        right_h = max(480, avail.height() - frame_top - frame_side)
        dialog.setGeometry(right_x, right_y, right_w, right_h)
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                ctypes.windll.user32.SetWindowPos(wintypes.HWND(int(dialog.winId())), wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            except Exception:
                pass

    before_hwnds: set[int] = set()
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _snap_before_cb(hwnd, _lp):
                if user32.IsWindowVisible(hwnd):
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    if cls.value in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
                        before_hwnds.add(int(hwnd))
                return True

            user32.EnumWindows(_snap_before_cb, 0)
        except Exception:
            pass

    browser_exe = ""
    if os.name == "nt":
        try:
            import winreg
            prog_id = ""
            for reg_sub in (
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoiceLatest\ProgId",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoiceLatest\ProgId",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
            ):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_sub) as k:
                        prog_id, _ = winreg.QueryValueEx(k, "ProgId")
                        if prog_id:
                            break
                except OSError:
                    continue
            if prog_id:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as ck:
                    cmd_str, _ = winreg.QueryValueEx(ck, "")
                parts = shlex.split(cmd_str, posix=False)
                if parts:
                    cand = parts[0].strip('"')
                    if os.path.isfile(cand):
                        browser_exe = cand
        except Exception:
            pass

    if not browser_exe:
        for cand in (
            shutil.which("chrome"),
            shutil.which("firefox"),
            shutil.which("msedge"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if cand and os.path.isfile(cand):
                browser_exe = cand
                break

    dpr = float(screen.devicePixelRatio()) if screen else 1.0
    lx_dip = avail.x() if avail is not None else 0
    ly_dip = avail.y() if avail is not None else 0
    lw_dip = max(480, avail.width() // 2) if avail is not None else 700
    lh_dip = avail.height() if avail is not None else 700

    lx = int(round(lx_dip * dpr)) - 7
    ly = int(round(ly_dip * dpr))
    lw = int(round(lw_dip * dpr)) + 14
    lh = int(round(lh_dip * dpr)) + 7

    target_exe_name = os.path.basename(browser_exe).lower() if browser_exe else ""
    is_firefox = any(f in target_exe_name for f in ("firefox", "librewolf", "waterfox"))
    if browser_exe:
        args = [browser_exe, "-new-window" if is_firefox else "--new-window"]
        if not is_firefox and avail is not None:
            args.extend([f"--window-position={lx_dip},{ly_dip}", f"--window-size={lw_dip},{lh_dip}"])
        args.append(url)
        try:
            subprocess.Popen(args)
        except Exception:
            QDesktopServices.openUrl(QUrl(url))
    else:
        QDesktopServices.openUrl(QUrl(url))

    if avail is not None and os.name == "nt":
        def _snap_browser_left() -> None:
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                own_hwnd = int(dialog.winId())
                found: list[int] = []

                @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                def _enum_cb(hwnd, _lp):
                    if hwnd == own_hwnd or not user32.IsWindowVisible(hwnd):
                        return True
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    if cls.value not in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
                        return True
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length <= 0:
                        return True
                    title_buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, title_buf, length + 1)
                    title_low = title_buf.value.lower()
                    # Verifica che appartenga davvero al browser (esclude Electron/Antigravity/altre app)
                    pid = wintypes.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    proc_name = ""
                    h_proc = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
                    if h_proc:
                        try:
                            p_buf = ctypes.create_unicode_buffer(512)
                            sz = wintypes.DWORD(512)
                            if kernel32.QueryFullProcessImageNameW(h_proc, 0, p_buf, ctypes.byref(sz)):
                                proc_name = os.path.basename(p_buf.value).lower()
                        finally:
                            kernel32.CloseHandle(h_proc)
                    if target_exe_name and proc_name != target_exe_name:
                        return True
                    if not target_exe_name and not any(
                        b in proc_name for b in ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")
                    ):
                        return True
                    is_new = int(hwnd) not in before_hwnds
                    if is_new and "google" in title_low:
                        found.insert(0, hwnd)
                        return False
                    if is_new or "google" in title_low or not found:
                        found.append(hwnd)
                    return True

                user32.EnumWindows(_enum_cb, 0)
                if found:
                    target = found[0]
                    opened = getattr(dialog, "_opened_browser_hwnds", None)
                    if opened is None:
                        opened = []
                        dialog._opened_browser_hwnds = opened
                    if int(target) not in opened:
                        opened.append(int(target))
                    user32.ShowWindow(target, 9)  # SW_RESTORE
                    user32.SetWindowPos(wintypes.HWND(int(target)), wintypes.HWND(-1), lx, ly, lw, lh, 0x0040)
            except Exception:
                pass

        QTimer.singleShot(650, _snap_browser_left)
        QTimer.singleShot(1400, _snap_browser_left)

        def _check_browser_alive() -> None:
            hwnds = getattr(dialog, "_opened_browser_hwnds", None)
            if not hwnds:
                return
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if not any(user32.IsWindow(h) and user32.IsWindowVisible(h) for h in hwnds):
                    _close_side_by_side_browser(dialog)
            except Exception:
                pass

        watch_t = getattr(dialog, "_browser_watch_timer", None)
        if watch_t is None:
            watch_t = QTimer(dialog)
            watch_t.setInterval(800)
            watch_t.timeout.connect(_check_browser_alive)
            dialog._browser_watch_timer = watch_t
        QTimer.singleShot(1800, watch_t.start)


# --------------------------------------------------------------------------
# Worker QThread per lo scraping (singolo e batch)
# --------------------------------------------------------------------------

class SingleScrapeWorker(QThread):
    finished_scrape = Signal(bool, str)

    def __init__(self, root: str, game_id: int):
        super().__init__()
        self.root = root
        self.game_id = game_id

    def run(self) -> None:
        db = Database(self.root)
        cfg = Config(self.root)
        try:
            game = db.get_game(self.game_id)
            if game is None:
                self.finished_scrape.emit(False, "Gioco non trovato.")
                return
            plat = db.get_platform(game["platform_id"])
            if plat is None:
                self.finished_scrape.emit(False, "Piattaforma non trovata.")
                return
            mgr = ScraperManager(db, cfg)
            ok, msg = mgr.scrape_game(game, plat, overwrite_manual=True)
            self.finished_scrape.emit(ok, msg)
        finally:
            db.close()


class BatchScrapeWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(dict)

    def __init__(self, root: str, platform_id: Optional[int], only_missing: bool):
        super().__init__()
        self.root = root
        self.platform_id = platform_id
        self.only_missing = only_missing
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        db = Database(self.root)
        cfg = Config(self.root)
        try:
            mgr = ScraperManager(db, cfg)
            stats = mgr.scrape_platform(
                self.platform_id,
                only_missing=self.only_missing,
                on_progress=lambda i, t, m: self.progress.emit(i, t, m),
                should_stop=lambda: self._stop,
            )
            self.completed.emit(stats)
        except Exception as exc:
            self.progress.emit(0, 0, f"Errore durante la scansione: {exc}")
            self.completed.emit({"updated": 0, "skipped": 0, "failed": 0})
        finally:
            db.close()


# --------------------------------------------------------------------------
# Gestione Emulatori
# --------------------------------------------------------------------------

class EmulatorEditDialog(QDialog):
    def __init__(self, parent: QWidget, db: Database, emu: Optional[sqlite3.Row] = None):
        super().__init__(parent)
        self.db = db
        self.emu = emu
        self.saved_id: Optional[int] = None
        self.setWindowTitle("Modifica Emulatore" if emu else "Nuovo Emulatore")
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.name_ed = QLineEdit(emu["name"] if emu else "")
        exe_init = db.resolve(emu["exe_path"]) if emu else ""
        exe_row, self.exe_ed = _browse_row(
            self, exe_init, file_filter="Eseguibili (*.exe *.bat *.cmd);;Tutti (*.*)"
        )
        self.args_ed = QLineEdit(emu["args"] if emu else '"{rom_path}"')
        wd_init = db.resolve(emu["work_dir"]) if (emu and emu["work_dir"]) else ""
        wd_row, self.wd_ed = _browse_row(self, wd_init, choose_dir=True)

        hint = QLabel(
            'Segnaposto supportati negli argomenti: {rom_path}, {rom_dir}, {rom_name}, {rom_stem}.\n'
            'Esempio RetroArch: -L cores/snes9x_libretro.dll "{rom_path}"'
        )
        hint.setObjectName("MutedHint")
        hint.setWordWrap(True)

        form.addRow("Nome Emulatore:", self.name_ed)
        form.addRow("Eseguibile (.exe):", exe_row)
        form.addRow("Parametri comando:", self.args_ed)
        form.addRow("Cartella lavoro (opz.):", wd_row)
        form.addRow("", hint)
        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.name_ed.setFocus()

    def _save(self) -> None:
        name = self.name_ed.text().strip()
        exe = self.exe_ed.text().strip()
        if not name or not exe:
            QMessageBox.warning(self, "Dati mancanti", "Nome ed eseguibile sono obbligatori.")
            return
        args = self.args_ed.text().strip()
        wd = self.wd_ed.text().strip()
        if self.emu is None:
            self.saved_id = self.db.add_emulator(name, exe, args, wd)
        else:
            self.db.update_emulator(self.emu["id"], name, exe, args, wd)
            self.saved_id = self.emu["id"]
        self.accept()


class EmulatorManagerDialog(QDialog):
    def __init__(self, parent: QWidget, db: Database):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Gestisci Emulatori")
        self.resize(620, 380)

        lay = QHBoxLayout(self)
        self.list_w = QListWidget()
        self.list_w.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_w.customContextMenuRequested.connect(self._on_context_menu)
        self.list_w.itemDoubleClicked.connect(lambda _: self._edit())
        lay.addWidget(self.list_w, 1)

        col = QVBoxLayout()
        for label, slot in (("Nuovo…", self._new), ("Modifica…", self._edit), ("Elimina", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            col.addWidget(b)
        col.addStretch(1)
        close_b = QPushButton("Chiudi")
        close_b.clicked.connect(self.accept)
        col.addWidget(close_b)
        lay.addLayout(col)

        self._reload()

    def _reload(self) -> None:
        self.list_w.clear()
        for e in self.db.list_emulators():
            it = QListWidgetItem(f"{e['name']}   —   {e['exe_path']}")
            it.setData(Qt.UserRole, e["id"])
            self.list_w.addItem(it)

    def _selected_id(self) -> Optional[int]:
        it = self.list_w.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _new(self) -> None:
        dlg = EmulatorEditDialog(self, self.db)
        if dlg.exec() == QDialog.Accepted:
            self._reload()

    def _edit(self) -> None:
        eid = self._selected_id()
        if eid is None:
            return
        emu = self.db.get_emulator(eid)
        if emu and EmulatorEditDialog(self, self.db, emu).exec() == QDialog.Accepted:
            self._reload()

    def _delete(self) -> None:
        eid = self._selected_id()
        if eid is None:
            return
        emu = self.db.get_emulator(eid)
        if emu and QMessageBox.question(self, "Elimina emulatore", f"Eliminare '{emu['name']}'?") == QMessageBox.Yes:
            self.db.delete_emulator(eid)
            self._reload()

    def _on_context_menu(self, pos) -> None:
        item = self.list_w.itemAt(pos)
        if item is None:
            return
        self.list_w.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Modifica Emulatore…", self._edit)
        menu.addSeparator()
        menu.addAction("Elimina Emulatore", self._delete)
        menu.exec(self.list_w.viewport().mapToGlobal(pos))


# --------------------------------------------------------------------------
# Gestione Piattaforme (Wizard / Modale con abbinamento multiplo emulatori)
# --------------------------------------------------------------------------

def _build_profile(text_srcs: list[str], media_srcs: list[str], rating_srcs: list[str]) -> dict[str, dict]:
    return {
        "title": {"enabled": True, "sources": list(text_srcs)},
        "genre": {"enabled": True, "sources": list(text_srcs)},
        "release_year": {"enabled": True, "sources": list(text_srcs)},
        "developer": {"enabled": True, "sources": list(text_srcs)},
        "publisher": {"enabled": True, "sources": list(text_srcs)},
        "players": {"enabled": True, "sources": [s for s in text_srcs if s in ("screenscraper", "thegamesdb")] or list(text_srcs)},
        "description": {"enabled": True, "sources": list(text_srcs)},
        "rating": {"enabled": True, "sources": list(rating_srcs)},
        "boxart": {"enabled": True, "sources": list(media_srcs)},
        "logo": {"enabled": True, "sources": list(media_srcs)},
        "screenshot": {"enabled": True, "sources": list(media_srcs)},
    }


_PROFILE_TEMPLATES: dict[str, dict[str, dict]] = {
    "retro_console": _build_profile(
        ["screenscraper", "igdb", "thegamesdb", "wikipedia"],
        ["libretro", "screenscraper", "igdb", "thegamesdb", "wikipedia"],
        ["igdb", "screenscraper"],
    ),
    "modern_console": _build_profile(
        ["igdb", "thegamesdb", "screenscraper", "steam", "wikipedia"],
        ["igdb", "thegamesdb", "screenscraper", "steam", "wikipedia"],
        ["igdb", "screenscraper"],
    ),
    "arcade": _build_profile(
        ["screenscraper", "igdb", "thegamesdb", "wikipedia"],
        ["screenscraper", "libretro", "igdb", "thegamesdb", "wikipedia"],
        ["igdb", "screenscraper"],
    ),
    "pc_dos": _build_profile(
        ["gog", "steam", "igdb", "screenscraper", "wikipedia"],
        ["gog", "steam", "igdb", "screenscraper", "wikipedia"],
        ["igdb", "steam", "gog"],
    ),
    "retro_computer": _build_profile(
        ["screenscraper", "thegamesdb", "igdb", "wikipedia"],
        ["screenscraper", "libretro", "thegamesdb", "igdb", "wikipedia"],
        ["igdb", "screenscraper"],
    ),
}

# (Nome piattaforma, file in icons/, estensioni tipiche, profilo metadati)
_PLATFORM_PRESETS: list[tuple[str, str, str, str]] = [
    ("Nintendo Entertainment System (NES)", "Nintendo Entertainment System.png", ".nes, .fds, .zip, .7z", "retro_console"),
    ("Super Nintendo (SNES)", "Super Nintendo Entertainment System.png", ".sfc, .smc, .zip, .7z", "retro_console"),
    ("Nintendo 64", "Nintendo 64.png", ".z64, .n64, .v64, .zip, .7z", "retro_console"),
    ("Nintendo GameCube", "Nintendo GameCube.png", ".iso, .gcm, .rvz, .gcz, .ciso", "retro_console"),
    ("Nintendo Wii", "Nintendo Wii.png", ".wbfs, .iso, .rvz, .wad", "retro_console"),
    ("Nintendo Wii U", "Nintendo Wii U.png", ".wud, .wux, .rpx, .wua", "modern_console"),
    ("Nintendo Switch", "Nintendo Switch.png", ".nsp, .xci, .nsz", "modern_console"),
    ("Game Boy", "Nintendo Game Boy.png", ".gb, .zip, .7z", "retro_console"),
    ("Game Boy Color", "Nintendo Game Boy Color.png", ".gbc, .zip, .7z", "retro_console"),
    ("Game Boy Advance", "Nintendo Game Boy Advance.png", ".gba, .zip, .7z", "retro_console"),
    ("Nintendo DS", "Nintendo DS.png", ".nds, .dsi, .zip, .7z", "retro_console"),
    ("Nintendo 3DS", "Nintendo 3DS.png", ".3ds, .cia, .cxi", "retro_console"),
    ("Sony PlayStation", "Sony Playstation.png", ".chd, .cue, .bin, .pbp, .iso", "retro_console"),
    ("Sony PlayStation 2", "Sony Playstation 2.png", ".iso, .chd, .cso, .gz", "retro_console"),
    ("Sony PlayStation 3", "Sony Playstation 3.png", ".iso, .pkg, .bin", "modern_console"),
    ("Sony PSP", "Sony PSP.png", ".iso, .cso, .pbp, .chd", "retro_console"),
    ("Sony PS Vita", "Sony PS Vita.png", ".vpk, .zip", "modern_console"),
    ("Sega Master System", "Sega Master System.png", ".sms, .zip, .7z", "retro_console"),
    ("Sega Mega Drive", "Sega Mega Drive.png", ".md, .gen, .bin, .zip, .7z", "retro_console"),
    ("Sega CD", "Sega CD.png", ".chd, .cue, .iso", "retro_console"),
    ("Sega 32X", "Sega 32X.png", ".32x, .bin, .zip", "retro_console"),
    ("Sega Saturn", "Sega Saturn.png", ".chd, .cue, .iso", "retro_console"),
    ("Sega Dreamcast", "Sega Dreamcast.png", ".chd, .cdi, .gdi", "retro_console"),
    ("Sega Game Gear", "Sega Game Gear.png", ".gg, .zip, .7z", "retro_console"),
    ("Microsoft Xbox", "Microsoft Xbox.png", ".iso, .xiso", "modern_console"),
    ("Microsoft Xbox 360", "Microsoft Xbox 360.png", ".iso, .xex, .god", "modern_console"),
    ("DOS", "DOS.png", ".exe, .bat, .com, .conf, .zip", "pc_dos"),
    ("ScummVM", "ScummVM.png", ".scummvm, .zip", "pc_dos"),
    ("Arcade (MAME)", "Arcade - MAME.png", ".zip, .7z, .chd", "arcade"),
    ("SNK Neo Geo", "SNK Neo Geo.png", ".zip, .7z", "arcade"),
    ("NEC PC Engine", "NEC PC Engine.png", ".pce, .chd, .cue, .zip", "arcade"),
    ("Atari 2600", "Atari 2600.png", ".a26, .bin, .zip", "retro_computer"),
    ("Atari Lynx", "Atari Lynx.png", ".lnx, .zip", "retro_computer"),
    ("Commodore Amiga", "Commodore Amiga.png", ".adf, .lha, .hdf, .ipf, .zip", "retro_computer"),
    ("Commodore 64", "Commodore 64.png", ".d64, .t64, .prg, .crt, .zip", "retro_computer"),
]


def _find_orig_logo(logo_path: str) -> str:
    if not logo_path or not os.path.isfile(logo_path):
        return ""
    folder = os.path.dirname(logo_path)
    for ext in IMAGE_EXTS:
        cand = os.path.join(folder, f"_platform_logo_orig{ext}")
        if os.path.isfile(cand) and not _looks_like_white_bg_logo(cand):
            return cand
    return ""


def _looks_like_white_bg_logo(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    img = QImage(path)
    if img.isNull() or img.width() < 8 or img.height() < 8:
        return False
    if img.width() > 256 or img.height() > 256:
        img = img.scaled(256, 256, Qt.KeepAspectRatio, Qt.FastTransformation)
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    # Controlla i punti medi dei 4 lati (funziona sia su rettangolo pieno che arrotondato)
    samples = [
        (w // 2, min(2, h - 1)),
        (w // 2, max(0, h - 3)),
        (min(2, w - 1), h // 2),
        (max(0, w - 3), h // 2),
    ]
    for x, y in samples:
        c = img.pixelColor(x, y)
        if c.alpha() < 240 or c.red() < 242 or c.green() < 242 or c.blue() < 242:
            return False
    return True


def _strip_white_bg_logo(src_path: str, out_path: str) -> bool:
    from collections import deque

    img = QImage(src_path)
    if img.isNull():
        return False
    if max(img.width(), img.height()) > 1000:
        img = img.scaled(1000, 1000, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    buf = memoryview(img.bits())

    def _is_removable(idx: int) -> bool:
        b = buf[idx]
        g = buf[idx + 1]
        r = buf[idx + 2]
        a = buf[idx + 3]
        return a < 16 or (r >= 240 and g >= 240 and b >= 240)

    visited = bytearray(w * h)
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        for y in (0, h - 1):
            p = y * w + x
            if not visited[p] and _is_removable(p * 4):
                visited[p] = 1
                q.append((x, y))
    for y in range(1, h - 1):
        for x in (0, w - 1):
            p = y * w + x
            if not visited[p] and _is_removable(p * 4):
                visited[p] = 1
                q.append((x, y))

    while q:
        cx, cy = q.popleft()
        idx = (cy * w + cx) * 4
        buf[idx + 3] = 0
        if cx > 0:
            np = cy * w + (cx - 1)
            if not visited[np] and _is_removable(np * 4):
                visited[np] = 1
                q.append((cx - 1, cy))
        if cx + 1 < w:
            np = cy * w + (cx + 1)
            if not visited[np] and _is_removable(np * 4):
                visited[np] = 1
                q.append((cx + 1, cy))
        if cy > 0:
            np = (cy - 1) * w + cx
            if not visited[np] and _is_removable(np * 4):
                visited[np] = 1
                q.append((cx, cy - 1))
        if cy + 1 < h:
            np = (cy + 1) * w + cx
            if not visited[np] and _is_removable(np * 4):
                visited[np] = 1
                q.append((cx, cy + 1))

    buf.release()
    img = _trim_transparent_margins(img)
    return img.save(out_path, "PNG")


def _trim_transparent_margins(img: QImage) -> QImage:
    if img.isNull():
        return img
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    buf = memoryview(img.bits())
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
        return img.copy(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)
    return img


class PlatformDialog(QDialog):
    def __init__(self, parent: QWidget, db: Database, plat: Optional[sqlite3.Row] = None, parent_id: Optional[int] = None):
        super().__init__(parent)
        self.db = db
        self.plat = plat
        self.parent_id = parent_id if plat is None else (plat["parent_id"] if "parent_id" in plat.keys() else None)
        self.saved_id: Optional[int] = None
        
        parent_plat = self.db.get_platform(self.parent_id) if self.parent_id else None
        if plat:
            title = f"Modifica Sotto-sezione ({parent_plat['name']})" if parent_plat else "Modifica Piattaforma"
        else:
            title = f"Nuova Sotto-sezione di {parent_plat['name']}" if parent_plat else "Nuova Piattaforma"
        self.setWindowTitle(title)
        self.resize(640, 620)

        lay = QVBoxLayout(self)
        form = QFormLayout()

        name_label = "Nome Sotto-sezione:" if parent_plat else "Nome Piattaforma:"
        self.name_ed = QLineEdit(plat["name"] if plat else "")

        img_filter = f"Immagini ({' '.join('*' + e for e in IMAGE_EXTS)} *.ico);;Tutti (*.*)"
        logo_init = db.resolve(plat["logo_path"]) if (plat and "logo_path" in plat.keys() and plat["logo_path"]) else ""
        logo_row, self.logo_ed = _browse_row(self, logo_init, file_filter=img_filter)
        _btn_dis_css = "QPushButton:disabled { background-color: #1a1d24; color: #5c6375; border: 1px solid #2b303c; }"
        self.logo_web_btn = QPushButton("🌐 Cerca su Google")
        self.logo_web_btn.setStyleSheet(_btn_dis_css)
        self.logo_web_btn.setToolTip("Apre Google Immagini affiancato per trascinare il logo qui")
        self.logo_web_btn.clicked.connect(self._pick_logo_web)
        self.logo_close_browser_btn = QPushButton("✕ Chiudi browser")
        self.logo_close_browser_btn.setStyleSheet(_btn_dis_css)
        self.logo_close_browser_btn.setEnabled(False)
        self.logo_close_browser_btn.setToolTip("Chiude la finestra del browser aperta per il logo e riporta la finestra al centro")
        self.logo_close_browser_btn.clicked.connect(lambda: _close_side_by_side_browser(self))

        def _on_logo_browser_state(is_open: bool) -> None:
            self.logo_web_btn.setEnabled(not is_open)
            self.logo_close_browser_btn.setEnabled(is_open)

        orig_init = _find_orig_logo(logo_init)
        if orig_init:
            self._logo_raw_path: str = orig_init
            self._logo_has_white_bg: bool = True
        elif logo_init and _looks_like_white_bg_logo(logo_init):
            self._logo_raw_path = ""
            self._logo_has_white_bg = True
        else:
            self._logo_raw_path = logo_init
            self._logo_has_white_bg = False
        self._applying_white_bg: bool = False
        self._browser_state_cb = _on_logo_browser_state
        self.logo_del_btn = QPushButton("🗑")
        self.logo_del_btn.setFixedWidth(34)
        self.logo_del_btn.setStyleSheet("padding: 5px;")
        self.logo_del_btn.setToolTip("Rimuovi il logo selezionato")
        self.logo_del_btn.setEnabled(bool(logo_init))
        self.logo_del_btn.clicked.connect(self._clear_logo)
        logo_btns_w = QWidget()
        logo_btns_lay = QHBoxLayout(logo_btns_w)
        logo_btns_lay.setContentsMargins(0, 0, 0, 0)
        logo_btns_lay.setSpacing(6)
        logo_btns_lay.addWidget(self.logo_web_btn)
        logo_btns_lay.addWidget(self.logo_close_browser_btn)
        logo_btns_lay.addWidget(self.logo_del_btn)
        logo_btns_lay.addStretch(1)

        icon_init = db.resolve(plat["icon_path"]) if (plat and plat["icon_path"]) else ""
        icons_dir = os.path.join(db.root, "icons")
        icon_row, self.icon_ed = _browse_row(
            self, icon_init, file_filter="Immagini (*.png *.jpg *.jpeg *.ico *.webp);;Tutti (*.*)",
            default_dir=icons_dir, prefer_default=True,
        )
        rom_init = db.resolve(plat["rom_dir"]) if (plat and plat["rom_dir"]) else ""
        rom_row, self.rom_ed = _browse_row(self, rom_init, choose_dir=True)

        pkeys = plat.keys() if plat else ()
        scan_sub_init = bool(plat["scan_subdirs"]) if (plat and "scan_subdirs" in pkeys) else True
        keep_str_init = bool(plat["keep_structure"]) if (plat and "keep_structure" in pkeys) else False

        scan_opts_w = QWidget()
        scan_opts_lay = QHBoxLayout(scan_opts_w)
        scan_opts_lay.setContentsMargins(0, 0, 0, 0)
        scan_opts_lay.setSpacing(14)
        self.scan_subdirs_chk = QCheckBox("Scansiona sottocartelle")
        self.scan_subdirs_chk.setChecked(scan_sub_init)
        self.keep_structure_chk = QCheckBox("Mantieni struttura cartelle (crea sotto-sezioni)")
        self.keep_structure_chk.setChecked(scan_sub_init and keep_str_init)
        self.keep_structure_chk.setEnabled(scan_sub_init)

        def _on_scan_subdirs_toggled(checked: bool) -> None:
            self.keep_structure_chk.setEnabled(checked)
            if not checked:
                self.keep_structure_chk.setChecked(False)

        self.scan_subdirs_chk.toggled.connect(_on_scan_subdirs_toggled)
        scan_opts_lay.addWidget(self.scan_subdirs_chk)
        scan_opts_lay.addWidget(self.keep_structure_chk)
        scan_opts_lay.addStretch(1)

        self.ext_ed = QLineEdit(plat["extensions"] if plat else ".zip, .7z")

        self.preset_cb: Optional[QComboBox] = None
        if plat is None and parent_plat is None:
            self.preset_cb = QComboBox()
            self.preset_cb.addItem("— Personalizzata (inserisci a mano) —", None)
            icons_root = os.path.join(db.root, "icons")
            for p_name, p_icon, p_exts, p_prof in sorted(_PLATFORM_PRESETS, key=lambda x: x[0].lower()):
                icon_file = os.path.join(icons_root, p_icon)
                ic = QIcon(icon_file) if os.path.isfile(icon_file) else QIcon()
                self.preset_cb.addItem(ic, p_name, (p_name, p_icon, p_exts, p_prof))
            self.preset_cb.currentIndexChanged.connect(self._on_preset_selected)
            form.addRow("Dispositivo / Console:", self.preset_cb)

        form.addRow(name_label, self.name_ed)
        form.addRow("Logo (opzionale):", logo_row)
        form.addRow("", logo_btns_w)

        _logo_norm_css = (
            "QFrame#LogoDropCard { background: transparent; border: 1px solid transparent; border-radius: 8px; }"
        )
        _logo_hover_css = (
            "QFrame#LogoDropCard { background: #212632; border: 2px dashed #f58a1f; border-radius: 8px; }"
        )
        logo_prev_wrap = _DropImageCard(normal_style=_logo_norm_css, hover_style=_logo_hover_css)
        logo_prev_wrap.setObjectName("LogoDropCard")
        logo_prev_wrap.setToolTip("Trascina qui un logo da Chrome o dal PC")
        logo_prev_wrap.image_dropped.connect(self.logo_ed.setText)
        logo_prev_wrap.setFixedHeight(114)
        lp_lay = QVBoxLayout(logo_prev_wrap)
        lp_lay.setContentsMargins(6, 4, 6, 4)
        lp_lay.setSpacing(3)
        self.logo_dim_lbl = QLabel("Nessun logo")
        self.logo_dim_lbl.setObjectName("MutedHint")
        self.logo_dim_lbl.setStyleSheet("font-size: 11px;")
        self.logo_preview = _ScaledPreviewLabel("Nessuna anteprima logo")
        self.logo_preview.setMinimumWidth(180)
        self.logo_preview.setFixedHeight(84)
        self.logo_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.logo_preview.setStyleSheet(
            "background: #12151c; border: 1px dashed #2b313e; border-radius: 6px; color: #6c7486;"
        )
        lp_lay.addWidget(self.logo_dim_lbl)
        preview_row = QHBoxLayout()
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.setSpacing(8)
        preview_row.addWidget(self.logo_preview, 1)
        self.white_bg_btn = QPushButton("Sfondo bianco")
        self.white_bg_btn.setToolTip("Applica in automatico uno sfondo bianco dietro il logo")
        self.white_bg_btn.setEnabled(False)
        self.white_bg_btn.clicked.connect(self._apply_white_logo_bg)
        preview_row.addWidget(self.white_bg_btn, 0, Qt.AlignVCenter)
        lp_lay.addLayout(preview_row)
        self.logo_ed.textChanged.connect(self._on_logo_text_changed)
        self._refresh_logo_preview()
        form.addRow("Anteprima Logo:", logo_prev_wrap)

        form.addRow("Icona:", icon_row)
        icon_credit = QLabel(
            'Icone predefinite: <a style="color:#9ba2b2;" href="https://github.com/libretro/retroarch-assets">'
            'Libretro / RetroArch Assets</a> (CC BY 4.0)'
        )
        icon_credit.setObjectName("MutedHint")
        icon_credit.setStyleSheet("font-size: 10.5px; color: #7e8699;")
        icon_credit.setOpenExternalLinks(True)
        form.addRow("", icon_credit)
        form.addRow("Cartella ROM (opzionale):", rom_row)
        form.addRow("", scan_opts_w)
        form.addRow("Estensioni supportate:", self.ext_ed)
        lay.addLayout(form)

        # Sezione abbinamento emulatori
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Emulatori abbinati (seleziona uno o più emulatori e scegli il predefinito):"))
        add_emu_btn = QPushButton("＋ Nuovo Emulatore…")
        add_emu_btn.clicked.connect(self._quick_new_emulator)
        hdr.addStretch(1)
        hdr.addWidget(add_emu_btn)
        lay.addLayout(hdr)

        self.emu_table = QTableWidget(0, 4)
        self.emu_table.setHorizontalHeaderLabels(["Abbina", "Predefinito", "Emulatore", "Argomenti specifici (opz.)"])
        self.emu_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.emu_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.emu_table.verticalHeader().setVisible(False)
        self.emu_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.emu_table.customContextMenuRequested.connect(self._on_emu_table_context)
        lay.addWidget(self.emu_table, 1)

        self._populate_emulators()

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.name_ed.setFocus()

    def _populate_emulators(self) -> None:
        linked: dict[int, sqlite3.Row] = {}
        if self.plat is not None:
            linked = {r["id"]: r for r in self.db.platform_emulators(self.plat["id"])}

        emus = self.db.list_emulators()
        self.emu_table.setRowCount(len(emus))
        self._radios: list[QRadioButton] = []

        for row_idx, e in enumerate(emus):
            eid = e["id"]
            info = linked.get(eid)

            chk = QCheckBox()
            chk.setCursor(Qt.PointingHandCursor)
            chk.setChecked(info is not None)
            chk_wrap = QWidget()
            cl = QHBoxLayout(chk_wrap)
            cl.setAlignment(Qt.AlignCenter)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.addWidget(chk)

            rb = QRadioButton()
            rb.setCursor(Qt.PointingHandCursor)
            rb.setChecked(bool(info and info["is_default"]))
            self._radios.append(rb)
            # Solo una radio attiva tra tutte le righe
            rb.toggled.connect(lambda checked, idx=row_idx: self._on_radio(idx, checked))
            rb_wrap = QWidget()
            rl = QHBoxLayout(rb_wrap)
            rl.setAlignment(Qt.AlignCenter)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.addWidget(rb)

            name_it = QTableWidgetItem(e["name"])
            name_it.setFlags(name_it.flags() & ~Qt.ItemIsEditable)
            name_it.setData(Qt.UserRole, eid)

            override_it = QTableWidgetItem(info["args_override"] if info else "")

            self.emu_table.setCellWidget(row_idx, 0, chk_wrap)
            self.emu_table.setCellWidget(row_idx, 1, rb_wrap)
            self.emu_table.setItem(row_idx, 2, name_it)
            self.emu_table.setItem(row_idx, 3, override_it)

        # Se clicco sulla riga dell'emulatore, seleziono automaticamente la checkbox
        self.emu_table.cellClicked.connect(self._on_table_cell_clicked)

    def _on_table_cell_clicked(self, row: int, column: int) -> None:
        wrap_chk = self.emu_table.cellWidget(row, 0)
        wrap_rb = self.emu_table.cellWidget(row, 1)
        if wrap_chk and column in (0, 2):
            chk = wrap_chk.findChild(QCheckBox)
            if chk:
                chk.setChecked(not chk.isChecked())
                if chk.isChecked() and not any(r.isChecked() for r in self._radios):
                    if wrap_rb:
                        rb = wrap_rb.findChild(QRadioButton)
                        if rb:
                            rb.setChecked(True)

    def _on_radio(self, active_idx: int, checked: bool) -> None:
        if not checked:
            return
        for idx, rb in enumerate(self._radios):
            if idx != active_idx and rb.isChecked():
                rb.blockSignals(True)
                rb.setChecked(False)
                rb.blockSignals(False)
        # Se marco come predefinito un emulatore, lo spunto anche come abbinato
        wrap = self.emu_table.cellWidget(active_idx, 0)
        if wrap:
            chk = wrap.findChild(QCheckBox)
            if chk and not chk.isChecked():
                chk.setChecked(True)

    def _on_emu_table_context(self, pos) -> None:
        row = self.emu_table.rowAt(pos.y())
        if row < 0:
            return
        item = self.emu_table.item(row, 2)
        if not item:
            return
        eid = item.data(Qt.UserRole)
        emu = self.db.get_emulator(eid)
        if not emu:
            return

        menu = QMenu(self)
        menu.addAction(f"Modifica '{emu['name']}'…", lambda: self._edit_emulator_by_id(eid))
        menu.addSeparator()
        menu.addAction(f"Elimina '{emu['name']}'", lambda: self._delete_emulator_by_id(eid))
        menu.exec(self.emu_table.viewport().mapToGlobal(pos))

    def _edit_emulator_by_id(self, eid: int) -> None:
        emu = self.db.get_emulator(eid)
        if emu and EmulatorEditDialog(self, self.db, emu).exec() == QDialog.Accepted:
            self._populate_emulators()

    def _delete_emulator_by_id(self, eid: int) -> None:
        emu = self.db.get_emulator(eid)
        if emu and QMessageBox.question(self, "Elimina emulatore", f"Eliminare '{emu['name']}'?") == QMessageBox.Yes:
            self.db.delete_emulator(eid)
            self._populate_emulators()

    def _on_logo_text_changed(self) -> None:
        if not self._applying_white_bg:
            p = self.logo_ed.text().strip()
            if p and _looks_like_white_bg_logo(p):
                self._logo_raw_path = ""
                self._logo_has_white_bg = True
            else:
                self._logo_raw_path = p
                self._logo_has_white_bg = False
        self._refresh_logo_preview()

    def _clear_logo(self) -> None:
        dirs_to_clean: set[str] = set()
        plat_name = self.name_ed.text().strip()
        if plat_name:
            dirs_to_clean.add(os.path.join(self.db.root, "media", safe_name(plat_name), "logos"))
        if self.plat and "logo_path" in self.plat.keys() and self.plat["logo_path"]:
            resolved = self.db.resolve(self.plat["logo_path"])
            if resolved:
                dirs_to_clean.add(os.path.dirname(resolved))
        for cur in (self.logo_ed.text().strip(), self._logo_raw_path):
            if cur and os.path.basename(cur).startswith("_platform_logo"):
                dirs_to_clean.add(os.path.dirname(cur))
        for d in dirs_to_clean:
            if not d or not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if fn.startswith("_platform_logo"):
                    try:
                        os.remove(os.path.join(d, fn))
                    except OSError:
                        pass
        if self.plat is not None:
            self.db._conn().execute("UPDATE platforms SET logo_path='' WHERE id=?", (self.plat["id"],))
            self.db._conn().commit()
        self._logo_raw_path = ""
        self._logo_has_white_bg = False
        self.logo_ed.clear()

    def _refresh_logo_preview(self) -> None:
        path = self.logo_ed.text().strip()
        if hasattr(self, "logo_del_btn"):
            self.logo_del_btn.setEnabled(bool(path))
        if hasattr(self, "white_bg_btn"):
            if self._logo_has_white_bg:
                self.white_bg_btn.setText("Rimuovi sfondo bianco")
                self.white_bg_btn.setToolTip("Ripristina il logo originale senza sfondo bianco")
            else:
                self.white_bg_btn.setText("Sfondo bianco")
                self.white_bg_btn.setToolTip("Applica in automatico uno sfondo bianco dietro il logo")
        if path and os.path.isfile(path):
            im = QImage(path)
            if not im.isNull():
                orig_w, orig_h = im.width(), im.height()
                if max(orig_w, orig_h) > 600:
                    im = im.scaled(600, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                im = _trim_transparent_margins(im)
                pm = QPixmap.fromImage(im)
                self.logo_dim_lbl.setText(f"{orig_w}×{orig_h} px")
                self.logo_preview.setPreviewPixmap(pm)
                self.logo_preview.setToolTip(path)
                if hasattr(self, "white_bg_btn"):
                    self.white_bg_btn.setEnabled(True)
                return
        self.logo_preview.setPreviewPixmap(None)
        self.logo_preview.setToolTip("")
        self.logo_dim_lbl.setText("Nessun logo")
        if hasattr(self, "white_bg_btn"):
            self.white_bg_btn.setEnabled(False)

    def _apply_white_logo_bg(self) -> None:
        if self._logo_has_white_bg:
            cur_path = self.logo_ed.text().strip()
            raw_path = self._logo_raw_path
            if not raw_path or not os.path.isfile(raw_path):
                plat_name = self.name_ed.text().strip()
                if plat_name:
                    dest_dir = os.path.join(self.db.root, "media", safe_name(plat_name), "logos")
                    os.makedirs(dest_dir, exist_ok=True)
                    nobg_path = os.path.join(dest_dir, "_platform_logo_nobg.png")
                else:
                    nobg_path = os.path.splitext(cur_path)[0] + "_nobg.png"
                if _strip_white_bg_logo(cur_path, nobg_path):
                    raw_path = nobg_path
                    self._logo_raw_path = nobg_path
                else:
                    return
            self._logo_has_white_bg = False
            self._applying_white_bg = True
            try:
                if raw_path and os.path.abspath(raw_path) != os.path.abspath(cur_path):
                    self.logo_ed.setText(raw_path)
                else:
                    self._refresh_logo_preview()
            finally:
                self._applying_white_bg = False
            return

        path = self._logo_raw_path or self.logo_ed.text().strip()
        if not path or not os.path.isfile(path):
            return
        img = QImage(path)
        if img.isNull():
            return
        if max(img.width(), img.height()) > 1000:
            img = img.scaled(1000, 1000, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        img = _trim_transparent_margins(img)
        pad_x = max(8, img.width() // 45)
        pad_y = max(4, img.height() // 25)
        out_w = img.width() + 2 * pad_x
        out_h = img.height() + 2 * pad_y
        canvas = QImage(out_w, out_h, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        radius = min(10, max(4, min(out_w, out_h) // 8))
        bg_path = QPainterPath()
        bg_path.addRoundedRect(0, 0, out_w, out_h, radius, radius)
        p.fillPath(bg_path, QColor("#ffffff"))
        p.drawImage(pad_x, pad_y, img)
        p.end()

        plat_name = self.name_ed.text().strip()
        if plat_name:
            dest_dir = os.path.join(self.db.root, "media", safe_name(plat_name), "logos")
            os.makedirs(dest_dir, exist_ok=True)
            out_path = os.path.join(dest_dir, "_platform_logo_whitebg.png")
        else:
            out_path = os.path.splitext(path)[0] + "_whitebg.png"

        if canvas.save(out_path, "PNG"):
            self._logo_has_white_bg = True
            self._applying_white_bg = True
            try:
                if os.path.abspath(out_path) == os.path.abspath(self.logo_ed.text().strip()):
                    self._refresh_logo_preview()
                else:
                    self.logo_ed.setText(out_path)
            finally:
                self._applying_white_bg = False

    def _on_preset_selected(self) -> None:
        if self.preset_cb is None:
            return
        data = self.preset_cb.currentData()
        if not data:
            return
        p_name, p_icon, p_exts, _p_prof = data
        self.name_ed.setText(p_name)
        icon_file = os.path.join(self.db.root, "icons", p_icon)
        if os.path.isfile(icon_file):
            self.icon_ed.setText(icon_file)
        self.ext_ed.setText(p_exts)

    def _pick_logo_web(self) -> None:
        name = self.name_ed.text().strip()
        if not name:
            QMessageBox.information(self, "Nome mancante", "Scrivi prima il nome della piattaforma per cercare il logo.")
            self.name_ed.setFocus()
            return
        self.logo_dim_lbl.setText("👉 Clicca il logo su Google per ingrandirlo, poi trascinalo qui")
        _open_browser_side_by_side(self, f"{name} console logo transparent")

    def _quick_new_emulator(self) -> None:
        dlg = EmulatorEditDialog(self, self.db)
        if dlg.exec() == QDialog.Accepted:
            self._populate_emulators()

    def _save(self) -> None:
        name = self.name_ed.text().strip()
        if not name:
            QMessageBox.warning(self, "Nome mancante", "Inserisci il nome della piattaforma.")
            return

        icon_raw = self.icon_ed.text().strip()
        logo_raw = self.logo_ed.text().strip()
        rom_dir = self.rom_ed.text().strip()
        exts = self.ext_ed.text().strip()

        # Copia icona e logo in media/<Console>/logos/ se scelti dall'esterno
        media_logos = os.path.join(self.db.root, "media", safe_name(name), "logos")
        icon_final = icon_raw
        if icon_raw and os.path.isfile(icon_raw):
            os.makedirs(media_logos, exist_ok=True)
            ext = os.path.splitext(icon_raw)[1].lower()
            dst = os.path.join(media_logos, f"_platform_icon{ext}")
            if os.path.abspath(icon_raw) != os.path.abspath(dst):
                shutil.copy2(icon_raw, dst)
            icon_final = dst

        logo_final = logo_raw
        if logo_raw and os.path.isfile(logo_raw):
            os.makedirs(media_logos, exist_ok=True)
            dst_orig_kept = ""
            # Salva prima l'originale, prima che dst_logo sovrascriva _platform_logo.*
            if self._logo_has_white_bg and self._logo_raw_path and os.path.isfile(self._logo_raw_path):
                orig_ext = os.path.splitext(self._logo_raw_path)[1].lower() or ".png"
                dst_orig = os.path.join(media_logos, f"_platform_logo_orig{orig_ext}")
                if os.path.abspath(self._logo_raw_path) != os.path.abspath(dst_orig):
                    shutil.copy2(self._logo_raw_path, dst_orig)
                dst_orig_kept = os.path.abspath(dst_orig)
            ext = os.path.splitext(logo_raw)[1].lower()
            dst_logo = os.path.join(media_logos, f"_platform_logo{ext}")
            if os.path.abspath(logo_raw) != os.path.abspath(dst_logo):
                shutil.copy2(logo_raw, dst_logo)
            logo_final = dst_logo
            # Solo dopo aver copiato dst_logo si possono rimuovere eventuali _platform_logo_orig o temporanei
            for old_ext in IMAGE_EXTS:
                old_orig = os.path.join(media_logos, f"_platform_logo_orig{old_ext}")
                if os.path.isfile(old_orig) and os.path.abspath(old_orig) != dst_orig_kept:
                    try:
                        os.remove(old_orig)
                    except OSError:
                        pass
            for tmp_name in ("_platform_logo_whitebg.png", "_platform_logo_nobg.png"):
                tmp_p = os.path.join(media_logos, tmp_name)
                if os.path.isfile(tmp_p):
                    try:
                        os.remove(tmp_p)
                    except OSError:
                        pass
        elif os.path.isdir(media_logos):
            for old_ext in IMAGE_EXTS:
                old_orig = os.path.join(media_logos, f"_platform_logo_orig{old_ext}")
                if os.path.isfile(old_orig):
                    try:
                        os.remove(old_orig)
                    except OSError:
                        pass

        scan_sub = 1 if self.scan_subdirs_chk.isChecked() else 0
        keep_str = 1 if (scan_sub and self.keep_structure_chk.isChecked()) else 0

        try:
            if self.plat is None:
                pid = self.db.add_platform(
                    name,
                    icon_final,
                    rom_dir,
                    exts,
                    parent_id=self.parent_id,
                    logo_path=logo_final,
                    scan_subdirs=scan_sub,
                    keep_structure=keep_str,
                )
                if self.parent_id is None:
                    from scraper import get_platform_default_fields
                    prof_name = name
                    if self.preset_cb is not None and self.preset_cb.currentData():
                        prof_name = self.preset_cb.currentData()[0]
                    prof = get_platform_default_fields(prof_name)
                    cfg = Config(self.db.root)
                    cfg.data.setdefault("metadata_fields_by_platform", {})[str(pid)] = prof
                    cfg.data["metadata_scope"] = "per_platform"
                    cfg.save()
            else:
                pid = self.plat["id"]
                self.db.update_platform(
                    pid,
                    name,
                    icon_final,
                    rom_dir,
                    exts,
                    parent_id=self.parent_id,
                    logo_path=logo_final,
                    scan_subdirs=scan_sub,
                    keep_structure=keep_str,
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                QMessageBox.warning(self, "Duplicato", f"Esiste già una piattaforma o sotto-sezione chiamata '{name}'.")
            else:
                QMessageBox.warning(self, "Errore database", f"Impossibile salvare la piattaforma:\n{exc}")
            return

        links: list[tuple[int, bool, str]] = []
        for row_idx in range(self.emu_table.rowCount()):
            chk = self.emu_table.cellWidget(row_idx, 0).findChild(QCheckBox)
            rb = self.emu_table.cellWidget(row_idx, 1).findChild(QRadioButton)
            if chk and chk.isChecked():
                eid = self.emu_table.item(row_idx, 2).data(Qt.UserRole)
                override = self.emu_table.item(row_idx, 3).text().strip()
                links.append((eid, bool(rb and rb.isChecked()), override))
        self.db.set_platform_emulators(pid, links)

        self.saved_id = pid
        self.accept()


class PlatformManagerDialog(QDialog):
    platforms_changed = Signal()

    def __init__(self, parent: QWidget, db: Database):
        super().__init__(parent)
        self.db = db
        self.changed = False
        self.setWindowTitle("Gestisci Piattaforme")
        self.resize(640, 400)

        lay = QHBoxLayout(self)
        self.list_w = QListWidget()
        self.list_w.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_w.customContextMenuRequested.connect(self._on_context_menu)
        self.list_w.itemDoubleClicked.connect(lambda _: self._edit())
        lay.addWidget(self.list_w, 1)

        col = QVBoxLayout()
        for label, slot in (
            ("Nuova Piattaforma…", self._new),
            ("Modifica…", self._edit),
            ("Scansiona ROM", self._scan),
            ("Elimina", self._delete),
        ):
            b = QPushButton(label)
            b.clicked.connect(slot)
            col.addWidget(b)
        col.addStretch(1)
        close_b = QPushButton("Chiudi")
        close_b.clicked.connect(self.accept)
        col.addWidget(close_b)
        lay.addLayout(col)

        self._reload()

    def _reload(self) -> None:
        self.list_w.clear()
        for p in self.db.list_platforms():
            emus = self.db.platform_emulators(p["id"])
            emu_lbl = emus[0]["name"] if emus else "Nessun emulatore"
            it = QListWidgetItem(f"{p['name']}  ({p['game_count']} giochi)   —   {emu_lbl}")
            it.setData(Qt.UserRole, p["id"])
            self.list_w.addItem(it)

    def _selected_id(self) -> Optional[int]:
        it = self.list_w.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _new(self) -> None:
        dlg = PlatformDialog(self, self.db)
        if dlg.exec() == QDialog.Accepted:
            if dlg.saved_id is not None:
                plat = self.db.get_platform(dlg.saved_id)
                if plat and plat["rom_dir"]:
                    self.db.scan_platform(dlg.saved_id)
            self.changed = True
            self._reload()
            self.platforms_changed.emit()

    def _edit(self) -> None:
        pid = self._selected_id()
        if pid is None:
            return
        plat = self.db.get_platform(pid)
        if plat and PlatformDialog(self, self.db, plat).exec() == QDialog.Accepted:
            new_plat = self.db.get_platform(pid)
            if new_plat and new_plat["rom_dir"]:
                changed_scan_cfg = (
                    new_plat["rom_dir"] != plat["rom_dir"]
                    or new_plat["extensions"] != plat["extensions"]
                    or new_plat["scan_subdirs"] != plat["scan_subdirs"]
                    or new_plat["keep_structure"] != plat["keep_structure"]
                    or not self.db.list_games(pid, include_subplatforms=False)
                )
                if changed_scan_cfg:
                    self.db.scan_platform(pid)
            self.changed = True
            self._reload()
            self.platforms_changed.emit()

    def _scan(self) -> None:
        pid = self._selected_id()
        if pid is None:
            return
        res = self.db.scan_platform(pid)
        self.changed = True
        self._reload()
        QMessageBox.information(
            self, "Scansione completata", f"Aggiunti: {res['added']}\nRimossi: {res['removed']}"
        )

    def _delete(self) -> None:
        pid = self._selected_id()
        if pid is None:
            return
        plat = self.db.get_platform(pid)
        if plat and QMessageBox.question(
            self,
            "Elimina piattaforma",
            f"Eliminare '{plat['name']}' e tutti i suoi giochi e metadati/immagini?\n(I file ROM originali su disco non verranno toccati)",
        ) == QMessageBox.Yes:
            self.db.delete_platform(pid)
            self.changed = True
            self._reload()
            self.platforms_changed.emit()

    def _on_context_menu(self, pos) -> None:
        it = self.list_w.itemAt(pos)
        if it is None:
            return
        self.list_w.setCurrentItem(it)
        menu = QMenu(self)
        menu.addAction("Modifica…", self._edit)
        menu.addAction("Scansiona ROM", self._scan)
        menu.addSeparator()
        menu.addAction("Elimina", self._delete)
        menu.exec(self.list_w.viewport().mapToGlobal(pos))


# --------------------------------------------------------------------------
# Selezione immagine dal web stile Playnite (griglia pulita con risoluzioni)
# --------------------------------------------------------------------------

_WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class _ImageSearchWorker(QThread):
    """Cerca immagini reali da fonti aperte/pubbliche (Libretro, Steam, Wikipedia/Wikimedia, API opzionali)."""

    results_ready = Signal(int, list)  # (generation, [url, ...])
    failed = Signal(int, str)

    def __init__(
        self,
        query: str,
        platform_name: str,
        card_offset: int,
        generation: int,
        search_mode: str = "game",
        db_root: str = "",
        kind: str = "boxart",
    ):
        super().__init__()
        self.query = query
        self.platform_name = platform_name.lower().strip()
        self.card_offset = card_offset
        self.generation = generation
        self.search_mode = search_mode
        self.db_root = db_root
        self.kind = kind

    def run(self) -> None:
        try:
            from urllib.parse import quote
            from scraper import LibretroScraper, TheGamesDBScraper, IGDBScraper
            from database import Config

            s = requests.Session()
            s.headers.update({"User-Agent": "EasyLauncher/1.0 (portable-retro-frontend)"})
            if self.search_mode == "logo":
                import re as _re
                _stop = {"logo", "logos", "wordmark", "icon", "png", "svg", "clearlogo", "banner", "transparent", "vector"}
                _raw_words = _re.findall(r"[A-Za-z0-9]+", self.query)
                core_words = [w for w in _raw_words if w.lower() not in _stop] or _raw_words
                core_query = " ".join(core_words)
                core_alnum = "".join(w.lower() for w in core_words)
                _bad_terms = (
                    "commons-logo", "wikimedia", "wikipedia", "wikidata", "wikiquote",
                    "edit-clear", "mesen", "emulator", "logopedia", "infowhite",
                    "collection charles cros", "promotional demo", "from ", "border",
                )

                def _is_coherent_logo(title: str) -> bool:
                    tl = title.lower()
                    if tl.endswith((".jpg", ".jpeg", ".gif")):
                        return False
                    if any(b in tl for b in _bad_terms):
                        return False
                    alnum = _re.sub(r"[^a-z0-9]+", "", tl.removeprefix("file:"))
                    return all(w.lower() in alnum for w in core_words)

                def _score_logo(title: str) -> int:
                    tl = title.lower().removeprefix("file:")
                    alnum = _re.sub(r"[^a-z0-9]+", "", tl)
                    sc = 0
                    if alnum.startswith(core_alnum):
                        sc += 12
                    if tl.endswith(".svg"):
                        sc += 6
                    elif tl.endswith(".png"):
                        sc += 4
                    if "logo" in tl or "wordmark" in tl:
                        sc += 5
                    if " - " in tl or " for " in tl:
                        sc -= 6
                    return sc

                scored_urls: list[tuple[int, str]] = []
                seen_logo_urls: set[str] = set()

                def _push_logo(title: str, u: str, bonus: int = 0) -> None:
                    base_u = u.split("?")[0] if u else ""
                    if u and base_u not in seen_logo_urls:
                        seen_logo_urls.add(base_u)
                        scored_urls.append((_score_logo(title) + bonus, u))

                # 1. Pagina Wikipedia della console (en.wikipedia.org, inclusi loghi locali)
                if self.card_offset == 0 and (core_query or self.platform_name):
                    wiki_title = core_query or self.platform_name
                    try:
                        wr = s.get(
                            "https://en.wikipedia.org/w/api.php",
                            params={
                                "action": "query",
                                "titles": wiki_title,
                                "redirects": "1",
                                "prop": "images",
                                "imlimit": "25",
                                "format": "json",
                            },
                            timeout=8,
                        )
                        if wr.ok:
                            wpages = list((wr.json().get("query", {}).get("pages") or {}).values())
                            titles = [
                                im["title"]
                                for wp in wpages
                                for im in (wp.get("images") or [])
                                if ("logo" in im.get("title", "").lower() or "wordmark" in im.get("title", "").lower())
                                and not any(bad in im.get("title", "").lower() for bad in _bad_terms)
                            ]
                            if titles:
                                for api_base in ("https://en.wikipedia.org/w/api.php", "https://commons.wikimedia.org/w/api.php"):
                                    ir = s.get(
                                        api_base,
                                        params={
                                            "action": "query",
                                            "titles": "|".join(titles[:12]),
                                            "prop": "imageinfo",
                                            "iiprop": "url",
                                            "iiurlwidth": "500",
                                            "format": "json",
                                        },
                                        timeout=8,
                                    )
                                    if ir.ok:
                                        for p in (ir.json().get("query", {}).get("pages") or {}).values():
                                            ii = (p.get("imageinfo") or [{}])[0]
                                            u = ii.get("thumburl") or ii.get("url") or ""
                                            if u:
                                                _push_logo(p.get("title", ""), u, bonus=10)
                    except Exception:
                        pass

                # 2. Logopedia (logos.fandom.com — archivio completo di loghi console e varianti ufficiali)
                if core_query:
                    if self.card_offset == 0:
                        try:
                            lp = s.get(
                                "https://logos.fandom.com/api.php",
                                params={
                                    "action": "query",
                                    "titles": core_query,
                                    "redirects": "1",
                                    "prop": "images",
                                    "imlimit": "30",
                                    "format": "json",
                                },
                                timeout=8,
                            )
                            if lp.ok:
                                lpages = list((lp.json().get("query", {}).get("pages") or {}).values())
                                ltitles = [
                                    im["title"]
                                    for wp in lpages
                                    for im in (wp.get("images") or [])
                                    if _is_coherent_logo(im.get("title", ""))
                                ]
                                if ltitles:
                                    li = s.get(
                                        "https://logos.fandom.com/api.php",
                                        params={
                                            "action": "query",
                                            "titles": "|".join(ltitles[:20]),
                                            "prop": "imageinfo",
                                            "iiprop": "url",
                                            "iiurlwidth": "500",
                                            "format": "json",
                                        },
                                        timeout=8,
                                    )
                                    if li.ok:
                                        for p in (li.json().get("query", {}).get("pages") or {}).values():
                                            ii = (p.get("imageinfo") or [{}])[0]
                                            u = ii.get("thumburl") or ii.get("url") or ""
                                            if u:
                                                _push_logo(p.get("title", ""), u, bonus=8)
                        except Exception:
                            pass

                    try:
                        ls = s.get(
                            "https://logos.fandom.com/api.php",
                            params={
                                "action": "query",
                                "generator": "search",
                                "gsrsearch": core_query,
                                "gsrnamespace": "6",
                                "gsrlimit": "25",
                                "gsroffset": str(self.card_offset * 6),
                                "prop": "imageinfo",
                                "iiprop": "url|mime",
                                "iiurlwidth": "500",
                                "format": "json",
                            },
                            timeout=10,
                        )
                        if ls.ok:
                            for p in (ls.json().get("query", {}).get("pages") or {}).values():
                                t = p.get("title", "")
                                if not _is_coherent_logo(t):
                                    continue
                                ii = (p.get("imageinfo") or [{}])[0]
                                u = ii.get("thumburl") or ii.get("url") or ""
                                if u:
                                    _push_logo(t, u, bonus=4)
                    except Exception:
                        pass

                # 3. Wikimedia Commons (solo intitle: sui termini della piattaforma, mai ricerca generica rumorosa)
                if core_words:
                    q_intitle = " ".join(f"intitle:{w}" for w in core_words)
                    if self.card_offset == 0:
                        q_intitle += " intitle:logo"
                    try:
                        r = s.get(
                            "https://commons.wikimedia.org/w/api.php",
                            params={
                                "action": "query",
                                "generator": "search",
                                "gsrsearch": q_intitle,
                                "gsrnamespace": "6",
                                "gsrlimit": "20",
                                "gsroffset": str(max(0, self.card_offset - 3) * 6),
                                "prop": "imageinfo",
                                "iiprop": "url|mime",
                                "iiurlwidth": "500",
                                "format": "json",
                            },
                            timeout=10,
                        )
                        if r.ok:
                            for p in (r.json().get("query", {}).get("pages") or {}).values():
                                t = p.get("title", "")
                                if not _is_coherent_logo(t):
                                    continue
                                ii = (p.get("imageinfo") or [{}])[0]
                                u = ii.get("thumburl") or ii.get("url") or ""
                                if u:
                                    _push_logo(t, u, bonus=6)
                    except Exception:
                        pass

                scored_urls.sort(key=lambda item: item[0], reverse=True)
                self.results_ready.emit(self.generation, [u for _, u in scored_urls])
                return

            # search_mode == "game": fonti coerenti per videogiochi
            urls: list[str] = []
            seen_urls: set[str] = set()

            def _add_url(u: str) -> None:
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    urls.append(u)

            if self.card_offset == 0:
                # 1. Libretro Thumbnails (open source, nessuna API key, con supporto articoli No-Intro)
                repo = LibretroScraper._resolve_repo(self.platform_name)
                if repo:
                    safe_q = self.query.replace("&", "_").strip()
                    variants = [safe_q]
                    for art in ("A ", "An ", "The "):
                        if safe_q.lower().startswith(art.lower()):
                            variants.append(f"{safe_q[len(art):]}, {safe_q[:len(art) - 1]}")
                    candidates: list[str] = []
                    for v in variants:
                        for reg in (" (USA)", " (Europe)", " (World)", " (Japan)", " (Italy)", " (USA, Europe)", ""):
                            candidates.append(f"{v}{reg}")

                    folder_order = {
                        "boxart": ("Named_Boxarts", "Named_Snaps", "Named_Titles"),
                        "screenshots": ("Named_Snaps", "Named_Titles", "Named_Boxarts"),
                        "logos": ("Named_Titles", "Named_Boxarts", "Named_Snaps"),
                    }.get(self.kind, ("Named_Boxarts", "Named_Snaps", "Named_Titles"))

                    for folder in folder_order:
                        for cand in candidates:
                            u = f"{LibretroScraper.BASE}/{quote(repo)}/{folder}/{quote(cand)}.png"
                            try:
                                hr = s.head(u, timeout=4)
                                if hr.status_code == 200:
                                    _add_url(u)
                                    break
                            except Exception:
                                pass

                # 2. Scraper configurati tramite ScraperManager (tutti i provider pertinenti, incluso LaunchBox/Nintendo/ADB)
                if self.db_root:
                    db_tmp: Optional[Database] = None
                    try:
                        db_tmp = Database(self.db_root)
                        cfg = Config(self.db_root)
                        mgr = ScraperManager(db_tmp, cfg)
                        registry = mgr._build_registry()
                        q_norm = re.sub(r"\s*-\s*", "-", self.query).strip()
                        q_norm = re.sub(r"(\d+)-in\s+(\d+)", r"\1-in-\2", q_norm, flags=re.I)
                        for sname in ("launchbox", "igdb", "nintendo", "adb", "thegamesdb", "screenscraper", "steamgriddb", "gog"):
                            sc = registry.get(sname)
                            if sc is None or not sc.is_configured():
                                continue
                            try:
                                res = sc.search(q_norm, self.platform_name)
                            except Exception:
                                res = None
                            if not res:
                                continue
                            if self.kind == "screenshots":
                                ordered = tuple(res.extra.get("screenshots") or []) + (res.screenshot_url, res.boxart_url, res.logo_url)
                            elif self.kind == "logos":
                                ordered = (res.logo_url, res.boxart_url, res.screenshot_url)
                            else:
                                ordered = (res.boxart_url, res.screenshot_url, res.logo_url)
                            for u in ordered:
                                _add_url(u)
                    except Exception:
                        pass
                    finally:
                        if db_tmp is not None:
                            db_tmp.close()

                # 2b. Galleria multipla da LaunchBox (estrae tutte le immagini della sezione richiesta)
                try:
                    from scraper import LaunchBoxScraper
                    q_norm = re.sub(r"\s*-\s*", "-", self.query).strip()
                    lb = LaunchBoxScraper(s, "it")
                    res_lb = lb.search(q_norm, self.platform_name)
                    if res_lb and res_lb.extra.get("detail_url"):
                        rd = s.get(res_lb.extra["detail_url"], headers={"User-Agent": lb._WEB_UA}, timeout=8)
                        if rd.ok:
                            sec_kw = {"screenshots": "Screenshot", "logos": "Clear Logo", "boxart": "Box -"}.get(self.kind, "Screenshot")
                            for sec_m in re.finditer(rf"<h3[^>]*>{re.escape(sec_kw)}[^<]*</h3>(.*?)(?=<h3|</section|$)", rd.text, re.S | re.I):
                                for img_u in re.findall(r'<img[^>]+src="(https://images\.launchbox-app\.com/[^"]+)"', sec_m.group(1)):
                                    _add_url(img_u)
                except Exception:
                    pass

                # 3. Steam Store public API (solo se il titolo corrisponde bene)
                try:
                    sr = s.get(
                        "https://store.steampowered.com/api/storesearch/",
                        params={"term": self.query, "cc": "us", "l": "en"},
                        timeout=6,
                    )
                    if sr.ok:
                        q_words = {w.lower() for w in re.findall(r"[a-z0-9]+", self.query) if len(w) > 2}
                        for item in (sr.json().get("items") or [])[:4]:
                            iname = (item.get("name") or "").lower()
                            if q_words and not all(w in iname for w in q_words):
                                continue
                            appid = item.get("id")
                            if not appid:
                                continue
                            if self.kind == "screenshots":
                                try:
                                    ap = s.get(
                                        "https://store.steampowered.com/api/appdetails",
                                        params={"appids": appid, "l": "en"},
                                        timeout=6,
                                    )
                                    if ap.ok:
                                        adata = ((ap.json() or {}).get(str(appid), {}) or {}).get("data") or {}
                                        for sh in (adata.get("screenshots") or [])[:8]:
                                            _add_url(sh.get("path_full") or "")
                                except Exception:
                                    pass
                            for path in (
                                f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900_2x.jpg",
                                f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
                            ):
                                try:
                                    hr = s.head(path, timeout=3)
                                    if hr.status_code == 200:
                                        _add_url(path)
                                except Exception:
                                    pass
                except Exception:
                    pass

                # 4. Wikipedia (solo pagine effettivamente di videogiochi, mai film/libri/foto generiche)
                try:
                    wiki_queries = [f"{self.query} {self.platform_name} video game", f"{self.query} video game"]
                    chosen_title = ""
                    for wq in wiki_queries:
                        ws = s.get(
                            "https://en.wikipedia.org/w/api.php",
                            params={
                                "action": "query",
                                "list": "search",
                                "srsearch": wq,
                                "srlimit": "5",
                                "format": "json",
                            },
                            timeout=8,
                        )
                        if not ws.ok:
                            continue
                        q_tokens = [w.lower() for w in re.findall(r"[a-z0-9]+", self.query) if len(w) > 2]
                        for hit in (ws.json().get("query", {}).get("search") or []):
                            htitle = hit.get("title", "")
                            ht_low = htitle.lower()
                            snip = re.sub(r"<[^>]+>", "", hit.get("snippet", "")).lower()
                            if any(bad in ht_low for bad in ("(film)", "(movie)", "(short story)", "(novel)", "(album)", "(song)")):
                                continue
                            if q_tokens and not all(tok in ht_low for tok in q_tokens):
                                continue
                            if "video game" in ht_low or "video game" in snip or (self.platform_name and self.platform_name in snip):
                                chosen_title = htitle
                                break
                        if chosen_title:
                            break
                    if chosen_title:
                        wi = s.get(
                            "https://en.wikipedia.org/w/api.php",
                            params={
                                "action": "query",
                                "titles": chosen_title,
                                "prop": "pageimages|images",
                                "pithumbsize": "1200",
                                "imlimit": "15",
                                "format": "json",
                            },
                            timeout=8,
                        )
                        if wi.ok:
                            wpages = list((wi.json().get("query", {}).get("pages") or {}).values())
                            im_titles: list[str] = []
                            for wp in wpages:
                                thumb = (wp.get("thumbnail") or {}).get("source")
                                if thumb:
                                    _add_url(thumb)
                                for im in (wp.get("images") or []):
                                    t = im.get("title", "")
                                    tl = t.lower()
                                    if any(
                                        bad in tl
                                        for bad in (
                                            "commons-logo",
                                            "wikimedia",
                                            "wikipedia",
                                            "wikidata",
                                            "wikiquote",
                                            "edit-clear",
                                            "padlock",
                                            "question_book",
                                            "ambox",
                                            "flag_of_",
                                            "nuvola_",
                                            "crystal_clear",
                                        )
                                    ):
                                        continue
                                    if tl.endswith((".png", ".jpg", ".jpeg", ".webp")):
                                        im_titles.append(t)
                            if im_titles:
                                ir = s.get(
                                    "https://en.wikipedia.org/w/api.php",
                                    params={
                                        "action": "query",
                                        "titles": "|".join(im_titles[:10]),
                                        "prop": "imageinfo",
                                        "iiprop": "url",
                                        "iiurlwidth": "1200",
                                        "format": "json",
                                    },
                                    timeout=8,
                                )
                                if ir.ok:
                                    for p in (ir.json().get("query", {}).get("pages") or {}).values():
                                        ii = (p.get("imageinfo") or [{}])[0]
                                        _add_url(ii.get("url") or ii.get("thumburl") or "")
                except Exception:
                    pass

            # 5. Internet Archive (risultati aggiuntivi anche su "Carica altro")
            if self.card_offset > 0 or len(urls) < 6:
                try:
                    q_ia = re.sub(r"[^A-Za-z0-9 ]+", " ", self.query).strip()
                    page_num = (self.card_offset // 3) + 1
                    ar = s.get(
                        "https://archive.org/advancedsearch.php",
                        params={
                            "q": f'title:("{q_ia}") AND mediatype:(software OR image)',
                            "fl[]": "identifier,title",
                            "rows": "10",
                            "page": str(page_num),
                            "output": "json",
                        },
                        timeout=8,
                    )
                    if ar.ok:
                        for doc in (ar.json().get("response", {}).get("docs") or []):
                            ident = doc.get("identifier") or ""
                            if ident:
                                _add_url(f"https://archive.org/services/img/{ident}")
                except Exception:
                    pass

            self.results_ready.emit(self.generation, urls)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class _ThumbSignals(QObject):
    ready = Signal(int, int, QImage, bytes, str, int, int)  # gen, row, img, raw_bytes, ext, w, h
    failed = Signal(int, int)  # gen, row


class _ImageFetchTask(QRunnable):
    def __init__(self, generation: int, row: int, murl: str, turl: str, signals: _ThumbSignals):
        super().__init__()
        self.generation = generation
        self.row = row
        self.murl = murl
        self.turl = turl
        self.signals = signals

    def run(self) -> None:
        import time
        raw = b""
        ext = ".png"
        for url in (self.murl, self.turl):
            if not url:
                continue
            try:
                ua = "EasyLauncher/1.0 (https://github.com/EasyLauncher; portable-retro-frontend)" if "wikimedia.org" in url else _WEB_UA
                req_headers = {
                    "User-Agent": ua,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                }
                if "wikia.nocookie.net" in url or "fandom.com" in url:
                    req_headers["Referer"] = "https://logos.fandom.com/"
                r = requests.get(url, headers=req_headers, timeout=6)
                if r.status_code == 429:
                    time.sleep(1.2)
                    clean_url = url.split("?")[0] if "utm_source=" in url else url
                    req_headers["User-Agent"] = _WEB_UA
                    r = requests.get(clean_url, headers=req_headers, timeout=6)
                if r.status_code != 200 or not r.content:
                    continue
                img = QImage.fromData(r.content)
                if img.isNull():
                    continue
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "jpeg" in ctype or url.lower().endswith((".jpg", ".jpeg")):
                    ext = ".jpg"
                elif "webp" in ctype or url.lower().endswith(".webp"):
                    ext = ".webp"
                elif "gif" in ctype or url.lower().endswith(".gif"):
                    ext = ".gif"
                else:
                    ext = ".png"
                raw = r.content
                w, h = img.width(), img.height()
                scaled = img.scaled(210, 175, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.signals.ready.emit(self.generation, self.row, scaled, raw, ext, w, h)
                return
            except Exception:
                continue
        self.signals.failed.emit(self.generation, self.row)


class _ImageTile(QFrame):
    """Singola cella cliccabile della griglia immagini stile Playnite."""

    clicked = Signal(int)
    double_clicked = Signal(int)

    def __init__(self, idx: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.idx = idx
        self.setFixedSize(228, 212)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self.img_lbl = QLabel("Caricamento…")
        self.img_lbl.setAlignment(Qt.AlignCenter)
        self.img_lbl.setFixedSize(214, 174)
        self.img_lbl.setStyleSheet("background-color: #14161b; border-radius: 4px;")
        self.res_lbl = QLabel("")
        self.res_lbl.setAlignment(Qt.AlignCenter)
        self.res_lbl.setObjectName("MutedHint")
        lay.addWidget(self.img_lbl)
        lay.addWidget(self.res_lbl)
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.setStyleSheet("QFrame { background-color: #2f3647; border: 2px solid #f58a1f; border-radius: 6px; }")
        else:
            self.setStyleSheet("QFrame { background-color: #1b1e25; border: 1px solid #232733; border-radius: 6px; } QFrame:hover { border-color: #4a5468; }")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.idx)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self.idx)
        super().mouseDoubleClickEvent(event)


class WebImagePickerDialog(QDialog):
    """Finestra stile Playnite: griglia pulita a 3 colonne di immagini reali con risoluzione."""

    def __init__(
        self,
        parent: QWidget,
        db: Database,
        game_id: int,
        kind: str,
        initial_query: str,
        platform_name: str = "",
        search_mode: str = "game",
        custom_dest: Optional[tuple[str, str]] = None,
    ):
        super().__init__(parent)
        self.db = db
        self.game_id = game_id
        self.kind = kind  # "boxart", "screenshots", "logos"
        self.platform_name = platform_name
        self.search_mode = search_mode
        self.custom_dest = custom_dest
        self.saved_path: str = ""
        self._generation = 0
        self._card_offset = 0
        self._seen_urls: set[str] = set()
        self._worker: Optional[_ImageSearchWorker] = None
        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_image_fetched)
        self._signals.failed.connect(self._on_image_failed)
        self._downloaded: dict[int, tuple[bytes, str]] = {}
        self._tiles: list[_ImageTile] = []
        self._selected_idx: Optional[int] = None

        label_kind = {
            "boxart": "Copertina (Box Art)",
            "screenshots": "Screenshot",
            "logos": "Logo Piattaforma" if search_mode == "logo" else "Logo / Locandina",
        }.get(kind, kind)
        self.setWindowTitle(f"Cerca Immagini Web — {label_kind}")
        self.resize(780, 620)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        # Griglia immagini a 3 colonne dentro QScrollArea (ogni cella cliccabile al 100%)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background-color: #14161b; border: 1px solid #232733; }")
        self._grid_container = QWidget()
        self._grid_lay = QGridLayout(self._grid_container)
        self._grid_lay.setContentsMargins(10, 10, 10, 10)
        self._grid_lay.setSpacing(10)
        self._grid_lay.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(self._grid_container)
        lay.addWidget(scroll, 1)

        # Pulsante Carica altro centrato
        more_row = QHBoxLayout()
        more_row.addStretch(1)
        self.more_btn = QPushButton("Carica altro")
        self.more_btn.setMinimumWidth(180)
        self.more_btn.clicked.connect(self._load_more)
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("MutedHint")
        more_row.addWidget(self.more_btn)
        more_row.addStretch(1)
        lay.addLayout(more_row)
        lay.addWidget(self.status_lbl)

        # Barra inferiore stile Playnite: Cerca | [query] | Seleziona | Cancella
        bot = QHBoxLayout()
        self.search_btn = QPushButton("Cerca")
        self.search_btn.clicked.connect(self._new_search)
        self.query_ed = QLineEdit(initial_query)
        self.query_ed.returnPressed.connect(self._new_search)

        self.select_btn = QPushButton("Seleziona")
        self.select_btn.setObjectName("AccentButton")
        self.select_btn.setEnabled(False)
        self.select_btn.clicked.connect(self._confirm_selected)

        cancel_btn = QPushButton("Cancella")
        cancel_btn.clicked.connect(self.reject)

        ext_btn = QPushButton("Google Browser")
        ext_btn.setToolTip("Apri la ricerca su Google Immagini nel tuo browser")
        ext_btn.clicked.connect(self._open_google_browser)

        bot.addWidget(self.search_btn)
        bot.addWidget(self.query_ed, 1)
        bot.addWidget(self.select_btn)
        bot.addWidget(ext_btn)
        bot.addWidget(cancel_btn)
        lay.addLayout(bot)

        self._new_search()

    def _open_google_browser(self) -> None:
        from urllib.parse import quote_plus
        q = self.query_ed.text().strip()
        if q:
            QDesktopServices.openUrl(QUrl(f"https://www.google.com/search?tbm=isch&q={quote_plus(q + ' ' + self.platform_name)}"))

    def _new_search(self) -> None:
        self._generation += 1
        self._card_offset = 0
        self._seen_urls.clear()
        self._downloaded.clear()
        self._selected_idx = None
        for t in self._tiles:
            self._grid_lay.removeWidget(t)
            t.deleteLater()
        self._tiles.clear()
        self.select_btn.setEnabled(False)
        self._fetch_page()

    def _load_more(self) -> None:
        self._fetch_page()

    def _fetch_page(self) -> None:
        q = self.query_ed.text().strip()
        if not q:
            return
        self.more_btn.setEnabled(False)
        self.search_btn.setEnabled(False)
        self.status_lbl.setText("Ricerca immagini in corso…")
        gen = self._generation
        self._worker = _ImageSearchWorker(
            q,
            self.platform_name,
            self._card_offset,
            gen,
            search_mode=self.search_mode,
            db_root=self.db.root,
            kind=self.kind,
        )
        self._worker.results_ready.connect(self._on_results)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_results(self, gen: int, urls: list[str]) -> None:
        if gen != self._generation:
            return
        self._card_offset += 3
        new_urls = [u for u in urls if u not in self._seen_urls]
        for u in new_urls:
            self._seen_urls.add(u)

        self.search_btn.setEnabled(True)
        self.more_btn.setEnabled(bool(new_urls))

        if not new_urls and not self._tiles:
            self.status_lbl.setText("Nessuna immagine trovata. Prova a scrivere solo il nome del gioco.")
            return

        for u in new_urls:
            idx = len(self._tiles)
            tile = _ImageTile(idx, self._grid_container)
            tile.clicked.connect(self._select_tile)
            tile.double_clicked.connect(self._on_tile_double_click)
            self._tiles.append(tile)
            self._grid_lay.addWidget(tile, idx // 3, idx % 3)
            self._pool.start(_ImageFetchTask(gen, idx, u, "", self._signals))

        self.status_lbl.setText(f"{len(self._tiles)} immagini trovate — clicca per selezionare.")

    def _on_failed(self, gen: int, err: str) -> None:
        if gen != self._generation:
            return
        self.more_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.status_lbl.setText(f"Errore ricerca: {err}")

    def _on_image_fetched(self, gen: int, idx: int, img: QImage, raw: bytes, ext: str, w: int, h: int) -> None:
        if gen != self._generation or idx >= len(self._tiles):
            return
        tile = self._tiles[idx]
        tile.img_lbl.setText("")
        tile.img_lbl.setPixmap(QPixmap.fromImage(img))
        tile.res_lbl.setText(f"{w}x{h}px")
        self._downloaded[idx] = (raw, ext)
        if self._selected_idx == idx:
            self.select_btn.setEnabled(True)

    def _on_image_failed(self, gen: int, idx: int) -> None:
        if gen != self._generation or idx >= len(self._tiles):
            return
        self._tiles[idx].hide()
        pos = 0
        for t in self._tiles:
            self._grid_lay.removeWidget(t)
            if not t.isHidden():
                self._grid_lay.addWidget(t, pos // 3, pos % 3)
                pos += 1
        if self._selected_idx == idx:
            self._selected_idx = None
            self.select_btn.setEnabled(False)
        self.status_lbl.setText(f"{pos} immagini disponibili — clicca per selezionare." if pos else "Nessuna immagine caricabile.")

    def _select_tile(self, idx: int) -> None:
        if self._selected_idx is not None and self._selected_idx < len(self._tiles):
            self._tiles[self._selected_idx].set_selected(False)
        self._selected_idx = idx
        if idx < len(self._tiles):
            self._tiles[idx].set_selected(True)
        self.select_btn.setEnabled(idx in self._downloaded)

    def _on_tile_double_click(self, idx: int) -> None:
        self._select_tile(idx)
        self._confirm_selected()

    def _confirm_selected(self) -> None:
        idx = self._selected_idx
        if idx is None or idx not in self._downloaded:
            return
        raw, ext = self._downloaded[idx]
        if self.custom_dest is not None:
            dest_dir, stem = self.custom_dest
            dest = os.path.join(dest_dir, stem + ext)
        else:
            g = self.db.get_game(self.game_id)
            if g is None:
                return
            plat = self.db.get_platform(g["platform_id"])
            if plat is None:
                return
            dest = self.db.media_file(plat, self.kind, g["rom_file"], ext)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(raw)
        self.saved_path = dest
        self.accept()


# --------------------------------------------------------------------------
# Modifica manuale gioco + Scarica Metadati singolo

class _ScaledPreviewLabel(QLabel):
    """QLabel che ridimensiona sempre l'anteprima intera adattandosi allo spazio disponibile senza tagli."""

    def __init__(self, placeholder: str = "Nessuna anteprima", parent: Optional[QWidget] = None):
        super().__init__(placeholder, parent)
        self._placeholder = placeholder
        self._orig_pixmap: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMinimumSize(120, 80)

    def setPreviewPixmap(self, pm: Optional[QPixmap]) -> None:
        self._orig_pixmap = pm if (pm is not None and not pm.isNull()) else None
        self.setText("" if self._orig_pixmap is not None else self._placeholder)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._orig_pixmap is None:
            return
        r = self.contentsRect()
        avail_w = max(1, r.width() - 8)
        avail_h = max(1, r.height() - 8)
        scaled = self._orig_pixmap.scaled(avail_w, avail_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = r.x() + (r.width() - scaled.width()) // 2
        y = r.y() + (r.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.drawPixmap(x, y, scaled)


class _BannerPositionPreview(QLabel):
    """Anteprima Logo / Locandina che riempie il riquadro e permette di trascinare l'immagine per scegliere la porzione."""

    def __init__(self, placeholder: str = "Trascina qui l'immagine\n(da Chrome o dal PC)", parent: Optional[QWidget] = None):
        super().__init__(placeholder, parent)
        self._placeholder = placeholder
        self._orig_pixmap: Optional[QPixmap] = None
        self.offset_x: float = 0.5
        self.offset_y: float = 0.5
        self._dragging: bool = False
        self._last_pos = None
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMinimumSize(120, 145)

    def setOffsets(self, ox: float, oy: float) -> None:
        self.offset_x = max(0.0, min(1.0, float(ox)))
        self.offset_y = max(0.0, min(1.0, float(oy)))
        self.update()

    def setPreviewPixmap(self, pm: Optional[QPixmap]) -> None:
        self._orig_pixmap = pm if (pm is not None and not pm.isNull()) else None
        self.setText("" if self._orig_pixmap is not None else self._placeholder)
        self._update_cursor()
        self.update()

    def _scaled_and_frame(self) -> tuple[Optional[QPixmap], QRect]:
        r = self.contentsRect().adjusted(3, 3, -3, -3)
        if self._orig_pixmap is None or r.width() <= 4 or r.height() <= 4:
            return None, r
        scaled = self._orig_pixmap.scaledToWidth(r.width(), Qt.SmoothTransformation)
        return scaled, r

    def _update_cursor(self) -> None:
        scaled, r = self._scaled_and_frame()
        can_pan = scaled is not None and scaled.height() > r.height() + 1
        self.setCursor(Qt.OpenHandCursor if can_pan else Qt.ArrowCursor)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_cursor()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._orig_pixmap is not None:
            scaled, r = self._scaled_and_frame()
            if scaled is not None and scaled.height() > r.height() + 1:
                self._dragging = True
                self._last_pos = event.position()
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and self._last_pos is not None:
            scaled, r = self._scaled_and_frame()
            if scaled is not None:
                cur = event.position()
                dy = cur.y() - self._last_pos.y()
                self._last_pos = cur
                max_y = max(0, scaled.height() - r.height())
                if max_y > 0:
                    self.offset_y = max(0.0, min(1.0, self.offset_y - dy / max_y))
                self.update()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging and event.button() == Qt.LeftButton:
            self._dragging = False
            self._last_pos = None
            self._update_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        scaled, r = self._scaled_and_frame()
        if scaled is None:
            return
        painter = QPainter(self)
        if scaled.height() > r.height() + 1:
            crop_y = max(0, int(round((scaled.height() - r.height()) * self.offset_y)))
            painter.drawPixmap(r.x(), r.y(), scaled, 0, crop_y, r.width(), r.height())
            painter.setPen(QColor(255, 255, 255, 190))
            painter.fillRect(r.x(), r.bottom() - 18, r.width(), 19, QColor(12, 14, 20, 155))
            painter.drawText(r.adjusted(4, r.height() - 18, -4, 0), Qt.AlignCenter, "✥ Trascina per scegliere la porzione")
        else:
            y = r.y() + (r.height() - scaled.height()) // 2
            painter.drawPixmap(r.x(), y, scaled)
        painter.end()


class ExtraScreenshotsDialog(QDialog):
    """Finestra dedicata per aggiungere (da PC o Web), vedere in anteprima ed eliminare singolarmente foto gameplay extra."""

    def __init__(
        self,
        parent: QWidget,
        db: Database,
        game_id: int,
        game_title: str,
        platform_name: str,
        shots_dir: str,
        rom_stem: str,
        initial_shots: list[str],
    ):
        super().__init__(parent)
        self.db = db
        self.game_id = game_id
        self.game_title = game_title
        self.platform_name = platform_name
        self.shots_dir = shots_dir
        self.rom_stem = rom_stem
        self.shots: list[str] = [p for p in initial_shots if p and os.path.isfile(p)]

        self.setWindowTitle("Foto Gameplay Extra")
        self.resize(720, 520)
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.pc_btn = QPushButton("📁 Aggiungi dal PC…")
        self.pc_btn.setToolTip("Seleziona una o più immagini gameplay dal computer")
        self.pc_btn.clicked.connect(self._add_from_pc)
        _btn_dis_css = "QPushButton:disabled { background-color: #1a1d24; color: #5c6375; border: 1px solid #2b303c; }"
        self.web_btn = QPushButton("🌐 Cerca su Google")
        self.web_btn.setStyleSheet(_btn_dis_css)
        self.web_btn.setToolTip("Apre Google Immagini affiancato per trascinare le foto qui")
        self.web_btn.clicked.connect(self._add_from_web)
        self.close_browser_btn = QPushButton("✕ Chiudi browser")
        self.close_browser_btn.setStyleSheet(_btn_dis_css)
        self.close_browser_btn.setEnabled(False)
        self.close_browser_btn.setToolTip("Chiude la finestra del browser aperta per le foto e riporta la finestra al centro")
        self.close_browser_btn.clicked.connect(lambda: _close_side_by_side_browser(self))

        def _on_extra_browser_state(is_open: bool) -> None:
            self.web_btn.setEnabled(not is_open)
            self.close_browser_btn.setEnabled(is_open)

        self._browser_state_cb = _on_extra_browser_state
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("MutedHint")
        top.addWidget(self.pc_btn)
        top.addWidget(self.web_btn)
        top.addWidget(self.close_browser_btn)
        top.addStretch(1)
        top.addWidget(self.count_lbl)
        lay.addLayout(top)

        self.drop_hint_lbl = QLabel("👉 Trascina qui le immagini (su Google clicca prima la foto per ingrandirla a risoluzione piena)")
        self.drop_hint_lbl.setStyleSheet("color: #f58a1f; font-size: 11.5px; padding: 2px 4px;")
        lay.addWidget(self.drop_hint_lbl)

        self._scroll_norm_css = "QScrollArea { background-color: #14161b; border: 1px solid #232733; border-radius: 6px; }"
        self._scroll_hover_css = "QScrollArea { background-color: #212632; border: 2px dashed #f58a1f; border-radius: 6px; }"
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(self._scroll_norm_css)
        self._scroll = scroll
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(10, 10, 10, 10)
        self._grid.setSpacing(10)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(self._grid_host)
        lay.addWidget(scroll, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        n = len(self.shots)
        self.count_lbl.setText(f"{n} foto" if n else "Nessuna foto extra")
        if not self.shots:
            empty = QLabel("Nessuna foto aggiuntiva.\nTrascina qui le immagini da Chrome o usa 'Aggiungi dal PC…' / 'Cerca su Google'.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setObjectName("MutedHint")
            empty.setMinimumHeight(180)
            self._grid.addWidget(empty, 0, 0)
            return

        for idx, path in enumerate(self.shots):
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background-color: #1b1e25; border: 1px solid #2b303c; border-radius: 6px; }"
            )
            card.setFixedSize(216, 178)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(8, 8, 8, 8)
            cl.setSpacing(5)

            prev = _ScaledPreviewLabel("Immagine non valida")
            prev.setFixedSize(198, 120)
            prev.setStyleSheet(
                "background: #12151c; border: 1px dashed #2b313e; border-radius: 4px; color: #6c7486;"
            )
            pm = QPixmap(path)
            if not pm.isNull():
                prev.setPreviewPixmap(pm)
                dim_txt = f"{pm.width()}×{pm.height()} px"
            else:
                dim_txt = "Non valida"
            prev.setToolTip(path)
            cl.addWidget(prev)

            bot = QHBoxLayout()
            bot.setContentsMargins(0, 0, 0, 0)
            bot.setSpacing(6)
            dim_lbl = QLabel(dim_txt)
            dim_lbl.setObjectName("MutedHint")
            dim_lbl.setStyleSheet("border: none; background: transparent; font-size: 11px;")
            del_b = QPushButton("🗑 Elimina")
            del_b.setToolTip("Rimuovi questa foto")
            del_b.setStyleSheet("padding: 3px 8px;")
            del_b.clicked.connect(lambda _=False, i=idx: self._remove_at(i))
            bot.addWidget(dim_lbl, 1)
            bot.addWidget(del_b)
            cl.addLayout(bot)

            self._grid.addWidget(card, idx // 3, idx % 3)

    def _remove_at(self, idx: int) -> None:
        if 0 <= idx < len(self.shots):
            self.shots.pop(idx)
            self._rebuild_grid()

    def _add_from_pc(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Seleziona foto gameplay",
            "",
            "Immagini (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        added = False
        for p in paths:
            if p and os.path.isfile(p) and p not in self.shots:
                self.shots.append(p)
                added = True
        if added:
            self._rebuild_grid()

    def _add_from_web(self) -> None:
        self.drop_hint_lbl.setText("👉 Clicca prima la foto su Google per ingrandirla a risoluzione piena, poi trascinala qui")
        q = f"{self.game_title} {self.platform_name} screenshot gameplay".strip()
        _open_browser_side_by_side(self, q)

    def dragEnterEvent(self, event) -> None:
        if _has_droppable_image(event.mimeData()):
            event.acceptProposedAction()
            self._scroll.setStyleSheet(self._scroll_hover_css)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._scroll.setStyleSheet(self._scroll_norm_css)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._scroll.setStyleSheet(self._scroll_norm_css)
        path = _extract_dropped_image(event.mimeData())
        if path and path not in self.shots:
            self.shots.append(path)
            self._rebuild_grid()
            event.acceptProposedAction()
        else:
            event.ignore()


class GameEditDialog(QDialog):
    def __init__(self, parent: QWidget, db: Database, game_id: int):
        super().__init__(parent)
        self.db = db
        self.game_id = game_id
        self.worker: Optional[SingleScrapeWorker] = None
        self.metadata_cleared = False
        self.setWindowTitle("Modifica / Scarica Metadati")
        self.resize(700, 620)

        lay = QVBoxLayout(self)

        # Barra superiore con pulsante Scraper singolo
        top_bar = QHBoxLayout()
        self.scrape_btn = QPushButton("🌐  Scarica Metadati Online")
        self.scrape_btn.setObjectName("AccentButton")
        self.scrape_btn.clicked.connect(self._scrape_now)
        self.meta_settings_btn = QPushButton("⚙  Impostazioni Metadati")
        self.meta_settings_btn.setToolTip("Apri le impostazioni di priorità e chiavi API dei metadati")
        self.meta_settings_btn.clicked.connect(self._open_metadata_settings)
        self.scrape_status = QLabel("")
        self.scrape_status.setObjectName("MutedHint")
        top_bar.addWidget(self.scrape_btn)
        top_bar.addWidget(self.meta_settings_btn)
        top_bar.addWidget(self.scrape_status, 1)
        lay.addLayout(top_bar)

        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # --- Tab Generale ---
        gen_tab = QWidget()
        form = QFormLayout(gen_tab)
        self.title_ed = QLineEdit()
        self.plat_ed = QLineEdit()
        self.plat_ed.setReadOnly(True)
        self.plat_ed.setStyleSheet("color: #9ba2b2;")
        self.year_ed = QLineEdit()
        self.genre_ed = QLineEdit()
        self.dev_ed = QLineEdit()
        self.pub_ed = QLineEdit()
        self.players_ed = QLineEdit()
        self.rating_sp = QDoubleSpinBox()
        self.rating_sp.setRange(0.0, 5.0)
        self.rating_sp.setSingleStep(0.5)
        self.fav_chk = QCheckBox("Segna come preferito")

        self.desc_ed = QPlainTextEdit()
        self.desc_ed.setMinimumHeight(140)

        form.addRow("Titolo:", self.title_ed)
        form.addRow("Piattaforma / Sotto-sezione:", self.plat_ed)
        form.addRow("Anno di uscita:", self.year_ed)
        form.addRow("Genere:", self.genre_ed)
        form.addRow("Sviluppatore:", self.dev_ed)
        form.addRow("Publisher:", self.pub_ed)
        form.addRow("Giocatori:", self.players_ed)
        form.addRow("Valutazione (0-5):", self.rating_sp)
        form.addRow("", self.fav_chk)
        form.addRow("Descrizione / Trama:", self.desc_ed)
        tabs.addTab(gen_tab, "Generale")

        # --- Tab Media (anteprima stile Playnite) ---
        media_tab = QWidget()
        media_grid = QGridLayout(media_tab)
        media_grid.setSpacing(14)
        media_grid.setColumnStretch(0, 1)
        media_grid.setColumnStretch(1, 1)
        media_grid.setRowStretch(0, 0)
        media_grid.setRowStretch(1, 1)
        media_grid.setRowStretch(2, 1)

        img_filter = f"Immagini ({' '.join('*' + e for e in IMAGE_EXTS)});;Tutti (*.*)"
        self._media_slots: list[tuple[QLineEdit, QLabel, QLabel, QSize]] = []
        self._google_btns: list[QPushButton] = []
        _btn_dis_css = "QPushButton:disabled { background-color: #1a1d24; color: #5c6375; border: 1px solid #2b303c; }"

        def _make_media_card(
            title_txt: str,
            kind: str,
            col: str,
            preview_size: QSize,
        ) -> tuple[QFrame, QLineEdit]:
            _norm_css = (
                "QFrame#MediaCard { background: #191d26; border: 1px solid #2b313e; border-radius: 8px; }"
            )
            _hover_css = (
                "QFrame#MediaCard { background: #212632; border: 2px dashed #f58a1f; border-radius: 8px; }"
            )
            card = _DropImageCard(normal_style=_norm_css, hover_style=_hover_css)
            card.setObjectName("MediaCard")
            card.setToolTip(f"Trascina qui un'immagine da Chrome o dal PC per impostare {title_txt}")
            c_lay = QVBoxLayout(card)
            c_lay.setContentsMargins(10, 10, 10, 10)
            c_lay.setSpacing(6)

            hdr = QLabel(title_txt)
            hdr.setStyleSheet("font-weight: bold; border: none; background: transparent;")
            c_lay.addWidget(hdr)

            ed = QLineEdit()
            ed.setVisible(False)
            card.image_dropped.connect(ed.setText)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(6)
            browse_btn = QPushButton("📁 Sfoglia…")
            browse_btn.setToolTip(f"Seleziona {title_txt} dal disco")
            web_btn = QPushButton("🌐 Cerca su Google")
            web_btn.setStyleSheet(_btn_dis_css)
            web_btn.setToolTip(f"Apre Google Immagini affiancato per cercare {title_txt}")
            self._google_btns.append(web_btn)
            del_btn = QPushButton("🗑")
            del_btn.setFixedWidth(34)
            del_btn.setStyleSheet("padding: 5px;")
            del_btn.setToolTip(f"Elimina definitivamente {title_txt} dal disco")

            btn_row.addWidget(browse_btn)
            btn_row.addWidget(web_btn)
            btn_row.addWidget(del_btn)
            btn_row.addStretch(1)
            c_lay.addLayout(btn_row)

            dim_lbl = QLabel("Nessuna immagine")
            dim_lbl.setObjectName("MutedHint")
            dim_lbl.setStyleSheet("border: none; background: transparent; font-size: 11px;")
            c_lay.addWidget(dim_lbl)

            prev_lbl = (
                _BannerPositionPreview("Trascina qui l'immagine\n(da Chrome o dal PC)")
                if kind == "logos"
                else _ScaledPreviewLabel("Trascina qui l'immagine\n(da Chrome o dal PC)")
            )
            if kind == "logos":
                self.logo_prev = prev_lbl
            prev_lbl.setStyleSheet(
                "background: #12151c; border: 1px dashed #2b313e; border-radius: 6px; color: #6c7486;"
            )
            c_lay.addWidget(prev_lbl, 1)

            def _browse() -> None:
                start_dir = os.path.dirname(ed.text()) if ed.text() and os.path.isfile(ed.text()) else ""
                path, _ = QFileDialog.getOpenFileName(
                    self, f"Seleziona {title_txt}", start_dir, img_filter
                )
                if path:
                    ed.setText(path)

            browse_btn.clicked.connect(_browse)
            web_btn.clicked.connect(lambda: self._pick_from_google(kind, ed))
            del_btn.clicked.connect(lambda: self._clear_single_image(col, title_txt, ed))
            def _on_ed_changed() -> None:
                if kind == "logos" and not getattr(self, "_loading_fields", False):
                    prev_lbl.setOffsets(0.5, 0.5)
                self._refresh_media_preview(ed, dim_lbl, prev_lbl, preview_size)

            ed.textChanged.connect(_on_ed_changed)

            self._media_slots.append((ed, dim_lbl, prev_lbl, preview_size))
            return card, ed

        box_card, self.box_ed = _make_media_card(
            "Copertina (Box Art)", "boxart", "boxart_path", QSize(240, 290)
        )
        logo_card, self.logo_ed = _make_media_card(
            "Logo / Locandina", "logos", "logo_path", QSize(260, 120)
        )
        shot_card, self.shot_ed = _make_media_card(
            "Screenshot", "screenshots", "screenshot_path", QSize(260, 150)
        )
        self._extra_shots: list[str] = []
        extra_row = QHBoxLayout()
        extra_row.setContentsMargins(0, 2, 0, 0)
        extra_row.setSpacing(6)
        self.add_extra_shot_btn = QPushButton("🖼 Foto Extra…")
        self.add_extra_shot_btn.setToolTip("Gestisci le foto di gameplay aggiuntive (dal PC o da Internet)")
        self.clear_extra_shot_btn = QPushButton("Svuota")
        self.clear_extra_shot_btn.setToolTip("Rimuovi tutte le foto gameplay aggiuntive")
        self.clear_extra_shot_btn.setEnabled(False)
        self.extra_shots_lbl = QLabel("Nessuna foto extra")
        self.extra_shots_lbl.setObjectName("MutedHint")
        self.extra_shots_lbl.setStyleSheet("border: none; background: transparent; font-size: 11px;")
        self.add_extra_shot_btn.clicked.connect(self._add_extra_screenshots)
        self.clear_extra_shot_btn.clicked.connect(self._clear_extra_screenshots)
        extra_row.addWidget(self.add_extra_shot_btn)
        extra_row.addWidget(self.extra_shots_lbl, 1)
        extra_row.addWidget(self.clear_extra_shot_btn)
        shot_card.layout().addLayout(extra_row)

        media_top_w = QWidget()
        media_top_lay = QHBoxLayout(media_top_w)
        media_top_lay.setContentsMargins(0, 0, 0, 0)
        media_top_lay.setSpacing(10)
        self.media_close_browser_btn = QPushButton("✕ Chiudi browser")
        self.media_close_browser_btn.setStyleSheet(_btn_dis_css)
        self.media_close_browser_btn.setEnabled(False)
        self.media_close_browser_btn.setToolTip("Chiude la finestra del browser aperta per le foto e riporta la finestra al centro")
        self.media_close_browser_btn.clicked.connect(lambda: _close_side_by_side_browser(self))

        def _on_browser_state(is_open: bool) -> None:
            for b in self._google_btns:
                b.setEnabled(not is_open)
            self.media_close_browser_btn.setEnabled(is_open)

        self._browser_state_cb = _on_browser_state
        self.media_drop_hint = QLabel("👉 Trascina qui le immagini da Chrome o dal PC (su Google cliccala prima per ingrandirla a risoluzione piena)")
        self.media_drop_hint.setStyleSheet("color: #f58a1f; font-size: 11.5px; padding: 2px 4px;")
        self.media_drop_hint.setWordWrap(True)
        media_top_lay.addWidget(self.media_close_browser_btn)
        media_top_lay.addWidget(self.media_drop_hint, 1)
        media_grid.addWidget(media_top_w, 0, 0, 1, 2)
        media_grid.addWidget(box_card, 1, 0, 2, 1)
        media_grid.addWidget(logo_card, 1, 1)
        media_grid.addWidget(shot_card, 2, 1)
        media_grid.setRowStretch(1, 3)
        media_grid.setRowStretch(2, 2)
        tabs.addTab(media_tab, "Media")

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.title_ed.setFocus()

        self._load_fields()

    def _refresh_media_preview(
        self, ed: QLineEdit, dim_lbl: QLabel, prev_lbl: _ScaledPreviewLabel, box_size: QSize
    ) -> None:
        path = ed.text().strip()
        if path and os.path.isfile(path):
            pm = QPixmap(path)
            if not pm.isNull():
                dim_lbl.setText(f"{pm.width()}×{pm.height()} px")
                prev_lbl.setPreviewPixmap(pm)
                prev_lbl.setToolTip(path)
                return
        prev_lbl.setPreviewPixmap(None)
        prev_lbl.setToolTip("")
        dim_lbl.setText("Nessuna immagine")

    def _load_fields(self) -> None:
        g = self.db.get_game(self.game_id)
        if g is None:
            return
        
        # Mostra il percorso gerarchico della piattaforma in sola lettura
        chain: list[str] = []
        cur_p = self.db.get_platform(g["platform_id"])
        while cur_p is not None:
            chain.insert(0, cur_p["name"])
            pid_parent = cur_p["parent_id"] if "parent_id" in cur_p.keys() else None
            cur_p = self.db.get_platform(pid_parent) if pid_parent else None
        self.plat_ed.setText("  ›  ".join(chain) if chain else "—")

        self.title_ed.setText(g["title"])
        self.year_ed.setText(str(g["release_year"]) if g["release_year"] else "")
        self.genre_ed.setText(g["genre"])
        self.dev_ed.setText(g["developer"])
        self.pub_ed.setText(g["publisher"])
        self.players_ed.setText(g["players"])
        self.rating_sp.setValue(float(g["rating"] or 0.0))
        self.fav_chk.setChecked(bool(g["favorite"]))
        self._loading_fields = True
        try:
            self.box_ed.setText(self.db.resolve(g["boxart_path"]) if g["boxart_path"] else "")
            if hasattr(self, "logo_prev"):
                ox = float(g["logo_offset_x"]) if ("logo_offset_x" in g.keys() and g["logo_offset_x"] is not None) else 0.5
                oy = float(g["logo_offset_y"]) if ("logo_offset_y" in g.keys() and g["logo_offset_y"] is not None) else 0.5
                self.logo_prev.setOffsets(ox, oy)
            self.logo_ed.setText(self.db.resolve(g["logo_path"]) if g["logo_path"] else "")
            self.shot_ed.setText(self.db.resolve(g["screenshot_path"]) if g["screenshot_path"] else "")
        finally:
            self._loading_fields = False
        raw_extra = g["extra_screenshots"] if "extra_screenshots" in g.keys() else "[]"
        try:
            extra_list = json.loads(raw_extra or "[]")
        except ValueError:
            extra_list = []
        self._extra_shots = [
            self.db.resolve(p) for p in extra_list if p and os.path.isfile(self.db.resolve(p))
        ]
        self._refresh_extra_shots_label()
        self.desc_ed.setPlainText(g["description"])

    def _refresh_extra_shots_label(self) -> None:
        n = len(self._extra_shots)
        self.extra_shots_lbl.setText(f"+{n} foto extra" if n else "Nessuna foto extra")
        self.clear_extra_shot_btn.setEnabled(n > 0)

    def _add_extra_screenshots(self) -> None:
        g = self.db.get_game(self.game_id)
        if g is None:
            return
        plat = self.db.get_platform(g["platform_id"])
        plat_cur = plat
        while plat_cur is not None and plat_cur["parent_id"]:
            parent_p = self.db.get_platform(plat_cur["parent_id"])
            if parent_p is None:
                break
            plat_cur = parent_p
        plat_name = plat_cur["name"] if plat_cur else (plat["name"] if plat else "")
        shots_dir = os.path.dirname(self.db.media_file(plat, "screenshots", g["rom_file"], ".png")) if plat else ""
        rom_stem = os.path.splitext(g["rom_file"])[0]
        dlg = ExtraScreenshotsDialog(
            self,
            self.db,
            self.game_id,
            self.title_ed.text().strip() or g["title"],
            plat_name,
            shots_dir,
            rom_stem,
            self._extra_shots,
        )
        if dlg.exec() == QDialog.Accepted:
            self._extra_shots = list(dlg.shots)
            self._refresh_extra_shots_label()

    def _clear_extra_screenshots(self) -> None:
        self._extra_shots.clear()
        self._refresh_extra_shots_label()

    def _pick_from_google(self, kind: str, target_ed: QLineEdit) -> None:
        title = self.title_ed.text().strip()
        g = self.db.get_game(self.game_id)
        plat = self.db.get_platform(g["platform_id"]) if g else None
        while plat is not None and plat["parent_id"]:
            parent_p = self.db.get_platform(plat["parent_id"])
            if parent_p is None:
                break
            plat = parent_p
        plat_name = plat["name"] if plat else ""
        suffix = {
            "boxart": "cover",
            "logos": "logo",
            "screenshots": "game play",
        }.get(kind, "")
        self.media_drop_hint.setText("👉 Su Google clicca prima l'immagine per ingrandirla a risoluzione piena, poi trascinala sulla casella")
        _open_browser_side_by_side(self, f"{title} {plat_name} {suffix}".strip())

    def _open_metadata_settings(self) -> None:
        g = self.db.get_game(self.game_id)
        plat_id = g["platform_id"] if g else None
        dlg = ScrapeBatchDialog(self, self.db, plat_id, config=Config(self.db.root), initial_tab=1)
        dlg.exec()

    def _scrape_now(self) -> None:
        self.scrape_btn.setEnabled(False)
        self.scrape_status.setText("Ricerca metadati in corso…")
        self.worker = SingleScrapeWorker(self.db.root, self.game_id)
        self.worker.finished_scrape.connect(self._on_scraped)
        self.worker.start()

    def _on_scraped(self, ok: bool, msg: str) -> None:
        self.scrape_btn.setEnabled(True)
        self.scrape_status.setText(msg)
        if ok:
            self._load_fields()

    def _clear_single_image(self, col: str, label: str, target_ed: QLineEdit) -> None:
        g = self.db.get_game(self.game_id)
        if g is None:
            return
        if not target_ed.text().strip() and not g[col]:
            return
        reply = QMessageBox.warning(
            self,
            "Conferma eliminazione immagine",
            f"Attenzione: l'immagine '{label}' di '{g['title']}' verrà eliminata definitivamente dal disco.\n\nVuoi continuare?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.db.clear_game_image(self.game_id, col)
        target_ed.clear()
        self.metadata_cleared = True
        self.scrape_status.setText(f"{label} eliminata.")

    def _copy_if_needed(self, plat: sqlite3.Row, kind: str, rom_file: str, chosen: str, current_stored: str) -> str:
        chosen = chosen.strip()
        if not chosen:
            return ""
        cur_abs = self.db.resolve(current_stored) if current_stored else ""
        if cur_abs and os.path.abspath(chosen) == os.path.abspath(cur_abs):
            return current_stored
        if not os.path.isfile(chosen):
            return current_stored
        ext = os.path.splitext(chosen)[1].lower() or ".png"
        dest = self.db.media_file(plat, kind, rom_file, ext)
        if os.path.abspath(chosen) != os.path.abspath(dest):
            shutil.copy2(chosen, dest)
        return self.db.store(dest)

    def _save(self) -> None:
        title = self.title_ed.text().strip()
        if not title:
            QMessageBox.warning(self, "Titolo mancante", "Il titolo non può essere vuoto.")
            return
        year_txt = self.year_ed.text().strip()
        year = int(year_txt) if year_txt.isdigit() else None

        g = self.db.get_game(self.game_id)
        plat = self.db.get_platform(g["platform_id"])
        box_stored = self._copy_if_needed(plat, "boxart", g["rom_file"], self.box_ed.text(), g["boxart_path"])
        logo_stored = self._copy_if_needed(plat, "logos", g["rom_file"], self.logo_ed.text(), g["logo_path"])
        shot_stored = self._copy_if_needed(plat, "screenshots", g["rom_file"], self.shot_ed.text(), g["screenshot_path"])

        extra_stored: list[str] = []
        shots_dir = os.path.dirname(self.db.media_file(plat, "screenshots", g["rom_file"], ".png"))
        stem = os.path.splitext(g["rom_file"])[0]
        os.makedirs(shots_dir, exist_ok=True)
        for idx, src in enumerate(self._extra_shots, start=1):
            if not src or not os.path.isfile(src):
                continue
            src_abs = os.path.abspath(src)
            if os.path.commonpath([src_abs, os.path.abspath(shots_dir)]) == os.path.abspath(shots_dir):
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

        self.db.update_game(
            self.game_id,
            title=title,
            release_year=year,
            genre=self.genre_ed.text().strip(),
            developer=self.dev_ed.text().strip(),
            publisher=self.pub_ed.text().strip(),
            players=self.players_ed.text().strip(),
            rating=self.rating_sp.value() if self.rating_sp.value() > 0 else None,
            favorite=1 if self.fav_chk.isChecked() else 0,
            boxart_path=box_stored,
            logo_path=logo_stored,
            logo_offset_x=self.logo_prev.offset_x if (logo_stored and hasattr(self, "logo_prev")) else 0.5,
            logo_offset_y=self.logo_prev.offset_y if (logo_stored and hasattr(self, "logo_prev")) else 0.5,
            screenshot_path=shot_stored,
            extra_screenshots=json.dumps(extra_stored),
            description=self.desc_ed.toPlainText().strip(),
            scraped=2,
        )
        self.accept()


# --------------------------------------------------------------------------
# Impostazioni API Keys e Priorita' (widget riutilizzabile)
# --------------------------------------------------------------------------

_ALL_SOURCES: list[tuple[str, str]] = [
    ("igdb", "IGDB"),
    ("nintendo", "Nintendo EU"),
    ("launchbox", "LaunchBox GamesDB"),
    ("adb", "ArcadeDB"),
    ("steamgriddb", "SteamGridDB"),
    ("steam", "Steam Store"),
    ("gog", "GOG"),
    ("wikipedia", "Wikipedia"),
    ("wikidata", "Wikidata"),
    ("screenscraper", "ScreenScraper.fr"),
    ("thegamesdb", "TheGamesDB"),
    ("libretro", "Libretro Thumbnails"),
]
_SOURCE_NAME_MAP: dict[str, str] = dict(_ALL_SOURCES)

_METADATA_FIELDS: list[tuple[str, str, list[str]]] = [
    ("title", "Titolo", ["screenscraper", "igdb", "nintendo", "launchbox", "adb", "thegamesdb", "steam", "gog", "wikipedia", "wikidata"]),
    ("genre", "Genere", ["igdb", "nintendo", "launchbox", "adb", "screenscraper", "thegamesdb", "steam", "gog", "wikidata"]),
    ("release_year", "Data di uscita / Anno", ["igdb", "nintendo", "launchbox", "adb", "screenscraper", "thegamesdb", "steam", "gog", "wikidata"]),
    ("developer", "Sviluppatore", ["igdb", "launchbox", "adb", "screenscraper", "thegamesdb", "steam", "gog", "wikidata"]),
    ("publisher", "Editore", ["igdb", "nintendo", "launchbox", "adb", "screenscraper", "thegamesdb", "steam", "gog", "wikidata"]),
    ("players", "Giocatori", ["screenscraper", "nintendo", "launchbox", "adb", "thegamesdb"]),
    ("description", "Descrizione", ["screenscraper", "nintendo", "igdb", "launchbox", "adb", "thegamesdb", "steam", "wikipedia", "wikidata"]),
    ("rating", "Valutazione", ["igdb", "screenscraper"]),
    ("boxart", "Immagine di copertina", ["libretro", "nintendo", "launchbox", "steamgriddb", "adb", "screenscraper", "thegamesdb", "igdb", "steam", "gog", "wikipedia", "wikidata"]),
    ("logo", "Logo / Locandina", ["libretro", "launchbox", "steamgriddb", "adb", "screenscraper", "steam", "gog", "thegamesdb", "igdb", "wikipedia", "wikidata"]),
    ("screenshot", "Screenshot", ["libretro", "launchbox", "adb", "screenscraper", "igdb", "thegamesdb", "steam", "gog"]),
]


class _SourceMultiCombo(QPushButton):
    """Pulsante stile Playnite che apre un popup con checkbox e priorita' ▲/▼ per le fonti."""

    changed = Signal()

    def __init__(self, active_sources: list[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._order: list[str] = list(active_sources)
        for sid, _ in _ALL_SOURCES:
            if sid not in self._order:
                self._order.append(sid)
        self._enabled: set[str] = set(active_sources)
        self._popup: Optional[QFrame] = None
        self._list_w: Optional[QListWidget] = None

        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QPushButton { text-align: left; padding: 5px 10px; background-color: #16181e; "
            "border: 1px solid #353b4a; border-radius: 4px; color: #e6e8ee; } "
            "QPushButton:hover { border-color: #f58a1f; }"
        )
        self.clicked.connect(self._open_popup)
        self._full_text = ""
        self._refresh_text()

    def _refresh_text(self) -> None:
        names = [_SOURCE_NAME_MAP[s] for s in self._order if s in self._enabled and s in _SOURCE_NAME_MAP]
        self._full_text = ", ".join(names) if names else "(Nessuna fonte selezionata)"
        self.setToolTip(self._full_text)
        self._apply_elide()

    def _apply_elide(self) -> None:
        avail = max(self.width() - 28, 220)
        elided = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, avail)
        self.setText(f"{elided}   ▾")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def selected_sources(self) -> list[str]:
        return [s for s in self._order if s in self._enabled]

    def set_sources(self, sources: list[str]) -> None:
        valid = [s for s in sources if s in _SOURCE_NAME_MAP]
        self._enabled = set(valid)
        new_order = list(valid)
        for sid, _ in _ALL_SOURCES:
            if sid not in new_order:
                new_order.append(sid)
        self._order = new_order
        self._refresh_text()

    def _open_popup(self) -> None:
        if self._popup and self._popup.isVisible():
            self._popup.close()
            return

        pop = QFrame(self, Qt.Popup)
        pop.setStyleSheet(
            "QFrame { background-color: #1b1e25; border: 1px solid #f58a1f; border-radius: 5px; }"
        )
        lay = QHBoxLayout(pop)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        lw = QListWidget(pop)
        lw.setFixedWidth(220)
        lw.setFixedHeight(min(300, 24 * len(self._order) + 12))
        for sid in self._order:
            it = QListWidgetItem(_SOURCE_NAME_MAP.get(sid, sid))
            it.setData(Qt.UserRole, sid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if sid in self._enabled else Qt.Unchecked)
            lw.addItem(it)

        def sync_from_list() -> None:
            self._order = [lw.item(i).data(Qt.UserRole) for i in range(lw.count())]
            self._enabled = {
                lw.item(i).data(Qt.UserRole)
                for i in range(lw.count())
                if lw.item(i).checkState() == Qt.Checked
            }
            self._refresh_text()
            self.changed.emit()

        lw.itemChanged.connect(lambda _: sync_from_list())
        lay.addWidget(lw)

        btns = QVBoxLayout()
        btns.setSpacing(4)
        up_b = QPushButton("▲", pop)
        dn_b = QPushButton("▼", pop)
        up_b.setFixedSize(28, 28)
        dn_b.setFixedSize(28, 28)

        def move_item(delta: int) -> None:
            r = lw.currentRow()
            if r < 0:
                return
            t = r + delta
            if not (0 <= t < lw.count()):
                return
            it = lw.takeItem(r)
            lw.insertItem(t, it)
            lw.setCurrentRow(t)
            sync_from_list()

        up_b.clicked.connect(lambda: move_item(-1))
        dn_b.clicked.connect(lambda: move_item(1))
        btns.addWidget(up_b)
        btns.addWidget(dn_b)
        btns.addStretch(1)
        lay.addLayout(btns)

        self._popup = pop
        self._list_w = lw
        pos = self.mapToGlobal(self.rect().bottomLeft())
        pop.move(pos)
        pop.show()


class MetadataSettingsWidget(QWidget):
    def __init__(
        self,
        config: Config,
        parent: Optional[QWidget] = None,
        standalone: bool = False,
        db: Optional[Database] = None,
        default_platform_id: Optional[int] = None,
    ):
        super().__init__(parent)
        self.config = config
        self._db = db

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setSpacing(8)
        lay.setContentsMargins(4, 4, 8, 4)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        keys = config.get("api_keys", {})
        saved_fields = config.get("metadata_fields", {})

        def _open_url(url: str) -> None:
            if not QDesktopServices.openUrl(QUrl(url)):
                import webbrowser
                webbrowser.open(url)

        def make_hint(html_txt: str) -> QLabel:
            lbl = QLabel(html_txt)
            lbl.setObjectName("MutedHint")
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.RichText)
            lbl.setTextInteractionFlags(Qt.TextBrowserInteraction)
            lbl.setOpenExternalLinks(False)
            lbl.linkActivated.connect(_open_url)
            lbl.setContentsMargins(0, 2, 0, 4)
            return lbl

        def make_section_title(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet("font-size: 10.5pt; font-weight: bold; color: #f58a1f; margin-top: 6px;")
            return lbl

        # --- 1. Lingua preferita ---
        lang_row = QHBoxLayout()
        lang_lbl = make_section_title(t("meta_lang_lbl"))
        self.lang_cb = QComboBox()
        for label, code in (
            ("Italiano", "it"),
            ("English", "en"),
            ("Français", "fr"),
            ("Español", "es"),
            ("Deutsch", "de"),
        ):
            self.lang_cb.addItem(label, code)
        cur_lang = config.get("metadata_language", get_lang())
        l_idx = self.lang_cb.findData(cur_lang)
        if l_idx >= 0:
            self.lang_cb.setCurrentIndex(l_idx)
        lang_hint = make_hint(t("meta_lang_hint"))
        lang_row.addWidget(lang_lbl)
        lang_row.addWidget(self.lang_cb)
        lang_row.addStretch(1)
        lay.addLayout(lang_row)
        lay.addWidget(lang_hint)

        # --- 2. Matrice Fonti Metadati per Campo (stile Playnite) ---
        lay.addWidget(make_section_title(t("meta_sources_title")))

        self._global_draft: dict = json.loads(json.dumps(saved_fields)) if saved_fields else {}
        raw_by_plat = config.get("metadata_fields_by_platform", {}) or {}
        self._per_plat_draft: dict[str, dict] = json.loads(json.dumps(raw_by_plat))

        scope_row = QHBoxLayout()
        scope_row.setSpacing(10)
        scope_lbl = QLabel("Piattaforma:")
        self.plat_scope_cb = QComboBox()
        self.plat_scope_cb.setMinimumWidth(180)
        self.plat_scope_cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

        db_tmp: Optional[Database] = None
        try:
            db_ref = self._db
            if db_ref is None and getattr(self.config, "root", ""):
                db_tmp = Database(self.config.root)
                db_ref = db_tmp
            if db_ref is not None:
                for p in db_ref.list_platforms():
                    if "parent_id" in p.keys() and p["parent_id"]:
                        continue
                    self.plat_scope_cb.addItem(p["name"], str(p["id"]))
                if default_platform_id is not None:
                    root_pid = default_platform_id
                    seen_pids: set[int] = set()
                    while root_pid not in seen_pids:
                        seen_pids.add(root_pid)
                        cur_p = db_ref.get_platform(root_pid)
                        if cur_p and "parent_id" in cur_p.keys() and cur_p["parent_id"]:
                            root_pid = int(cur_p["parent_id"])
                        else:
                            break
                    p_idx = self.plat_scope_cb.findData(str(root_pid))
                    if p_idx >= 0:
                        self.plat_scope_cb.setCurrentIndex(p_idx)
        finally:
            if db_tmp is not None:
                db_tmp.close()

        self.plat_scope_cb.setEnabled(self.plat_scope_cb.count() > 0)
        scope_row.addWidget(scope_lbl)
        scope_row.addWidget(self.plat_scope_cb, 1)
        lay.addLayout(scope_row)

        self._active_plat_key = str(self.plat_scope_cb.currentData() or "")
        from scraper import get_platform_default_fields
        for i in range(self.plat_scope_cb.count()):
            pkey = str(self.plat_scope_cb.itemData(i) or "")
            pname = self.plat_scope_cb.itemText(i)
            if pkey and not self._per_plat_draft.get(pkey):
                self._per_plat_draft[pkey] = get_platform_default_fields(pname)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(6)
        self.global_src_cb = _SourceMultiCombo(["libretro", "igdb", "screenscraper"], self)
        apply_all_btn = QPushButton("Applica")
        ctrl_row.addWidget(self.global_src_cb, 1)
        ctrl_row.addWidget(apply_all_btn)
        lay.addLayout(ctrl_row)

        ctrl_row2 = QHBoxLayout()
        ctrl_row2.setSpacing(6)
        select_all_btn = QPushButton("Seleziona tutto")
        deselect_all_btn = QPushButton("Deseleziona tutto")
        self.reset_defaults_btn = QPushButton("↺ Ripristina ordine default")
        self.reset_defaults_btn.setToolTip("Ripristina l'ordine consigliato delle fonti per tutti i campi")
        ctrl_row2.addWidget(select_all_btn)
        ctrl_row2.addWidget(deselect_all_btn)
        ctrl_row2.addWidget(self.reset_defaults_btn)
        ctrl_row2.addStretch(1)
        lay.addLayout(ctrl_row2)

        matrix_frame = QFrame()
        matrix_frame.setStyleSheet(
            "QFrame#MatrixBox { background-color: #1b1e25; border: 1px solid #2b303c; border-radius: 6px; }"
        )
        matrix_frame.setObjectName("MatrixBox")
        matrix_lay = QGridLayout(matrix_frame)
        matrix_lay.setContentsMargins(12, 10, 12, 10)
        matrix_lay.setVerticalSpacing(6)
        matrix_lay.setHorizontalSpacing(10)

        self._field_controls: dict[str, tuple[QCheckBox, _SourceMultiCombo]] = {}
        self.extra_shots_cb = QComboBox(matrix_frame)
        self.extra_shots_cb.addItem(t("meta_extra_shots_0"), 0)
        for n_ex in (1, 2, 3):
            self.extra_shots_cb.addItem(t("meta_extra_shots_n", n=n_ex), n_ex)
        self.extra_shots_cb.setToolTip(t("meta_extra_shots_tip"))
        self.extra_shots_cb.setMinimumWidth(96)

        active_fields = (
            self._per_plat_draft.get(self._active_plat_key, self._global_draft)
            if self._active_plat_key
            else self._global_draft
        )
        for row_idx, (fkey, flabel, default_srcs) in enumerate(_METADATA_FIELDS):
            f_cfg = active_fields.get(fkey, {})
            enabled = f_cfg.get("enabled", True)
            active_srcs = f_cfg.get("sources", default_srcs)

            lbl = QLabel(flabel)
            lbl.setMinimumWidth(160)
            chk = QCheckBox()
            chk.setChecked(bool(enabled))
            src_combo = _SourceMultiCombo(active_srcs, matrix_frame)

            matrix_lay.addWidget(lbl, row_idx, 0)
            matrix_lay.addWidget(chk, row_idx, 1)
            if fkey == "screenshot":
                ex_val = max(0, min(3, int(f_cfg.get("extra_count", 0) or 0)))
                ex_idx = self.extra_shots_cb.findData(ex_val)
                self.extra_shots_cb.setCurrentIndex(ex_idx if ex_idx >= 0 else 0)
                shot_wrap = QWidget(matrix_frame)
                shot_lay = QHBoxLayout(shot_wrap)
                shot_lay.setContentsMargins(0, 0, 0, 0)
                shot_lay.setSpacing(6)
                shot_lay.addWidget(src_combo, 1)
                shot_lay.addWidget(self.extra_shots_cb, 0)
                matrix_lay.addWidget(shot_wrap, row_idx, 2)
            else:
                matrix_lay.addWidget(src_combo, row_idx, 2)
            self._field_controls[fkey] = (chk, src_combo)

        matrix_lay.setColumnStretch(2, 1)
        lay.addWidget(matrix_frame)

        def _snapshot_matrix() -> None:
            snap = self._collect_matrix_dict()
            if self._active_plat_key:
                self._per_plat_draft[self._active_plat_key] = snap
            else:
                self._global_draft = snap

        def _apply_matrix_from(src_dict: dict) -> None:
            for fkey, _flabel, default_srcs in _METADATA_FIELDS:
                chk, combo = self._field_controls[fkey]
                f_cfg = src_dict.get(fkey, {})
                chk.setChecked(bool(f_cfg.get("enabled", True)))
                combo.set_sources(f_cfg.get("sources", default_srcs))
                if fkey == "screenshot":
                    ex_val = max(0, min(3, int(f_cfg.get("extra_count", 0) or 0)))
                    ex_idx = self.extra_shots_cb.findData(ex_val)
                    self.extra_shots_cb.setCurrentIndex(ex_idx if ex_idx >= 0 else 0)

        def _on_scope_changed() -> None:
            _snapshot_matrix()
            self._active_plat_key = str(self.plat_scope_cb.currentData() or "")
            if self._active_plat_key:
                _apply_matrix_from(self._per_plat_draft.get(self._active_plat_key, self._global_draft))
            else:
                _apply_matrix_from(self._global_draft)

        self.plat_scope_cb.currentIndexChanged.connect(lambda _idx: _on_scope_changed())

        def on_apply_all() -> None:
            srcs = self.global_src_cb.selected_sources()
            for _, combo in self._field_controls.values():
                combo.set_sources(srcs)

        def on_reset_defaults() -> None:
            plat_name = self.plat_scope_cb.currentText()
            plat_defaults = get_platform_default_fields(plat_name)
            for fkey, _flabel, default_srcs in _METADATA_FIELDS:
                chk, combo = self._field_controls[fkey]
                f_def = plat_defaults.get(fkey, {})
                chk.setChecked(bool(f_def.get("enabled", True)))
                combo.set_sources(f_def.get("sources", default_srcs))

        apply_all_btn.clicked.connect(on_apply_all)
        select_all_btn.clicked.connect(lambda: [c.setChecked(True) for c, _ in self._field_controls.values()])
        deselect_all_btn.clicked.connect(lambda: [c.setChecked(False) for c, _ in self._field_controls.values()])
        self.reset_defaults_btn.clicked.connect(on_reset_defaults)

        # --- 3. Account / API Key opzionali ---
        lay.addWidget(make_section_title("Account & Chiavi API (opzionali)"))
        lay.addWidget(
            make_hint(
                '<b>Nintendo EU</b>, <b>LaunchBox GamesDB</b>, <b>ArcadeDB</b>, <b>Wikidata</b>, <b>Steam Store</b>, <b>GOG</b>, <b>Wikipedia</b> e <b>Libretro Thumbnails</b> '
                'funzionano subito senza alcuna chiave API. '
                'Inserisci qui sotto le credenziali opzionali per <b>IGDB</b>, <b>ScreenScraper.fr</b>, <b>TheGamesDB</b> e <b>SteamGridDB</b>.'
            )
        )

        # IGDB
        igdb_lbl = QLabel("<b>IGDB</b>  <span style='color:#9ba2b2;'>(Twitch Developer)</span>")
        lay.addWidget(igdb_lbl)
        form3 = QFormLayout()
        form3.setVerticalSpacing(4)
        form3.setContentsMargins(0, 0, 0, 0)
        self.igdb_id_ed = QLineEdit(keys.get("igdb_client_id", ""))
        self.igdb_sec_ed = QLineEdit(keys.get("igdb_client_secret", ""))
        self.igdb_sec_ed.setEchoMode(QLineEdit.Password)
        form3.addRow("Client ID:", self.igdb_id_ed)
        form3.addRow("Client Secret:", self.igdb_sec_ed)
        lay.addLayout(form3)

        igdb_guide_row = QHBoxLayout()
        igdb_guide_row.setContentsMargins(0, 2, 0, 4)
        igdb_guide_row.setSpacing(8)
        self.igdb_guide_btn = QPushButton(t("meta_guide_btn"))
        self.igdb_guide_btn.setToolTip(t("meta_guide_tip"))
        self.igdb_guide_btn.setStyleSheet("padding: 4px 12px;")

        def _open_igdb_guide() -> None:
            lang = get_lang()
            candidates = [f"igdb_guide_{lang}.html"] if lang != "it" else []
            candidates.append("igdb_guide.html")
            for base in (self.config.root, os.path.dirname(os.path.abspath(__file__))):
                for fname in candidates:
                    gp = os.path.join(base, "docs", fname)
                    if os.path.isfile(gp):
                        if not QDesktopServices.openUrl(QUrl.fromLocalFile(gp)):
                            import webbrowser
                            webbrowser.open(f"file:///{gp.replace(os.sep, '/')}")
                        return
            QMessageBox.information(self, t("meta_guide_btn"), "File guida non trovato (docs/igdb_guide.html).")

        self.igdb_guide_btn.clicked.connect(_open_igdb_guide)
        igdb_guide_hint = QLabel(t("meta_guide_hint"))
        igdb_guide_hint.setObjectName("MutedHint")
        igdb_guide_row.addWidget(self.igdb_guide_btn)
        igdb_guide_row.addWidget(igdb_guide_hint)
        igdb_guide_row.addStretch(1)
        lay.addLayout(igdb_guide_row)

        # ScreenScraper
        ss_lbl = QLabel("<b>ScreenScraper.fr</b>  <span style='color:#9ba2b2;'>(Retrogaming IT/EN)</span>")
        lay.addWidget(ss_lbl)
        form2 = QFormLayout()
        form2.setVerticalSpacing(4)
        form2.setContentsMargins(0, 0, 0, 0)
        self.ss_user_ed = QLineEdit(keys.get("screenscraper_user", ""))
        self.ss_pass_ed = QLineEdit(keys.get("screenscraper_password", ""))
        self.ss_pass_ed.setEchoMode(QLineEdit.Password)
        form2.addRow("Utente:", self.ss_user_ed)
        form2.addRow("Password:", self.ss_pass_ed)
        lay.addLayout(form2)
        lay.addWidget(
            make_hint(
                'Sito web / Crea account gratuito: '
                '<a style="color:#f58a1f;" href="https://www.screenscraper.fr/">https://www.screenscraper.fr/</a>'
            )
        )

        # TheGamesDB
        tgdb_lbl = QLabel("<b>TheGamesDB</b>")
        lay.addWidget(tgdb_lbl)
        form1 = QFormLayout()
        form1.setVerticalSpacing(4)
        form1.setContentsMargins(0, 0, 0, 0)
        self.tgdb_ed = QLineEdit(keys.get("thegamesdb", ""))
        form1.addRow("API Key:", self.tgdb_ed)
        lay.addLayout(form1)
        lay.addWidget(
            make_hint(
                'Sito web / Richiedi API Key gratuita: '
                '<a style="color:#f58a1f;" href="https://thegamesdb.net/">https://thegamesdb.net/</a>'
            )
        )

        # SteamGridDB
        sgdb_lbl = QLabel("<b>SteamGridDB</b>  <span style='color:#9ba2b2;'>(Copertine verticali 600×900 e loghi PNG)</span>")
        lay.addWidget(sgdb_lbl)
        form4 = QFormLayout()
        form4.setVerticalSpacing(4)
        form4.setContentsMargins(0, 0, 0, 0)
        self.sgdb_key_ed = QLineEdit(keys.get("steamgriddb", ""))
        self.sgdb_key_ed.setEchoMode(QLineEdit.Password)
        form4.addRow("API Key:", self.sgdb_key_ed)
        lay.addLayout(form4)
        lay.addWidget(
            make_hint(
                'Sito web / Genera API Key gratuita: '
                '<a style="color:#f58a1f;" href="https://www.steamgriddb.com/profile/preferences/api">'
                'https://www.steamgriddb.com/profile/preferences/api</a>'
            )
        )

        # Barra inferiore fissa
        bottom = QHBoxLayout()
        note = QLabel("Impostazioni salvate in data/config.json nella cartella del programma.")
        note.setObjectName("MutedHint")
        bottom.addWidget(note, 1)
        self.save_btn: Optional[QPushButton] = None
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        if not standalone:
            self.saved_lbl = QLabel("")
            self.saved_lbl.setStyleSheet("color: #f58a1f; font-weight: bold;")
            self._saved_timer.timeout.connect(lambda: self.saved_lbl.setText(""))
            self.save_btn = QPushButton(t("meta_save_btn"))
            self.save_btn.setObjectName("AccentButton")
            self.save_btn.setStyleSheet(
                "QPushButton#AccentButton:disabled { background-color: #1f232c; color: #5c6375; border: 1px solid #2b303c; }"
            )
            self.save_btn.clicked.connect(self._save_inline)
            bottom.addWidget(self.saved_lbl)
            bottom.addWidget(self.save_btn)
        outer.addLayout(bottom)

        self._saved_snapshot: dict = self._build_payload()
        self._update_dirty_state()

        # Collega tutti i controlli al rilevamento modifiche
        self.lang_cb.currentIndexChanged.connect(self._update_dirty_state)
        self.plat_scope_cb.currentIndexChanged.connect(lambda _i: self._update_dirty_state())
        for chk, combo in self._field_controls.values():
            chk.stateChanged.connect(self._update_dirty_state)
            combo.changed.connect(self._update_dirty_state)
        self.extra_shots_cb.currentIndexChanged.connect(lambda _i: self._update_dirty_state())
        apply_all_btn.clicked.connect(self._update_dirty_state)
        select_all_btn.clicked.connect(self._update_dirty_state)
        deselect_all_btn.clicked.connect(self._update_dirty_state)
        self.reset_defaults_btn.clicked.connect(self._update_dirty_state)
        for ed in (self.igdb_id_ed, self.igdb_sec_ed, self.ss_user_ed, self.ss_pass_ed, self.tgdb_ed, self.sgdb_key_ed):
            ed.textChanged.connect(self._update_dirty_state)

    def _collect_matrix_dict(self) -> dict:
        out: dict = {}
        for fkey, (chk, combo) in self._field_controls.items():
            entry: dict = {
                "enabled": chk.isChecked(),
                "sources": combo.selected_sources(),
            }
            if fkey == "screenshot":
                entry["extra_count"] = max(0, min(3, int(self.extra_shots_cb.currentData() or 0)))
            out[fkey] = entry
        return out

    def _build_payload(self) -> dict:
        global_draft = json.loads(json.dumps(self._global_draft))
        per_plat_draft = json.loads(json.dumps(self._per_plat_draft))
        current_snap = self._collect_matrix_dict()
        key = str(self.plat_scope_cb.currentData() or self._active_plat_key)
        if key:
            per_plat_draft[key] = current_snap
        else:
            global_draft = current_snap
        return {
            "api_keys": {
                "thegamesdb": self.tgdb_ed.text().strip(),
                "screenscraper_user": self.ss_user_ed.text().strip(),
                "screenscraper_password": self.ss_pass_ed.text().strip(),
                "igdb_client_id": self.igdb_id_ed.text().strip(),
                "igdb_client_secret": self.igdb_sec_ed.text().strip(),
                "steamgriddb": self.sgdb_key_ed.text().strip(),
            },
            "metadata_scope": "per_platform",
            "metadata_fields": global_draft,
            "metadata_fields_by_platform": per_plat_draft,
            "metadata_language": self.lang_cb.currentData() or "it",
        }

    def _update_dirty_state(self) -> None:
        if self.save_btn is not None:
            dirty = self._build_payload() != self._saved_snapshot
            self.save_btn.setEnabled(dirty)

    def save_to_config(self) -> None:
        payload = self._build_payload()
        self._global_draft = json.loads(json.dumps(payload["metadata_fields"]))
        self._per_plat_draft = json.loads(json.dumps(payload["metadata_fields_by_platform"]))
        for k, v in payload.items():
            self.config.data[k] = v
        self.config.save()
        self._saved_snapshot = json.loads(json.dumps(payload))
        self._update_dirty_state()

    def _save_inline(self) -> None:
        self.save_to_config()
        if hasattr(self, "saved_lbl"):
            self.saved_lbl.setText("Salvato!")
            self._saved_timer.start(1000)


# --------------------------------------------------------------------------
# Scraping Batch + Tab Impostazione Metadati
# --------------------------------------------------------------------------

class ScrapeBatchDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        db: Database,
        default_platform_id: Optional[int] = None,
        config: Optional[Config] = None,
        initial_tab: int = 0,
    ):
        super().__init__(parent)
        self.db = db
        self.config = config or Config(db.root)
        self.config.load()
        self.worker: Optional[BatchScrapeWorker] = None
        self.updated_any = False
        self.continued_in_background = False
        self.setWindowTitle("Scarica Metadati e Copertine")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(620, 480)
        self.resize(980, 800)
        self._drag_pos = None

        outer_lay = QVBoxLayout(self)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("FramelessCard")
        card.setStyleSheet(
            "QFrame#FramelessCard { background-color: #16181e; border: 1px solid #353b4a; border-radius: 9px; }"
        )
        outer_lay.addWidget(card)

        root_lay = QVBoxLayout(card)
        root_lay.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        root_lay.addWidget(self.tabs, 1)

        # --- Tab 1: Scarica Metadati ---
        tab_dl = QWidget()
        lay = QVBoxLayout(tab_dl)
        form = QFormLayout()

        self.plat_cb = QComboBox()
        self.plat_cb.addItem(t("all_platforms"), None)
        for p in db.list_platforms():
            self.plat_cb.addItem(f"{p['name']} ({p['game_count']})", p["id"])
        if default_platform_id is not None:
            idx = self.plat_cb.findData(default_platform_id)
            if idx >= 0:
                self.plat_cb.setCurrentIndex(idx)

        only_row = QWidget()
        only_lay = QHBoxLayout(only_row)
        only_lay.setContentsMargins(0, 0, 0, 0)
        only_lay.setSpacing(8)
        self.only_missing_chk = QCheckBox("Scarica solo metadati mancanti")
        self.only_missing_chk.setChecked(True)
        self.only_missing_chk.setStyleSheet(
            "QCheckBox:disabled { color: #4b5162; } "
            "QCheckBox::indicator:disabled { background-color: #191c24; border: 1px solid #2b303c; }"
        )
        only_hint = QLabel("(se disattivato tutti i metadati che verranno trovati sovrascriveranno quelli esistenti)")
        only_hint.setObjectName("MutedHint")
        only_hint.setStyleSheet("QLabel:disabled { color: #3f4554; }")
        only_hint.setWordWrap(True)
        only_lay.addWidget(self.only_missing_chk)
        only_lay.addWidget(only_hint, 1)

        self.clear_before_chk = QCheckBox("Cancella tutti i metadati prima della scansione")
        self.clear_before_chk.setChecked(False)
        self.clear_before_chk.toggled.connect(
            lambda checked: (
                self.only_missing_chk.setEnabled(not checked),
                only_hint.setEnabled(not checked),
            )
        )

        form.addRow("Piattaforma:", self.plat_cb)
        form.addRow("", only_row)
        form.addRow("", self.clear_before_chk)
        lay.addLayout(form)

        self.pbar = QProgressBar()
        self.pbar.setValue(0)
        lay.addWidget(self.pbar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

        btns = QHBoxLayout()
        self.start_btn = QPushButton("Avvia Download")
        self.start_btn.setObjectName("AccentButton")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Interrompi")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.tabs.addTab(tab_dl, t("meta_tab_dl"))

        # --- Tab 2: Impostazioni Metadati ---
        self.settings_w = MetadataSettingsWidget(
            self.config,
            self,
            standalone=False,
            db=self.db,
            default_platform_id=default_platform_id,
        )
        self.tabs.addTab(self.settings_w, t("meta_tab_settings"))
        self.tabs.setCurrentIndex(initial_tab)

        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch(1)
        self.bg_btn = QPushButton("Continua in background")
        self.bg_btn.setEnabled(False)
        self.bg_btn.clicked.connect(self._continue_in_background)
        self.close_btn = QPushButton("Chiudi")
        self.close_btn.clicked.connect(self.accept)
        bottom_bar.addWidget(self.bg_btn)
        bottom_bar.addWidget(self.close_btn)
        bottom_bar.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        root_lay.addLayout(bottom_bar)

    def attach_running_worker(self, worker: BatchScrapeWorker, idx: int = 0, total: int = 0, last_msg: str = "") -> None:
        """Riaggancia un download già avviato in background per seguirlo nella finestra."""
        self.worker = worker
        self.continued_in_background = False
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.bg_btn.setEnabled(True)
        self.close_btn.setEnabled(False)
        if total > 0:
            self.pbar.setMaximum(total)
            self.pbar.setValue(idx)
        if last_msg:
            self.log.appendPlainText(f"[{idx}/{total}] {last_msg}")
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)

    def _hit_edges(self, pos) -> Qt.Edges:
        m = 8
        edges = Qt.Edges()
        if pos.x() <= m:
            edges |= Qt.LeftEdge
        elif pos.x() >= self.width() - m:
            edges |= Qt.RightEdge
        if pos.y() <= m:
            edges |= Qt.TopEdge
        elif pos.y() >= self.height() - m:
            edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            edges = self._hit_edges(pos)
            wh = self.windowHandle()
            if edges and wh and wh.startSystemResize(edges):
                event.accept()
                return
            if wh and wh.startSystemMove():
                event.accept()
                return
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        edges = self._hit_edges(event.position().toPoint())
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge | Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def reject(self) -> None:
        # Impedisce chiusura accidentale con Esc mentre il download è in corso
        if self.worker and self.worker.isRunning():
            return
        super().reject()

    def _start(self) -> None:
        pid = self.plat_cb.currentData()
        if self.clear_before_chk.isChecked():
            reply = QMessageBox.warning(
                self,
                "Conferma cancellazione metadati",
                "Attenzione: proseguendo tutti i metadati verranno cancellati prima di eseguire la scansione.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            cleared = self.db.clear_platform_metadata(pid)
            self.updated_any = True
            self.log.clear()
            self.log.appendPlainText(f"Cancellati i metadati di {cleared} giochi. Avvio scansione…")
        else:
            self.log.clear()

        # Salva automaticamente eventuali modifiche fatte nella tab Impostazione Metadati prima di avviare
        self.settings_w.save_to_config()
        self.pbar.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.bg_btn.setEnabled(True)
        self.close_btn.setEnabled(False)

        self.worker = BatchScrapeWorker(self.db.root, pid, self.only_missing_chk.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.start()

    def _continue_in_background(self) -> None:
        if not self.worker or not self.worker.isRunning():
            self.accept()
            return
        try:
            self.worker.progress.disconnect(self._on_progress)
        except (TypeError, RuntimeError):
            pass
        try:
            self.worker.completed.disconnect(self._on_completed)
        except (TypeError, RuntimeError):
            pass
        self.continued_in_background = True
        self.accept()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.log.appendPlainText("Interruzione richiesta…")

    def _on_progress(self, idx: int, total: int, msg: str) -> None:
        self.pbar.setMaximum(max(total, 1))
        self.pbar.setValue(idx)
        self.log.appendPlainText(f"[{idx}/{total}] {msg}")

    def _on_completed(self, stats: dict) -> None:
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.bg_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        if stats.get("updated", 0) > 0:
            self.updated_any = True
        self.log.appendPlainText(
            f"\nCompletato — Analizzati: {stats.get('total', 0)}, "
            f"Aggiornati: {stats.get('updated', 0)}, Non trovati: {stats.get('missed', 0)}"
        )


class SettingsDialog(QDialog):
    """Finestra autonoma (mantenuta per compatibilità; usa MetadataSettingsWidget)."""

    def __init__(self, parent: QWidget, config: Config):
        super().__init__(parent)
        self.setWindowTitle("Impostazioni Metadati")
        self.resize(920, 720)
        lay = QVBoxLayout(self)
        self.widget = MetadataSettingsWidget(config, self, standalone=True)
        lay.addWidget(self.widget, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _save(self) -> None:
        self.widget.save_to_config()
        self.accept()


class _BackupRestoreWorker(QThread):
    progress = Signal(int, int, str)
    done_ok = Signal(dict)
    done_err = Signal(str)

    def __init__(self, mode: str, db: Database, zip_path: str):
        super().__init__()
        self.mode = mode  # "backup" | "restore"
        self.db = db
        self.zip_path = zip_path

    def run(self) -> None:
        try:
            if self.mode == "backup":
                res = create_backup(
                    self.db,
                    self.zip_path,
                    progress_cb=lambda d, t, m: self.progress.emit(d, t, m),
                )
            else:
                res = restore_backup(
                    self.db.root,
                    self.zip_path,
                    progress_cb=lambda d, t, m: self.progress.emit(d, t, m),
                )
            self.done_ok.emit(res)
        except Exception as exc:
            self.done_err.emit(str(exc))


class BackupRestoreDialog(QDialog):
    """Finestra per creare un backup completo portabile (.zip) e ripristinarlo su una nuova versione."""

    def __init__(self, parent: QWidget, db: Database, config: Config):
        super().__init__(parent)
        self.db = db
        self.config = config
        self.restored = False
        self._worker: Optional[_BackupRestoreWorker] = None

        self.setWindowTitle(t("backup_title"))
        self.resize(560, 360)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        title = QLabel(t("backup_heading"))
        title.setStyleSheet("font-size: 13pt; font-weight: bold; color: #f58a1f;")
        lay.addWidget(title)

        info = QLabel(t("backup_info"))
        info.setWordWrap(True)
        info.setObjectName("MutedHint")
        lay.addWidget(info)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.backup_btn = QPushButton(t("backup_btn_create"))
        self.backup_btn.setObjectName("AccentButton")
        self.backup_btn.setMinimumHeight(38)
        self.backup_btn.clicked.connect(self._start_backup)

        self.restore_btn = QPushButton(t("backup_btn_restore"))
        self.restore_btn.setMinimumHeight(38)
        self.restore_btn.clicked.connect(self._start_restore)

        btn_row.addWidget(self.backup_btn, 1)
        btn_row.addWidget(self.restore_btn, 1)
        lay.addLayout(btn_row)

        self.pbar = QProgressBar()
        self.pbar.setValue(0)
        self.pbar.setVisible(False)
        lay.addWidget(self.pbar)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("MutedHint")
        self.status_lbl.setWordWrap(True)
        lay.addWidget(self.status_lbl)

        lay.addStretch(1)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        self.close_btn = btns.button(QDialogButtonBox.Close)
        btns.rejected.connect(self.accept)
        lay.addWidget(btns)

    def _set_busy(self, busy: bool) -> None:
        self.backup_btn.setEnabled(not busy)
        self.restore_btn.setEnabled(not busy)
        self.close_btn.setEnabled(not busy)
        self.pbar.setVisible(busy)

    def _start_backup(self) -> None:
        import datetime

        default_name = f"EasyLauncher_Backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.zip"
        default_path = os.path.join(self.db.root, default_name)
        zip_path, _ = QFileDialog.getSaveFileName(
            self,
            "Salva file di Backup completo",
            default_path,
            "Archivio Backup EasyLauncher (*.zip)",
        )
        if not zip_path:
            return
        if not zip_path.lower().endswith(".zip"):
            zip_path += ".zip"

        self.config.save()
        self._set_busy(True)
        self.pbar.setRange(0, 0)
        self.status_lbl.setText("Creazione archivio di backup in corso…")

        self._worker = _BackupRestoreWorker("backup", self.db, zip_path)
        self._worker.progress.connect(self._on_progress)
        self._worker.done_ok.connect(lambda res: self._on_backup_ok(res, zip_path))
        self._worker.done_err.connect(self._on_error)
        self._worker.start()

    def _start_restore(self) -> None:
        zip_path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleziona file di Backup da ripristinare",
            self.db.root,
            "Archivio Backup EasyLauncher (*.zip)",
        )
        if not zip_path:
            return
        try:
            manifest = inspect_backup(zip_path)
        except Exception as exc:
            QMessageBox.warning(self, "Backup non valido", f"Impossibile leggere il backup:\n{exc}")
            return

        created = manifest.get("created_at", "") or "data sconosciuta"
        msg = (
            f"Ripristinare il backup selezionato?\n\n"
            f"• Data backup: {created}\n"
            f"• Piattaforme: {manifest.get('platforms', '?')}  |  Giochi: {manifest.get('games', '?')}\n"
            f"• Preferiti: {manifest.get('favorites', '?')}  |  Emulatori: {manifest.get('emulators', '?')}\n"
            f"• File inclusi: {manifest.get('files', '?')}\n\n"
            "I dati e le impostazioni attuali verranno sostituiti con quelli del backup."
        )
        if QMessageBox.question(self, "Conferma Ripristino", msg) != QMessageBox.Yes:
            return

        # Chiude la connessione DB sul thread principale prima di sostituire i file
        self.db.close()
        self._set_busy(True)
        self.pbar.setRange(0, 0)
        self.status_lbl.setText("Ripristino dati e immagini in corso…")

        self._worker = _BackupRestoreWorker("restore", self.db, zip_path)
        self._worker.progress.connect(self._on_progress)
        self._worker.done_ok.connect(self._on_restore_ok)
        self._worker.done_err.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        self.pbar.setRange(0, max(total, 1))
        self.pbar.setValue(min(done, max(total, 1)))
        self.status_lbl.setText(f"[{done}/{total}] {msg}")

    def _on_backup_ok(self, res: dict, zip_path: str) -> None:
        self._set_busy(False)
        size_mb = os.path.getsize(zip_path) / (1024 * 1024) if os.path.isfile(zip_path) else 0.0
        summary = (
            f"Backup completato ({size_mb:.1f} MB): {os.path.basename(zip_path)}\n"
            f"Piattaforme: {res.get('platforms', 0)} · Giochi: {res.get('games', 0)} · "
            f"Preferiti: {res.get('favorites', 0)} · File: {res.get('files', 0)}"
        )
        self.status_lbl.setText(summary)
        QMessageBox.information(self, "Backup completato", f"{summary}\n\nPercorso:\n{zip_path}")

    def _on_restore_ok(self, res: dict) -> None:
        # Riapre e migra il DB ripristinato e ricarica la configurazione
        try:
            self.db._migrate()
            self.config.load()
        except Exception as exc:
            self._on_error(f"Errore riapertura database dopo il ripristino:\n{exc}")
            return

        self.restored = True
        self._set_busy(False)
        summary = (
            f"Ripristino completato!\n"
            f"Piattaforme: {res.get('platforms', '?')} · Giochi: {res.get('games', '?')} · "
            f"Preferiti: {res.get('favorites', '?')}"
        )
        self.status_lbl.setText(summary)
        QMessageBox.information(self, "Ripristino completato", summary)

    def _on_error(self, err: str) -> None:
        try:
            self.db._migrate()
        except Exception:
            pass
        self._set_busy(False)
        self.status_lbl.setText(f"Errore: {err}")
        QMessageBox.critical(self, "Errore Backup / Ripristino", err)


class AppearanceDialog(QDialog):
    """Finestra per personalizzare profili, tema (scuro/bianco), colore selezione, sfondi e linee divisorie."""

    _DEFAULT_APPEARANCE = {
        "theme": "dark",
        "accent_color": "#f58a1f",
        "divider_color": "#1a4b25",
        "divider_transparent": True,
        "menubar_color": "",
        "card_color": "",
        "bg_unified_enabled": False,
        "bg_unified": "",
        "bg_color_unified": "",
        "bg_dynamic_blur": True,
        "bg_dynamic_left": True,
        "bg_dynamic_center": True,
        "bg_dynamic_right": True,
        "bg_dynamic_blur_radius": 38,
        "bg_dynamic_blur_intensity": 43,
        "bg_left": "",
        "bg_center": "",
        "bg_right": "",
        "bg_color_left": "",
        "bg_color_center": "",
        "bg_color_right": "",
    }

    def __init__(self, parent: QWidget, db: Database, config: Config, on_apply=None, on_blur_live=None):
        super().__init__(parent)
        self.db = db
        self.config = config
        self._on_apply = on_apply
        self._on_blur_live = on_blur_live
        self._closing_confirmed = False
        self._blur_live_timer = QTimer(self)
        self._blur_live_timer.setSingleShot(True)
        self._blur_live_timer.setInterval(35)
        self._blur_live_timer.timeout.connect(self._run_blur_live)
        self._blur_save_timer = QTimer(self)
        self._blur_save_timer.setSingleShot(True)
        self._blur_save_timer.setInterval(220)
        self._blur_save_timer.timeout.connect(self._emit_apply)

        self.setWindowTitle(t("app_title"))
        self.resize(640, 470)

        lay = QVBoxLayout(self)
        lay.setSpacing(14)

        # 0) Profilo salvato (Profilo Default non modificabile)
        prof_row = QHBoxLayout()
        prof_lbl = QLabel(t("app_profile_lbl"))
        prof_lbl.setStyleSheet("font-weight: bold;")
        self.profile_cb = QComboBox()
        self.profile_cb.currentIndexChanged.connect(self._on_profile_changed)
        prof_row.addWidget(prof_lbl)
        prof_row.addWidget(self.profile_cb, 1)
        lay.addLayout(prof_row)

        # 1) Tema (Scuro / Bianco)
        theme_row = QHBoxLayout()
        theme_lbl = QLabel(t("app_theme_lbl"))
        theme_lbl.setStyleSheet("font-weight: bold;")
        self.theme_cb = QComboBox()
        self.theme_cb.addItem(t("app_theme_dark"), "dark")
        self.theme_cb.addItem(t("app_theme_light"), "light")
        cur_theme = self.config.get("theme", "dark")
        idx = self.theme_cb.findData(cur_theme)
        if idx >= 0:
            self.theme_cb.setCurrentIndex(idx)
        self.theme_cb.currentIndexChanged.connect(self._on_theme_changed)
        theme_row.addWidget(theme_lbl)
        theme_row.addWidget(self.theme_cb, 1)
        lay.addLayout(theme_row)

        # 2) Colore/Sfondo unificato + per le 3 sezioni (cornice dinamica con accent_color)
        self.bg_frame = QGroupBox(t("app_bg_group"))
        self.bg_frame.setObjectName("BgGroupBox")
        bg_grid = QGridLayout(self.bg_frame)
        bg_grid.setContentsMargins(10, 12, 10, 10)
        bg_grid.setVerticalSpacing(8)
        bg_grid.setHorizontalSpacing(8)
        self._bg_labels: dict[str, tuple[QFrame, QLabel]] = {}
        self._bg_clear_btns: dict[str, QPushButton] = {}
        self._unified_controls: list[QWidget] = []
        self._per_section_widgets: list[QWidget] = []
        self._per_section_rows: dict[str, list[QWidget]] = {}

        def _make_val_cell() -> tuple[QWidget, QFrame, QLabel]:
            box = QWidget()
            hl = QHBoxLayout(box)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(8)
            sw = QFrame()
            sw.setFixedSize(16, 16)
            sw.setVisible(False)
            lb = QLabel("")
            lb.setObjectName("MutedHint")
            hl.addWidget(sw)
            hl.addWidget(lb, 1)
            box.setMinimumWidth(160)
            return box, sw, lb

        sections = (
            ("bg_left", "bg_color_left", t("app_bg_left")),
            ("bg_center", "bg_color_center", t("app_bg_center")),
            ("bg_right", "bg_color_right", t("app_bg_right")),
        )
        for row_idx, (img_key, col_key, caption) in enumerate(sections):
            lbl = QLabel(caption)
            lbl.setStyleSheet("font-weight: bold;")
            val_box, sw, path_lbl = _make_val_cell()
            color_btn = QPushButton(t("app_bg_color_btn"))
            color_btn.clicked.connect(lambda _c=False, ik=img_key, ck=col_key: self._pick_bg_color(ik, ck))
            browse_btn = QPushButton(t("btn_browse"))
            browse_btn.clicked.connect(lambda _c=False, ik=img_key, ck=col_key: self._browse_bg(ik, ck))
            clear_btn = QPushButton("🗑")
            clear_btn.setToolTip(t("app_bg_clear"))
            clear_btn.setFixedWidth(36)
            clear_btn.clicked.connect(lambda _c=False, ik=img_key, ck=col_key: self._clear_bg(ik, ck))

            bg_grid.addWidget(lbl, row_idx, 0)
            bg_grid.addWidget(val_box, row_idx, 1)
            bg_grid.addWidget(color_btn, row_idx, 2)
            bg_grid.addWidget(browse_btn, row_idx, 3)
            bg_grid.addWidget(clear_btn, row_idx, 4)
            self._bg_labels[img_key] = (sw, path_lbl)
            self._bg_clear_btns[img_key] = clear_btn
            row_ws = [lbl, val_box, color_btn, browse_btn, clear_btn]
            self._per_section_widgets.extend(row_ws)
            self._per_section_rows[img_key] = row_ws

        self.unified_cb = QCheckBox(t("app_bg_unified"))
        self.unified_cb.setStyleSheet("font-weight: bold;")
        self.unified_cb.setChecked(bool(self.config.get("bg_unified_enabled", False)))
        self.unified_cb.toggled.connect(self._on_unified_toggled)
        u_box, u_sw, u_path_lbl = _make_val_cell()
        u_color_btn = QPushButton(t("app_bg_color_btn"))
        u_color_btn.clicked.connect(lambda _c=False: self._pick_bg_color("bg_unified", "bg_color_unified"))
        u_browse_btn = QPushButton(t("btn_browse"))
        u_browse_btn.clicked.connect(lambda _c=False: self._browse_bg("bg_unified", "bg_color_unified"))
        u_clear_btn = QPushButton("🗑")
        u_clear_btn.setToolTip(t("app_bg_clear"))
        u_clear_btn.setFixedWidth(36)
        u_clear_btn.clicked.connect(lambda _c=False: self._clear_bg("bg_unified", "bg_color_unified"))
        bg_grid.addWidget(self.unified_cb, 3, 0)
        bg_grid.addWidget(u_box, 3, 1)
        bg_grid.addWidget(u_color_btn, 3, 2)
        bg_grid.addWidget(u_browse_btn, 3, 3)
        bg_grid.addWidget(u_clear_btn, 3, 4)
        self._bg_labels["bg_unified"] = (u_sw, u_path_lbl)
        self._bg_clear_btns["bg_unified"] = u_clear_btn
        self._unified_controls.extend([u_box, u_color_btn, u_browse_btn, u_clear_btn])

        # Sfondo sfocato dinamico (LaunchBox / Playnite) + scelta sezioni SX / Centrale / DX
        dyn_hdr = QWidget()
        dyn_hdr_hl = QHBoxLayout(dyn_hdr)
        dyn_hdr_hl.setContentsMargins(0, 0, 0, 0)
        dyn_hdr_hl.setSpacing(12)
        self.dyn_blur_cb = QCheckBox(t("app_bg_dynamic_blur"))
        self.dyn_blur_cb.setStyleSheet("font-weight: bold;")
        self.dyn_blur_cb.setChecked(bool(self.config.get("bg_dynamic_blur", True)))
        self.dyn_blur_cb.toggled.connect(self._on_dyn_blur_toggled)
        self.dyn_left_cb = QCheckBox(t("app_bg_dyn_left"))
        self.dyn_left_cb.setChecked(bool(self.config.get("bg_dynamic_left", False)))
        self.dyn_left_cb.toggled.connect(lambda c: self._on_dyn_section_toggled("bg_dynamic_left", c))
        self.dyn_center_cb = QCheckBox(t("app_bg_dyn_center"))
        self.dyn_center_cb.setChecked(bool(self.config.get("bg_dynamic_center", True)))
        self.dyn_center_cb.toggled.connect(lambda c: self._on_dyn_section_toggled("bg_dynamic_center", c))
        self.dyn_right_cb = QCheckBox(t("app_bg_dyn_right"))
        self.dyn_right_cb.setChecked(bool(self.config.get("bg_dynamic_right", True)))
        self.dyn_right_cb.toggled.connect(lambda c: self._on_dyn_section_toggled("bg_dynamic_right", c))
        dyn_hdr_hl.addWidget(self.dyn_blur_cb)
        dyn_hdr_hl.addWidget(self.dyn_left_cb)
        dyn_hdr_hl.addWidget(self.dyn_center_cb)
        dyn_hdr_hl.addWidget(self.dyn_right_cb)
        dyn_hdr_hl.addStretch(1)
        bg_grid.addWidget(dyn_hdr, 4, 0, 1, 5)
        self._dyn_section_cbs = [self.dyn_left_cb, self.dyn_center_cb, self.dyn_right_cb]

        dyn_row = QWidget()
        dyn_hl = QHBoxLayout(dyn_row)
        dyn_hl.setContentsMargins(20, 0, 0, 0)
        dyn_hl.setSpacing(10)
        self._dyn_blur_lbl = QLabel(t("app_bg_blur_radius"))
        self._dyn_blur_slider = QSlider(Qt.Horizontal)
        self._dyn_blur_slider.setRange(8, 80)
        self._dyn_blur_slider.setValue(int(self.config.get("bg_dynamic_blur_radius", 36) or 36))
        self._dyn_blur_slider.valueChanged.connect(self._on_blur_radius_changed)
        self._dyn_blur_slider.sliderReleased.connect(self._on_blur_slider_released)
        self._dyn_blur_val = QLabel(f"{self._dyn_blur_slider.value()} px")
        self._dyn_blur_val.setMinimumWidth(42)

        self._dyn_int_lbl = QLabel(t("app_bg_blur_intensity"))
        self._dyn_int_slider = QSlider(Qt.Horizontal)
        self._dyn_int_slider.setRange(10, 80)
        self._dyn_int_slider.setValue(int(self.config.get("bg_dynamic_blur_intensity", 50) or 50))
        self._dyn_int_slider.valueChanged.connect(self._on_blur_intensity_changed)
        self._dyn_int_slider.sliderReleased.connect(self._on_blur_slider_released)
        self._dyn_int_val = QLabel(f"{self._dyn_int_slider.value()}%")
        self._dyn_int_val.setMinimumWidth(36)

        dyn_hl.addWidget(self._dyn_blur_lbl)
        dyn_hl.addWidget(self._dyn_blur_slider, 1)
        dyn_hl.addWidget(self._dyn_blur_val)
        dyn_hl.addSpacing(8)
        dyn_hl.addWidget(self._dyn_int_lbl)
        dyn_hl.addWidget(self._dyn_int_slider, 1)
        dyn_hl.addWidget(self._dyn_int_val)
        bg_grid.addWidget(dyn_row, 5, 0, 1, 5)
        self._dyn_blur_controls = [
            self._dyn_blur_lbl, self._dyn_blur_slider, self._dyn_blur_val,
            self._dyn_int_lbl, self._dyn_int_slider, self._dyn_int_val,
        ]

        bg_grid.setColumnStretch(1, 1)
        lay.addWidget(self.bg_frame)

        # 3) Colore selezione e linee divisorie (righe uniformi)
        line_grid = QGridLayout()
        line_grid.setVerticalSpacing(8)
        line_grid.setHorizontalSpacing(8)
        self._line_labels: dict[str, tuple[QFrame, QLabel]] = {}
        self._line_clear_btns: dict[str, QPushButton] = {}
        self._divider_row_widgets: list[QWidget] = []
        for row_idx, (key, caption) in enumerate((
            ("accent_color", t("app_accent_lbl")),
            ("divider_color", t("app_divider_lbl")),
            ("menubar_color", t("app_menubar_lbl")),
            ("card_color", t("app_card_lbl")),
        )):
            lbl = QLabel(caption)
            lbl.setStyleSheet("font-weight: bold;")
            val_box, sw, val_lbl = _make_val_cell()
            pick_btn = QPushButton(t("app_bg_color_btn"))
            pick_btn.clicked.connect(
                (lambda _c=False: self._choose_custom_color())
                if key == "accent_color"
                else (lambda _c=False, k=key: self._pick_line_color(k))
            )
            clear_btn = QPushButton("🗑")
            clear_btn.setToolTip(t("app_bg_clear"))
            clear_btn.setFixedWidth(36)
            clear_btn.clicked.connect(
                (lambda _c=False: self._set_accent("#f58a1f"))
                if key == "accent_color"
                else (lambda _c=False, k=key: self._clear_line_color(k))
            )
            line_grid.addWidget(lbl, row_idx, 0)
            line_grid.addWidget(val_box, row_idx, 1)
            line_grid.addWidget(pick_btn, row_idx, 2)
            line_grid.addWidget(clear_btn, row_idx, 3)
            if key == "divider_color":
                self.divider_transp_cb = QCheckBox(t("app_divider_transparent"))
                self.divider_transp_cb.setChecked(bool(self.config.get("divider_transparent", False)))
                self.divider_transp_cb.toggled.connect(self._on_divider_transp_toggled)
                line_grid.addWidget(self.divider_transp_cb, row_idx, 4)
                self._divider_row_widgets.extend([lbl, val_box, pick_btn, clear_btn])
            self._line_labels[key] = (sw, val_lbl)
            self._line_clear_btns[key] = clear_btn
        line_grid.setColumnStretch(1, 1)
        lay.addLayout(line_grid)

        lay.addStretch(1)

        # 4) Azioni profilo + Chiudi
        bottom = QHBoxLayout()
        self.save_profile_btn = QPushButton(t("app_profile_save"))
        self.save_profile_btn.clicked.connect(self._save_current_profile)
        self.save_as_profile_btn = QPushButton(t("app_profile_save_as"))
        self.save_as_profile_btn.clicked.connect(self._save_profile_as)
        self.del_profile_btn = QPushButton(t("app_profile_delete"))
        self.del_profile_btn.clicked.connect(self._delete_current_profile)
        bottom.addWidget(self.save_profile_btn)
        bottom.addWidget(self.save_as_profile_btn)
        bottom.addWidget(self.del_profile_btn)
        bottom.addStretch(1)
        close_btn = QPushButton(t("dlg_btn_close"))
        close_btn.setObjectName("AccentButton")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        lay.addLayout(bottom)

        self._populate_profiles()
        self._refresh_controls()

    @staticmethod
    def _set_trash_enabled(btn: QPushButton, enabled: bool) -> None:
        btn.setEnabled(enabled)
        eff = btn.graphicsEffect()
        if not isinstance(eff, QGraphicsOpacityEffect):
            eff = QGraphicsOpacityEffect(btn)
            btn.setGraphicsEffect(eff)
        eff.setOpacity(1.0 if enabled else 0.28)

    def _has_unsaved_profile_changes(self, prof_id: str) -> bool:
        if not prof_id or prof_id == "__default__":
            for k, default_v in self._DEFAULT_APPEARANCE.items():
                if self.config.get(k, default_v) != default_v:
                    return True
            return False
        profiles = self.config.get("appearance_profiles") or {}
        snap = profiles.get(prof_id) if isinstance(profiles, dict) else None
        if not isinstance(snap, dict):
            return True
        for k, default_v in self._DEFAULT_APPEARANCE.items():
            if self.config.get(k, default_v) != snap.get(k, default_v):
                return True
        return False

    def _refresh_controls(self) -> None:
        if hasattr(self, "profile_cb"):
            cur_prof = self.config.get("active_appearance_profile", "__default__") or "__default__"
            p_idx = self.profile_cb.findData(cur_prof)
            if p_idx < 0:
                p_idx = 0
            if self.profile_cb.currentIndex() != p_idx:
                self.profile_cb.blockSignals(True)
                self.profile_cb.setCurrentIndex(p_idx)
                self.profile_cb.blockSignals(False)
            active_prof = self.profile_cb.currentData() or "__default__"
            is_def = active_prof == "__default__"
            self.save_profile_btn.setEnabled((not is_def) and self._has_unsaved_profile_changes(active_prof))
            self.del_profile_btn.setEnabled(not is_def)

        cur_theme = self.config.get("theme", "dark")
        t_idx = self.theme_cb.findData(cur_theme)
        if t_idx >= 0 and self.theme_cb.currentIndex() != t_idx:
            self.theme_cb.blockSignals(True)
            self.theme_cb.setCurrentIndex(t_idx)
            self.theme_cb.blockSignals(False)

        acc = self.config.get("accent_color", "#f58a1f") or "#f58a1f"
        if not QColor(acc).isValid():
            acc = "#f58a1f"
        if hasattr(self, "bg_frame"):
            dis_bg = "#e5e8ef" if cur_theme == "light" else "#12151c"
            dis_fg = "#9ca3af" if cur_theme == "light" else "#4b5263"
            dis_bd = "#d1d5db" if cur_theme == "light" else "#1e222e"
            self.setStyleSheet(
                f"QPushButton:disabled {{ background-color: {dis_bg}; color: {dis_fg}; border-color: {dis_bd}; }} "
                f"QLabel:disabled, QCheckBox:disabled {{ color: {dis_fg}; }} "
                f"QCheckBox::indicator:disabled {{ background-color: {dis_bg}; border-color: {dis_bd}; }}"
            )
            self.bg_frame.setStyleSheet(
                f"QGroupBox#BgGroupBox {{"
                f" border: 1px solid {acc};"
                f" border-radius: 6px;"
                f" margin-top: 10px;"
                f" padding-top: 8px;"
                f" background-color: transparent;"
                f"}}"
                f"QGroupBox#BgGroupBox::title {{"
                f" subcontrol-origin: margin;"
                f" subcontrol-position: top left;"
                f" left: 10px;"
                f" padding: 0 6px;"
                f" color: {acc};"
                f" font-weight: bold;"
                f"}}"
            )

        unified = bool(self.config.get("bg_unified_enabled", False))
        dyn_blur = bool(self.config.get("bg_dynamic_blur", True))
        dyn_l = dyn_blur and bool(self.config.get("bg_dynamic_left", False))
        dyn_c = dyn_blur and bool(self.config.get("bg_dynamic_center", True))
        dyn_r = dyn_blur and bool(self.config.get("bg_dynamic_right", True))
        any_dyn = dyn_l or dyn_c or dyn_r
        dyn_sec_map = {"bg_left": dyn_l, "bg_center": dyn_c, "bg_right": dyn_r}

        if hasattr(self, "unified_cb"):
            self.unified_cb.blockSignals(True)
            self.unified_cb.setChecked(unified)
            self._set_trash_enabled(self.unified_cb, not any_dyn)
            self.unified_cb.blockSignals(False)
        for w in self._unified_controls:
            self._set_trash_enabled(w, (not any_dyn) and unified)
        for ik, row_ws in self._per_section_rows.items():
            row_on = (not dyn_sec_map[ik]) and (any_dyn or (not unified))
            for w in row_ws:
                self._set_trash_enabled(w, row_on)

        if hasattr(self, "dyn_blur_cb"):
            self.dyn_blur_cb.blockSignals(True)
            self.dyn_blur_cb.setChecked(dyn_blur)
            self.dyn_blur_cb.blockSignals(False)
            for cb, key in (
                (self.dyn_left_cb, "bg_dynamic_left"),
                (self.dyn_center_cb, "bg_dynamic_center"),
                (self.dyn_right_cb, "bg_dynamic_right"),
            ):
                cb.blockSignals(True)
                cb.setChecked(bool(self.config.get(key, key != "bg_dynamic_left")))
                cb.blockSignals(False)
                self._set_trash_enabled(cb, dyn_blur)
            rad = int(self.config.get("bg_dynamic_blur_radius", 36) or 36)
            inten = int(self.config.get("bg_dynamic_blur_intensity", 50) or 50)
            self._dyn_blur_slider.blockSignals(True)
            self._dyn_blur_slider.setValue(rad)
            self._dyn_blur_slider.blockSignals(False)
            self._dyn_blur_val.setText(f"{rad} px")
            self._dyn_int_slider.blockSignals(True)
            self._dyn_int_slider.setValue(inten)
            self._dyn_int_slider.blockSignals(False)
            self._dyn_int_val.setText(f"{inten}%")
            for w in self._dyn_blur_controls:
                self._set_trash_enabled(w, any_dyn)

        col_keys = {
            "bg_unified": "bg_color_unified",
            "bg_left": "bg_color_left",
            "bg_center": "bg_color_center",
            "bg_right": "bg_color_right",
        }
        text_fg = "#1a1d24" if cur_theme == "light" else "#eef1f7"
        sw_border = "#6b7280" if cur_theme == "light" else "#8892a4"
        for key, (sw, lbl) in self._bg_labels.items():
            val = self.config.get(key, "") or ""
            col = self.config.get(col_keys[key], "") or ""
            if val:
                sw.setVisible(False)
                lbl.setText(os.path.basename(val))
                lbl.setStyleSheet("" if not lbl.isEnabled() else f"color: {text_fg};")
            elif col:
                sw.setVisible(True)
                sw.setStyleSheet(
                    f"background-color: {col}; border: 1px solid {sw_border}; border-radius: 3px;"
                )
                lbl.setText(col.upper())
                lbl.setStyleSheet("" if not lbl.isEnabled() else f"color: {text_fg}; font-weight: bold;")
            else:
                sw.setVisible(False)
                lbl.setText(t("app_bg_none"))
                lbl.setStyleSheet("")
            if key in self._bg_clear_btns:
                if key == "bg_unified":
                    row_enabled = (not any_dyn) and unified
                else:
                    row_enabled = (not dyn_sec_map[key]) and (any_dyn or (not unified))
                self._set_trash_enabled(self._bg_clear_btns[key], row_enabled and bool(val or col))

        div_transp = bool(self.config.get("divider_transparent", False))
        if hasattr(self, "divider_transp_cb"):
            self.divider_transp_cb.blockSignals(True)
            self.divider_transp_cb.setChecked(div_transp)
            self.divider_transp_cb.blockSignals(False)
        for w in getattr(self, "_divider_row_widgets", []):
            self._set_trash_enabled(w, not div_transp)

        for key, (sw, lbl) in self._line_labels.items():
            col = self.config.get(key, "") or ""
            if col and QColor(col).isValid():
                sw.setVisible(True)
                sw.setStyleSheet(
                    f"background-color: {col}; border: 1px solid {sw_border}; border-radius: 3px;"
                )
                lbl.setText(col.upper())
                lbl.setStyleSheet(f"color: {text_fg}; font-weight: bold;")
            else:
                sw.setVisible(False)
                lbl.setText(t("app_default_lbl"))
                lbl.setStyleSheet("")
            if key in self._line_clear_btns:
                is_custom = (col.lower() != "#f58a1f") if key == "accent_color" else bool(col and QColor(col).isValid())
                btn_on = (not div_transp and is_custom) if key == "divider_color" else is_custom
                self._set_trash_enabled(self._line_clear_btns[key], btn_on)

    def _emit_apply(self) -> None:
        self.config.save()
        if callable(self._on_apply):
            self._on_apply()
        self._refresh_controls()

    def _on_unified_toggled(self, checked: bool) -> None:
        self.config.data["bg_unified_enabled"] = bool(checked)
        self._emit_apply()

    def _on_dyn_blur_toggled(self, checked: bool) -> None:
        self.config.data["bg_dynamic_blur"] = bool(checked)
        self._emit_apply()

    def _on_dyn_section_toggled(self, key: str, checked: bool) -> None:
        self.config.data[key] = bool(checked)
        self._emit_apply()

    def _on_divider_transp_toggled(self, checked: bool) -> None:
        self.config.data["divider_transparent"] = bool(checked)
        self._emit_apply()

    def _run_blur_live(self) -> None:
        if callable(self._on_blur_live):
            self._on_blur_live(True)
        elif callable(self._on_apply):
            self._on_apply()

    def _on_blur_slider_released(self) -> None:
        self._blur_live_timer.stop()
        self._blur_save_timer.stop()
        self._emit_apply()

    def _on_blur_radius_changed(self, value: int) -> None:
        self.config.data["bg_dynamic_blur_radius"] = int(value)
        self._dyn_blur_val.setText(f"{int(value)} px")
        self._blur_live_timer.start()
        if not self._dyn_blur_slider.isSliderDown():
            self._blur_save_timer.start()

    def _on_blur_intensity_changed(self, value: int) -> None:
        self.config.data["bg_dynamic_blur_intensity"] = int(value)
        self._dyn_int_val.setText(f"{int(value)}%")
        if callable(self._on_blur_live):
            self._on_blur_live(False)
        if not self._dyn_int_slider.isSliderDown():
            self._blur_save_timer.start()

    def _on_theme_changed(self) -> None:
        self.config.data["theme"] = self.theme_cb.currentData() or "dark"
        self._emit_apply()

    def _set_accent(self, hex_color: str) -> None:
        self.config.data["accent_color"] = hex_color
        self._emit_apply()

    def _choose_custom_color(self) -> None:
        cur = QColor(self.config.get("accent_color", "#f58a1f"))
        chosen = QColorDialog.getColor(cur, self, t("app_accent_lbl"))
        if chosen.isValid():
            self._set_accent(chosen.name())

    def _delete_bg_file(self, img_key: str) -> None:
        """Elimina da media/ui_bg la copia locale dello sfondo (esclusi i file nei profili salvati)."""
        rel = (self.config.get(img_key, "") or "").replace("\\", "/").lstrip("/")
        if not rel.startswith("media/ui_bg/") or rel.startswith("media/ui_bg/profiles/"):
            return
        ui_bg_dir = os.path.abspath(os.path.join(self.db.root, "media", "ui_bg"))
        full_path = os.path.abspath(self.db.resolve(rel))
        if full_path.startswith(ui_bg_dir + os.sep) and os.path.isfile(full_path):
            try:
                os.remove(full_path)
            except OSError:
                pass

    def _pick_bg_color(self, img_key: str, col_key: str) -> None:
        cur = QColor(self.config.get(col_key, "") or ("#f3f5f8" if self.config.get("theme") == "light" else "#181b22"))
        chosen = QColorDialog.getColor(cur, self, t("app_bg_section"))
        if chosen.isValid():
            self._delete_bg_file(img_key)
            self.config.data[img_key] = ""
            self.config.data[col_key] = chosen.name()
            self._emit_apply()

    def _browse_bg(self, img_key: str, col_key: str) -> None:
        img_filter = "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
        fp, _ = QFileDialog.getOpenFileName(self, t("app_bg_section"), self.db.root, img_filter)
        if not fp or not os.path.isfile(fp):
            return
        bg_dir = os.path.join(self.db.root, "media", "ui_bg")
        os.makedirs(bg_dir, exist_ok=True)
        ext = os.path.splitext(fp)[1].lower() or ".png"
        dest_name = f"{img_key}{ext}"
        dest_path = os.path.join(bg_dir, dest_name)
        if os.path.abspath(fp) != os.path.abspath(dest_path):
            self._delete_bg_file(img_key)
            shutil.copy2(fp, dest_path)
        self.config.data[img_key] = f"media/ui_bg/{dest_name}"
        self.config.data[col_key] = ""
        self._emit_apply()

    def _clear_bg(self, img_key: str, col_key: str) -> None:
        self._delete_bg_file(img_key)
        self.config.data[img_key] = ""
        self.config.data[col_key] = ""
        self._emit_apply()

    def _pick_line_color(self, key: str) -> None:
        cur = QColor(self.config.get(key, "") or ("#b4becf" if self.config.get("theme") == "light" else "#363c4b"))
        chosen = QColorDialog.getColor(cur, self, t("app_title"))
        if chosen.isValid():
            self.config.data[key] = chosen.name()
            self._emit_apply()

    def _clear_line_color(self, key: str) -> None:
        self.config.data[key] = ""
        self._emit_apply()

    def _populate_profiles(self) -> None:
        self.profile_cb.blockSignals(True)
        self.profile_cb.clear()
        self.profile_cb.addItem(t("app_profile_default"), "__default__")
        profiles = self.config.get("appearance_profiles") or {}
        if isinstance(profiles, dict):
            for pname in sorted(profiles.keys(), key=str.casefold):
                self.profile_cb.addItem(pname, pname)
        cur_prof = self.config.get("active_appearance_profile", "__default__") or "__default__"
        idx = self.profile_cb.findData(cur_prof)
        self.profile_cb.setCurrentIndex(idx if idx >= 0 else 0)
        self.profile_cb.blockSignals(False)

    def _on_profile_changed(self) -> None:
        prof_id = self.profile_cb.currentData() or "__default__"
        self.config.data["active_appearance_profile"] = prof_id
        for ik in ("bg_unified", "bg_left", "bg_center", "bg_right"):
            self._delete_bg_file(ik)
        if prof_id == "__default__":
            for k, v in self._DEFAULT_APPEARANCE.items():
                self.config.data[k] = v
        else:
            profiles = self.config.get("appearance_profiles") or {}
            snap = profiles.get(prof_id) if isinstance(profiles, dict) else None
            if isinstance(snap, dict):
                for k, default_v in self._DEFAULT_APPEARANCE.items():
                    self.config.data[k] = snap.get(k, default_v)
        self._emit_apply()

    def _profile_slug(self, name: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_") or "profile"
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
        return f"{clean}_{digest}"

    def _save_profile_snapshot(self, name: str) -> None:
        slug = self._profile_slug(name)
        prof_dir = os.path.join(self.db.root, "media", "ui_bg", "profiles", slug)
        snap: dict = {}
        for k, default_v in self._DEFAULT_APPEARANCE.items():
            snap[k] = self.config.get(k, default_v)
        for ik in ("bg_unified", "bg_left", "bg_center", "bg_right"):
            rel = snap.get(ik, "") or ""
            src = self.db.resolve(rel) if rel else ""
            if src and os.path.isfile(src):
                os.makedirs(prof_dir, exist_ok=True)
                ext = os.path.splitext(src)[1].lower() or ".png"
                dest_name = f"{ik}{ext}"
                dest_path = os.path.join(prof_dir, dest_name)
                for old_f in os.listdir(prof_dir):
                    if old_f.startswith(f"{ik}.") and old_f != dest_name:
                        try:
                            os.remove(os.path.join(prof_dir, old_f))
                        except OSError:
                            pass
                if os.path.abspath(src) != os.path.abspath(dest_path):
                    shutil.copy2(src, dest_path)
                snap[ik] = f"media/ui_bg/profiles/{slug}/{dest_name}"
                self.config.data[ik] = snap[ik]
            else:
                snap[ik] = ""
                if os.path.isdir(prof_dir):
                    for old_f in os.listdir(prof_dir):
                        if old_f.startswith(f"{ik}."):
                            try:
                                os.remove(os.path.join(prof_dir, old_f))
                            except OSError:
                                pass
        profiles = dict(self.config.get("appearance_profiles") or {})
        profiles[name] = snap
        self.config.data["appearance_profiles"] = profiles
        self.config.data["active_appearance_profile"] = name
        self.config.save()
        self._populate_profiles()
        self._refresh_controls()

    def _save_current_profile(self) -> None:
        prof_id = self.profile_cb.currentData() or "__default__"
        if prof_id == "__default__":
            return
        self._save_profile_snapshot(prof_id)
        QMessageBox.information(self, t("app_title"), t("app_profile_saved", name=prof_id))

    def _save_profile_as(self) -> None:
        cur = self.profile_cb.currentData() or "__default__"
        initial = "" if cur == "__default__" else cur
        name, ok = QInputDialog.getText(self, t("app_profile_save_as"), t("app_profile_name_prompt"), text=initial)
        if not ok:
            return
        name = name.strip()
        if not name or name == "__default__" or name.casefold() == t("app_profile_default").casefold():
            return
        self._save_profile_snapshot(name)
        QMessageBox.information(self, t("app_title"), t("app_profile_saved", name=name))

    def _delete_current_profile(self) -> None:
        prof_id = self.profile_cb.currentData() or "__default__"
        if prof_id == "__default__":
            return
        ans = QMessageBox.question(
            self,
            t("app_profile_delete"),
            t("app_profile_delete_confirm", name=prof_id),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        profiles = dict(self.config.get("appearance_profiles") or {})
        profiles.pop(prof_id, None)
        self.config.data["appearance_profiles"] = profiles
        prof_dir = os.path.join(self.db.root, "media", "ui_bg", "profiles", self._profile_slug(prof_id))
        if os.path.isdir(prof_dir):
            shutil.rmtree(prof_dir, ignore_errors=True)
        self.config.data["active_appearance_profile"] = "__default__"
        for k, v in self._DEFAULT_APPEARANCE.items():
            self.config.data[k] = v
        self._populate_profiles()
        self._emit_apply()

    def _revert_to_profile(self, prof_id: str) -> None:
        for ik in ("bg_unified", "bg_left", "bg_center", "bg_right"):
            self._delete_bg_file(ik)
        if not prof_id or prof_id == "__default__":
            self.config.data["active_appearance_profile"] = "__default__"
            for k, v in self._DEFAULT_APPEARANCE.items():
                self.config.data[k] = v
        else:
            profiles = self.config.get("appearance_profiles") or {}
            snap = profiles.get(prof_id) if isinstance(profiles, dict) else None
            self.config.data["active_appearance_profile"] = prof_id
            if isinstance(snap, dict):
                for k, default_v in self._DEFAULT_APPEARANCE.items():
                    self.config.data[k] = snap.get(k, default_v)
        self.config.save()
        if callable(self._on_apply):
            self._on_apply()

    def _confirm_before_close(self) -> bool:
        if self._closing_confirmed:
            return True
        prof_id = self.profile_cb.currentData() or "__default__"
        if not self._has_unsaved_profile_changes(prof_id):
            self._closing_confirmed = True
            return True
        dlg = QDialog(self)
        dlg.setWindowTitle(t("app_title"))
        vlay = QVBoxLayout(dlg)
        vlay.setSpacing(14)
        msg_lbl = QLabel(t("app_unsaved_close_msg"))
        vlay.addWidget(msg_lbl)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        save_btn = QPushButton(t("app_profile_save"))
        save_as_btn = QPushButton(t("app_profile_save_as"))
        dont_save_btn = QPushButton(t("app_profile_dont_save"))
        save_btn.setEnabled(prof_id != "__default__")
        if prof_id == "__default__":
            save_as_btn.setObjectName("AccentButton")
        else:
            save_btn.setObjectName("AccentButton")
        choice = {"val": None}
        save_btn.clicked.connect(lambda _c=False: (choice.__setitem__("val", "save"), dlg.accept()))
        save_as_btn.clicked.connect(lambda _c=False: (choice.__setitem__("val", "save_as"), dlg.accept()))
        dont_save_btn.clicked.connect(lambda _c=False: (choice.__setitem__("val", "dont_save"), dlg.accept()))
        btn_row.addWidget(save_btn)
        btn_row.addWidget(save_as_btn)
        btn_row.addWidget(dont_save_btn)
        vlay.addLayout(btn_row)
        dlg.exec()

        res = choice["val"]
        if res == "save" and prof_id != "__default__":
            self._save_profile_snapshot(prof_id)
            self._closing_confirmed = True
            return True
        if res == "save_as":
            initial = "" if prof_id == "__default__" else prof_id
            name, ok = QInputDialog.getText(self, t("app_profile_save_as"), t("app_profile_name_prompt"), text=initial)
            if not ok:
                return False
            name = name.strip()
            if not name or name == "__default__" or name.casefold() == t("app_profile_default").casefold():
                return False
            self._save_profile_snapshot(name)
            self._closing_confirmed = True
            return True
        if res == "dont_save":
            self._revert_to_profile(prof_id)
            self._closing_confirmed = True
            return True
        return False

    def accept(self) -> None:
        if not self._confirm_before_close():
            return
        super().accept()

    def reject(self) -> None:
        if not self._confirm_before_close():
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if not self._confirm_before_close():
            event.ignore()
            return
        super().closeEvent(event)

    def _reset_defaults(self) -> None:
        for ik in ("bg_unified", "bg_left", "bg_center", "bg_right"):
            self._delete_bg_file(ik)
        self.config.data["active_appearance_profile"] = "__default__"
        for k, v in self._DEFAULT_APPEARANCE.items():
            self.config.data[k] = v
        self._populate_profiles()
        self._emit_apply()
        QMessageBox.information(self, t("app_title"), t("app_reset_status"))


class LayoutLockDialog(QDialog):
    """Finestra non-modale per bloccare/sbloccare il ridimensionamento dei pannelli e ripristinare le dimensioni di default."""

    def __init__(self, parent: QWidget, config: Config, on_apply=None, on_reset=None):
        super().__init__(parent)
        self.config = config
        self._on_apply = on_apply
        self._on_reset = on_reset

        self.setWindowTitle(t("lock_dlg_title"))
        self.setModal(False)
        self.resize(450, 215)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        intro_lbl = QLabel(t("lock_intro_msg"))
        intro_lbl.setWordWrap(True)
        lay.addWidget(intro_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.lock_btn = QPushButton(t("lock_btn_lock"))
        self.lock_btn.setMinimumHeight(38)
        self.lock_btn.clicked.connect(lambda _c=False: self._set_locked(True))
        self.unlock_btn = QPushButton(t("lock_btn_unlock"))
        self.unlock_btn.setMinimumHeight(38)
        self.unlock_btn.clicked.connect(lambda _c=False: self._set_locked(False))
        btn_row.addWidget(self.lock_btn, 1)
        btn_row.addWidget(self.unlock_btn, 1)
        lay.addLayout(btn_row)

        self.hint_lbl = QLabel("")
        self.hint_lbl.setObjectName("MutedHint")
        self.hint_lbl.setWordWrap(True)
        lay.addWidget(self.hint_lbl)

        lay.addStretch(1)

        bottom = QHBoxLayout()
        self.reset_btn = QPushButton(t("lock_reset_btn"))
        self.reset_btn.clicked.connect(self._do_reset)
        bottom.addWidget(self.reset_btn)
        bottom.addStretch(1)
        close_btn = QPushButton(t("dlg_btn_close"))
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        lay.addLayout(bottom)

        self._sync_btn_ui()

    def _sync_btn_ui(self) -> None:
        locked = bool(self.config.get("lock_layout", True))
        self.lock_btn.setObjectName("AccentButton" if locked else "")
        self.unlock_btn.setObjectName("" if locked else "AccentButton")
        for b in (self.lock_btn, self.unlock_btn):
            b.style().unpolish(b)
            b.style().polish(b)
            b.update()
        self.hint_lbl.setText(t("lock_hint_on") if locked else t("lock_hint_off"))

    def _set_locked(self, locked: bool) -> None:
        self.config.data["lock_layout"] = bool(locked)
        self.config.save()
        self._sync_btn_ui()
        if callable(self._on_apply):
            self._on_apply()

    def _do_reset(self) -> None:
        if callable(self._on_reset):
            self._on_reset()
        self._set_locked(True)


class ChooseFavoriteFolderDialog(QDialog):
    """Finestra con spunte per inserire un gioco in una o più cartelle preferiti o rimuoverlo da tutte."""

    def __init__(self, parent: QWidget, db: Database, game_id: int, game_title: str):
        super().__init__(parent)
        self.db = db
        self.game_id = game_id
        self.selected_folder_ids: list[int] = []
        self._checked_ids: set[int] = db.get_game_favorite_folder_ids(game_id)

        self.setWindowTitle(t("fav_choose_folder_title"))
        self.resize(430, 320)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        lbl = QLabel(t("fav_choose_folder_msg", title=game_title))
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self.list_w = QListWidget()
        self.list_w.setSelectionMode(QListWidget.NoSelection)
        self.list_w.setFocusPolicy(Qt.NoFocus)
        cfg = Config(self.db.root)
        acc = cfg.get("accent_color", "#f58a1f") or "#f58a1f"
        is_light = cfg.get("theme", "dark") == "light"
        ind_bg = "#ffffff" if is_light else "#1a1e28"
        ind_bd = "#94a3b8" if is_light else "#7a8499"
        row_fg = "#0f172a" if is_light else "#eef1f7"
        row_hov = "rgba(15, 23, 42, 14)" if is_light else "rgba(255, 255, 255, 14)"
        self.list_w.setStyleSheet(
            f"QListWidget::item {{ padding: 6px 8px; color: {row_fg}; border-radius: 6px; }} "
            f"QListWidget::item:hover {{ background-color: {row_hov}; }} "
            f"QListWidget::item:selected {{ background-color: transparent; color: {row_fg}; }} "
            "QListWidget::indicator { width: 18px; height: 18px; border-radius: 4px; "
            f"border: 2px solid {ind_bd}; background-color: {ind_bg}; }} "
            f"QListWidget::indicator:hover {{ border-color: {acc}; }} "
            f"QListWidget::indicator:checked {{ background-color: {acc}; border-color: {acc}; }}"
        )
        self.list_w.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.list_w, 1)

        bottom = QHBoxLayout()
        new_btn = QPushButton(t("fav_new_folder"))
        new_btn.clicked.connect(self._create_new_folder)
        bottom.addWidget(new_btn)

        if self._checked_ids:
            rem_btn = QPushButton(t("fav_remove_all"))
            rem_btn.clicked.connect(self._remove_all)
            bottom.addWidget(rem_btn)

        bottom.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        bottom.addWidget(btns)
        lay.addLayout(bottom)

        self._reload()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        fid = item.data(Qt.UserRole)
        if fid is None:
            return
        if item.checkState() == Qt.Checked:
            self._checked_ids.add(int(fid))
        else:
            self._checked_ids.discard(int(fid))

    def _reload(self) -> None:
        self.list_w.blockSignals(True)
        self.list_w.clear()
        for f in self.db.list_favorite_folders():
            fid = int(f["id"])
            it = QListWidgetItem(f["name"])
            raw_ic = f["icon_path"] if "icon_path" in f.keys() else ""
            ic_abs = self.db.resolve(raw_ic) if raw_ic else ""
            if ic_abs and os.path.isfile(ic_abs):
                it.setIcon(QIcon(ic_abs))
            else:
                it.setIcon(self.style().standardIcon(QStyle.SP_DirIcon))
            it.setData(Qt.UserRole, fid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if fid in self._checked_ids else Qt.Unchecked)
            self.list_w.addItem(it)
        self.list_w.blockSignals(False)

    def _create_new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, t("fav_new_folder"), t("fav_folder_prompt"))
        if not ok or not name.strip():
            return
        fid = self.db.create_favorite_folder(name.strip())
        self._checked_ids.add(fid)
        self._reload()

    def _remove_all(self) -> None:
        self._checked_ids.clear()
        self.selected_folder_ids = []
        super().accept()

    def accept(self) -> None:
        self.selected_folder_ids = sorted(self._checked_ids)
        super().accept()
