"""Routes of the /config page: read and edit the settings files (see app/core/settings_editor.py)."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import riavvio, settings_editor
from app.core.ruoli import Ruolo
from app.core.lingua import scegli


class SaveRequest(BaseModel):
    changes: dict[str, Any]


async def get_fields(request: Request):
    """Every setting with its current value (secrets only as set / not set) and the files they live in."""
    ruolo = getattr(request.state, "ruolo", None)
    return {**settings_editor.describe(), "can_restart": ruolo is not None and ruolo >= Ruolo.PRIMARIO}


async def save_fields(body: SaveRequest):
    """Writes the changes into the files, all or nothing; the previous version of each file is kept as `<file>.bak`."""
    if not body.changes:
        return {"saved": [], "restart": []}
    try:
        return settings_editor.save(body.changes)
    except settings_editor.SettingsError as e:
        message = next(iter(e.errors.values()))
        return JSONResponse(status_code=422, content={"detail": message, "errors": e.errors})


async def restore_backups():
    """Puts back the version of each file from before the last save."""
    restored = settings_editor.restore_backups()
    if not restored:
        return JSONResponse(status_code=404, content={"detail": scegli("Nessuna copia precedente da ripristinare.", "No previous version to restore.")})
    return {"restored": restored}


async def restart_app():
    """Stops the application cleanly and starts it again with the same command (Docker: the container restarts)."""
    return {"restarting": True, "mode": riavvio.programma_riavvio(), "delay": riavvio.RITARDO_SECONDI}


ROUTES = [
    ("GET", "/config/api/fields", get_fields),
    ("POST", "/config/api/save", save_fields),
    ("POST", "/config/api/restore", restore_backups),
    ("POST", "/config/api/restart", restart_app),
]


def register_routes(app: FastAPI) -> None:
    for method, path, handler in ROUTES:
        app.add_api_route(path, handler, methods=[method], tags=["Settings"])
