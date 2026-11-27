# Licenze Open Source e Attribuzioni (Third-Party Notices)

**EasyLauncher** è distribuito sotto licenza **MIT** (vedi il file [`LICENSE`](LICENSE)).  
Di seguito sono elencati i componenti open source di terze parti, le risorse grafiche e le fonti dati utilizzate dal progetto.

---

## 1. Librerie e Dipendenze Software

| Componente | Versione / Ruolo | Licenza | Note / Fonte |
| :--- | :--- | :--- | :--- |
| **Python** | 3.11+ (Runtime) | PSF License | https://www.python.org |
| **PySide6 / Qt 6** | GUI Framework (`QtCore`, `QtGui`, `QtWidgets`, `QtNetwork`) | LGPL-3.0 / GPL-3.0 | https://doc.qt.io/qtforpython-6/ |
| **requests** | Client HTTP per scraper | Apache-2.0 | https://requests.readthedocs.io |
| **urllib3** | Trasporto HTTP | MIT | https://urllib3.readthedocs.io |
| **certifi** | Certificati CA Mozilla | MPL-2.0 | https://github.com/certifi/certifi |
| **charset-normalizer** | Rilevamento codifica testo | MIT | https://github.com/Ousret/charset_normalizer |
| **idna** | Supporto domini internazionalizzati | BSD-3-Clause | https://github.com/kjd/idna |
| **PyInstaller** | Packaging eseguibile Windows (solo in fase di build) | GPL-2.0-or-later (con eccezione bootloader per binari) | https://pyinstaller.org |

> **Nota LGPL-3.0 (PySide6 / Qt 6):** EasyLauncher utilizza PySide6 collegato dinamicamente nella distribuzione `--onedir` (`_internal/PySide6/`). Gli utenti possono sostituire o aggiornare le librerie condivise Qt/PySide6 conformemente ai termini della licenza LGPL v3.

---

## 2. Icone e Risorse Grafiche (`icons/`)

- **Icona Applicazione (`app_icon.png`, `app_icon.ico`, `splash_logo.png`) e Icone UI Programmatiche:**  
  Create appositamente per EasyLauncher (inclusi `make_white_gamepad_icon` e `make_star_icon` in `ui_main.py`) e rilasciate con il progetto sotto licenza **MIT**.
- **Icone Piattaforme Console (`icons/*.png`):**  
  Le icone delle console presenti nella cartella `icons/` sono incluse esclusivamente a scopo identificativo dell'interfaccia. Tutti i nomi di piattaforme, marchi registrati e loghi raffigurati appartengono ai rispettivi proprietari (Nintendo, Sony, Sega, Microsoft, Atari, SNK, Commodore, NEC, ecc.) e non sono coperti dalla licenza MIT di EasyLauncher.
- **Icone di Sistema / Emoji Unicode:**  
  Le icone testuali nei menu e nei pulsanti utilizzano glifi standard di sistema / Unicode.

---

## 3. Fonti Metadati, Copertine e Screenshot

EasyLauncher non distribuisce ROM, BIOS o immagini protette da copyright nel pacchetto di rilascio.  
Tutte le copertine, i loghi, le descrizioni e gli screenshot vengono scaricati dall'utente on-demand dalle seguenti fonti pubbliche o API esterne, nel rispetto dei relativi termini di servizio:

- **Libretro Thumbnails** (https://thumbnails.libretro.com) — immagini e thumbnail open-source della community Libretro.
- **LaunchBox GamesDB** (https://gamesdb.launchbox-app.com) — database pubblico di metadati e media.
- **ArcadeDB / ArcadeItalia** (http://adb.arcadeitalia.net) — metadati arcade/MAME.
- **IGDB** (https://www.igdb.com) — API Twitch/IGDB (richiede chiavi utente gratuite).
- **ScreenScraper** (https://www.screenscraper.fr) — API community retro (richiede account utente).
- **TheGamesDB** (https://thegamesdb.net) & **SteamGridDB** (https://www.steamgriddb.com) — API opzionali con chiave utente.
- **Steam / GOG / Wikipedia / Wikidata / Nintendo** — fallback pubblici per metadati e descrizioni.
