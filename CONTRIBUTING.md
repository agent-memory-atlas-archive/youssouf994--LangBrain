# Contribuire a LangBrain

Grazie per l'interesse. Il progetto è in italiano (codice, commenti, messaggi e documentazione); i README hanno anche
la versione inglese.

## Ambiente

Serve Python 3.12 o superiore.

```bash
python -m venv venv
source venv/bin/activate            # su Windows: .\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cp .env.example .env                # poi inserisci almeno una chiave di un provider LLM
```

## Prima di aprire una pull request

1. `python -m pytest tests` deve passare. I test usano database temporanei e non chiamano LLM reali: un test nuovo
   non deve dipendere dal tuo `.env` né toccare `langbrain.db`.
2. Una correzione di bug porta un test che fallisce senza la correzione.
3. `bash scripts/scansione_sicurezza.sh` non deve trovare segreti né vulnerabilità (servono `gitleaks` e `pip-audit`).
4. Se cambi comportamento o configurazione, aggiorna `docs/HOW_TO_CUSTOMIZE.md` e, se serve, i README.

## Convenzioni

- Nomi, commenti e messaggi in italiano; commenti solo dove il perché non è ovvio.
- Le scelte dell'utente stanno in `configurazione.toml`, i segreti nel `.env`, mai nel codice. Ordine di precedenza:
  variabile d'ambiente, poi `configurazione.toml`, poi valore predefinito nel codice.
- Un tool o un sotto-agente non solleva eccezioni al padre: restituisce un risultato strutturato
  (`app/core/risultati.py`), così l'errore risale la gerarchia con il suo motivo.
- Le dipendenze hanno versione esatta in `requirements.txt`; per aggiornarle rigenera `requirements.lock` da un ambiente
  pulito e rilancia i test.
- Ogni nuovo endpoint va aggiunto alla matrice dei ruoli in `app/core/ruoli.py` (un test verifica che non ne manchi nessuno).

## Provare le modifiche a mano

`python examples/avvia_demo.py` avvia il server con uno scenario di prova su un database dedicato (`demo.db`) e apre la
pagina che mostra il grafo in azione.
