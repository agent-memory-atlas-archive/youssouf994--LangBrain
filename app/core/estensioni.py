"""
Estensioni: come un nodo (un caso d'uso) si aggancia al core senza che il core lo conosca.

Un modulo messo in `app/nodes/` (pacchetto vuoto nel core) che espone `ESTENSIONE` viene caricato all'avvio
dell'API. L'estensione dichiara tutto ciò che porta con sé:
  - rotte e pagine (`registra_rotte(app)`), con il ruolo minimo di ognuna (`permessi`) e le pagine pubbliche;
  - voci della barra di navigazione (`nav`);
  - compiti in background legati al ciclo di vita dell'API (`compiti`, `chiusure`);
  - prompt propri (`moduli_prompt`, `alias_prompt`);
  - testi delle pagine (`testi`: cartella con `<lingua>.json`, sezioni unite a quelle del core);
  - file e campi della pagina delle impostazioni (`sorgenti`, `campi`).

Il core non importa mai un'estensione per nome: se `app/nodes/` non contiene nulla l'API funziona come framework puro.
"""

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoceNav:
    pagina: str        # identificatore usato da `<!--NAV:pagina-->`
    href: str
    chiave: str        # chiave del testo (sezione `common` dei file di testi)
    ordine: int = 50   # il core usa 100 per le impostazioni: sotto 100 viene prima


@dataclass(frozen=True)
class SorgenteImpostazioni:
    """Un file di configurazione in più che la pagina delle impostazioni sa modificare (es. il TOML di un nodo)."""
    id: str                                   # "mynode": prefisso degli id dei campi ("mynode:limit")
    percorso: Callable[[], Path]
    sezione: str                              # intestazione TOML che contiene le voci (file piatto ammesso)
    valida: Callable[[Path], None]            # carica il file col caricatore vero: solleva ValueError se non è valido
    vista: str = "config"                     # pagina che mostra il file come scheda: "config" o il nome di una pagina del nodo
    riavvio: bool = True                      # i campi del file si leggono solo all'avvio


@dataclass(frozen=True)
class Estensione:
    nome: str
    registra_rotte: Callable[[Any], None] | None = None
    permessi: dict[tuple[str, str], Any] = field(default_factory=dict)                  # (metodo, percorso) -> Ruolo
    rotte_pubbliche: frozenset[tuple[str, str]] = frozenset()
    nav: tuple[VoceNav, ...] = ()
    compiti: tuple[Callable[[], Awaitable[None]], ...] = ()                              # funzioni async lanciate in background
    chiusure: tuple[Callable[[], Awaitable[None]], ...] = ()                             # funzioni async chiamate allo spegnimento
    moduli_prompt: tuple[str, ...] = ()
    alias_prompt: dict[str, str] = field(default_factory=dict)                           # nome inglese -> nome italiano nel TOML
    testi: Path | None = None
    sorgenti: tuple[SorgenteImpostazioni, ...] = ()
    campi: tuple[Any, ...] = ()                                                          # settings_editor.Field


_caricate: list[Estensione] | None = None


def _moduli_estensione() -> list[str]:
    try:
        import app.nodes as pacchetto
    except ImportError:
        return []
    return sorted(f"{pacchetto.__name__}.{m.name}" for m in pkgutil.iter_modules(pacchetto.__path__) if not m.name.startswith("_"))


def carica_estensioni() -> list[Estensione]:
    """Importa le estensioni (una volta) e ne applica ruoli e prompt. Un'estensione rotta non ferma l'API."""
    global _caricate
    if _caricate is not None:
        return _caricate
    _caricate = []
    from app.core import ruoli

    for nome in _moduli_estensione():
        try:
            estensione = importlib.import_module(nome).ESTENSIONE
        except Exception:
            logger.exception("Estensione %s non caricata.", nome)
            continue
        ruoli.PERMESSI.update(estensione.permessi)
        ruoli.ROTTE_PUBBLICHE.update(estensione.rotte_pubbliche)
        _caricate.append(estensione)
    return _caricate


def estensioni() -> list[Estensione]:
    return carica_estensioni()


def voci_nav() -> list[VoceNav]:
    return sorted((v for e in estensioni() for v in e.nav), key=lambda v: v.ordine)


def cartelle_testi() -> list[Path]:
    return [e.testi for e in estensioni() if e.testi is not None]
