"""
Configurazione delle scelte dell'utente (file `configurazione.toml`).

Contiene ciò che è una scelta strutturata dell'utente e non un segreto: l'elenco dei dispositivi con i valori
ammessi per i comandi e la politica di ripiego dei provider LLM. Il `.env` resta per chiavi API e parametri d'ambiente.
Priorità: variabile d'ambiente > configurazione.toml > default nel codice.

Il file viene riletto a caldo quando cambia. Se la nuova versione è errata resta attiva la precedente.
"""

import fnmatch
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.constants import is_control_flag

logger = logging.getLogger(__name__)

PERCORSO_PREDEFINITO = Path(__file__).resolve().parents[2] / "configurazione.toml"

POLITICA_RIFIUTA = "rifiuta"
POLITICA_CONSENTI = "consenti"

# Cosa succede quando scade il timer HITL senza una risposta dell'operatore.
SCADENZA_SISTEMA = "sistema"   # decide il sistema: il Brain valuta con il suo modello, come senza HITL
SCADENZA_RESPINGI = "respingi"  # respinge in automatico, nessuna azione fisica
SCADENZA_UMANO = "umano"        # il grafo resta in pausa e aspetta l'operatore
_ALIAS_SCADENZA = {"nessuna": SCADENZA_UMANO}
TIMER_HITL_PREDEFINITO_SECONDI = 300

# Dove si applica l'HITL: sui nodi del grafo (wrapper), sulle escalation valutate dal Brain, oppure su entrambi.
HITL_LIVELLO_NODI = "nodi"
HITL_LIVELLO_BRAIN = "brain"
HITL_LIVELLO_ENTRAMBI = "entrambi"
TARGET_CRITICI_BRAIN_PREDEFINITI = ["alarm_system", "front_door_lock"]

# Ordine e modelli di ripiego usati se il file manca o non li indica: solo modelli gratuiti o molto economici.
ORDINE_PROVIDER_PREDEFINITO = ["google_studio", "mistral", "openrouter", "local"]
MODELLI_DI_RIPIEGO_PREDEFINITI = {
    "openrouter": ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free"],
    "mistral": ["ministral-3b-latest"],
    "google_studio": ["gemini-flash-latest"],
}


class ErroreConfigurazione(ValueError):
    """Il file di configurazione non è valido."""


@dataclass(frozen=True)
class RegolaDispositivo:
    nome: str
    valori: tuple[str, ...] = ()
    intervallo: tuple[float, float] | None = None
    unita: str = ""
    valore_attivo: str | None = None

    @property
    def senza_vincoli(self) -> bool:
        return not self.valori and self.intervallo is None

    def descrizione(self) -> str:
        parti = []
        if self.valori:
            parti.append(str(list(self.valori)))
        if self.intervallo is not None:
            minimo, massimo = self.intervallo
            parti.append(f"numero tra {minimo:g} e {massimo:g}{(' ' + self.unita) if self.unita else ''}")
        return " oppure ".join(parti) if parti else "qualsiasi valore"


@dataclass(frozen=True)
class EsitoValidazione:
    ammesso: bool
    valore: Any = None
    """Valore da inviare al dispositivo (forma canonica del valore accettato)."""
    motivo: str = ""


