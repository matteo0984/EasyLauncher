"""
launcher.py - Avvio dei giochi e gestione dei comandi emulatore.

Non importa nulla di Qt: puo' essere testato e usato indipendentemente dalla UI.
Risolve i percorsi relativi rispetto alla radice portabile, sostituisce i
segnaposto nella riga di comando (es. "{rom_path}") e avvia l'emulatore
tramite subprocess.Popen impostando la cartella di lavoro corretta.
"""
from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Optional

from database import Database


@dataclass
class LaunchResult:
    """Esito del tentativo di avvio di un gioco."""
    ok: bool
    message: str
    command: list[str]
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None


def build_command(exe_path: str, args_template: str, rom_path: str) -> list[str]:
    """Costruisce la lista argv per subprocess.Popen.

    Supporta i segnaposto:
      - {rom_path} / {rom} : percorso completo della ROM
      - {rom_dir}          : cartella della ROM
      - {rom_name}         : nome file della ROM con estensione
      - {rom_stem}         : nome file della ROM senza estensione

    Se nessun segnaposto della ROM e' presente negli argomenti, il percorso
    della ROM viene accodato automaticamente alla fine.
    """
    rom_dir = os.path.dirname(rom_path)
    rom_name = os.path.basename(rom_path)
    rom_stem = os.path.splitext(rom_name)[0]

    tokens: list[str] = []
    has_rom_placeholder = False

    if args_template.strip():
        raw_tokens = shlex.split(args_template, posix=False)
        for tok in raw_tokens:
            # Rimuove eventuali doppi apici esterni: Popen su Windows gestisce il quoting.
            if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
                tok = tok[1:-1]
            if "{rom_path}" in tok or "{rom}" in tok:
                has_rom_placeholder = True
            tok = (
                tok.replace("{rom_path}", rom_path)
                .replace("{rom}", rom_path)
                .replace("{rom_dir}", rom_dir)
                .replace("{rom_name}", rom_name)
                .replace("{rom_stem}", rom_stem)
            )
            tokens.append(tok)

    if not has_rom_placeholder:
        tokens.append(rom_path)

    return [exe_path] + tokens


def launch_game(db: Database, game_id: int, emulator_override_id: Optional[int] = None) -> LaunchResult:
    """Avvia un gioco dal database usando l'emulatore abbinato (o un override).

    Aggiorna play_count e last_played nel database solo se il processo parte
    senza errori.
    """
    game: Optional[sqlite3.Row] = db.get_game(game_id)
    if game is None:
        return LaunchResult(False, "Gioco non trovato nel database.", [])

    if emulator_override_id is not None:
        row = db.get_emulator(emulator_override_id)
        emu = dict(row) if row is not None else None
    else:
        emu = db.emulator_for_game(game)

    if emu is None:
        return LaunchResult(
            False,
            "Nessun emulatore abbinato a questa piattaforma.\n"
            "Apri Strumenti -> Gestisci Piattaforme per collegarne uno.",
            [],
        )

    exe_path = db.resolve(emu["exe_path"])
    rom_path = db.resolve(game["rom_path"])

    if not exe_path or not os.path.isfile(exe_path):
        return LaunchResult(False, f"Eseguibile emulatore non trovato:\n{exe_path}", [])

    if not rom_path or not os.path.isfile(rom_path):
        return LaunchResult(False, f"File ROM non trovato:\n{rom_path}", [])

    work_dir = db.resolve(emu.get("work_dir", "")) if emu.get("work_dir") else os.path.dirname(exe_path)
    if not os.path.isdir(work_dir):
        work_dir = os.path.dirname(exe_path)

    cmd = build_command(exe_path, emu.get("args", ""), rom_path)

    try:
        # Su Windows subprocess.Popen accetta una lista e gestisce correttamente spazi nei percorsi.
        proc = subprocess.Popen(cmd, cwd=work_dir)
    except OSError as exc:
        return LaunchResult(False, f"Impossibile avviare l'emulatore:\n{exc}", cmd)

    db.record_play(game_id)
    return LaunchResult(True, f"Avviato: {game['title']}", cmd, pid=proc.pid, process=proc)
