"""
Regole di priorità tra attori sui blocchi di un dispositivo.

Un agente non può agire su un target se un altro attore vi ha imposto un blocco attivo e con priorità
STRETTAMENTE maggiore: a parità di peso il blocco non prevale. Il Brain ha priorità massima per costruzione.
Se il peso dell'attore che ha imposto il blocco non è noto (operatore umano, sistema esterno, agente non più
istanziato) il blocco prevale per prudenza.

I pesi noti sono quelli degli agenti istanziati nel processo (ogni `BaseAgent` si registra alla creazione),
quindi valgono anche per gli agenti nativi che non compaiono nel registro su DB.
"""

from typing import Any

from app.core.constants import is_control_flag, is_flag_expired

NOME_BRAIN = "Brain"
PRIORITA_BRAIN = float("inf")

# Azioni che rappresentano di per sé un blocco, indipendentemente dal valore scritto.
AZIONI_DI_BLOCCO = ("FORCE_SHUTDOWN", "SECURITY_LOCK")

# Eventi già chiusi: non bloccano più nulla.
_PREFISSI_EVENTO_CHIUSO = ("EXPIRED_", "RESOLVED_", "UNBLOCKED", "RECONCILED_")

_priorita_note: dict[str, float] = {}


def registra_priorita(nome: str, peso: float) -> None:
    """Memorizza il peso di un agente. Una nuova registrazione con lo stesso nome sostituisce la precedente."""
    _priorita_note[nome] = float(peso)


def priorita_attore(attore: str) -> float | None:
    """Restituisce il peso dell'attore, oppure None se non è noto."""
    if attore == NOME_BRAIN:
        return PRIORITA_BRAIN
    return _priorita_note.get(attore)


def trova_blocco_prevalente(
    eventi: list[dict[str, Any]],
    target: str,
    richiedente: str,
    priorita_richiedente: float,
    ttl_minuti: int,
) -> str | None:
    """
    Cerca tra gli eventi un blocco attivo su `target` imposto da un altro attore con priorità maggiore
    di `priorita_richiedente` (o sconosciuta). Restituisce il motivo del rifiuto, oppure None se l'azione è consentita.
    """
    for evento in eventi:
        if evento.get("target") != target:
            continue
        azione = str(evento.get("action", ""))
        if azione.startswith(_PREFISSI_EVENTO_CHIUSO):
            continue
        if is_flag_expired(str(evento.get("timestamp", "")), ttl_minutes=ttl_minuti):
            continue

        attore = str(evento.get("actor", ""))
        nuovo_valore = str(evento.get("new_value", "")).upper()
        if not attore or attore == richiedente:
            continue
        if not (azione in AZIONI_DI_BLOCCO or is_control_flag(nuovo_valore) or nuovo_valore == "OFF"):
            continue

        peso = priorita_attore(attore)
        if peso is None:
            return (
                f"Dispositivo '{target}' bloccato da '{attore}' con azione '{azione}' ({nuovo_valore}): "
                "priorità dell'attore sconosciuta, il blocco prevale"
            )
        if peso > float(priorita_richiedente):
            return f"Dispositivo '{target}' bloccato da '{attore}' (priorità {peso} > {priorita_richiedente})"
    return None
