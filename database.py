"""
database.py - Gestione dati di EasyLauncher (100% portable).

Contiene:
  * risoluzione della radice portabile (cartella dell'exe o dello script);
  * creazione della gerarchia di cartelle alla prima esecuzione;
  * helper per percorsi relativi/assoluti e nomi di file sicuri;
  * pulizia dei titoli ROM (rimozione di tag come "(USA)", "[!]");
  * Config  -> data/config.json (impostazioni, finestra, chiavi API);
  * Database -> data/library.db (SQLite: platforms, emulators, games).

Nessun file viene mai scritto in AppData, nel Registro o in %TEMP%.
Il modulo non importa nulla di Qt, quindi e' testabile da solo.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Radice portabile e struttura cartelle
# --------------------------------------------------------------------------

#: Sottocartelle di media create per ogni piattaforma.
MEDIA_KINDS = ("boxart", "screenshots", "logos", "videos")

#: Estensioni immagine accettate quando si cercano file di media.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def app_root() -> str:
    """Cartella radice dell'app: quella dell'exe (PyInstaller) o dello script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_tree(root: str) -> dict[str, str]:
    """Crea (se mancano) le cartelle data/, emulators/, roms/, media/, icons/.

    E' idempotente: si puo' chiamare a ogni avvio. Restituisce i percorsi.
    """
    paths = {name: os.path.join(root, name) for name in ("data", "emulators", "roms", "media", "icons")}
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


# --------------------------------------------------------------------------
# Percorsi portabili
# --------------------------------------------------------------------------

def to_stored_path(path: str, root: str) -> str:
    """Converte un percorso nel formato da salvare.

    - Se il percorso e' DENTRO la radice del programma viene salvato relativo
      (con '/').
    - Se il percorso e' sullo stesso disco in una cartella contenitore comune
      (es. Emulatori/EasyLauncher e Emulatori/rom -> ../rom/...) o su disco
      rimovibile (non C:), viene salvato relativo con '../' fino a 3 livelli,
      escludendo le cartelle di sistema di Windows su C:.
    - Altrimenti resta assoluto.
    """
    if not path:
        return ""
    absolute = os.path.normpath(os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path)))
    root_abs = os.path.normpath(os.path.abspath(root))
    try:
        common = os.path.commonpath([absolute, root_abs])
    except ValueError:  # dischi diversi su Windows
        return absolute
    rel = os.path.relpath(absolute, root_abs).replace("\\", "/")
    if common == root_abs:
        return rel
    up_levels = 0
    for part in rel.split("/"):
        if part == "..":
            up_levels += 1
        else:
            break
    drive = os.path.splitdrive(root_abs)[0]
    drive_lower = drive.lower()
    is_drive_root = os.path.dirname(common) == common
    rel_from_drive = os.path.relpath(absolute, drive + os.sep).lower() if drive else ""
    is_system_dir = drive_lower == "c:" and any(
        rel_from_drive == s or rel_from_drive.startswith(s + os.sep)
        for s in ("windows", "program files", "program files (x86)", "programdata")
    )
    if not is_system_dir and up_levels <= 3 and (not is_drive_root or drive_lower != "c:"):
        return rel
    return absolute


def resolve_path(stored: str, root: str) -> str:
    """Trasforma un percorso salvato (relativo o assoluto) in assoluto utilizzabile."""
    if not stored:
        return ""
    if os.path.isabs(stored):
        return os.path.normpath(stored)
    return os.path.normpath(os.path.join(root, stored))


_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """Nome valido come file/cartella su Windows (sostituisce i caratteri vietati)."""
    cleaned = _INVALID_FS_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or "_"


# --------------------------------------------------------------------------
# Pulizia dei titoli
# --------------------------------------------------------------------------

_TAGS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")        # (USA) [!] (Rev 1) [b1] ...
_ARTICLE = re.compile(r"^(?P<main>.+?),\s*(?P<art>The|A|An|Il|Lo|La|Le|Gli|I|L')$", re.IGNORECASE)
_SPACES = re.compile(r"\s{2,}")


def clean_title(file_name: str) -> str:
    """Ricava un titolo leggibile dal nome del file ROM.

    "Super Mario Bros. (USA) [!].zip" -> "Super Mario Bros."
    "Legend of Zelda, The (Europe).nes" -> "The Legend of Zelda"
    """
    base = os.path.basename(file_name)
    root_n, ext_n = os.path.splitext(base)
    stem = root_n if re.fullmatch(r"\.[A-Za-z0-9]{1,7}", ext_n) else base
    title = _TAGS.sub("", stem).replace("_", " ")
    title = _SPACES.sub(" ", title).strip(" -")
    m = _ARTICLE.match(title)
    if m:
        art = m.group("art")
        title = f"{art if art.endswith(chr(39)) else art + ' '}{m.group('main')}".replace("' ", "'")
    return title or stem


