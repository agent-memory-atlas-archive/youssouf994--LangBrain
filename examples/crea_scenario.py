"""
Azzera un database di LangBrain e vi crea uno scenario di dati riproducibile (vedi app/db/scenario.py).

Uso:
    python examples/crea_scenario.py [--db PERCORSO] [--scenario base|vuoto|conflitto_porta|finestra_aperta]
                                     [--modello AGENTE=PROVIDER[:MODELLO]]... [--senza-backup] [--si]

Senza --db usa il database di DB_PATH (dal .env). Prima di azzerare fa una copia di sicurezza accanto al file
(`<nome>.backup-<data>.db`, ignorata da git), a meno di --senza-backup. Senza --si chiede conferma.

Esempi:
    python examples/crea_scenario.py --scenario base --si
    python examples/crea_scenario.py --scenario conflitto_porta --modello Brain=mistral --modello organ_security=mistral:ministral-8b-latest --si
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))

# Il .env va caricato prima di importare i moduli dell'applicazione: DB_PATH viene letto all'importazione.
from dotenv import load_dotenv

load_dotenv(RADICE / ".env")

from app.db.database import DB_PATH  # noqa: E402
from app.db.scenario import SCENARI, crea_backup, crea_scenario  # noqa: E402


def leggi_modelli(voci: list[str]) -> dict[str, tuple[str, str | None]]:
    modelli = {}
    for voce in voci:
        agente, _, resto = voce.partition("=")
        provider, _, modello = resto.partition(":")
        if not agente or not provider:
            raise SystemExit(f"--modello '{voce}' non valido: usa AGENTE=PROVIDER[:MODELLO]")
        modelli[agente] = (provider, modello or None)
    return modelli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_PATH, help=f"database da azzerare (default: {DB_PATH})")
    parser.add_argument("--scenario", default="base", choices=SCENARI)
    parser.add_argument("--modello", action="append", default=[], metavar="AGENTE=PROVIDER[:MODELLO]")
    parser.add_argument("--senza-backup", action="store_true")
    parser.add_argument("--si", action="store_true", help="non chiedere conferma")
    argomenti = parser.parse_args()

    modelli = leggi_modelli(argomenti.modello)
    percorso = os.path.abspath(argomenti.db)
    print(f"Database: {percorso}\nScenario: {argomenti.scenario}  (TUTTI i dati esistenti verranno cancellati)")
    if not argomenti.si and input("Procedere? [s/N] ").strip().lower() not in ("s", "si", "sì", "y", "yes"):
        raise SystemExit("Annullato.")

    if not argomenti.senza_backup:
        backup = crea_backup(percorso)
        print(f"Backup: {backup}" if backup else "Backup: nessuno (database assente o vuoto)")

    riepilogo = asyncio.run(crea_scenario(percorso, argomenti.scenario, modelli))
    print(f"Fatto: {len(riepilogo['agenti'])} agenti, {riepilogo['eventi']} eventi.")
    if riepilogo["modelli"]:
        print("Modelli per agente:", riepilogo["modelli"])


if __name__ == "__main__":
    main()