@dataclass
class Configurazione:
    llm_fallback: bool = False
    ordine_provider: list[str] = field(default_factory=lambda: list(ORDINE_PROVIDER_PREDEFINITO))
    modelli_di_ripiego: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in MODELLI_DI_RIPIEGO_PREDEFINITI.items()}
    )
    dispositivi_non_elencati: str = POLITICA_RIFIUTA
    regole: list[RegolaDispositivo] = field(default_factory=list)
    hitl_timer_attivo: bool = False
    hitl_timer_secondi: int = TIMER_HITL_PREDEFINITO_SECONDI
    hitl_azione_alla_scadenza: str = SCADENZA_UMANO
    hitl_livello: str = HITL_LIVELLO_ENTRAMBI
    hitl_target_critici_brain: list[str] = field(default_factory=lambda: list(TARGET_CRITICI_BRAIN_PREDEFINITI))
    demo_conflitto_all_avvio: bool = False
    demo_pagina_web: bool = True

    # ------------------------------------------------------------------ dispositivi

    def regola_per(self, dispositivo: str) -> RegolaDispositivo | None:
        """Regola del dispositivo: prima il nome esatto, poi il primo pattern che corrisponde."""
        for regola in self.regole:
            if regola.nome == dispositivo:
                return regola
        for regola in self.regole:
            if any(c in regola.nome for c in "*?[") and fnmatch.fnmatchcase(dispositivo, regola.nome):
                return regola
        return None

    def dispositivo_ammesso(self, dispositivo: str) -> bool:
        """True se il dispositivo è elencato oppure la politica consente quelli non elencati."""
        return self.regola_per(dispositivo) is not None or self.dispositivi_non_elencati == POLITICA_CONSENTI

    def valida_comando(self, dispositivo: str, valore: Any) -> EsitoValidazione:
        """Verifica che `valore` sia un comando ammesso per `dispositivo` e ne restituisce la forma canonica."""
        if valore is None or str(valore).strip() == "":
            return EsitoValidazione(False, motivo=f"Valore mancante per il dispositivo '{dispositivo}'.")
        testo = str(valore).strip()
        if is_control_flag(testo):
            return EsitoValidazione(True, valore)

        regola = self.regola_per(dispositivo)
        if regola is None:
            if self.dispositivi_non_elencati == POLITICA_CONSENTI:
                return EsitoValidazione(True, valore)
            return EsitoValidazione(
                False, motivo=f"Il dispositivo '{dispositivo}' non è elencato in configurazione.toml: comando non ammesso."
            )
        if regola.senza_vincoli:
            return EsitoValidazione(True, valore)

        for ammesso in regola.valori:
            if ammesso.casefold() == testo.casefold():
                return EsitoValidazione(True, ammesso)
        if regola.intervallo is not None:
            unita = re.escape(regola.unita) if regola.unita else ""
            trovato = re.fullmatch(rf"\s*(-?\d+(?:[.,]\d+)?)\s*(?:{unita})?\s*" if unita else r"\s*(-?\d+(?:[.,]\d+)?)\s*", testo, re.IGNORECASE)
            if trovato:
                numero = float(trovato.group(1).replace(",", "."))
                if regola.intervallo[0] <= numero <= regola.intervallo[1]:
                    return EsitoValidazione(True, valore)
        return EsitoValidazione(
            False, motivo=f"Valore '{testo}' non ammesso per '{dispositivo}': ammessi {regola.descrizione()}."
        )

    def valore_attivo(self, dispositivo: str, default: str = "ON") -> str:
        """Valore da inviare quando un agente decide genericamente di attivare il dispositivo."""
        regola = self.regola_per(dispositivo)
        return regola.valore_attivo if regola and regola.valore_attivo else default

    def descrizione_valori(self, dispositivo: str) -> str | None:
        """Testo dei valori ammessi per guidare l'LLM, oppure None se il dispositivo non è elencato o non ha vincoli."""
        regola = self.regola_per(dispositivo)
        return None if regola is None or regola.senza_vincoli else regola.descrizione()


# --------------------------------------------------------------------------- lettura del file


def _regola_da_dati(nome: str, dati: Any) -> RegolaDispositivo:
    if not isinstance(dati, dict):
        raise ErroreConfigurazione(f"[dispositivi.{nome}] deve essere una sezione.")
    valori = dati.get("valori", [])
    if not isinstance(valori, list) or not all(isinstance(v, str) and v.strip() for v in valori):
        raise ErroreConfigurazione(f"[dispositivi.{nome}] 'valori' deve essere una lista di stringhe non vuote.")
    intervallo = dati.get("intervallo")
    if intervallo is not None:
        if (
            not isinstance(intervallo, list) or len(intervallo) != 2
            or not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in intervallo)
            or intervallo[0] > intervallo[1]
        ):
            raise ErroreConfigurazione(f"[dispositivi.{nome}] 'intervallo' deve essere [minimo, massimo] numerico.")
        intervallo = (float(intervallo[0]), float(intervallo[1]))
    regola = RegolaDispositivo(
        nome=nome,
        valori=tuple(valori),
        intervallo=intervallo,
        unita=str(dati.get("unita", "")),
        valore_attivo=dati.get("valore_attivo"),
    )
    return regola