def search_title(title: str) -> str:
    """Versione normalizzata (minuscola, solo alfanumerici) usata dagli scraper."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


# --------------------------------------------------------------------------
# Configurazione (data/config.json)
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "theme": "dark",
    "accent_color": "#f58a1f",
    "divider_color": "#1a4b25",
    "divider_transparent": True,
    "menubar_color": "",
    "card_color": "",
    "bg_left": "",
    "bg_center": "",
    "bg_right": "",
    "bg_color_left": "",
    "bg_color_center": "",
    "bg_color_right": "",
    "bg_unified_enabled": False,
    "bg_unified": "",
    "bg_color_unified": "",
    "bg_dynamic_blur": True,
    "bg_dynamic_left": True,
    "bg_dynamic_center": True,
    "bg_dynamic_right": True,
    "bg_dynamic_blur_radius": 38,
    "bg_dynamic_blur_intensity": 43,
    "active_appearance_profile": "__default__",
    "appearance_profiles": {},
    "cover_radius": 6,
    "logo_radius": 6,
    "screenshot_radius": 6,
    "grid_zoom": 255,
    "list_zoom": 191,
    "lock_layout": True,
    "window": {
        "width": 1920,
        "height": 1032,
        "maximized": True,
        "splitter": [302, 1120, 498],
        "right_splitter": [520, 462],
        "preview_splitter": [195, 325],
    },
    "last_platform_id": None,
    "api_keys": {"thegamesdb": "", "screenscraper_user": "", "screenscraper_password": "", "igdb_client_id": "", "igdb_client_secret": "", "steamgriddb": ""},
    "scraper_order": ["thegamesdb", "screenscraper", "igdb", "libretro"],
    "ui_language": "en",
    "metadata_language": "en",
    "metadata_scope": "per_platform",
    "metadata_fields_by_platform": {},
}


def _merge_defaults(base: dict, data: dict) -> dict:
    """Unisce ricorsivamente i valori letti con quelli di default."""
    out = dict(base)
    for k, v in data.items():
        out[k] = _merge_defaults(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


class Config:
    """Impostazioni generali salvate in data/config.json (scrittura atomica)."""

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, "data", "config.json")
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.save()
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = _merge_defaults(DEFAULT_CONFIG, json.load(f))
        except (OSError, ValueError):
            # File corrotto: lo conservo come .bak e riparto dai default.
            os.replace(self.path, self.path + ".bak")
            self.save()

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()


# --------------------------------------------------------------------------
# Database SQLite
# --------------------------------------------------------------------------

SCHEMA_VERSION = 9

_SCHEMA_V1 = """
CREATE TABLE emulators (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    exe_path  TEXT NOT NULL,
    args      TEXT NOT NULL DEFAULT '',
    work_dir  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE platforms (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    icon_path      TEXT NOT NULL DEFAULT '',
    logo_path      TEXT NOT NULL DEFAULT '',
    rom_dir        TEXT NOT NULL DEFAULT '',
    extensions     TEXT NOT NULL DEFAULT '',
    media_dir      TEXT NOT NULL DEFAULT '',
    sort_order     INTEGER NOT NULL DEFAULT 0,
    parent_id      INTEGER REFERENCES platforms(id) ON DELETE CASCADE,
    scan_subdirs   INTEGER NOT NULL DEFAULT 1,
    keep_structure INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE platform_emulators (
    platform_id   INTEGER NOT NULL REFERENCES platforms(id) ON DELETE CASCADE,
    emulator_id   INTEGER NOT NULL REFERENCES emulators(id) ON DELETE CASCADE,
    is_default    INTEGER NOT NULL DEFAULT 0,
    args_override TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (platform_id, emulator_id)
);

CREATE TABLE games (
    id              INTEGER PRIMARY KEY,
    platform_id     INTEGER NOT NULL REFERENCES platforms(id) ON DELETE CASCADE,
    rom_path        TEXT NOT NULL,
    rom_file        TEXT NOT NULL,
    title           TEXT NOT NULL,
    search_title    TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    genre           TEXT NOT NULL DEFAULT '',
    release_year    INTEGER,
    developer       TEXT NOT NULL DEFAULT '',
    publisher       TEXT NOT NULL DEFAULT '',
    players         TEXT NOT NULL DEFAULT '',
    rating          REAL,
    boxart_path     TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT NOT NULL DEFAULT '',
    extra_screenshots TEXT NOT NULL DEFAULT '[]',
    logo_path       TEXT NOT NULL DEFAULT '',
    logo_offset_x   REAL NOT NULL DEFAULT 0.5,
    logo_offset_y   REAL NOT NULL DEFAULT 0.5,
    video_path      TEXT NOT NULL DEFAULT '',
    favorite        INTEGER NOT NULL DEFAULT 0,
    play_count      INTEGER NOT NULL DEFAULT 0,
    play_time       INTEGER NOT NULL DEFAULT 0,
    last_played     TEXT,
    date_added      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scraped         INTEGER NOT NULL DEFAULT 0,   -- 0 mai, 1 scaricato, 2 modificato a mano
    emulator_id     INTEGER REFERENCES emulators(id) ON DELETE SET NULL,
    subfolder       TEXT NOT NULL DEFAULT '',
    UNIQUE (platform_id, rom_path)
);

CREATE INDEX idx_games_platform ON games(platform_id, title COLLATE NOCASE);
CREATE INDEX idx_games_fav ON games(favorite) WHERE favorite = 1;

CREATE TABLE IF NOT EXISTS favorite_folders (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    icon_path   TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS favorite_items (
    folder_id   INTEGER NOT NULL REFERENCES favorite_folders(id) ON DELETE CASCADE,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    PRIMARY KEY (folder_id, game_id)
);
"""

#: Colonne di `games` che si possono modificare con update_game().
GAME_FIELDS = (
    "title", "search_title", "description", "genre", "release_year", "developer", "publisher",
    "players", "rating", "boxart_path", "screenshot_path", "extra_screenshots", "logo_path",
    "logo_offset_x", "logo_offset_y", "video_path",
    "favorite", "scraped", "emulator_id", "rom_path", "rom_file", "subfolder", "platform_id",
)

_ORDERS = {
    "title": "g.title COLLATE NOCASE",
    "year": "g.release_year IS NULL, g.release_year, g.title COLLATE NOCASE",
    "developer": "g.developer COLLATE NOCASE, g.title COLLATE NOCASE",
    "played": "g.play_count DESC, g.title COLLATE NOCASE",
    "added": "g.date_added DESC",
}


class Database:
    """Accesso a data/library.db.

    SQLite non permette di condividere una connessione fra thread, quindi ogni
    thread (interfaccia, pool immagini, scraper) ottiene la propria connessione
    tramite threading.local().  Si usa journal_mode=DELETE: il database resta un
    singolo file, comodo su chiavetta USB.
    """

    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(root or app_root())
        ensure_tree(self.root)
        self.path = os.path.join(self.root, "data", "library.db")
        self._local = threading.local()
        self._migrate()

    # ----- connessione e migrazioni -----
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = DELETE")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Chiude la connessione del thread corrente."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _migrate(self) -> None:
        conn = self._conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            with conn:
                conn.executescript(_SCHEMA_V1)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version < 2:
            with conn:
                try:
                    conn.execute("ALTER TABLE platforms ADD COLUMN parent_id INTEGER REFERENCES platforms(id) ON DELETE CASCADE")
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("ALTER TABLE games ADD COLUMN subfolder TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
                conn.execute("PRAGMA user_version = 2")
        if version < 3:
            with conn:
                try:
                    conn.execute("ALTER TABLE platforms ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
                conn.execute("PRAGMA user_version = 3")
        if version < 4:
            with conn:
                for stmt in (
                    "ALTER TABLE platforms ADD COLUMN scan_subdirs INTEGER NOT NULL DEFAULT 1",
                    "ALTER TABLE platforms ADD COLUMN keep_structure INTEGER NOT NULL DEFAULT 0",
                ):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass
                conn.execute("PRAGMA user_version = 4")
        if version < 5:
            with conn:
                try:
                    conn.execute("ALTER TABLE games ADD COLUMN play_time INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
                conn.execute("PRAGMA user_version = 5")
        if version < 6:
            with conn:
                try:
                    conn.execute("ALTER TABLE games ADD COLUMN extra_screenshots TEXT NOT NULL DEFAULT '[]'")
                except sqlite3.OperationalError:
                    pass
                conn.execute("PRAGMA user_version = 6")
        if version < 7:
            with conn:
                for stmt in (
                    "ALTER TABLE games ADD COLUMN logo_offset_x REAL NOT NULL DEFAULT 0.5",
                    "ALTER TABLE games ADD COLUMN logo_offset_y REAL NOT NULL DEFAULT 0.5",
                ):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass
                conn.execute("PRAGMA user_version = 7")
        if version < 8:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS favorite_folders (
                        id          INTEGER PRIMARY KEY,
                        name        TEXT NOT NULL UNIQUE,
                        sort_order  INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS favorite_items (
                        folder_id   INTEGER NOT NULL REFERENCES favorite_folders(id) ON DELETE CASCADE,
                        game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                        PRIMARY KEY (folder_id, game_id)
                    );
                    """
                )
                conn.execute("PRAGMA user_version = 8")
        if version < 9:
            with conn:
                try:
                    conn.execute("ALTER TABLE favorite_folders ADD COLUMN icon_path TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
                conn.execute("PRAGMA user_version = 9")

    # ----- percorsi -----
    def store(self, path: str) -> str:
        return to_stored_path(path, self.root)

    def resolve(self, stored: str) -> str:
        return resolve_path(stored, self.root)

    def media_dir(self, platform: sqlite3.Row, kind: str) -> str:
        """Cartella media/<Console>/<kind>/ (creata se manca)."""
        if kind not in MEDIA_KINDS:
            raise ValueError(f"tipo media sconosciuto: {kind}")
        folder = os.path.join(self.root, "media", platform["media_dir"], kind)
        os.makedirs(folder, exist_ok=True)
        return folder

    def media_file(self, platform: sqlite3.Row, kind: str, rom_file: str, ext: str = ".png") -> str:
        """Percorso assoluto di un'immagine col nome della ROM (senza estensione ROM)."""
        stem = safe_name(os.path.splitext(rom_file)[0])
        return os.path.join(self.media_dir(platform, kind), stem + ext)

    def find_media(self, platform: sqlite3.Row, kind: str, rom_file: str) -> str:
        """Cerca un'immagine gia' presente con il nome della ROM; '' se assente."""
        stem = safe_name(os.path.splitext(rom_file)[0])
        folder = os.path.join(self.root, "media", platform["media_dir"], kind)
        for ext in IMAGE_EXTS + (".mp4", ".avi", ".mkv", ".webm"):
            p = os.path.join(folder, stem + ext)
            if os.path.isfile(p):
                return p
        return ""

    # ----- emulatori -----
    def add_emulator(self, name: str, exe_path: str, args: str = "", work_dir: str = "") -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO emulators(name, exe_path, args, work_dir) VALUES (?,?,?,?)",
                            (name.strip(), self.store(exe_path), args.strip(), self.store(work_dir)))
        return cur.lastrowid

    def update_emulator(self, emu_id: int, name: str, exe_path: str, args: str = "", work_dir: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE emulators SET name=?, exe_path=?, args=?, work_dir=? WHERE id=?",
                      (name.strip(), self.store(exe_path), args.strip(), self.store(work_dir), emu_id))

    def delete_emulator(self, emu_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM emulators WHERE id=?", (emu_id,))

    def list_emulators(self) -> list[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM emulators ORDER BY name COLLATE NOCASE").fetchall()

    def get_emulator(self, emu_id: int) -> Optional[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM emulators WHERE id=?", (emu_id,)).fetchone()

    # ----- piattaforme -----
    def add_platform(self, name: str, icon_path: str = "", rom_dir: str = "", extensions: str = "",
                     parent_id: Optional[int] = None, logo_path: str = "",
                     scan_subdirs: int = 1, keep_structure: int = 0) -> int:
        """Crea la piattaforma (o sotto-sezione se parent_id e' valorizzato) e le sue cartelle media."""
        if isinstance(parent_id, bool) or parent_id == 0:
            parent_id = None
        name = name.strip()
        media_dir = safe_name(name)
        s_sub = 1 if scan_subdirs else 0
        k_str = 1 if (s_sub and keep_structure) else 0
        with self._conn() as c:
            nxt = c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM platforms").fetchone()[0]
            cur = c.execute(
                "INSERT INTO platforms(name, icon_path, logo_path, rom_dir, extensions, media_dir, sort_order, parent_id, scan_subdirs, keep_structure) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, self.store(icon_path), self.store(logo_path), self.store(rom_dir),
                 self.normalize_extensions(extensions), media_dir, nxt, parent_id, s_sub, k_str))
        plat = self.get_platform(cur.lastrowid)
        for kind in MEDIA_KINDS:
            self.media_dir(plat, kind)
        return cur.lastrowid

    def update_platform(self, plat_id: int, name: str, icon_path: str, rom_dir: str, extensions: str,
                        parent_id: Optional[int] = None, logo_path: str = "",
                        scan_subdirs: int = 1, keep_structure: int = 0) -> None:
        """Aggiorna i dati. La cartella media NON viene rinominata (resta quella originale)."""
        s_sub = 1 if scan_subdirs else 0
        k_str = 1 if (s_sub and keep_structure) else 0
        with self._conn() as c:
            c.execute(
                "UPDATE platforms SET name=?, icon_path=?, logo_path=?, rom_dir=?, extensions=?, parent_id=?, scan_subdirs=?, keep_structure=? WHERE id=?",
                (name.strip(), self.store(icon_path), self.store(logo_path), self.store(rom_dir),
                 self.normalize_extensions(extensions), parent_id, s_sub, k_str, plat_id))

    def delete_platform(self, plat_id: int) -> None:
        """Elimina piattaforma, giochi, sotto-sezioni, media su disco (media/<Console>/) e compatta il DB.

        I file ROM originali non vengono mai toccati.
        """
        plats_to_clean: list[sqlite3.Row] = []
        root_p = self.get_platform(plat_id)
        if root_p is not None:
            plats_to_clean.append(root_p)
            plats_to_clean.extend(self.list_subplatforms(plat_id))

        media_root = os.path.normpath(os.path.abspath(os.path.join(self.root, "media")))
        for p in plats_to_clean:
            mdir = p["media_dir"]
            if mdir:
                target = os.path.normpath(os.path.abspath(os.path.join(media_root, mdir)))
                if target != media_root and os.path.commonpath([target, media_root]) == media_root and os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)

        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM platforms WHERE id=?", (plat_id,))
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass

    def get_platform(self, plat_id: int) -> Optional[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM platforms WHERE id=?", (plat_id,)).fetchone()

    def list_platforms(self, root_only: bool = False) -> list[sqlite3.Row]:
        """Piattaforme con il numero di giochi (colonna `game_count`)."""
        where = "WHERE p.parent_id IS NULL " if root_only else ""
        return self._conn().execute(
            f"SELECT p.*, (SELECT COUNT(*) FROM games g WHERE g.platform_id = p.id) AS game_count "
            f"FROM platforms p {where}ORDER BY p.sort_order, p.name COLLATE NOCASE").fetchall()

    def list_subplatforms(self, parent_id: int) -> list[sqlite3.Row]:
        """Sotto-sezioni / cartelle figlie di una piattaforma specifica."""
        return self._conn().execute(
            "SELECT p.*, (SELECT COUNT(*) FROM games g WHERE g.platform_id = p.id) AS game_count "
            "FROM platforms p WHERE p.parent_id=? ORDER BY p.sort_order, p.name COLLATE NOCASE", (parent_id,)).fetchall()

    def reorder_platforms(self, order: list[tuple[int, int]]) -> None:
        """Aggiorna il sort_order delle piattaforme/sotto-sezioni secondo [(platform_id, sort_order), ...]."""
        with self._conn() as c:
            c.executemany("UPDATE platforms SET sort_order=? WHERE id=?", [(so, pid) for pid, so in order])

    @staticmethod
    def normalize_extensions(text: str) -> str:
        """'nes, .ZIP;7z' -> '.nes,.zip,.7z'."""
        parts = [p.strip().lower() for p in re.split(r"[,;\s]+", text) if p.strip()]
        return ",".join(p if p.startswith(".") else "." + p for p in parts)

    # ----- abbinamento piattaforme <-> emulatori -----
    def set_platform_emulators(self, plat_id: int, links: Iterable[tuple[int, bool, str]]) -> None:
        """Sostituisce gli emulatori della piattaforma.

        `links` = (emulator_id, is_default, args_override). Se nessuno e' marcato
        come predefinito lo diventa il primo.
        """
        links = list(links)
        if links and not any(d for _, d, _ in links):
            links[0] = (links[0][0], True, links[0][2])
        with self._conn() as c:
            c.execute("DELETE FROM platform_emulators WHERE platform_id=?", (plat_id,))
            seen_default = False
            for emu_id, is_default, override in links:
                flag = 1 if (is_default and not seen_default) else 0
                seen_default = seen_default or bool(flag)
                c.execute("INSERT INTO platform_emulators(platform_id, emulator_id, is_default, args_override) VALUES (?,?,?,?)",
                          (plat_id, emu_id, flag, override.strip()))

    def platform_emulators(self, plat_id: int) -> list[sqlite3.Row]:
        """Emulatori abbinati (default per primo) con args_override e is_default."""
        return self._conn().execute(
            "SELECT e.*, pe.is_default, pe.args_override FROM platform_emulators pe "
            "JOIN emulators e ON e.id = pe.emulator_id WHERE pe.platform_id=? "
            "ORDER BY pe.is_default DESC, e.name COLLATE NOCASE", (plat_id,)).fetchall()

    def emulator_for_game(self, game: sqlite3.Row) -> Optional[dict]:
        """Emulatore da usare per un gioco: override del gioco, altrimenti il predefinito della piattaforma.

        Restituisce un dict con le chiavi dell'emulatore piu' `args` gia' risolto
        (override della piattaforma se presente), oppure None se non c'e' nulla di abbinato.
        """
        if game["emulator_id"]:
            emu = self.get_emulator(game["emulator_id"])
            if emu:
                return dict(emu)
        rows = self.platform_emulators(game["platform_id"])
        if not rows:
            plat = self.get_platform(game["platform_id"])
            if plat and plat["parent_id"]:
                rows = self.platform_emulators(plat["parent_id"])
        if not rows:
            return None
        emu = dict(rows[0])
        if emu.get("args_override"):
            emu["args"] = emu["args_override"]
        return emu

    # ----- giochi -----
    def add_games(self, platform_id: int, rom_paths: Iterable[str]) -> int:
        """Inserisce in blocco le ROM (ignora quelle gia' presenti). Restituisce quante ne ha aggiunte."""
        rows = []
        for p in rom_paths:
            file = os.path.basename(p)
            title = clean_title(file)
            rows.append((platform_id, self.store(p), file, title, search_title(title)))
        with self._conn() as c:
            before = c.total_changes
            c.executemany("INSERT OR IGNORE INTO games(platform_id, rom_path, rom_file, title, search_title) VALUES (?,?,?,?,?)", rows)
            return c.total_changes - before

    def get_game(self, game_id: int) -> Optional[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()

    def list_games(self, platform_id: Optional[int] = None, search: str = "", favorites_only: bool = False,
                   order: str = "title", include_subplatforms: bool = True,
                   favorite_folder_id: Optional[int] = None) -> list[sqlite3.Row]:
        """Giochi filtrati; con platform_id=None restituisce tutte le piattaforme.
        Se include_subplatforms e' True, include anche i giochi delle sotto-sezioni."""
        where, params = [], []
        if platform_id is not None:
            if include_subplatforms:
                sub_ids = [r["id"] for r in self.list_subplatforms(platform_id)]
                all_pids = [platform_id] + sub_ids
                placeholders = ",".join("?" for _ in all_pids)
                where.append(f"g.platform_id IN ({placeholders})")
                params.extend(all_pids)
            else:
                where.append("g.platform_id = ?")
                params.append(platform_id)
        if favorite_folder_id is not None:
            where.append("fi.folder_id = ?")
            params.append(int(favorite_folder_id))
        elif favorites_only:
            where.append("(g.favorite = 1 OR EXISTS (SELECT 1 FROM favorite_items fi WHERE fi.game_id = g.id))")
        if search.strip():
            where.append("(g.title LIKE ? ESCAPE '\\' OR g.developer LIKE ? ESCAPE '\\' OR g.genre LIKE ? ESCAPE '\\')")
            like = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like, like]
        join_sql = " JOIN favorite_items fi ON fi.game_id = g.id" if favorite_folder_id is not None else ""
        sql = f"SELECT g.* FROM games g{join_sql}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + _ORDERS.get(order, _ORDERS["title"])
        return self._conn().execute(sql, params).fetchall()

    # ----- cartelle preferiti -----
    def list_favorite_folders(self) -> list[sqlite3.Row]:
        return self._conn().execute(
            """
            SELECT f.*, COUNT(fi.game_id) AS game_count
            FROM favorite_folders f
            LEFT JOIN favorite_items fi ON fi.folder_id = f.id
            GROUP BY f.id
            ORDER BY f.sort_order, f.name COLLATE NOCASE
            """
        ).fetchall()

    def get_favorite_folder(self, folder_id: int) -> Optional[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM favorite_folders WHERE id=?", (int(folder_id),)).fetchone()

    def create_favorite_folder(self, name: str) -> int:
        clean = name.strip()
        if not clean:
            raise ValueError("Il nome della cartella non può essere vuoto")
        existing = self._conn().execute(
            "SELECT id FROM favorite_folders WHERE LOWER(name) = LOWER(?)", (clean,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        with self._conn() as c:
            mx = c.execute("SELECT COALESCE(MAX(sort_order), 0) FROM favorite_folders").fetchone()[0]
            cur = c.execute(
                "INSERT INTO favorite_folders(name, sort_order) VALUES (?, ?)",
                (clean, int(mx) + 1),
            )
            return int(cur.lastrowid)

    def rename_favorite_folder(self, folder_id: int, new_name: str) -> None:
        clean = new_name.strip()
        if not clean:
            return
        with self._conn() as c:
            c.execute("UPDATE favorite_folders SET name=? WHERE id=?", (clean, int(folder_id)))

    def update_favorite_folder_icon(self, folder_id: int, icon_path: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE favorite_folders SET icon_path=? WHERE id=?", (icon_path, int(folder_id)))

    def delete_favorite_folder(self, folder_id: int) -> None:
        with self._conn() as c:
            gids = [
                int(r[0])
                for r in c.execute("SELECT game_id FROM favorite_items WHERE folder_id=?", (int(folder_id),)).fetchall()
            ]
            c.execute("DELETE FROM favorite_folders WHERE id=?", (int(folder_id),))
            for gid in gids:
                still = c.execute("SELECT 1 FROM favorite_items WHERE game_id=? LIMIT 1", (gid,)).fetchone()
                if still is None:
                    c.execute("UPDATE games SET favorite=0 WHERE id=?", (gid,))

    def add_game_to_favorite_folder(self, game_id: int, folder_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO favorite_items(folder_id, game_id) VALUES (?, ?)",
                (int(folder_id), int(game_id)),
            )
            c.execute("UPDATE games SET favorite=1 WHERE id=?", (int(game_id),))

    def remove_game_from_favorite_folder(self, game_id: int, folder_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM favorite_items WHERE folder_id=? AND game_id=?",
                (int(folder_id), int(game_id)),
            )
            still = c.execute("SELECT 1 FROM favorite_items WHERE game_id=? LIMIT 1", (int(game_id),)).fetchone()
            if still is None:
                c.execute("UPDATE games SET favorite=0 WHERE id=?", (int(game_id),))

    def remove_game_from_all_favorites(self, game_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM favorite_items WHERE game_id=?", (int(game_id),))
            c.execute("UPDATE games SET favorite=0 WHERE id=?", (int(game_id),))

    def get_game_favorite_folder_ids(self, game_id: int) -> set[int]:
        rows = self._conn().execute(
            "SELECT folder_id FROM favorite_items WHERE game_id=?", (int(game_id),)
        ).fetchall()
        return {int(r[0]) for r in rows}

    def set_game_favorite_folders(self, game_id: int, folder_ids: Iterable[int]) -> None:
        clean_ids = sorted({int(fid) for fid in folder_ids})
        with self._conn() as c:
            c.execute("DELETE FROM favorite_items WHERE game_id=?", (int(game_id),))
            if clean_ids:
                c.executemany(
                    "INSERT OR IGNORE INTO favorite_items(folder_id, game_id) VALUES (?, ?)",
                    [(fid, int(game_id)) for fid in clean_ids],
                )
            c.execute("UPDATE games SET favorite=? WHERE id=?", (1 if clean_ids else 0, int(game_id)))

    def update_game(self, game_id: int, **fields: Any) -> None:
        """Aggiorna solo i campi indicati (whitelist GAME_FIELDS)."""
        bad = set(fields) - set(GAME_FIELDS)
        if bad:
            raise ValueError(f"campi non modificabili: {', '.join(sorted(bad))}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE games SET {sets} WHERE id=?", (*fields.values(), game_id))

    def delete_games(self, game_ids: Iterable[int]) -> None:
        with self._conn() as c:
            c.executemany("DELETE FROM games WHERE id=?", [(i,) for i in game_ids])

    def record_play(self, game_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE games SET play_count = play_count + 1, last_played = CURRENT_TIMESTAMP WHERE id=?", (game_id,))

    def add_play_time(self, game_id: int, seconds: int) -> None:
        if seconds <= 0:
            return
        with self._conn() as c:
            c.execute("UPDATE games SET play_time = COALESCE(play_time, 0) + ? WHERE id=?", (int(seconds), game_id))

    def total_games(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM games").fetchone()[0]

    def clear_game_image(self, game_id: int, col: str) -> None:
        """Elimina dal disco (solo dentro media/) e azzera una singola immagine del gioco."""
        if col not in ("boxart_path", "screenshot_path", "logo_path"):
            raise ValueError(f"colonna immagine non valida: {col}")
        g = self.get_game(game_id)
        if g is None:
            return
        media_root = os.path.normpath(os.path.abspath(os.path.join(self.root, "media")))
        rel = g[col]
        if rel:
            abs_p = os.path.normpath(os.path.abspath(self.resolve(rel)))
            if os.path.commonpath([abs_p, media_root]) == media_root and os.path.isfile(abs_p):
                try:
                    os.remove(abs_p)
                except OSError:
                    pass
        with self._conn() as c:
            c.execute(f"UPDATE games SET {col}='' WHERE id=?", (game_id,))

    def clear_game_metadata(self, game_id: int) -> None:
        """Azzera tutti i metadati e rimuove i media scaricati per un singolo gioco (senza toccare la ROM)."""
        g = self.get_game(game_id)
        if g is None:
            return
        media_root = os.path.normpath(os.path.abspath(os.path.join(self.root, "media")))
        for col in ("boxart_path", "screenshot_path", "logo_path"):
            rel = g[col]
            if rel:
                abs_p = os.path.normpath(os.path.abspath(self.resolve(rel)))
                if os.path.commonpath([abs_p, media_root]) == media_root and os.path.isfile(abs_p):
                    try:
                        os.remove(abs_p)
                    except OSError:
                        pass
        t = clean_title(g["rom_file"])
        with self._conn() as c:
            c.execute(
                "UPDATE games SET title=?, search_title=?, description='', genre='', release_year=NULL, "
                "developer='', publisher='', players='', rating=NULL, "
                "boxart_path='', screenshot_path='', extra_screenshots='[]', logo_path='', scraped=0 WHERE id=?",
                (t, search_title(t), game_id),
            )

    def clear_platform_metadata(self, platform_id: Optional[int]) -> int:
        """Azzera tutti i metadati e rimuove i media scaricati per la piattaforma (o tutte se None).

        I file ROM originali non vengono mai toccati.
        """
        media_root = os.path.normpath(os.path.abspath(os.path.join(self.root, "media")))
        conn = self._conn()

        if platform_id is None:
            games = conn.execute("SELECT id, rom_file, boxart_path, screenshot_path, logo_path FROM games").fetchall()
        else:
            pids: list[int] = []
            queue = [platform_id]
            while queue:
                cur = queue.pop(0)
                if cur in pids:
                    continue
                pids.append(cur)
                for sub in self.list_subplatforms(cur):
                    queue.append(sub["id"])
            placeholders = ",".join("?" for _ in pids)
            games = conn.execute(
                f"SELECT id, rom_file, boxart_path, screenshot_path, logo_path FROM games WHERE platform_id IN ({placeholders})",
                pids,
            ).fetchall()

        updates: list[tuple[str, str, int]] = []
        for g in games:
            for col in ("boxart_path", "screenshot_path", "logo_path"):
                rel = g[col]
                if rel:
                    abs_p = os.path.normpath(os.path.abspath(self.resolve(rel)))
                    if os.path.commonpath([abs_p, media_root]) == media_root and os.path.isfile(abs_p):
                        try:
                            os.remove(abs_p)
                        except OSError:
                            pass
            t = clean_title(g["rom_file"])
            updates.append((t, search_title(t), g["id"]))

        with conn:
            conn.executemany(
                "UPDATE games SET title=?, search_title=?, description='', genre='', release_year=NULL, "
                "developer='', publisher='', players='', rating=NULL, "
                "boxart_path='', screenshot_path='', extra_screenshots='[]', logo_path='', scraped=0 WHERE id=?",
                updates,
            )
        return len(updates)

    # ----- scanner ROM -----
    def scan_platform(self, platform_id: int) -> dict[str, int]:
        """Allinea il database alla cartella ROM della piattaforma.

        Aggiunge le ROM nuove (filtrando per estensioni, se indicate) e rimuove
        dalla libreria i giochi il cui file non esiste piu'.  I giochi inseriti a
        mano con ROM fuori dalla cartella non vengono toccati finche' il file esiste.
        Restituisce {'added': n, 'removed': m}.
        """
        plat = self.get_platform(platform_id)
        if plat is None:
            raise ValueError("piattaforma inesistente")
        exts = tuple(
            (e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower())
            for e in (plat["extensions"] or "").split(",")
            if e.strip()
        )
        folder = self.resolve(plat["rom_dir"])
        pkeys = plat.keys()
        scan_subdirs = bool(plat["scan_subdirs"]) if "scan_subdirs" in pkeys else True
        keep_structure = bool(plat["keep_structure"]) if "keep_structure" in pkeys else False

        _re_tid = re.compile(r"\[([0-9A-Fa-f]{16})\]")
        _re_upd_word = re.compile(
            r"(?i)(?:\b(?:updates?|dlcs?|patch(?:es)?|aggiornament[oi])\b|\[(?:upd|dlc)\])"
        )

        def _is_update_or_dlc(filepath: str) -> bool:
            fname = os.path.basename(filepath)
            m = _re_tid.search(fname)
            if m:
                return m.group(1)[-3:].upper() != "000"
            if _re_upd_word.search(fname):
                return True
            if folder:
                try:
                    rel_dir = os.path.relpath(os.path.dirname(filepath), folder)
                except ValueError:
                    rel_dir = ""
                if rel_dir and rel_dir != "." and not rel_dir.startswith(".."):
                    for part in rel_dir.split(os.sep):
                        cleaned = re.sub(r"\([^)]*\)", "", part)
                        if _re_upd_word.search(cleaned):
                            return True
            return False

        def _collect_direct(d: str) -> list[str]:
            res: list[str] = []
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        if entry.is_file():
                            if exts and os.path.splitext(entry.name)[1].lower() not in exts:
                                continue
                            if _is_update_or_dlc(entry.path):
                                continue
                            res.append(entry.path)
            except OSError:
                pass
            return res

        total_added = 0
        total_removed = 0

        if folder and os.path.isdir(folder):
            if scan_subdirs and keep_structure:
                # 1. File nella cartella principale -> piattaforma corrente
                root_found = _collect_direct(folder)
                total_added += self.add_games(platform_id, root_found) if root_found else 0

                # 2. Ogni sottocartella -> sotto-sezione automatica con struttura ad albero
                parent_emus = self.platform_emulators(platform_id)
                emu_links = [(e["id"], bool(e["is_default"]), e["args_override"]) for e in parent_emus]
                dir_to_pid: dict[str, int] = {os.path.normpath(folder): platform_id}

                for dirpath, dirnames, _files in os.walk(folder):
                    dirnames[:] = [
                        d for d in sorted(dirnames)
                        if not _re_upd_word.search(re.sub(r"\([^)]*\)", "", d))
                    ]
                    norm_dir = os.path.normpath(dirpath)
                    if norm_dir == os.path.normpath(folder):
                        continue
                    parent_dir = os.path.dirname(norm_dir)
                    cur_parent_id = dir_to_pid.get(parent_dir, platform_id)

                    stored_dir = self.store(norm_dir)
                    existing = self._conn().execute(
                        "SELECT id FROM platforms WHERE parent_id=? AND rom_dir=?",
                        (cur_parent_id, stored_dir),
                    ).fetchone()
                    if existing:
                        sub_pid = existing["id"]
                    else:
                        base_name = os.path.basename(norm_dir)
                        cand_name = base_name
                        suffix = 2
                        while self._conn().execute("SELECT 1 FROM platforms WHERE name=?", (cand_name,)).fetchone():
                            if suffix == 2:
                                cand_name = f"{base_name} ({plat['name']})"
                            else:
                                cand_name = f"{base_name} ({plat['name']} {suffix})"
                            suffix += 1
                        sub_pid = self.add_platform(
                            cand_name,
                            icon_path=plat["icon_path"],
                            rom_dir=norm_dir,
                            extensions=plat["extensions"],
                            parent_id=cur_parent_id,
                            scan_subdirs=0,
                            keep_structure=0,
                        )
                        if emu_links:
                            self.set_platform_emulators(sub_pid, emu_links)

                    dir_to_pid[norm_dir] = sub_pid
                    sub_found = _collect_direct(norm_dir)
                    total_added += self.add_games(sub_pid, sub_found) if sub_found else 0

                    # Rimuove eventuali ROM non piu' presenti nella sotto-sezione (o file Update/DLC)
                    sub_removed = []
                    for g in self.list_games(sub_pid, include_subplatforms=False):
                        rp = self.resolve(g["rom_path"])
                        if not os.path.isfile(rp) or _is_update_or_dlc(rp):
                            sub_removed.append(g["id"])
                    if sub_removed:
                        self.delete_games(sub_removed)
                        total_removed += len(sub_removed)
                    self.link_existing_media(sub_pid)
            else:
                found: list[str] = []
                if scan_subdirs:
                    for dirpath, _dirs, files in os.walk(folder):
                        for f in files:
                            if exts and os.path.splitext(f)[1].lower() not in exts:
                                continue
                            full_p = os.path.join(dirpath, f)
                            if _is_update_or_dlc(full_p):
                                continue
                            found.append(full_p)
                else:
                    found = _collect_direct(folder)
                total_added = self.add_games(platform_id, found) if found else 0

            # Se la cartella ROM e' raggiungibile, rimuove i giochi il cui file non esiste piu'
            # o eventuali Update/DLC presenti dentro la cartella scansionata.
            removed_ids = []
            norm_root = os.path.normcase(os.path.abspath(folder))
            for g in self.list_games(platform_id, include_subplatforms=False):
                rp = self.resolve(g["rom_path"])
                if not os.path.isfile(rp):
                    removed_ids.append(g["id"])
                else:
                    try:
                        in_folder = os.path.commonpath([norm_root, os.path.normcase(os.path.abspath(rp))]) == norm_root
                    except ValueError:
                        in_folder = False
                    if in_folder and _is_update_or_dlc(rp):
                        removed_ids.append(g["id"])
            if removed_ids:
                self.delete_games(removed_ids)
                total_removed += len(removed_ids)

        # Collega le immagini gia' presenti su disco ai giochi appena aggiunti.
        self.link_existing_media(platform_id)
        return {"added": total_added, "removed": total_removed}

    def link_existing_media(self, platform_id: int) -> int:
        """Per i giochi senza immagine controlla se in media/ esiste un file col nome della ROM."""
        plat = self.get_platform(platform_id)
        linked = 0
        columns = (("boxart", "boxart_path"), ("screenshots", "screenshot_path"),
                   ("logos", "logo_path"), ("videos", "video_path"))
        with self._conn() as c:
            for g in self.list_games(platform_id, include_subplatforms=False):
                for kind, col in columns:
                    if g[col]:
                        continue
                    found = self.find_media(plat, kind, g["rom_file"])
                    if found:
                        c.execute(f"UPDATE games SET {col}=? WHERE id=?", (self.store(found), g["id"]))
                        linked += 1
        return linked


BACKUP_FOLDERS = ("media", "emulators", "roms", "icons")


def create_backup(
    db: Database,
    zip_path: str,
    progress_cb=None,
) -> dict:
    """Crea un archivio ZIP completo con DB, configurazione, media, icone, emulatori e ROM portatili."""
    import datetime
    import tempfile
    import zipfile

    root = os.path.abspath(db.root)
    files_to_pack: list[tuple[str, str]] = []
    for folder in BACKUP_FOLDERS:
        base = os.path.join(root, folder)
        if not os.path.isdir(base):
            continue
        for dp, _ds, fns in os.walk(base):
            for fn in fns:
                abs_p = os.path.join(dp, fn)
                rel_p = os.path.relpath(abs_p, root).replace("\\", "/")
                files_to_pack.append((abs_p, rel_p))

    cfg_path = os.path.join(root, "data", "config.json")
    if os.path.isfile(cfg_path):
        files_to_pack.append((cfg_path, "data/config.json"))

    # Conteggi dal DB corrente
    c = sqlite3.connect(db.path, timeout=15)
    try:
        plat_count = c.execute("SELECT COUNT(*) FROM platforms").fetchone()[0]
        game_count = c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        emu_count = c.execute("SELECT COUNT(*) FROM emulators").fetchone()[0]
        fav_count = c.execute(
            "SELECT COUNT(*) FROM games g WHERE g.favorite = 1 OR EXISTS (SELECT 1 FROM favorite_items fi WHERE fi.game_id = g.id)"
        ).fetchone()[0]
    finally:
        c.close()

    manifest = {
        "app": "EasyLauncher",
        "format_version": 1,
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "platforms": plat_count,
        "games": game_count,
        "emulators": emu_count,
        "favorites": fav_count,
        "files": len(files_to_pack) + 1,
    }

    total = len(files_to_pack) + 2
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    tmp_zip = zip_path + ".tmp"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp_db = tf.name
    try:
        # Snapshot coerente del database SQLite
        src_conn = sqlite3.connect(db.path, timeout=15)
        dst_conn = sqlite3.connect(tmp_db)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if progress_cb:
                progress_cb(0, total, "data/library.db")
            zf.write(tmp_db, "data/library.db")
            zf.writestr("backup_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for idx, (abs_p, rel_p) in enumerate(files_to_pack, start=1):
                if progress_cb and (idx == 1 or idx % 25 == 0 or idx == len(files_to_pack)):
                    progress_cb(idx + 1, total, rel_p)
                zf.write(abs_p, rel_p)
        os.replace(tmp_zip, zip_path)
    finally:
        for p in (tmp_db, tmp_zip):
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return manifest


def inspect_backup(zip_path: str) -> dict:
    """Verifica che il file sia un backup EasyLauncher valido e restituisce il riepilogo."""
    import zipfile

    if not zip_path or not os.path.isfile(zip_path):
        raise ValueError("File di backup non trovato.")
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        if "data/library.db" not in names:
            raise ValueError("L'archivio selezionato non contiene 'data/library.db'.")
        if "backup_manifest.json" in names:
            try:
                manifest = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
                if isinstance(manifest, dict):
                    manifest.setdefault("files", len(names))
                    return manifest
            except Exception:
                pass
        return {
            "app": "EasyLauncher",
            "created_at": "",
            "platforms": "?",
            "games": "?",
            "emulators": "?",
            "favorites": "?",
            "files": len(names),
        }


def restore_backup(
    root_dir: str,
    zip_path: str,
    progress_cb=None,
) -> dict:
    """Ripristina tutti i dati da un archivio ZIP di backup dentro root_dir (chiamare a DB chiuso)."""
    import zipfile

    manifest = inspect_backup(zip_path)
    root = os.path.abspath(root_dir)
    os.makedirs(root, exist_ok=True)

    # Pulisce le cartelle dati/media/emulatori/roms prima di estrarre (icons viene unito)
    for folder in ("data", "media", "emulators", "roms"):
        target = os.path.join(root, folder)
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(target, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [
            m for m in zf.infolist()
            if not m.is_dir() and m.filename != "backup_manifest.json"
        ]
        total = max(1, len(members))
        allowed_prefixes = ("data/", "media/", "emulators/", "roms/", "icons/")
        for idx, member in enumerate(members, start=1):
            rel = member.filename.replace("\\", "/").lstrip("/")
            if not rel.startswith(allowed_prefixes):
                continue
            dst = os.path.abspath(os.path.join(root, rel))
            if os.path.commonpath([root, dst]) != root:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with zf.open(member, "r") as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            if progress_cb and (idx == 1 or idx % 25 == 0 or idx == total):
                progress_cb(idx, total, rel)
    return manifest
