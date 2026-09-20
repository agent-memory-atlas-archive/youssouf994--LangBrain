#!/usr/bin/env bash
# Scansione di sicurezza di LangBrain: segreti (cronologia git e file da committare) e dipendenze note vulnerabili.
#
# Uso: scripts/scansione_sicurezza.sh
# Richiede: gitleaks (https://github.com/gitleaks/gitleaks/releases) e pip-audit (python -m pip install pip-audit).
# Esce con codice diverso da zero se trova qualcosa o se manca uno strumento.
#
# Cosa controlla:
#   1. il file .env non è tracciato da git ed è ignorato;
#   2. nessun segreto in tutta la cronologia git (tutti i rami): è ciò che un repository pubblico espone;
#   3. nessun segreto nei file che verrebbero committati (tracciati e nuovi non ignorati): il .env reale resta fuori;
#   4. nessuna vulnerabilità nota nelle dipendenze fissate in requirements.lock (database PyPI e OSV).

set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

esito=0
segnala() { echo "  ✗ $1"; esito=1; }
ok() { echo "  ✓ $1"; }

for strumento in gitleaks pip-audit; do
  if ! command -v "$strumento" >/dev/null 2>&1; then
    echo "Manca '$strumento'. Installazione: gitleaks -> release su GitHub o 'brew install gitleaks'; pip-audit -> 'python -m pip install pip-audit'."
    exit 2
  fi
done

echo "[1/4] File .env"
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  segnala ".env è tracciato da git: rimuoverlo con 'git rm --cached .env' e cambiare tutte le chiavi che contiene"
elif [ -f .env ] && ! git check-ignore -q .env; then
  segnala ".env esiste ma non è ignorato da .gitignore"
else
  ok ".env non è tracciato ed è ignorato"
fi

echo "[2/4] Cronologia git (tutti i rami)"
if gitleaks git . --log-opts="--all" --redact --no-banner >/dev/null 2>&1; then
  ok "nessun segreto nei $(git rev-list --all --count) commit"
else
  segnala "segreti nella cronologia: eseguire 'gitleaks git . --log-opts=--all --redact -v' per i dettagli (le chiavi trovate vanno considerate compromesse)"
fi

echo "[3/4] File da committare (senza il .env reale)"
esportazione="$(mktemp -d)"
trap 'rm -rf "$esportazione"' EXIT
git ls-files -co --exclude-standard -z | xargs -0 -I{} cp --parents {} "$esportazione" 2>/dev/null
if gitleaks dir "$esportazione" --redact --no-banner >/dev/null 2>&1; then
  ok "nessun segreto nei file"
else
  segnala "segreti nei file: eseguire 'gitleaks dir . --redact -v' (il .env reale sarà segnalato: è atteso)"
fi

echo "[4/4] Dipendenze note vulnerabili (requirements.lock)"
for servizio in pypi osv; do
  if pip-audit -r requirements.lock --no-deps -s "$servizio" --progress-spinner off >/dev/null 2>&1; then
    ok "nessuna vulnerabilità nota ($servizio)"
  else
    segnala "vulnerabilità o errore ($servizio): eseguire 'pip-audit -r requirements.lock --no-deps -s $servizio'"
  fi
done

echo
[ "$esito" -eq 0 ] && echo "Scansione completata: nessun problema." || echo "Scansione completata: ci sono problemi da risolvere."
exit "$esito"