def _interruttore(sezione: str, nome: str, valore: Any) -> bool:
    """Legge una voce 0/1 (accetta anche true/false)."""
    if isinstance(valore, bool):
        return valore
    if valore in (0, 1) and isinstance(valore, int):
        return bool(valore)
    raise ErroreConfigurazione(f"[{sezione}] '{nome}' deve essere 0 o 1.")


def costruisci_configurazione(dati: dict[str, Any]) -> Configurazione:
    """Costruisce e valida la configurazione a partire dal contenuto del file TOML."""
    llm = dati.get("llm", {})
    if not isinstance(llm.get("fallback", False), bool):
        raise ErroreConfigurazione("[llm] 'fallback' deve essere true o false.")
    ordine = llm.get("ordine_provider", ORDINE_PROVIDER_PREDEFINITO)
    if not isinstance(ordine, list) or not all(isinstance(p, str) for p in ordine):
        raise ErroreConfigurazione("[llm] 'ordine_provider' deve essere una lista di nomi di provider.")
    ripiego = {k: list(v) for k, v in MODELLI_DI_RIPIEGO_PREDEFINITI.items()}
    for provider, modelli in llm.get("modelli_di_ripiego", {}).items():
        if not isinstance(modelli, list) or not all(isinstance(m, str) for m in modelli):
            raise ErroreConfigurazione(f"[llm.modelli_di_ripiego] '{provider}' deve essere una lista di nomi di modelli.")
        ripiego[provider] = modelli

    politica = dati.get("politica", {}).get("dispositivi_non_elencati", POLITICA_RIFIUTA)
    if politica not in (POLITICA_RIFIUTA, POLITICA_CONSENTI):
        raise ErroreConfigurazione(
            f"[politica] 'dispositivi_non_elencati' deve essere \"{POLITICA_RIFIUTA}\" o \"{POLITICA_CONSENTI}\"."
        )

    hitl = dati.get("hitl", {})
    durata = hitl.get("timer_predefinito_secondi", TIMER_HITL_PREDEFINITO_SECONDI)
    if not isinstance(durata, int) or isinstance(durata, bool) or durata <= 0:
        raise ErroreConfigurazione("[hitl] 'timer_predefinito_secondi' deve essere un intero maggiore di zero.")
    azione = _ALIAS_SCADENZA.get(hitl.get("azione_alla_scadenza", SCADENZA_UMANO), hitl.get("azione_alla_scadenza", SCADENZA_UMANO))
    if azione not in (SCADENZA_SISTEMA, SCADENZA_RESPINGI, SCADENZA_UMANO):
        raise ErroreConfigurazione(
            f"[hitl] 'azione_alla_scadenza' deve essere \"{SCADENZA_SISTEMA}\", \"{SCADENZA_RESPINGI}\" o \"{SCADENZA_UMANO}\"."
        )
    livello = hitl.get("livello", HITL_LIVELLO_ENTRAMBI)
    if livello not in (HITL_LIVELLO_NODI, HITL_LIVELLO_BRAIN, HITL_LIVELLO_ENTRAMBI):
        raise ErroreConfigurazione(
            f"[hitl] 'livello' deve essere \"{HITL_LIVELLO_NODI}\", \"{HITL_LIVELLO_BRAIN}\" o \"{HITL_LIVELLO_ENTRAMBI}\"."
        )
    critici = hitl.get("target_critici_brain", TARGET_CRITICI_BRAIN_PREDEFINITI)
    if not isinstance(critici, list) or not all(isinstance(t, str) and t for t in critici):
        raise ErroreConfigurazione("[hitl] 'target_critici_brain' deve essere una lista di nomi di dispositivi.")

    configurazione = Configurazione(
        hitl_timer_attivo=_interruttore("hitl", "timer_attivo", hitl.get("timer_attivo", 0)),
        hitl_timer_secondi=durata,
        hitl_azione_alla_scadenza=azione,
        hitl_livello=livello,
        hitl_target_critici_brain=list(critici),
        demo_conflitto_all_avvio=_interruttore("demo", "conflitto_all_avvio", dati.get("demo", {}).get("conflitto_all_avvio", 0)),
        demo_pagina_web=_interruttore("demo", "pagina_web", dati.get("demo", {}).get("pagina_web", 1)),
        llm_fallback=llm.get("fallback", False),
        ordine_provider=ordine,
        modelli_di_ripiego=ripiego,
        dispositivi_non_elencati=politica,
        regole=[_regola_da_dati(nome, sezione) for nome, sezione in dati.get("dispositivi", {}).items()],
    )
    # Il valore di attivazione deve essere a sua volta un comando ammesso dal dispositivo.
    for regola in configurazione.regole:
        if regola.valore_attivo is not None and not configurazione.valida_comando(regola.nome, regola.valore_attivo).ammesso:
            raise ErroreConfigurazione(
                f"[dispositivi.{regola.nome}] 'valore_attivo' = '{regola.valore_attivo}' non rientra nei valori ammessi "
                f"({regola.descrizione()})."
            )
    return configurazione


