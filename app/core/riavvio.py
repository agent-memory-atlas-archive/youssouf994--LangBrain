"""
Riavvio dell'applicazione dalla pagina /config (solo amministratore).

Il processo si arresta in modo ordinato (stesso percorso di Ctrl+C: il `lifespan` chiude checkpointer e agente) e ne
riparte uno uguale, con gli stessi argomenti, quando il vecchio è terminato. In Docker non si rilancia nulla: basta
uscire, ci pensa `restart: unless-stopped`. Con `uvicorn --reload` il riavvio spetta al reloader.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

RITARDO_SECONDI = 1.0  # lascia il tempo di consegnare la risposta HTTP alla pagina

# Attende la fine del processo `pid` e rilancia il comando. Gira in un interprete a parte, senza importare l'app.
_ATTENDI_E_RILANCIA = """
import os, subprocess, sys, time
pid = int(sys.argv[1]); comando = sys.argv[2:]
if os.name == "nt":
    import ctypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        k.WaitForSingleObject(h, 60000)
else:
    for _ in range(600):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
time.sleep(0.5)
subprocess.Popen(comando)
"""


def in_docker() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("LANGBRAIN_RESTART") == "exit"


def comando_di_avvio() -> list[str]:
    """Il comando con cui è stato avviato questo processo (interprete incluso)."""
    return list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])


def _avvia_rilancio() -> None:
    opzioni: dict = {"stdin": subprocess.DEVNULL, "cwd": os.getcwd()}
    if os.name == "nt":
        opzioni["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        opzioni["start_new_session"] = True
    subprocess.Popen([sys.executable, "-c", _ATTENDI_E_RILANCIA, str(os.getpid()), *comando_di_avvio()], **opzioni)


async def _riavvia_dopo_ritardo() -> None:
    await asyncio.sleep(RITARDO_SECONDI)
    try:
        if not in_docker():
            _avvia_rilancio()
    except OSError:
        logger.exception("Riavvio annullato: impossibile avviare il nuovo processo.")
        return
    logger.warning("Riavvio dell'applicazione richiesto dalla pagina delle impostazioni.")
    signal.raise_signal(signal.SIGINT)


_compito: asyncio.Task | None = None


def programma_riavvio() -> str:
    """Pianifica il riavvio e torna subito. Restituisce `docker` o `process` (come ripartirà)."""
    global _compito
    _compito = asyncio.get_running_loop().create_task(_riavvia_dopo_ritardo())
    return "docker" if in_docker() else "process"
