"""
Pagine web nella lingua di chi le apre.

I testi di ogni lingua stanno in `app/static/i18n/<lingua>.json`, una sezione per pagina più `common`; le estensioni
(app/core/estensioni.py) aggiungono i propri file con lo stesso formato, uniti a quelli del core. La lingua è,
in ordine: `?lang=` (ricordata in un cookie), il cookie, l'intestazione `Accept-Language` del browser, `[generale]
lingua` della configurazione. Nel markup `{{chiave}}` diventa il testo (con escape HTML); gli script leggono lo stesso
dizionario dal blocco `<script type="application/json" id="i18n">/*I18N*/</script>`. Così la pagina resta un solo file
senza risorse esterne, compatibile con la Content-Security-Policy.
"""

import html
import json
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.core.configurazione import LINGUE
from app.core.estensioni import VoceNav, cartelle_testi, voci_nav
from app.core.lingua import lingua

CARTELLA_TESTI = Path(__file__).resolve().parent.parent / "static" / "i18n"
COOKIE_LINGUA = "langbrain_lang"
# La pagina è autosufficiente: nessuna risorsa esterna, chiamate solo verso questa stessa API.
INTESTAZIONI_PAGINA = {
    "Content-Security-Policy": "default-src 'none'; img-src data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'",
    "Cache-Control": "no-store",
}
SEGNAPOSTO = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_SEGNAPOSTO_SCRIPT = "/*I18N*/"
_ATTRIBUTO_LINGUA = re.compile(r'<html lang="[a-z-]*">')

# Colori e barra di navigazione comuni a tutte le pagine (static/common.css, tema chiaro/scuro/automatico). Segnaposto:
#   /*COMMON_CSS*/        dentro <style>: i token dei colori e lo stile della barra
#   <!--THEME_INIT-->     in <head>: applica subito il tema scelto, senza lampeggi
#   <!--NAV:pagina-->     nell'intestazione: i link a tutte le pagine (la corrente evidenziata) e il pulsante del tema
#   /*SETTINGS_FORM_JS*/  dentro <script>: il modulo delle impostazioni (static/settings_form.js), comune alle pagine che lo usano
CSS_COMUNE = Path(__file__).resolve().parent.parent / "static" / "common.css"
JS_IMPOSTAZIONI = Path(__file__).resolve().parent.parent / "static" / "settings_form.js"
_INIZIO_TEMA = ('<script>(function(){try{var m=localStorage.getItem("langbrain_theme");'
                'if(m==="light"||m==="dark")document.documentElement.dataset.theme=m}catch(e){}})()</script>')
_PAGINE_NAV = (VoceNav("settings", "/config", "settings", 100),)  # a cui si aggiungono le voci delle estensioni
_NAV = re.compile(r"<!--NAV:([a-z]+)-->")
_SCRIPT_TEMA = (
    '<script>(function(){var b=document.getElementById("theme-toggle"),L=%s,M=["auto","light","dark"];'
    'function get(){try{return localStorage.getItem("langbrain_theme")||"auto"}catch(e){return"auto"}}'
    'function show(m){if(m==="auto")delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=m;'
    'b.textContent=L[m];b.setAttribute("aria-label",L.label+": "+L[m])}'
    'show(get());b.addEventListener("click",function(){var m=M[(M.indexOf(get())+1)%%3];'
    'try{if(m==="auto")localStorage.removeItem("langbrain_theme");else localStorage.setItem("langbrain_theme",m)}catch(e){}show(m)})})();</script>')


def navigazione(corrente: str, dizionario: dict[str, str]) -> str:
    """La barra comune: i link alle pagine (quella aperta evidenziata) e il pulsante del tema."""
    voci = sorted((*voci_nav(), *_PAGINE_NAV), key=lambda v: v.ordine)
    link = "".join(
        f'<a href="{v.href}"{" aria-current=\"page\"" if v.pagina == corrente else ""}>{html.escape(dizionario.get(v.chiave, v.chiave))}</a>'
        for v in voci)
    etichette = {"label": dizionario.get("theme_label", "Theme"), "auto": dizionario.get("theme_auto", "Auto"),
                 "light": dizionario.get("theme_light", "Light"), "dark": dizionario.get("theme_dark", "Dark")}
    codice = json.dumps(etichette, ensure_ascii=False).replace("<", "\u003c")
    return (f'<nav class="topnav">{link}<button type="button" id="theme-toggle"></button></nav>' + (_SCRIPT_TEMA % codice))