def carica_configurazione(percorso: str | os.PathLike | None = None) -> Configurazione:
    """
    Legge il file di configurazione. Se il file non esiste usa i default con la politica "consenti" (comportamento
    precedente) e un avviso: senza elenco esplicito non c'è nulla da rifiutare.
    Solleva ErroreConfigurazione se il file esiste ma non è valido.
    """
    percorso = Path(percorso or os.getenv("LANGBRAIN_CONFIG") or PERCORSO_PREDEFINITO)
    if not percorso.exists():
        logger.warning("[Configurazione] File %s non trovato: uso i default, dispositivi non elencati consentiti.", percorso)
        return Configurazione(dispositivi_non_elencati=POLITICA_CONSENTI)
    try:
        with open(percorso, "rb") as f:
            return costruisci_configurazione(tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        raise ErroreConfigurazione(f"{percorso.name}: TOML non valido ({e}).") from e


# --------------------------------------------------------------------------- istanza condivisa con ricarica a caldo

_configurazione: Configurazione | None = None
_percorso_letto: Path | None = None
_mtime_letto: int | None = None


def _mtime(percorso: Path) -> int | None:
    try:
        return percorso.stat().st_mtime_ns
    except OSError:
        return None


def get_configurazione() -> Configurazione:
    """Configurazione corrente; se il file è cambiato dall'ultima lettura viene riletto (errori: resta la precedente)."""
    global _configurazione, _percorso_letto, _mtime_letto
    percorso = Path(os.getenv("LANGBRAIN_CONFIG") or PERCORSO_PREDEFINITO)
    mtime = _mtime(percorso)
    if _configurazione is not None and percorso == _percorso_letto and mtime == _mtime_letto:
        return _configurazione
    try:
        nuova = carica_configurazione(percorso)
    except ErroreConfigurazione as e:
        if _configurazione is None:
            raise
        logger.error("[Configurazione] File modificato ma non valido, resta attiva la versione precedente: %s", e)
        _mtime_letto = mtime
        return _configurazione
    if _configurazione is not None:
        logger.info("[Configurazione] File %s ricaricato.", percorso.name)
    _configurazione, _percorso_letto, _mtime_letto = nuova, percorso, mtime
    return nuova


def imposta_configurazione(configurazione: Configurazione | None) -> None:
    """Sostituisce la configurazione condivisa (test). None forza una nuova lettura dal file."""
    global _configurazione, _percorso_letto, _mtime_letto
    _configurazione = configurazione
    _percorso_letto = Path(os.getenv("LANGBRAIN_CONFIG") or PERCORSO_PREDEFINITO) if configurazione else None
    _mtime_letto = _mtime(_percorso_letto) if configurazione else None
