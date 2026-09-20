"""
Avvia LangBrain con uno scenario di prova e la pagina che mostra il grafo in azione.

Uso:
    python examples/avvia_demo.py [--porta 8765] [--scenario conflitto_porta] [--db demo.db] [--senza-browser]
                                  [--modello AGENTE=PROVIDER[:MODELLO]]...

Crea (azzerandolo) un database dedicato, `demo.db`, quindi i tuoi dati non vengono toccati, ci mette lo scenario e
avvia il server su http://127.0.0.1:<porta>/demo. Serve almeno un provider LLM configurato nel `.env`: ogni nodo del
grafo è una chiamata reale al modello. Ctrl+C ferma il server.
"""

import argparse
import asyncio
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))

SCENARI = ("base", "conflitto_porta", "finestra_aperta")


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
    parser.add_argument("--porta", type=int, default=8765)
    parser.add_argument("--scenario", default="conflitto_porta", choices=SCENARI)
    parser.add_argument("--db", default=str(RADICE / "demo.db"))
    parser.add_argument("--modello", action="append", default=[], metavar="AGENTE=PROVIDER[:MODELLO]")
    parser.add_argument("--senza-browser", action="store_true")
    argomenti = parser.parse_args()

    # DB_PATH va impostato prima di importare l'applicazione, che lo legge all'importazione; il .env non lo sovrascrive.
    percorso_db = os.path.abspath(argomenti.db)
    os.environ["DB_PATH"] = percorso_db
    from dotenv import load_dotenv

    load_dotenv(RADICE / ".env")

    from app.db.scenario import crea_scenario

    riepilogo = asyncio.run(crea_scenario(percorso_db, argomenti.scenario, leggi_modelli(argomenti.modello)))
    print(f"Database della demo: {percorso_db} (scenario '{argomenti.scenario}', {len(riepilogo['agenti'])} agenti, {riepilogo['eventi']} eventi)")

    chiave = os.getenv("API_KEY_PRIMARIO") or os.getenv("API_KEY") or ""
    indirizzo = f"http://127.0.0.1:{argomenti.porta}/demo"
    print(f"Pagina: {indirizzo}" + ("  (la chiave API del .env viene passata al browser, non stampata)" if chiave else ""))

    if not argomenti.senza_browser:
        def apri() -> None:
            time.sleep(2.5)
            webbrowser.open(indirizzo + (f"#k={chiave}" if chiave else ""))
        threading.Thread(target=apri, daemon=True).start()

    import uvicorn

    uvicorn.run("app.api.main:app", host="127.0.0.1", port=argomenti.porta, log_level="info")


if __name__ == "__main__":
    main()
