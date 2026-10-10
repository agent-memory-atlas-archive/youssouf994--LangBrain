"""
Registro degli accessi di uvicorn senza il rumore.

Le pagine interrogano l'API di continuo (`GET /events/stats`, `/graph/stats`...) e uvicorn scrive una riga INFO per
ogni richiesta. Qui le richieste riuscite (stato inferiore a 400) non vengono scritte; errori e rifiuti (401, 403, 404, 422,
500...) restano visibili. `LANGBRAIN_ACCESS_LOG=1` ripristina il registro completo. Funziona comunque si avvii il server
(`python -m uvicorn`, `python -m app.serve`, Docker).
"""

import logging
import os

NOME_LOGGER = "uvicorn.access"


def accessi_completi() -> bool:
    return os.getenv("LANGBRAIN_ACCESS_LOG", "").strip().lower() in ("1", "true", "yes", "on")


class SoloAccessiConProblemi(logging.Filter):
    """Lascia passare solo le richieste con stato 400 o più (o che non si riescono a leggere)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if accessi_completi():
            return True
        try:
            return int(record.args[-1]) >= 400  # uvicorn: (client, metodo, percorso, versione HTTP, stato)
        except (TypeError, ValueError, IndexError):
            return True


def installa_filtro_accessi() -> None:
    logger = logging.getLogger(NOME_LOGGER)
    if not any(isinstance(f, SoloAccessiConProblemi) for f in logger.filters):
        logger.addFilter(SoloAccessiConProblemi())