def _file_testi(lingua_pagina: str) -> list[dict]:
    """Il file del core e quelli delle estensioni, nell'ordine in cui si sovrappongono."""
    percorsi = [CARTELLA_TESTI, *cartelle_testi()]
    return [json.loads((p / f"{lingua_pagina}.json").read_text(encoding="utf-8")) for p in percorsi if (p / f"{lingua_pagina}.json").is_file()]


def testi(lingua_pagina: str, sezione: str) -> dict[str, str]:
    """I testi comuni e quelli della pagina (core ed estensioni); la pagina prevale su `common` a parità di chiave."""
    file = _file_testi(lingua_pagina)
    if not any(sezione in dati for dati in file):
        raise KeyError(sezione)
    return {**{k: v for dati in file for k, v in dati.get("common", {}).items()},
            **{k: v for dati in file for k, v in dati.get(sezione, {}).items()}}


def _da_accept_language(valore: str) -> str | None:
    scelte = []
    for posizione, parte in enumerate(valore.split(",")):
        codice, _, parametri = parte.strip().partition(";")
        peso = 1.0
        if parametri.strip().startswith("q="):
            try:
                peso = float(parametri.strip()[2:])
            except ValueError:
                peso = 0.0
        scelte.append((-peso, posizione, codice.strip().lower()[:2]))
    for _, _, codice in sorted(scelte):
        if codice in LINGUE:
            return codice
    return None


def lingua_richiesta(request: Request) -> str:
    for scelta in (request.query_params.get("lang"), request.cookies.get(COOKIE_LINGUA)):
        if scelta in LINGUE:
            return scelta
    return _da_accept_language(request.headers.get("accept-language", "")) or lingua()


def componi(sorgente: str, lingua_pagina: str, sezione: str, vista: str | None = None) -> str:
    """`vista`: la scheda delle impostazioni che la pagina mostra (`data-view` su <html>; vuota = quella del core)."""
    dizionario = testi(lingua_pagina, sezione)
    attributo_vista = f' data-view="{html.escape(vista, quote=True)}"' if vista else ""
    pagina = _ATTRIBUTO_LINGUA.sub(f'<html lang="{lingua_pagina}"{attributo_vista}>', sorgente, count=1)
    pagina = SEGNAPOSTO.sub(lambda m: html.escape(dizionario.get(m.group(1), m.group(1))), pagina)
    if "/*COMMON_CSS*/" in pagina:
        pagina = pagina.replace("/*COMMON_CSS*/", CSS_COMUNE.read_text(encoding="utf-8"), 1)
    if "/*SETTINGS_FORM_JS*/" in pagina:
        pagina = pagina.replace("/*SETTINGS_FORM_JS*/", JS_IMPOSTAZIONI.read_text(encoding="utf-8"), 1)
    pagina = pagina.replace("<!--THEME_INIT-->", _INIZIO_TEMA, 1)
    pagina = _NAV.sub(lambda m: navigazione(m.group(1), dizionario), pagina)
    # `<` codificato: nessun testo può chiudere il blocco <script> che contiene il dizionario.
    dati = json.dumps({"lang": lingua_pagina, "t": dizionario}, ensure_ascii=False).replace("<", "\\u003c")
    return pagina.replace(_SEGNAPOSTO_SCRIPT, dati, 1)


def risposta_pagina(percorso: Path, sezione: str, request: Request, intestazioni: dict[str, str], vista: str | None = None) -> HTMLResponse:
    lingua_pagina = lingua_richiesta(request)
    risposta = HTMLResponse(componi(percorso.read_text(encoding="utf-8"), lingua_pagina, sezione, vista),
                            headers={**intestazioni, "Content-Language": lingua_pagina, "Vary": "Accept-Language, Cookie"})
    if request.query_params.get("lang") in LINGUE:
        risposta.set_cookie(COOKIE_LINGUA, lingua_pagina, max_age=365 * 24 * 3600, samesite="lax", httponly=True)
    return risposta
