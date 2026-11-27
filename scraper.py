"""
scraper.py - Modulo modulare per lo scraping di metadati e copertine.

Non importa nulla di Qt: viene eseguito nei QThread della UI.
Supporta:
  1. TheGamesDB (API v1, richiede api_key in data/config.json)
  2. ScreenScraper (API v2, richiede credenziali utente in data/config.json)
  3. IGDB (Twitch OAuth + API v4, richiede client_id e client_secret)
  4. Libretro Thumbnails (fallback pubblico HTTP senza chiave API)

Tutti i metadati testuali vengono salvati in SQLite e le immagini vengono
scaricate fisicamente in media/<Console>/boxart/ e media/<Console>/screenshots/
rinominandole esattamente come il file della ROM.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import zipfile
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote

import requests

from database import Config, Database, clean_title

USER_AGENT = "EasyLauncher/1.0 (Portable Retro Frontend)"
TIMEOUT = 12


@dataclass
class ScrapeResult:
    """Metadati e URL media raccolti da uno scraper."""
    source: str = ""
    title: str = ""
    description: str = ""
    genre: str = ""
    release_year: Optional[int] = None
    developer: str = ""
    publisher: str = ""
    players: str = ""
    rating: Optional[float] = None
    boxart_url: str = ""
    screenshot_url: str = ""
    logo_url: str = ""
    extra: dict = field(default_factory=dict)

    def has_text(self) -> bool:
        return bool(self.description or self.genre or self.release_year or self.developer)

    def has_media(self) -> bool:
        return bool(self.boxart_url or self.screenshot_url)

    def merge_missing(self, other: "ScrapeResult") -> None:
        """Integra solo i campi ancora vuoti usando un altro risultato."""
        if not self.title and other.title:
            self.title = other.title
        if not self.description and other.description:
            self.description = other.description
        if not self.genre and other.genre:
            self.genre = other.genre
        if self.release_year is None and other.release_year is not None:
            self.release_year = other.release_year
        if not self.developer and other.developer:
            self.developer = other.developer
        if not self.publisher and other.publisher:
            self.publisher = other.publisher
        if not self.players and other.players:
            self.players = other.players
        if self.rating is None and other.rating is not None:
            self.rating = other.rating
        if not self.boxart_url and other.boxart_url:
            self.boxart_url = other.boxart_url
        if not self.screenshot_url and other.screenshot_url:
            self.screenshot_url = other.screenshot_url
        if not self.logo_url and other.logo_url:
            self.logo_url = other.logo_url


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _parse_year(value: object) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(19\d{2}|20\d{2})", str(value))
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# Classe base
# --------------------------------------------------------------------------

class BaseScraper(ABC):
    """Interfaccia comune per tutti i provider di metadati."""

    name: str = "base"

    def __init__(self, session: Optional[requests.Session] = None, lang: str = "it"):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.lang = (lang or "it").lower()

    @abstractmethod
    def is_configured(self) -> bool:
        """True se dispone delle credenziali necessarie (o non ne richiede)."""

    @abstractmethod
    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        """Cerca un gioco e restituisce il miglior risultato trovato, oppure None."""


# --------------------------------------------------------------------------
# 1. TheGamesDB (API v1)
# --------------------------------------------------------------------------

class TheGamesDBScraper(BaseScraper):
    name = "thegamesdb"
    BASE = "https://api.thegamesdb.net/v1"

    def __init__(self, api_key: str, session: Optional[requests.Session] = None, lang: str = "it"):
        super().__init__(session, lang=lang)
        self.api_key = api_key.strip()
        self._platforms_cache: Optional[dict[str, int]] = None
        self._genres_cache: Optional[dict[int, str]] = None
        self._devs_cache: Optional[dict[int, str]] = None
        self._pubs_cache: Optional[dict[int, str]] = None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, endpoint: str, params: dict) -> dict:
        p = dict(params, apikey=self.api_key)
        r = self.session.get(f"{self.BASE}/{endpoint}", params=p, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _platform_id(self, platform_name: str) -> Optional[int]:
        if self._platforms_cache is None:
            data = self._get("Platforms", {})
            items = data.get("data", {}).get("platforms", {})
            self._platforms_cache = {
                _norm(v.get("name", "")): int(k) for k, v in items.items() if v.get("name")
            }
        target = _norm(platform_name)
        if target in self._platforms_cache:
            return self._platforms_cache[target]
        for k, pid in self._platforms_cache.items():
            if target in k or k in target:
                return pid
        return None

    def _lookup_tables(self) -> None:
        if self._genres_cache is None:
            g = self._get("Genres", {}).get("data", {}).get("genres", {})
            self._genres_cache = {int(k): v.get("name", "") for k, v in g.items()}
        if self._devs_cache is None:
            d = self._get("Developers", {}).get("data", {}).get("developers", {})
            self._devs_cache = {int(k): v.get("name", "") for k, v in d.items()}
        if self._pubs_cache is None:
            p = self._get("Publishers", {}).get("data", {}).get("publishers", {})
            self._pubs_cache = {int(k): v.get("name", "") for k, v in p.items()}

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        if not self.is_configured():
            return None
        params = {
            "name": title,
            "fields": "players,publishers,genres,overview,rating",
            "include": "boxart",
        }
        pid = self._platform_id(platform_name)
        if pid is not None:
            params["filter[platform]"] = pid

        payload = self._get("Games/ByGameName", params)
        games = payload.get("data", {}).get("games") or []
        if not games:
            return None

        self._lookup_tables()
        best = games[0]
        gid = str(best["id"])

        box_url = ""
        shot_urls: list[str] = []
        inc = payload.get("include", {}).get("boxart", {})
        base_img = inc.get("base_url", {}).get("original") or inc.get("base_url", {}).get("large") or inc.get("base_url", {}).get("medium") or ""
        for img in inc.get("data", {}).get(gid, []):
            fn = img.get("filename", "")
            if not fn or not base_img:
                continue
            if img.get("type") == "boxart" and img.get("side") == "front" and not box_url:
                box_url = base_img + fn
            elif img.get("type") == "screenshot":
                u = base_img + fn
                if u not in shot_urls:
                    shot_urls.append(u)
        shot_url = shot_urls[0] if shot_urls else ""

        genres = [self._genres_cache.get(i, "") for i in (best.get("genres") or [])]
        devs = [self._devs_cache.get(i, "") for i in (best.get("developers") or [])]
        pubs = [self._pubs_cache.get(i, "") for i in (best.get("publishers") or [])]

        return ScrapeResult(
            source=self.name,
            title=best.get("game_title", ""),
            description=best.get("overview", "") or "",
            genre=", ".join(x for x in genres if x),
            release_year=_parse_year(best.get("release_date")),
            developer=", ".join(x for x in devs if x),
            publisher=", ".join(x for x in pubs if x),
            players=str(best.get("players") or ""),
            boxart_url=box_url,
            screenshot_url=shot_url,
            extra={"screenshots": shot_urls[:10]},
        )


# --------------------------------------------------------------------------
# 2. ScreenScraper (API v2)
# --------------------------------------------------------------------------

def _compute_rom_hashes(rom_path: str, max_bytes: int = 256 * 1024 * 1024) -> tuple[str, str, int]:
    """Calcola (CRC32_hex, MD5_hex, size_bytes) del file ROM (o della ROM interna se .zip)."""
    if not rom_path or not os.path.isfile(rom_path):
        return "", "", 0
    try:
        if rom_path.lower().endswith(".zip"):
            with zipfile.ZipFile(rom_path, "r") as zf:
                files = [zi for zi in zf.infolist() if not zi.is_dir() and zi.file_size > 0]
                if files:
                    best = max(files, key=lambda zi: zi.file_size)
                    if best.file_size > max_bytes:
                        return "", "", best.file_size
                    crc_hex = f"{best.CRC & 0xFFFFFFFF:08X}"
                    md5 = hashlib.md5()
                    with zf.open(best, "r") as fp:
                        while True:
                            chunk = fp.read(1024 * 1024)
                            if not chunk:
                                break
                            md5.update(chunk)
                    return crc_hex, md5.hexdigest(), best.file_size

        size = os.path.getsize(rom_path)
        if size > max_bytes:
            return "", "", size
        crc = 0
        md5 = hashlib.md5()
        with open(rom_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
                md5.update(chunk)
        return f"{crc & 0xFFFFFFFF:08X}", md5.hexdigest(), size
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return "", "", 0


class ScreenScraper(BaseScraper):
    name = "screenscraper"
    URL = "https://api.screenscraper.fr/api2/jeuInfos.php"

    def __init__(self, user: str, password: str, session: Optional[requests.Session] = None, lang: str = "it"):
        super().__init__(session, lang=lang)
        self.user = user.strip()
        self.password = password.strip()

    def is_configured(self) -> bool:
        return bool(self.user and self.password)

    def search(self, title: str, platform_name: str, rom_file: str = "", rom_path: str = "") -> Optional[ScrapeResult]:
        if not self.is_configured():
            return None
        params = {
            "ssid": self.user,
            "sspassword": self.password,
            "softname": "EasyLauncher",
            "output": "json",
            "romnom": rom_file or f"{title}.zip",
        }
        crc, md5, rom_size = _compute_rom_hashes(rom_path)
        if crc:
            params["crc"] = crc
        if md5:
            params["md5"] = md5
        if rom_size > 0:
            params["romtaille"] = str(rom_size)
        r = self.session.get(self.URL, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        jeu = r.json().get("response", {}).get("jeu")
        if not jeu:
            return None

        # Priorita': lingua scelta, poi inglese/USA/Europa, poi resto
        lang_order = []
        for code in (self.lang, "en", "us", "eu", "wor", "jp"):
            if code and code not in lang_order:
                lang_order.append(code)
        reg_order = []
        if self.lang in ("it", "fr", "es", "de"):
            reg_order.append(self.lang)
        for code in ("us", "eu", "wor", "uk", "en", "jp"):
            if code not in reg_order:
                reg_order.append(code)

        def pick_lang(entries: list[dict], key: str = "langue") -> str:
            order = reg_order if key == "region" else lang_order
            for lang in order:
                for item in entries or []:
                    if item.get(key) == lang and item.get("text"):
                        return item["text"]
            return (entries[0].get("text", "") if entries else "")

        box_candidates: list[dict] = []
        shot_urls: list[str] = []
        for m in jeu.get("medias") or []:
            t = m.get("type", "")
            if t == "box-2D" and m.get("url"):
                box_candidates.append(m)
            elif t in ("ss", "sstitle") and m.get("url"):
                u = m.get("url", "")
                if u and u not in shot_urls:
                    shot_urls.append(u)
        shot_url = shot_urls[0] if shot_urls else ""

        box_url = ""
        for reg in reg_order:
            for m in box_candidates:
                if (m.get("region") or "").lower() == reg:
                    box_url = m["url"]
                    break
            if box_url:
                break
        if not box_url and box_candidates:
            box_url = box_candidates[0]["url"]

        rating = None
        note = (jeu.get("note") or {}).get("text")
        if note:
            try:
                rating = round(float(note) / 4.0, 1)  # ScreenScraper usa 0..20
            except ValueError:
                pass

        genres = [pick_lang(g.get("noms", [])) for g in (jeu.get("genres") or [])]
        dates = [d.get("text", "") for d in (jeu.get("dates") or [])]

        return ScrapeResult(
            source=self.name,
            title=pick_lang(jeu.get("noms", []), "region"),
            description=pick_lang(jeu.get("synopsis", [])),
            genre=", ".join(x for x in genres if x),
            release_year=_parse_year(dates[0] if dates else None),
            developer=(jeu.get("developpeur") or {}).get("text", ""),
            publisher=(jeu.get("editeur") or {}).get("text", ""),
            players=(jeu.get("joueurs") or {}).get("text", ""),
            rating=rating,
            boxart_url=box_url,
            screenshot_url=shot_url,
            extra={"screenshots": shot_urls[:10]},
        )


# --------------------------------------------------------------------------
# 3. IGDB (Twitch OAuth + API v4)
# --------------------------------------------------------------------------

class IGDBScraper(BaseScraper):
    name = "igdb"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    GAMES_URL = "https://api.igdb.com/v4/games"
    _PLATFORM_IDS = {
        "nintendo entertainment system (nes)": 18, "nintendo entertainment system": 18, "nes": 18,
        "super nintendo (snes)": 19, "super nintendo": 19, "snes": 19,
        "nintendo 64": 4, "n64": 4,
        "nintendo gamecube": 21, "gamecube": 21, "ngc": 21,
        "nintendo wii u": 41, "wii u": 41,
        "nintendo wii": 5, "wii": 5,
        "nintendo switch": 130, "switch": 130,
        "game boy advance": 24, "gba": 24,
        "game boy color": 22, "gbc": 22,
        "game boy": 33, "gb": 33,
        "nintendo 3ds": 37, "3ds": 37,
        "nintendo ds": 20, "nds": 20,
        "sony playstation 3": 9, "playstation 3": 9, "ps3": 9,
        "sony playstation 2": 8, "playstation 2": 8, "ps2": 8,
        "sony playstation": 7, "playstation": 7, "ps1": 7, "psx": 7,
        "sony ps vita": 46, "ps vita": 46,
        "sony psp": 38, "psp": 38,
        "sega master system": 64, "master system": 64,
        "sega mega drive": 29, "sega genesis": 29, "mega drive": 29, "megadrive": 29,
        "sega cd": 78, "sega 32x": 30,
        "sega saturn": 32, "saturn": 32,
        "sega dreamcast": 23, "dreamcast": 23,
        "sega game gear": 35, "game gear": 35,
        "microsoft xbox 360": 12, "xbox 360": 12,
        "microsoft xbox": 11, "xbox": 11,
        "dos": 13, "pc dos": 13,
        "arcade (mame)": 52, "arcade": 52, "mame": 52,
        "snk neo geo": 80, "neo geo": 80,
        "nec pc engine": 86, "pc engine": 86,
        "atari 2600": 59, "atari lynx": 61,
        "commodore amiga": 16, "amiga": 16,
        "commodore 64": 15, "c64": 15,
    }

    def __init__(self, client_id: str, client_secret: str, session: Optional[requests.Session] = None, lang: str = "it"):
        super().__init__(session, lang=lang)
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._token: str = ""

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _authenticate(self) -> str:
        if self._token:
            return self._token
        r = self.session.post(
            self.TOKEN_URL,
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self._token = r.json().get("access_token", "")
        return self._token

    def _resolve_platform_id(self, platform_name: str) -> Optional[int]:
        low = platform_name.lower().strip()
        if low in self._PLATFORM_IDS:
            return self._PLATFORM_IDS[low]
        for k, pid in sorted(self._PLATFORM_IDS.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"\b{re.escape(k)}\b", low):
                return pid
        return None

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        if not self.is_configured():
            return None
        token = self._authenticate()
        if not token:
            return None
        import datetime
        import time
        clean = clean_title(title)
        safe_q = clean.replace('"', '\\"')
        plat_id = self._resolve_platform_id(platform_name)
        where_clause = f"where platforms = ({plat_id}); " if plat_id else ""
        fields = (
            "fields name,summary,first_release_date,release_dates.date,release_dates.platform,"
            "rating,genres.name,"
            "involved_companies.developer,involved_companies.publisher,involved_companies.company.name,"
            "cover.image_id,screenshots.image_id; limit 10;"
        )
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token}"}

        time.sleep(0.26)
        query = f'search "{safe_q}"; {where_clause}{fields}'
        r = self.session.post(self.GAMES_URL, data=query, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        items = r.json() or []
        if not items:
            return None

        best = _best_title_match(clean, items, lambda x: x.get("name", ""), min_ratio=0.75)
        if not best:
            return None

        year = None
        if plat_id and best.get("release_dates"):
            plat_years = [
                datetime.datetime.utcfromtimestamp(rd["date"]).year
                for rd in best["release_dates"]
                if rd.get("platform") == plat_id and rd.get("date")
            ]
            if plat_years:
                year = min(plat_years)
        if year is None and best.get("first_release_date"):
            year = datetime.datetime.utcfromtimestamp(best["first_release_date"]).year

        devs, pubs = [], []
        for ic in best.get("involved_companies") or []:
            cname = (ic.get("company") or {}).get("name", "")
            if not cname:
                continue
            if ic.get("developer"):
                devs.append(cname)
            if ic.get("publisher"):
                pubs.append(cname)

        cover_id = (best.get("cover") or {}).get("image_id", "")
        shots = best.get("screenshots") or []
        shot_urls = [
            f"https://images.igdb.com/igdb/image/upload/t_1080p/{sh['image_id']}.jpg"
            for sh in shots
            if sh.get("image_id")
        ]
        shot_url = shot_urls[0] if shot_urls else ""

        rating = round(float(best["rating"]) / 20.0, 1) if best.get("rating") else None

        return ScrapeResult(
            source=self.name,
            title=best.get("name", ""),
            description=best.get("summary", "") or "",
            genre=", ".join(g.get("name", "") for g in (best.get("genres") or []) if g.get("name")),
            release_year=year,
            developer=", ".join(devs),
            publisher=", ".join(pubs),
            rating=rating,
            boxart_url=f"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{cover_id}.jpg" if cover_id else "",
            screenshot_url=shot_url,
            extra={"screenshots": shot_urls[:10]},
        )


# --------------------------------------------------------------------------
# 4. Libretro Thumbnails (fallback pubblico HTTP senza API key)
# --------------------------------------------------------------------------

_LIBRETRO_PLATFORMS = {
    "nes": "Nintendo - Nintendo Entertainment System",
    "nintendo entertainment system": "Nintendo - Nintendo Entertainment System",
    "famicom": "Nintendo - Nintendo Entertainment System",
    "snes": "Nintendo - Super Nintendo Entertainment System",
    "super nintendo": "Nintendo - Super Nintendo Entertainment System",
    "super nintendo entertainment system": "Nintendo - Super Nintendo Entertainment System",
    "n64": "Nintendo - Nintendo 64",
    "nintendo 64": "Nintendo - Nintendo 64",
    "gb": "Nintendo - Game Boy",
    "game boy": "Nintendo - Game Boy",
    "nintendo game boy": "Nintendo - Game Boy",
    "gbc": "Nintendo - Game Boy Color",
    "game boy color": "Nintendo - Game Boy Color",
    "nintendo game boy color": "Nintendo - Game Boy Color",
    "gba": "Nintendo - Game Boy Advance",
    "game boy advance": "Nintendo - Game Boy Advance",
    "nintendo game boy advance": "Nintendo - Game Boy Advance",
    "nds": "Nintendo - Nintendo DS",
    "nintendo ds": "Nintendo - Nintendo DS",
    "3ds": "Nintendo - Nintendo 3DS",
    "nintendo 3ds": "Nintendo - Nintendo 3DS",
    "gamecube": "Nintendo - GameCube",
    "wii": "Nintendo - Wii",
    "megadrive": "Sega - Mega Drive - Genesis",
    "mega drive": "Sega - Mega Drive - Genesis",
    "genesis": "Sega - Mega Drive - Genesis",
    "sega genesis": "Sega - Mega Drive - Genesis",
    "master system": "Sega - Master System - Mark III",
    "sega master system": "Sega - Master System - Mark III",
    "game gear": "Sega - Game Gear",
    "dreamcast": "Sega - Dreamcast",
    "saturn": "Sega - Saturn",
    "ps1": "Sony - PlayStation",
    "psx": "Sony - PlayStation",
    "playstation": "Sony - PlayStation",
    "ps2": "Sony - PlayStation 2",
    "playstation 2": "Sony - PlayStation 2",
    "psp": "Sony - PlayStation Portable",
    "arcade": "MAME",
    "mame": "MAME",
    "neo geo": "SNK - Neo Geo",
    "snk neo geo": "SNK - Neo Geo",
    "pc engine": "NEC - PC Engine - TurboGrafx 16",
}


class LibretroScraper(BaseScraper):
    """Fallback HTTP su thumbnails.libretro.com (nessuna chiave API richiesta)."""

    name = "libretro"
    BASE = "https://thumbnails.libretro.com"

    def is_configured(self) -> bool:
        return True

    @staticmethod
    def _resolve_repo(platform_name: str) -> str:
        key = _norm(platform_name)
        if key in _LIBRETRO_PLATFORMS:
            return _LIBRETRO_PLATFORMS[key]
        for k, repo in _LIBRETRO_PLATFORMS.items():
            if k in key or key in k:
                return repo
        return platform_name

    def _exists(self, url: str) -> bool:
        try:
            r = self.session.head(url, timeout=6, allow_redirects=True)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        repo = self._resolve_repo(platform_name)
        candidates = []
        if rom_file:
            candidates.append(os.path.splitext(os.path.basename(rom_file))[0])
        if title and title not in candidates:
            candidates.append(title)
        cleaned = clean_title(rom_file) if rom_file else ""
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
        # Varianti comuni No-Intro ordinate in base alla lingua preferita (fallback inglese)
        region_by_lang = {
            "it": ("(Italy)", "(Europe)", "(USA)", "(World)", "(Japan)"),
            "fr": ("(France)", "(Europe)", "(USA)", "(World)", "(Japan)"),
            "es": ("(Spain)", "(Europe)", "(USA)", "(World)", "(Japan)"),
            "de": ("(Germany)", "(Europe)", "(USA)", "(World)", "(Japan)"),
            "en": ("(USA)", "(Europe)", "(World)", "(Japan)"),
        }
        regions = region_by_lang.get(self.lang, region_by_lang["en"])
        for base in list(candidates):
            for region in regions:
                if "(" not in base:
                    candidates.append(f"{base} {region}")

        box_url = ""
        shot_url = ""
        logo_url = ""
        repo_q = quote(repo)

        for name in candidates:
            # Libretro sostituisce & * / : ` < > ? \ | con _
            safe = re.sub(r'[&*/:`<>?\\|"]', "_", name)
            name_q = quote(safe)
            if not box_url:
                u = f"{self.BASE}/{repo_q}/Named_Boxarts/{name_q}.png"
                if self._exists(u):
                    box_url = u
            if not shot_url:
                u = f"{self.BASE}/{repo_q}/Named_Snaps/{name_q}.png"
                if self._exists(u):
                    shot_url = u
            if not logo_url:
                u = f"{self.BASE}/{repo_q}/Named_Titles/{name_q}.png"
                if self._exists(u):
                    logo_url = u
            if box_url and shot_url and logo_url:
                break

        if not box_url and not shot_url and not logo_url:
            return None

        return ScrapeResult(
            source=self.name,
            boxart_url=box_url,
            screenshot_url=shot_url,
            logo_url=logo_url,
        )


def _norm_title(text: str) -> str:
    t = re.sub(r"\([^)]*\)", " ", text.lower())
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return t


def _best_title_match(query: str, items: list, title_fn: Callable[[object], str], min_ratio: float = 0.80):
    from difflib import SequenceMatcher

    nq = _norm_title(query)
    if not nq:
        return None
    best_item = None
    best_score = 0.0
    for it in items:
        cand = title_fn(it) or ""
        nc = _norm_title(cand)
        if not nc:
            continue
        if nc == nq:
            return it
        score = SequenceMatcher(None, nq, nc).ratio()
        if score > best_score:
            best_score = score
            best_item = it
    return best_item if best_score >= min_ratio else None


class SteamScraper(BaseScraper):
    """Scraper pubblico Steam Store (nessuna API key richiesta)."""

    name = "steam"
    _LANG_MAP = {
        "it": "italian",
        "en": "english",
        "fr": "french",
        "es": "spanish",
        "de": "german",
    }

    def is_configured(self) -> bool:
        return True

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        clean = clean_title(title)
        r = self.session.get(
            "https://store.steampowered.com/api/storesearch/",
            params={"term": clean, "cc": "us", "l": "en"},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return None
        items = r.json().get("items") or []
        best = _best_title_match(clean, items, lambda x: x.get("name", ""))
        if not best:
            return None
        appid = best.get("id")
        if not appid:
            return None

        steam_lang = self._LANG_MAP.get(self.lang, "english")
        rd = self.session.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": appid, "l": steam_lang},
            timeout=TIMEOUT,
        )
        if not rd.ok:
            return None
        entry = (rd.json() or {}).get(str(appid), {})
        if not entry.get("success") or not entry.get("data"):
            return None
        data = entry["data"]

        desc_raw = data.get("short_description") or data.get("about_the_game") or ""
        desc = re.sub(r"<[^>]+>", " ", desc_raw)
        desc = re.sub(r"\s+", " ", desc).strip()

        genres = ", ".join(g.get("description", "") for g in (data.get("genres") or []) if g.get("description"))
        devs = ", ".join(data.get("developers") or [])
        pubs = ", ".join(data.get("publishers") or [])

        year = None
        rel = (data.get("release_date") or {}).get("date", "")
        m = re.search(r"(\d{4})", rel)
        if m:
            year = int(m.group(1))

        box_url = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900_2x.jpg"
        try:
            if self.session.head(box_url, timeout=4).status_code != 200:
                box_url = data.get("header_image") or ""
        except requests.RequestException:
            box_url = data.get("header_image") or ""

        shots = data.get("screenshots") or []
        shot_url = shots[0].get("path_full", "") if shots else (data.get("header_image") or "")

        return ScrapeResult(
            source=self.name,
            title=data.get("name") or "",
            description=desc,
            genre=genres,
            release_year=year,
            developer=devs,
            publisher=pubs,
            boxart_url=box_url,
            screenshot_url=shot_url,
            logo_url=data.get("header_image") or "",
        )


class GOGScraper(BaseScraper):
    """Scraper pubblico catalogo GOG (nessuna API key richiesta)."""

    name = "gog"

    def is_configured(self) -> bool:
        return True

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        clean = clean_title(title)
        r = self.session.get(
            "https://catalog.gog.com/v1/catalog",
            params={"query": f"like:{clean}", "limit": "5"},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return None
        products = r.json().get("products") or []
        p = _best_title_match(clean, products, lambda x: x.get("title", ""))
        if not p:
            return None

        genres = ", ".join(g.get("name", "") for g in (p.get("genres") or []) if g.get("name"))
        devs = ", ".join(p.get("developers") or [])
        pubs = ", ".join(p.get("publishers") or [])
        year = None
        rel = str(p.get("releaseDate") or "")
        m = re.search(r"(\d{4})", rel)
        if m:
            year = int(m.group(1))

        box_url = p.get("coverVertical") or p.get("coverHorizontal") or ""
        shots = p.get("screenshots") or []
        shot_url = ""
        if shots and isinstance(shots[0], str):
            shot_url = shots[0].replace("{formatter}", "ggvgm_2x")

        return ScrapeResult(
            source=self.name,
            title=p.get("title") or "",
            genre=genres,
            release_year=year,
            developer=devs,
            publisher=pubs,
            boxart_url=box_url,
            screenshot_url=shot_url,
            logo_url=p.get("coverHorizontal") or "",
        )


class WikipediaScraper(BaseScraper):
    """Scraper pubblico Wikipedia (nessuna API key richiesta)."""

    name = "wikipedia"

    def is_configured(self) -> bool:
        return True

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        clean = clean_title(title)
        hosts = [f"{self.lang}.wikipedia.org", "en.wikipedia.org"] if self.lang != "en" else ["en.wikipedia.org"]
        for host in hosts:
            suffix = "videogioco" if host.startswith("it.") else "video game"
            r = self.session.get(
                f"https://{host}/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": f"{clean} {suffix}",
                    "srlimit": "1",
                    "format": "json",
                },
                timeout=TIMEOUT,
            )
            if not r.ok:
                continue
            hits = r.json().get("query", {}).get("search") or []
            best_hit = _best_title_match(clean, hits, lambda x: x.get("title", ""), min_ratio=0.72)
            if not best_hit:
                continue
            page_title = best_hit["title"]
            rs = self.session.get(
                f"https://{host}/api/rest_v1/page/summary/{quote(page_title.replace(' ', '_'))}",
                timeout=TIMEOUT,
            )
            if not rs.ok:
                continue
            data = rs.json()
            desc = (data.get("extract") or "").strip()
            img = (data.get("originalimage") or {}).get("source") or (data.get("thumbnail") or {}).get("source") or ""
            if desc or img:
                return ScrapeResult(
                    source=self.name,
                    title=data.get("title") or "",
                    description=desc,
                    boxart_url=img,
                )
        return None


class LaunchBoxScraper(BaseScraper):
    """Scraper pubblico LaunchBox GamesDB (nessuna API key richiesta)."""

    name = "launchbox"
    _WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    def is_configured(self) -> bool:
        return True

    @staticmethod
    def _lb_platform_aliases(platform_name: str) -> list[str]:
        low = platform_name.lower().strip()
        aliases = {
            "nintendo entertainment system (nes)": ["nintendo entertainment system"],
            "super nintendo (snes)": ["super nintendo entertainment system", "super nintendo"],
            "sega mega drive": ["sega genesis", "sega mega drive"],
            "arcade (mame)": ["arcade"],
            "dos": ["ms-dos", "dos"],
            "nec pc engine": ["nec turbografx-16", "pc engine"],
            "sony playstation": ["sony playstation"],
            "sony playstation 2": ["sony playstation 2"],
            "sony playstation 3": ["sony playstation 3"],
        }
        if low in aliases:
            return aliases[low]
        cleaned = re.sub(r"\s*\([^)]*\)", "", low).strip()
        return [cleaned] if cleaned else []

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        from difflib import SequenceMatcher
        import html as _html

        clean = clean_title(title)
        if not clean:
            return None
        headers = {"User-Agent": self._WEB_UA}
        r = self.session.get(
            "https://gamesdb.launchbox-app.com/games/results",
            params={"title": clean},
            headers=headers,
            timeout=8,
        )
        if not r.ok:
            return None

        cards = re.findall(r'<a[^>]+href="(/games/details/[^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
        if not cards:
            return None

        plat_aliases = self._lb_platform_aliases(platform_name)
        best_href = ""
        best_score = -1.0
        clean_low = clean.lower()
        for href, body in cards[:30]:
            txt = " ".join(re.sub(r"<[^>]+>", " ", body).split())
            if not txt:
                continue
            tl = txt.lower()
            # Richiesta rigorosa di corrispondenza piattaforma esatta (evita N64 su DS o PS1 su PS2)
            if plat_aliases and not any(re.search(rf"\b{re.escape(pa)}\b", tl) for pa in plat_aliases):
                continue
            # Verifica che il titolo della card sia coerente (confine di parola, mai sottostringhe tipo 'dr' in 'dragon')
            if not re.search(rf"\b{re.escape(clean_low)}\b", tl) and SequenceMatcher(None, clean_low, txt[:len(clean) + 25].lower()).ratio() < 0.72:
                continue
            sc = 5.0
            if tl.startswith(clean_low):
                sc += 3.0
            if "released" in tl:
                sc += 1.5
            if any(bad in tl for bad in ("rom hack", "homebrew", "unlicensed", "unreleased")):
                sc -= 3.0
            if sc > best_score:
                best_score = sc
                best_href = href

        if not best_href or best_score < 2.0:
            return None

        rd = self.session.get(f"https://gamesdb.launchbox-app.com{best_href}", headers=headers, timeout=8)
        if not rd.ok:
            return None
        page = rd.text

        def _extract_dd(label: str) -> str:
            m = re.search(
                rf"<dt[^>]*>\s*{re.escape(label)}\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
                page,
                re.S | re.I,
            )
            if not m:
                return ""
            val = _html.unescape(" ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split()))
            return "" if val.lower().startswith("no information") else val

        def _extract_section_img(section_prefix: str) -> str:
            m = re.search(
                rf"<h3[^>]*>{re.escape(section_prefix)}[^<]*</h3>.*?<img[^>]+src=\"(https://images\.launchbox-app\.com/[^\"]+)\"",
                page,
                re.S | re.I,
            )
            return m.group(1) if m else ""

        def _extract_section_imgs(section_prefix: str) -> list[str]:
            urls: list[str] = []
            for sec_m in re.finditer(
                rf"<h3[^>]*>{re.escape(section_prefix)}[^<]*</h3>(.*?)(?=<h3|\Z)",
                page,
                re.S | re.I,
            ):
                block = sec_m.group(1)
                for img_m in re.finditer(r'<img[^>]+src="(https://images\.launchbox-app\.com/[^"]+)"', block, re.I):
                    u = img_m.group(1)
                    if u and u not in urls:
                        urls.append(u)
            return urls

        m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        found_title = _html.unescape(" ".join(re.sub(r"<[^>]+>", " ", m_h1.group(1)).split())) if m_h1 else clean
        rel_txt = _extract_dd("Release Date")
        year = _parse_year(rel_txt)
        devs = _extract_dd("Developers")
        pubs = _extract_dd("Publishers")
        genres = _extract_dd("Genres")
        players = _extract_dd("Max Players")

        m_ov = re.search(
            r"<dt[^>]*>\s*Overview\s*</dt>\s*<dd[^>]*>(.*?)</dd>|<div[^>]+class=\"[^\"]*overview[^\"]*\"[^>]*>(.*?)</div>",
            page,
            re.S | re.I,
        )
        desc = ""
        if m_ov:
            raw_ov = m_ov.group(1) or m_ov.group(2) or ""
            desc = _html.unescape(" ".join(re.sub(r"<[^>]+>", " ", raw_ov).split()))
            if desc.lower().startswith("no overview"):
                desc = ""

        box_url = _extract_section_img("Box - Front")
        logo_url = _extract_section_img("Clear Logo")
        snap_urls = _extract_section_imgs("Screenshot")
        snap_url = snap_urls[0] if snap_urls else ""

        if not any((found_title, desc, genres, year, devs, box_url, logo_url, snap_url)):
            return None

        return ScrapeResult(
            source=self.name,
            title=found_title,
            description=desc,
            genre=genres,
            release_year=year,
            developer=devs,
            publisher=pubs,
            players=players,
            boxart_url=box_url,
            logo_url=logo_url,
            screenshot_url=snap_url,
            extra={"detail_url": f"https://gamesdb.launchbox-app.com{best_href}", "screenshots": snap_urls[:10]},
        )


class ArcadeDBScraper(BaseScraper):
    """Scraper pubblico ArcadeDB / ArcadeItalia (gratuito, senza API key, per MAME/Arcade/Neo Geo)."""

    name = "adb"

    def is_configured(self) -> bool:
        return True

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        rom_name = os.path.splitext(os.path.basename(rom_file or ""))[0].strip()
        if not rom_name:
            rom_name = re.sub(r"[^a-z0-9_]+", "", clean_title(title).lower())
        if not rom_name:
            return None

        r = self.session.get(
            "http://adb.arcadeitalia.net/service_scraper.php",
            params={
                "ajax": "query_mame",
                "game_name": rom_name,
                "lang": "it" if self.lang == "it" else "en",
            },
            timeout=8,
        )
        if not r.ok:
            return None
        results = r.json().get("result") or []
        if not results:
            return None
        it = results[0]
        raw_title = (it.get("title") or "").strip()
        if not raw_title:
            return None
        # Rimuove eventuali tag tra parentesi tipo "(World)" dal titolo ArcadeDB
        clean_t = re.sub(r"\s*\([^)]*\)\s*$", "", raw_title).strip() or raw_title
        manuf = (it.get("manufacturer") or "").strip()
        return ScrapeResult(
            source=self.name,
            title=clean_t,
            description=(it.get("history") or "").strip(),
            genre=(it.get("genre") or "").strip(),
            release_year=_parse_year(str(it.get("year") or "")),
            developer=manuf,
            publisher=manuf,
            players=str(it.get("players") or "").strip(),
            boxart_url=(it.get("url_image_flyer") or it.get("url_image_cabinet") or "").strip(),
            logo_url=(it.get("url_image_marquee") or it.get("url_image_title") or "").strip(),
            screenshot_url=(it.get("url_image_ingame") or "").strip(),
        )


class SteamGridDBScraper(BaseScraper):
    """Scraper SteamGridDB API v2 (richiede API Key gratuita opzionale; ottimo per cover 600x900 e loghi PNG)."""

    name = "steamgriddb"
    BASE = "https://www.steamgriddb.com/api/v2"

    def __init__(self, api_key: str, session: requests.Session, lang: str = "en"):
        super().__init__(session, lang=lang)
        self.api_key = (api_key or "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        if not self.is_configured():
            return None
        clean = clean_title(title)
        if not clean:
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = self.session.get(f"{self.BASE}/search/autocomplete/{quote(clean)}", headers=headers, timeout=8)
        if not r.ok:
            return None
        hits = r.json().get("data") or []
        best = _best_title_match(clean, hits, lambda x: x.get("name", ""), min_ratio=0.72)
        if not best:
            return None
        gid = best.get("id")
        if not gid:
            return None

        box_url = ""
        rg = self.session.get(
            f"{self.BASE}/grids/game/{gid}",
            params={"dimensions": "600x900"},
            headers=headers,
            timeout=8,
        )
        if rg.ok and (rg.json().get("data") or []):
            box_url = rg.json()["data"][0].get("url") or ""
        if not box_url:
            rg2 = self.session.get(f"{self.BASE}/grids/game/{gid}", headers=headers, timeout=8)
            if rg2.ok and (rg2.json().get("data") or []):
                box_url = rg2.json()["data"][0].get("url") or ""

        logo_url = ""
        rl = self.session.get(f"{self.BASE}/logos/game/{gid}", headers=headers, timeout=8)
        if rl.ok and (rl.json().get("data") or []):
            logo_url = rl.json()["data"][0].get("url") or ""

        if not box_url and not logo_url:
            return None
        return ScrapeResult(
            source=self.name,
            title=best.get("name") or "",
            boxart_url=box_url,
            logo_url=logo_url,
        )


class WikidataScraper(BaseScraper):
    """Scraper pubblico Wikidata (nessuna API key richiesta, multilingua IT/EN)."""

    name = "wikidata"
    API = "https://www.wikidata.org/w/api.php"

    def is_configured(self) -> bool:
        return True

    def _claim_ids(self, claims: dict, prop: str) -> list[str]:
        out: list[str] = []
        for st in claims.get(prop) or []:
            dv = (st.get("mainsnak") or {}).get("datavalue") or {}
            val = dv.get("value")
            if isinstance(val, dict) and val.get("id"):
                out.append(val["id"])
        return out

    def _claim_file_url(self, claims: dict, prop: str) -> str:
        for st in claims.get(prop) or []:
            dv = (st.get("mainsnak") or {}).get("datavalue") or {}
            val = dv.get("value")
            if isinstance(val, str) and val.strip():
                return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(val.strip().replace(' ', '_'))}?width=600"
        return ""

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        clean = clean_title(title)
        if not clean:
            return None
        r = self.session.get(
            self.API,
            params={
                "action": "wbsearchentities",
                "search": clean,
                "language": self.lang,
                "uselang": self.lang,
                "format": "json",
                "limit": "6",
            },
            timeout=8,
        )
        if not r.ok:
            return None
        hits = r.json().get("search") or []
        game_kws = ("videogioco", "video game", "arcade", "gioco", "game")
        game_hits = [h for h in hits if any(k in (h.get("description") or "").lower() for k in game_kws)]
        best = _best_title_match(clean, game_hits or hits, lambda x: x.get("label", ""), min_ratio=0.75)
        if not best:
            return None
        qid = best.get("id")
        if not qid:
            return None

        re_ent = self.session.get(
            self.API,
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels|descriptions|claims",
                "languages": f"{self.lang}|en",
                "format": "json",
            },
            timeout=8,
        )
        if not re_ent.ok:
            return None
        ent = (re_ent.json().get("entities") or {}).get(qid) or {}
        claims = ent.get("claims") or {}

        # Se non ha descrizione da videogioco, verifica P31 (istanza di) per evitare omonimie
        if not game_hits:
            p31 = set(self._claim_ids(claims, "P31"))
            if "Q7889" not in p31 and "Q7058673" not in p31:
                return None

        labels = ent.get("labels") or {}
        descs = ent.get("descriptions") or {}
        loc_title = (labels.get(self.lang) or labels.get("en") or {}).get("value", "")
        loc_desc = (descs.get(self.lang) or descs.get("en") or {}).get("value", "")

        year: Optional[int] = None
        for st in claims.get("P577") or []:
            tval = (((st.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("time", "")
            y = _parse_year(tval)
            if y and (year is None or y < year):
                year = y

        dev_ids = self._claim_ids(claims, "P178")[:2]
        pub_ids = self._claim_ids(claims, "P123")[:2]
        gen_ids = self._claim_ids(claims, "P136")[:3]
        ref_ids = list(dict.fromkeys(dev_ids + pub_ids + gen_ids))
        ref_labels: dict[str, str] = {}
        if ref_ids:
            rr = self.session.get(
                self.API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(ref_ids),
                    "props": "labels",
                    "languages": f"{self.lang}|en",
                    "format": "json",
                },
                timeout=8,
            )
            if rr.ok:
                for rid, rent in (rr.json().get("entities") or {}).items():
                    rl = rent.get("labels") or {}
                    val = (rl.get(self.lang) or rl.get("en") or {}).get("value", "")
                    if val:
                        ref_labels[rid] = val

        return ScrapeResult(
            source=self.name,
            title=loc_title or clean,
            description=loc_desc,
            genre=", ".join(ref_labels[i] for i in gen_ids if i in ref_labels),
            release_year=year,
            developer=", ".join(ref_labels[i] for i in dev_ids if i in ref_labels),
            publisher=", ".join(ref_labels[i] for i in pub_ids if i in ref_labels),
            boxart_url=self._claim_file_url(claims, "P18"),
            logo_url=self._claim_file_url(claims, "P154"),
        )


class NintendoScraper(BaseScraper):
    """Scraper pubblico Nintendo Europe / Italia (nessuna API key richiesta)."""

    name = "nintendo"
    _LANGS = {"it", "en", "fr", "es", "de"}

    def is_configured(self) -> bool:
        return True

    @staticmethod
    def _map_system(platform_name: str) -> str:
        p = platform_name.lower().strip()
        if "switch" in p:
            return "Nintendo Switch"
        if "3ds" in p:
            return "Nintendo 3DS"
        if "ds" in p:
            return "Nintendo DS"
        if "wii u" in p or "wiiu" in p:
            return "Wii U"
        if "wii" in p:
            return "Wii"
        if "gamecube" in p:
            return "Nintendo GameCube"
        if "game boy advance" in p or "gba" in p:
            return "Game Boy Advance"
        if "64" in p:
            return "Nintendo 64"
        if "snes" in p or "super nintendo" in p:
            return "Super Nintendo Entertainment System"
        if "nes" in p:
            return "Nintendo Entertainment System"
        return ""

    def search(self, title: str, platform_name: str, rom_file: str = "") -> Optional[ScrapeResult]:
        clean = clean_title(title)
        if not clean:
            return None
        lang_code = self.lang if self.lang in self._LANGS else "en"
        sys_name = self._map_system(platform_name)
        r = self.session.get(
            f"https://searching.nintendo-europe.com/{lang_code}/select",
            params={"q": clean, "fq": "type:GAME", "rows": "20", "wt": "json"},
            timeout=8,
        )
        if not r.ok:
            return None
        docs = r.json().get("response", {}).get("docs") or []
        if sys_name:
            sys_docs = [d for d in docs if sys_name in (d.get("system_names_txt") or [])]
            if sys_docs:
                docs = sys_docs
        best = _best_title_match(clean, docs, lambda d: d.get("title", ""), min_ratio=0.70)
        if not best:
            return None

        cats = best.get("game_categories_txt") or []
        genres = ", ".join(str(c).replace("_", " ").title() for c in cats[:3] if c)
        players_to = best.get("players_to")
        players_txt = f"1-{players_to}" if isinstance(players_to, int) and players_to > 1 else (str(players_to) if players_to else "")
        box_url = (best.get("image_url") or best.get("image_url_sq_s") or "").strip()
        if box_url.startswith("//"):
            box_url = "https:" + box_url

        return ScrapeResult(
            source=self.name,
            title=(best.get("title") or "").strip(),
            description=(best.get("excerpt") or "").strip(),
            genre=genres,
            release_year=_parse_year(best.get("pretty_date_s")),
            publisher=(best.get("publisher") or "").strip(),
            players=players_txt,
            boxart_url=box_url,
        )


def _make_profile(
    *,
    title: list[str],
    genre: list[str],
    release_year: list[str],
    developer: list[str],
    publisher: list[str],
    players: list[str],
    description: list[str],
    rating: list[str],
    boxart: list[str],
    logo: list[str],
    screenshot: list[str],
) -> dict[str, dict]:
    return {
        "title": {"enabled": True, "sources": list(title)},
        "genre": {"enabled": True, "sources": list(genre)},
        "release_year": {"enabled": True, "sources": list(release_year)},
        "developer": {"enabled": True, "sources": list(developer)},
        "publisher": {"enabled": True, "sources": list(publisher)},
        "players": {"enabled": True, "sources": list(players)},
        "description": {"enabled": True, "sources": list(description)},
        "rating": {"enabled": True, "sources": list(rating)},
        "boxart": {"enabled": True, "sources": list(boxart)},
        "logo": {"enabled": True, "sources": list(logo)},
        "screenshot": {"enabled": True, "sources": list(screenshot)},
    }


_PROF_GENERIC = _make_profile(
    title=["screenscraper", "igdb", "launchbox", "thegamesdb", "nintendo", "adb", "steam", "gog", "wikipedia", "wikidata"],
    genre=["igdb", "screenscraper", "launchbox", "thegamesdb", "nintendo", "adb", "steam", "gog", "wikidata"],
    release_year=["igdb", "screenscraper", "launchbox", "thegamesdb", "nintendo", "adb", "steam", "gog", "wikidata"],
    developer=["igdb", "screenscraper", "launchbox", "thegamesdb", "adb", "steam", "gog", "wikidata"],
    publisher=["igdb", "screenscraper", "launchbox", "thegamesdb", "nintendo", "adb", "steam", "gog", "wikidata"],
    players=["screenscraper", "launchbox", "thegamesdb", "nintendo", "adb"],
    description=["screenscraper", "igdb", "launchbox", "thegamesdb", "nintendo", "adb", "steam", "gog", "wikipedia", "wikidata"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "launchbox", "screenscraper", "thegamesdb", "igdb", "steamgriddb", "nintendo", "adb", "steam", "gog", "wikipedia", "wikidata"],
    logo=["libretro", "launchbox", "steamgriddb", "screenscraper", "thegamesdb", "igdb", "adb", "steam", "gog", "wikipedia", "wikidata"],
    screenshot=["libretro", "screenscraper", "launchbox", "igdb", "thegamesdb", "adb", "steam", "gog"],
)

_PROF_NINTENDO_MODERN = _make_profile(
    title=["nintendo", "igdb", "launchbox", "thegamesdb", "screenscraper"],
    genre=["nintendo", "igdb", "launchbox", "thegamesdb", "screenscraper"],
    release_year=["nintendo", "igdb", "launchbox", "thegamesdb", "screenscraper"],
    developer=["igdb", "launchbox", "thegamesdb", "screenscraper"],
    publisher=["nintendo", "igdb", "launchbox", "thegamesdb", "screenscraper"],
    players=["nintendo", "launchbox", "thegamesdb", "screenscraper"],
    description=["nintendo", "igdb", "launchbox", "thegamesdb", "screenscraper"],
    rating=["igdb", "screenscraper"],
    boxart=["launchbox", "thegamesdb", "igdb", "screenscraper", "nintendo"],
    logo=["steamgriddb", "launchbox", "igdb", "thegamesdb", "screenscraper"],
    screenshot=["igdb", "launchbox", "thegamesdb", "screenscraper"],
)

_PROF_NINTENDO_DS_WII = _make_profile(
    title=["screenscraper", "nintendo", "launchbox", "igdb", "thegamesdb"],
    genre=["igdb", "nintendo", "launchbox", "screenscraper", "thegamesdb"],
    release_year=["nintendo", "igdb", "screenscraper", "launchbox", "thegamesdb"],
    developer=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    publisher=["nintendo", "igdb", "screenscraper", "launchbox", "thegamesdb"],
    players=["nintendo", "screenscraper", "launchbox", "thegamesdb"],
    description=["nintendo", "screenscraper", "igdb", "launchbox", "thegamesdb"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "screenscraper", "launchbox", "thegamesdb", "nintendo"],
    logo=["libretro", "launchbox", "screenscraper", "thegamesdb", "steamgriddb", "igdb"],
    screenshot=["libretro", "screenscraper", "launchbox", "igdb", "thegamesdb"],
)

_PROF_NINTENDO_RETRO = _make_profile(
    title=["screenscraper", "launchbox", "igdb", "thegamesdb", "nintendo"],
    genre=["igdb", "screenscraper", "launchbox", "thegamesdb", "nintendo"],
    release_year=["igdb", "screenscraper", "launchbox", "thegamesdb", "nintendo"],
    developer=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    publisher=["igdb", "screenscraper", "launchbox", "thegamesdb", "nintendo"],
    players=["screenscraper", "launchbox", "thegamesdb", "nintendo"],
    description=["screenscraper", "igdb", "launchbox", "thegamesdb", "nintendo"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "screenscraper", "launchbox", "thegamesdb"],
    logo=["libretro", "launchbox", "screenscraper", "thegamesdb", "steamgriddb", "igdb"],
    screenshot=["libretro", "screenscraper", "launchbox", "igdb", "thegamesdb"],
)

_PROF_CONSOLE_RETRO = _make_profile(
    title=["screenscraper", "launchbox", "igdb", "thegamesdb"],
    genre=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    release_year=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    developer=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    publisher=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    players=["screenscraper", "launchbox", "thegamesdb"],
    description=["screenscraper", "igdb", "launchbox", "thegamesdb"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "screenscraper", "launchbox", "thegamesdb"],
    logo=["libretro", "launchbox", "screenscraper", "thegamesdb", "steamgriddb", "igdb"],
    screenshot=["libretro", "screenscraper", "launchbox", "igdb", "thegamesdb"],
)

_PROF_CONSOLE_MODERN = _make_profile(
    title=["igdb", "launchbox", "screenscraper", "thegamesdb"],
    genre=["igdb", "launchbox", "screenscraper", "thegamesdb"],
    release_year=["igdb", "launchbox", "screenscraper", "thegamesdb"],
    developer=["igdb", "launchbox", "screenscraper", "thegamesdb"],
    publisher=["igdb", "launchbox", "screenscraper", "thegamesdb"],
    players=["screenscraper", "launchbox", "thegamesdb"],
    description=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    rating=["igdb", "screenscraper"],
    boxart=["launchbox", "screenscraper", "thegamesdb", "libretro", "steamgriddb"],
    logo=["steamgriddb", "launchbox", "igdb", "screenscraper", "thegamesdb"],
    screenshot=["igdb", "launchbox", "screenscraper", "thegamesdb"],
)

_PROF_ARCADE = _make_profile(
    title=["adb", "screenscraper", "launchbox", "igdb", "thegamesdb"],
    genre=["adb", "screenscraper", "launchbox", "igdb", "thegamesdb"],
    release_year=["adb", "screenscraper", "launchbox", "igdb", "thegamesdb"],
    developer=["adb", "screenscraper", "launchbox", "igdb", "thegamesdb"],
    publisher=["adb", "screenscraper", "launchbox", "igdb", "thegamesdb"],
    players=["adb", "screenscraper", "launchbox", "thegamesdb"],
    description=["adb", "screenscraper", "launchbox", "igdb", "thegamesdb"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "adb", "launchbox", "screenscraper", "thegamesdb"],
    logo=["libretro", "adb", "launchbox", "screenscraper", "thegamesdb"],
    screenshot=["adb", "libretro", "screenscraper", "launchbox", "igdb", "thegamesdb"],
)

_PROF_PC_DOS = _make_profile(
    title=["gog", "steam", "igdb", "launchbox", "screenscraper", "thegamesdb", "wikipedia", "wikidata"],
    genre=["igdb", "gog", "steam", "launchbox", "screenscraper", "thegamesdb", "wikidata"],
    release_year=["igdb", "gog", "steam", "launchbox", "screenscraper", "thegamesdb", "wikidata"],
    developer=["igdb", "gog", "steam", "launchbox", "screenscraper", "thegamesdb", "wikidata"],
    publisher=["igdb", "gog", "steam", "launchbox", "screenscraper", "thegamesdb", "wikidata"],
    players=["screenscraper", "launchbox", "thegamesdb"],
    description=["gog", "steam", "igdb", "launchbox", "screenscraper", "thegamesdb", "wikipedia", "wikidata"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "gog", "launchbox", "steamgriddb", "steam", "screenscraper", "thegamesdb"],
    logo=["steamgriddb", "launchbox", "gog", "steam", "libretro", "igdb", "screenscraper", "thegamesdb"],
    screenshot=["gog", "steam", "libretro", "igdb", "launchbox", "screenscraper", "thegamesdb"],
)

_PROF_RETRO_COMPUTER = _make_profile(
    title=["screenscraper", "launchbox", "igdb", "thegamesdb"],
    genre=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    release_year=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    developer=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    publisher=["igdb", "screenscraper", "launchbox", "thegamesdb"],
    players=["screenscraper", "launchbox", "thegamesdb"],
    description=["screenscraper", "launchbox", "igdb", "thegamesdb"],
    rating=["igdb", "screenscraper"],
    boxart=["libretro", "screenscraper", "launchbox", "thegamesdb"],
    logo=["libretro", "launchbox", "screenscraper", "thegamesdb", "steamgriddb", "igdb"],
    screenshot=["libretro", "screenscraper", "launchbox", "igdb", "thegamesdb"],
)

PLATFORM_DEFAULT_PROFILES: dict[str, dict[str, dict]] = {
    "nintendo switch": _PROF_NINTENDO_MODERN,
    "nintendo wii u": _PROF_NINTENDO_MODERN,
    "nintendo 3ds": _PROF_NINTENDO_MODERN,
    "nintendo ds": _PROF_NINTENDO_DS_WII,
    "nintendo wii": _PROF_NINTENDO_DS_WII,
    "nintendo entertainment system (nes)": _PROF_NINTENDO_RETRO,
    "super nintendo (snes)": _PROF_NINTENDO_RETRO,
    "nintendo 64": _PROF_NINTENDO_RETRO,
    "nintendo gamecube": _PROF_NINTENDO_RETRO,
    "game boy": _PROF_NINTENDO_RETRO,
    "game boy color": _PROF_NINTENDO_RETRO,
    "game boy advance": _PROF_NINTENDO_RETRO,
    "sony playstation": _PROF_CONSOLE_RETRO,
    "sony playstation 2": _PROF_CONSOLE_RETRO,
    "sony psp": _PROF_CONSOLE_RETRO,
    "sony playstation 3": _PROF_CONSOLE_MODERN,
    "sony ps vita": _PROF_CONSOLE_MODERN,
    "sega master system": _PROF_CONSOLE_RETRO,
    "sega mega drive": _PROF_CONSOLE_RETRO,
    "sega cd": _PROF_CONSOLE_RETRO,
    "sega 32x": _PROF_CONSOLE_RETRO,
    "sega saturn": _PROF_CONSOLE_RETRO,
    "sega dreamcast": _PROF_CONSOLE_RETRO,
    "sega game gear": _PROF_CONSOLE_RETRO,
    "microsoft xbox": _PROF_CONSOLE_MODERN,
    "microsoft xbox 360": _PROF_CONSOLE_MODERN,
    "dos": _PROF_PC_DOS,
    "scummvm": _PROF_PC_DOS,
    "arcade (mame)": _PROF_ARCADE,
    "snk neo geo": _PROF_ARCADE,
    "nec pc engine": _PROF_CONSOLE_RETRO,
    "atari 2600": _PROF_RETRO_COMPUTER,
    "atari lynx": _PROF_RETRO_COMPUTER,
    "commodore amiga": _PROF_RETRO_COMPUTER,
    "commodore 64": _PROF_RETRO_COMPUTER,
}


def get_platform_default_fields(platform_name: str) -> dict[str, dict]:
    """Restituisce il profilo metadati ottimale di default per la piattaforma indicata."""
    key = (platform_name or "").lower().strip()
    if key in PLATFORM_DEFAULT_PROFILES:
        return {k: {"enabled": v["enabled"], "sources": list(v["sources"])} for k, v in PLATFORM_DEFAULT_PROFILES[key].items()}
    # Match parziale per nomi personalizzati (es. "GBA", "MAME", "PS2", "DS", "Switch")
    if any(t in key for t in ("mame", "arcade", "neo geo", "neogeo", "fba", "fbneo")):
        prof = _PROF_ARCADE
    elif any(t in key for t in ("switch", "wii u", "wiiu", "3ds")):
        prof = _PROF_NINTENDO_MODERN
    elif "ds" in key or "wii" in key:
        prof = _PROF_NINTENDO_DS_WII
    elif any(t in key for t in ("nes", "snes", "n64", "64", "gamecube", "game boy", "gba", "gbc")):
        prof = _PROF_NINTENDO_RETRO
    elif any(t in key for t in ("ps3", "playstation 3", "vita", "xbox")):
        prof = _PROF_CONSOLE_MODERN
    elif any(t in key for t in ("dos", "scummvm")):
        prof = _PROF_PC_DOS
    elif any(t in key for t in ("amiga", "c64", "commodore", "atari")):
        prof = _PROF_RETRO_COMPUTER
    elif any(t in key for t in ("sega", "mega drive", "genesis", "saturn", "dreamcast", "ps1", "ps2", "psp", "playstation", "pc engine")):
        prof = _PROF_CONSOLE_RETRO
    else:
        prof = _PROF_GENERIC
    return {k: {"enabled": v["enabled"], "sources": list(v["sources"])} for k, v in prof.items()}


# --------------------------------------------------------------------------
# Orchestratore e salvataggio locale
# --------------------------------------------------------------------------

class ScraperManager:
    """Esegue gli scraper in cascata, scarica le immagini e aggiorna SQLite."""

    DEFAULT_ORDER = [
        "screenscraper",
        "thegamesdb",
        "igdb",
        "nintendo",
        "launchbox",
        "adb",
        "steamgriddb",
        "steam",
        "gog",
        "wikipedia",
        "wikidata",
        "libretro",
    ]

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.session = requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    def _build_registry(self) -> dict[str, BaseScraper]:
        keys = self.config.get("api_keys", {})
        lang = self.config.get("metadata_language", "it")
        return {
            "thegamesdb": TheGamesDBScraper(keys.get("thegamesdb", ""), self.session, lang=lang),
            "screenscraper": ScreenScraper(
                keys.get("screenscraper_user", ""),
                keys.get("screenscraper_password", ""),
                self.session,
                lang=lang,
            ),
            "igdb": IGDBScraper(
                keys.get("igdb_client_id", ""),
                keys.get("igdb_client_secret", ""),
                self.session,
                lang=lang,
            ),
            "launchbox": LaunchBoxScraper(self.session, lang=lang),
            "nintendo": NintendoScraper(self.session, lang=lang),
            "adb": ArcadeDBScraper(self.session, lang=lang),
            "steamgriddb": SteamGridDBScraper(keys.get("steamgriddb", ""), self.session, lang=lang),
            "steam": SteamScraper(self.session, lang=lang),
            "gog": GOGScraper(self.session, lang=lang),
            "wikipedia": WikipediaScraper(self.session, lang=lang),
            "wikidata": WikidataScraper(self.session, lang=lang),
            "libretro": LibretroScraper(self.session, lang=lang),
        }

    def _build_scrapers(self) -> list[BaseScraper]:
        registry = self._build_registry()
        order = self.config.get("scraper_order", self.DEFAULT_ORDER)
        scrapers: list[BaseScraper] = []
        for name in order:
            sc = registry.get(name)
            if sc and sc.is_configured():
                scrapers.append(sc)
        if not any(isinstance(s, LibretroScraper) for s in scrapers):
            scrapers.append(registry["libretro"])
        return scrapers

    def _download_image(
        self,
        url: str,
        dest_without_ext: str,
        seen_hashes: Optional[set[str]] = None,
    ) -> str:
        """Scarica un'immagine e restituisce il percorso salvato nel DB (relativo)."""
        if not url:
            return ""
        r = self.session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        if not r.content:
            return ""
        if seen_hashes is not None:
            digest = hashlib.sha256(r.content).hexdigest()
            if digest in seen_hashes:
                return ""
            seen_hashes.add(digest)
        ctype = (r.headers.get("Content-Type") or "").lower()
        ext = ".jpg" if "jpeg" in ctype or url.lower().endswith((".jpg", ".jpeg")) else ".png"
        dest = os.path.splitext(dest_without_ext)[0] + ext
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(r.content)
        return self.db.store(dest)

    def _resolve_field_prefs(self, platform: sqlite3.Row) -> dict:
        """Restituisce le priorita' per campo personalizzate per piattaforma (con fallback al profilo ottimale della piattaforma)."""
        by_plat = self.config.get("metadata_fields_by_platform", {})
        cur: Optional[sqlite3.Row] = platform
        root_name = platform["name"]
        while cur is not None:
            pid_key = str(cur["id"])
            root_name = cur["name"]
            if by_plat and pid_key in by_plat and by_plat[pid_key]:
                return by_plat[pid_key]
            parent_id = cur["parent_id"] if "parent_id" in cur.keys() else None
            cur = self.db.get_platform(parent_id) if parent_id else None
        return get_platform_default_fields(root_name)

    def scrape_game(
        self,
        game: sqlite3.Row,
        platform: sqlite3.Row,
        overwrite_manual: bool = False,
    ) -> tuple[bool, str]:
        """Scarica metadati e immagini per un singolo gioco.

        Restituisce (trovato, messaggio_stato).
        """
        if game["scraped"] == 2 and not overwrite_manual:
            return False, f"Saltato (modificato a mano): {game['title']}"

        field_prefs = self._resolve_field_prefs(platform)
        registry = self._build_registry()
        rom_abs = self.db.resolve(game["rom_path"]) if game["rom_path"] else ""

        if field_prefs:
            # Raccoglie solo gli scraper configurati effettivamente richiesti da almeno un campo abilitato
            needed_sources: list[str] = []
            for fkey, fcfg in field_prefs.items():
                if not fcfg.get("enabled", True):
                    continue
                for sname in fcfg.get("sources", []):
                    if sname not in needed_sources and sname in registry and registry[sname].is_configured():
                        needed_sources.append(sname)

            cache: dict[str, ScrapeResult] = {}
            sources_used: list[str] = []
            for sname in needed_sources:
                try:
                    sc_obj = registry[sname]
                    if isinstance(sc_obj, ScreenScraper):
                        res = sc_obj.search(game["title"], platform["name"], game["rom_file"], rom_path=rom_abs)
                    else:
                        res = sc_obj.search(game["title"], platform["name"], game["rom_file"])
                except Exception:
                    continue
                if res is not None:
                    cache[sname] = res
                    sources_used.append(sname)

            if not sources_used:
                return False, f"Nessun risultato: {game['title']}"

            def _pick(fkey: str, attr: str):
                fcfg = field_prefs.get(fkey, {})
                if not fcfg.get("enabled", True):
                    return None
                for sname in fcfg.get("sources", []):
                    res = cache.get(sname)
                    if res is None:
                        continue
                    val = getattr(res, attr, None)
                    if val is not None and val != "":
                        return val
                return None

            merged = ScrapeResult(
                title=_pick("title", "title") or "",
                description=_pick("description", "description") or "",
                genre=_pick("genre", "genre") or "",
                release_year=_pick("release_year", "release_year"),
                developer=_pick("developer", "developer") or "",
                publisher=_pick("publisher", "publisher") or "",
                players=_pick("players", "players") or "",
                rating=_pick("rating", "rating"),
                boxart_url=_pick("boxart", "boxart_url") or "",
                screenshot_url=_pick("screenshot", "screenshot_url") or "",
                logo_url=_pick("logo", "logo_url") or "",
            )
        else:
            merged = ScrapeResult()
            sources_used = []
            for sc in self._build_scrapers():
                try:
                    if isinstance(sc, ScreenScraper):
                        res = sc.search(game["title"], platform["name"], game["rom_file"], rom_path=rom_abs)
                    else:
                        res = sc.search(game["title"], platform["name"], game["rom_file"])
                except Exception:
                    continue
                if res is None:
                    continue
                sources_used.append(sc.name)
                merged.merge_missing(res)
                if merged.has_text() and merged.has_media():
                    break

            if not sources_used:
                return False, f"Nessun risultato: {game['title']}"

        updates: dict[str, object] = {"scraped": 1}
        if merged.title and overwrite_manual:
            updates["title"] = merged.title
        if merged.description and (not game["description"] or overwrite_manual):
            updates["description"] = merged.description
        if merged.genre and (not game["genre"] or overwrite_manual):
            updates["genre"] = merged.genre
        if merged.release_year and (not game["release_year"] or overwrite_manual):
            updates["release_year"] = merged.release_year
        if merged.developer and (not game["developer"] or overwrite_manual):
            updates["developer"] = merged.developer
        if merged.publisher and (not game["publisher"] or overwrite_manual):
            updates["publisher"] = merged.publisher
        if merged.players and (not game["players"] or overwrite_manual):
            updates["players"] = merged.players
        if merged.rating is not None and (game["rating"] is None or overwrite_manual):
            updates["rating"] = merged.rating

        media_root = os.path.normpath(os.path.abspath(os.path.join(self.db.root, "media")))

        def _replace_media(kind: str, col: str, url: str) -> None:
            if not url or (game[col] and not overwrite_manual):
                return
            try:
                target = self.db.media_file(platform, kind, game["rom_file"])
                new_stored = self._download_image(url, target)
            except requests.RequestException:
                return
            if not new_stored:
                return
            old_rel = game[col]
            if old_rel and old_rel != new_stored:
                old_abs = os.path.normpath(os.path.abspath(self.db.resolve(old_rel)))
                if os.path.commonpath([old_abs, media_root]) == media_root and os.path.isfile(old_abs):
                    try:
                        os.remove(old_abs)
                    except OSError:
                        pass
            updates[col] = new_stored

        _replace_media("boxart", "boxart_path", merged.boxart_url)
        _replace_media("screenshots", "screenshot_path", merged.screenshot_url)
        _replace_media("logos", "logo_path", merged.logo_url)

        # Scarica eventuali foto gameplay extra (1..3) tutte diverse tra loro e dalla principale
        shot_cfg = field_prefs.get("screenshot", {}) if field_prefs else {}
        extra_target = max(0, min(3, int(shot_cfg.get("extra_count", 0) or 0))) if shot_cfg.get("enabled", True) else 0
        if extra_target > 0 and field_prefs:
            candidate_urls: list[str] = []
            for sname in shot_cfg.get("sources", []):
                res_s = cache.get(sname)
                if res_s is None:
                    continue
                for u in [res_s.screenshot_url] + list((res_s.extra or {}).get("screenshots", []) or []):
                    if u and u != merged.screenshot_url and u not in candidate_urls:
                        candidate_urls.append(u)

            def _file_sha256(rel_path: str) -> str:
                if not rel_path:
                    return ""
                abs_p = self.db.resolve(rel_path)
                if not abs_p or not os.path.isfile(abs_p):
                    return ""
                try:
                    with open(abs_p, "rb") as fh:
                        return hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    return ""

            seen_hashes: set[str] = set()
            main_rel = str(updates.get("screenshot_path") or game["screenshot_path"] or "")
            h_main = _file_sha256(main_rel)
            if h_main:
                seen_hashes.add(h_main)

            raw_old_extra = game["extra_screenshots"] if "extra_screenshots" in game.keys() else "[]"
            try:
                old_extra_list = json.loads(raw_old_extra) if raw_old_extra else []
            except Exception:
                old_extra_list = []

            extra_stored: list[str] = []
            if not overwrite_manual:
                for rel_p in old_extra_list:
                    h_ex = _file_sha256(rel_p)
                    if h_ex and h_ex not in seen_hashes:
                        seen_hashes.add(h_ex)
                        extra_stored.append(rel_p)
            else:
                for rel_p in old_extra_list:
                    old_abs = os.path.normpath(os.path.abspath(self.db.resolve(rel_p)))
                    if os.path.commonpath([old_abs, media_root]) == media_root and os.path.isfile(old_abs):
                        try:
                            os.remove(old_abs)
                        except OSError:
                            pass

            base_shot_target = os.path.splitext(self.db.media_file(platform, "screenshots", game["rom_file"]))[0]
            slot = 1
            for cand_url in candidate_urls:
                if len(extra_stored) >= extra_target:
                    break
                while os.path.isfile(f"{base_shot_target}_extra_{slot}.png") or os.path.isfile(f"{base_shot_target}_extra_{slot}.jpg"):
                    if overwrite_manual:
                        break
                    slot += 1
                try:
                    stored = self._download_image(cand_url, f"{base_shot_target}_extra_{slot}", seen_hashes=seen_hashes)
                except requests.RequestException:
                    continue
                if stored:
                    extra_stored.append(stored)
                    slot += 1
            updates["extra_screenshots"] = json.dumps(extra_stored[:extra_target])

        self.db.update_game(game["id"], **updates)
        return True, f"Aggiornato ({', '.join(sources_used)}): {game['title']}"

    def scrape_platform(
        self,
        platform_id: Optional[int],
        only_missing: bool = True,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict[str, int]:
        """Scraping in batch per una piattaforma (o tutte se platform_id is None)."""
        plat_cache = {p["id"]: p for p in self.db.list_platforms()}
        games = self.db.list_games(platform_id=platform_id)
        if only_missing:
            def _needs_extra(g_row: sqlite3.Row) -> bool:
                pl = plat_cache.get(g_row["platform_id"])
                if pl is None:
                    return False
                fp = self._resolve_field_prefs(pl)
                sc = fp.get("screenshot", {}) if fp else {}
                ex_t = max(0, min(3, int(sc.get("extra_count", 0) or 0))) if sc.get("enabled", True) else 0
                if ex_t <= 0:
                    return False
                raw_ex = g_row["extra_screenshots"] if "extra_screenshots" in g_row.keys() else "[]"
                try:
                    cur_ex = json.loads(raw_ex) if raw_ex else []
                except Exception:
                    cur_ex = []
                return len(cur_ex) < ex_t

            games = [
                g
                for g in games
                if g["scraped"] != 2
                and (
                    not g["description"]
                    or not g["genre"]
                    or not g["release_year"]
                    or not g["developer"]
                    or not g["publisher"]
                    or not g["boxart_path"]
                    or not g["screenshot_path"]
                    or _needs_extra(g)
                )
            ]

        total = len(games)
        ok_count = 0
        fail_count = 0

        for idx, g in enumerate(games, start=1):
            if should_stop and should_stop():
                break
            plat = plat_cache.get(g["platform_id"])
            if plat is None:
                continue
            ok, msg = self.scrape_game(g, plat, overwrite_manual=not only_missing)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
            if on_progress:
                on_progress(idx, total, msg)

        return {"total": total, "updated": ok_count, "missed": fail_count}
