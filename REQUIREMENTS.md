🇮🇹 Italiano | 🇬🇧 [English](REQUIREMENTS.en.md)

# Progetto: LangBrain

## Obiettivo del Prodotto
Boilerplate LangGraph dimostrativo rivolto a dev/indie hacker generici. Mostra un pattern di agenti gerarchici (orchestratore + sotto-agenti), ma **non è pronto per produzione**. Il caso d'uso concreto è IoT/Smart Home e l'architettura è riadattabile ad altri domini dopo aver implementato sicurezza, persistenza e contratti specifici del dominio.

## Stack Tecnologico
- **Framework Agenti**: LangGraph
- **API Server**: FastAPI (streaming del grafo con Server-Sent Events; WebSocket non implementato)
- **Database**: SQLite per il boilerplate (schema facilmente migrabile a Postgres)
- **Containerizzazione**: Docker & Docker Compose

## Architettura Concettuale

### Cervello (Orchestratore)
- Modello ad alte prestazioni (es. Mistral Large).
- Intervallo di esecuzione ciclico (non reattivo in tempo reale).
- Legge lo storico eventi dal DB ad ogni ciclo.
- Decide aggiustamenti macro basati su pattern storici.
- Ha accesso ai tool di attivazione/regolazione con priorità/ultima parola nel proprio ciclo.

### Sotto-agenti (Per dominio sensoristico)
- Esempi: Clima, Sicurezza, Illuminazione.
- Modello leggero ed economico (es. Mistral Small).
- Ricevono dati mock da sensori (dati simulati/scenari precaricati).
- Decidono in autonomia entro soglie note.
- Escalation al cervello in caso di caso ambiguo o fuori soglia.
- Tool per gestire sensori/attuatori indipendentemente dal cervello.

## Gestione Conflitti (Log Eventi + Reconciliation)
1. **Reading recent events**: Il sotto-agente chiama `get_recent_events(target)` (finestra 5-10 minuti).
2. **Action/Logging**: Se non ci sono conflitti, agisce e scrive `log_event()`.
3. **Escalation**: Se rileva un conflitto (es. azione recente del cervello), fa escalation al cervello.
4. **Reconciliation**: Il cervello nel suo ciclo legge tutti gli eventi recenti ed effettua la reconciliation (conferma, corregge o ignora).

## Human-in-the-Loop (HITL)
Configurabile per nodo, dispositivo o azione tramite il manager dinamico `hitl_config.py` o via API REST (`POST /hitl/config`).
I cicli del grafo inviati tramite `POST /graph/run` accettano un `thread_id` univoco per legare senza discontinuità lo stato di avanzamento (`GET /graph/state?thread_id=...`) e l'eventuale decisione dell'operatore umano (`POST /graph/resume`).

### Tre modalità di decisione HITL (`POST /graph/resume`)
| `decision` | Comportamento |
|---|---|
| `APPROVA` | Il wrapper scrive `RECONCILED_<action>` nel DB e termina il ciclo. |
| `RESPINGI` | Il wrapper scrive `REJECTED_<action>` nel DB, blocca il device, termina il ciclo. |
| `OVERRIDE` | **God Mode Semantico**: il campo `reasoning` viene inviato al MAO con il prompt di Arbitrato Semantico. Il MAO traduce la frase in linguaggio naturale in un array JSON di comandi `{target, action, value}`. Ogni comando viene eseguito via `force_execute_tool` che bypassa deliberatamente i lock di priorità (`check_priority_lock`) e registra l'azione con `actor: "Brain_Override"` nel DB. |

### Tool On-Demand
I dispositivi non pre-registrati vengono creati automaticamente come `IoTDeviceTool` con stato `OFF` al primo accesso tramite `GET/POST /tools/{device_id}` o durante l'esecuzione di un Override semantico.

### Timeout MAO e contratti di dominio

- Il MAO usa client asincroni e un timeout HTTP configurato da `MAO_TIMEOUT_SECONDS` (default: `40` secondi).
- Il MAO usa client asincroni e un timeout HTTP configurato da `MAO_TIMEOUT_SECONDS` (default: `40` secondi).

Nota operativa (2026-09-04):
- È stata aggiunta una variabile di ambiente di sviluppo `MAO_ENABLE_MOCK` (valore `1`) che abilita una risposta mock del MAO per test locali senza credenziali LLM.
- Il progetto include un checkpointer persistente su SQLite (`AsyncSqliteSaver`) in `app/checkpointer.py`, sullo stesso file del DB applicativo (tabelle `checkpoints` e `writes`). Thread e interrupt HITL pendenti sopravvivono a riavvii e ricompilazioni; per un deployment multi-worker resta necessario un backend condiviso come Postgres/Redis.
- `LOCAL_MODEL_BASE_URL` configura l'accesso locale; `LOCAL_MODEL_DOCKER_BASE_URL` configura l'endpoint visto dal container. Su una macchina LAN possono coincidere.
- `LOCAL_MODEL` deve corrispondere esattamente a un ID restituito dall'endpoint OpenAI-compatible `/v1/models`.
- La semantica e la normalizzazione di `action`, `old_value` e `new_value` sono contratti del dominio applicativo. Ogni sviluppatore deve validare i valori ammessi nel proprio tool/agente; il core non converte automaticamente azioni simboliche in stati fisici.
- L'health check segnala flag di controllo (`REJECTED`, `BLOCKED`, ecc.) come `MACRO_ADJUSTMENT_REQUIRED`, senza scegliere uno stato fisico sostitutivo.

## Modelli Dati & DB

### DB Schema (SQLite)
- `events`: `event_id` (PK), `actor`, `action`, `target`, `old_value`, `new_value`, `reasoning`, `timestamp`, `escalated` (bool). Indici su `target` e `timestamp`.
- `readings`: `reading_id` (PK), `sensor_id`, `agent_owner`, `value`, `unit`, `timestamp`.
- `agents_registry`: `name` (PK), `level`, `parent_agent_name`, `managed_targets`, `sub_agent_names`, `system_prompt_template`, `user_prompt_template`, `conflict_window_minutes`, `priority_weight`.

### Shared State (`GraphState`)
- `readings`: Letture correnti dei sensori.
- `recent_events`: Finestra recente di eventi letti dal DB.
- `pending_escalations`: Lista delle escalation pendenti.
- `hitl_required`: Flag/Stato per Human-in-the-loop.
- `next_agent`: Nodo di destinazione nel grafo LangGraph (`"brain"`, `"organ_security"`, `"END"`, etc.).
- `config`: Soglie, flag HITL, configurazioni dinamiche.

## Struttura del Progetto

```text
LangBrain/
├── README.md / README.en.md
├── REQUIREMENTS.md / REQUIREMENTS.en.md
├── SECURITY.md                     # Come segnalare vulnerabilità e cosa aspettarsi dalla sicurezza
├── CONTRIBUTING.md                 # Come contribuire
├── LICENCE
├── Dockerfile
├── docker-compose.yml
├── requirements.txt                # Dipendenze dirette, versioni esatte
├── requirements.lock               # Elenco completo fissato (usato dal Dockerfile)
├── requirements-dev.txt            # In più pytest
├── .env.example                    # Template dei segreti (chiavi dei provider e dell'API); il tuo .env resta fuori da Git
├── configurazione.toml             # Scelte dell'utente: dispositivi e valori ammessi, provider LLM, HITL e timer
├── run_loop.py                     # Loop event-driven con produttore di eventi dei sensori
├── .github/workflows/ci.yml        # CI: test su Python 3.12/3.14, scansione segreti e dipendenze
├── app/
│   ├── MAO/
│   │   └── model_access_object.py  # Model Access Object (OpenRouter, Google AI Studio, Mistral, LLM locale)
│   ├── agents/
│   │   ├── base_agent.py           # DNA comune di ogni agente (applica stato, priorità, escalation)
│   │   ├── agent_climate.py        # Agente Clima nativo
│   │   ├── dynamic_agent.py        # Agente dinamico configurabile a runtime (Livelli 1..N)
│   │   ├── agent_registry.py       # Registro gerarchico su SQLite
│   │   └── medical_agents.py       # Agenti fisiologici (Cardiovascolare, Respiratorio)
│   ├── core/
│   │   ├── configurazione.py       # Lettura e validazione di configurazione.toml
│   │   ├── constants.py            # Flag di controllo e TTL
│   │   ├── errori_llm.py           # Errori del modello classificati (token, chiave, limiti) e oscuramento segreti
│   │   ├── modelli_agenti.py       # Modello LLM per singolo agente, con ereditarietà dal padre
│   │   ├── priorita.py             # Regole di priorità dei blocchi
│   │   ├── risultati.py            # Esito strutturato di lettura/attuazione dei tool
│   │   └── ruoli.py                # Ruoli di chi chiama l'API e matrice dei permessi
│   ├── graph/
│   │   ├── orchestrator.py         # Cervello (BrainAgent - Livello 0)
│   │   ├── builder.py              # Builder del grafo LangGraph con wrapper HITL
│   │   ├── hitl_config.py          # Configurazione dinamica HITL
│   │   ├── timer_hitl.py           # Timer di attesa dell'operatore e azione alla scadenza
│   │   └── state.py                # GraphState condiviso
│   ├── tools/
│   │   ├── baseTool.py             # Classe base astratta per i tool
│   │   ├── sensor_tools.py         # Tool smart home simulati e registry condiviso (registra_tool)
│   │   ├── medical_tools.py        # Tool medici (Pacemaker, Ventilatore SpO2, Normalizzatore)
│   │   ├── event_log.py            # Sistema nervoso: audit log degli eventi e sblocchi
│   │   └── tool_wrapper.py         # Attuazione con controllo di priorità e override
│   ├── db/
│   │   ├── database.py             # Schema SQLite (events, readings, ...)
│   │   └── scenario.py             # Scenari di dati riproducibili
│   ├── api/
│   │   └── main.py                 # API REST FastAPI (grafo, streaming, HITL, agenti, tool, eventi)
│   ├── static/
│   │   └── demo_grafo.html         # Pagina "grafo in azione", servita su GET /demo
│   └── checkpointer.py             # Checkpointer LangGraph persistente su SQLite
├── examples/
│   ├── avvia_demo.py               # Server + scenario di prova + pagina web del grafo
│   ├── crea_scenario.py            # Azzera un database e crea uno scenario di prova
│   ├── hierarchical_pattern/
│   │   └── demo_hierarchy.py       # Gerarchia smart home N-livelli da codice
│   └── medical_homeostasis/
│       └── demo_medical_homeostasis.py # Omeostasi fisiologica e risoluzione di patologie
├── scripts/
│   └── scansione_sicurezza.sh      # Scansione di segreti e dipendenze
├── tests/                          # Suite pytest (database temporanei, nessun LLM reale)
└── docs/
    ├── HOW_TO_CUSTOMIZE.md         # Guida alla personalizzazione e mappatura API
    ├── PROJECT_STATUS.md           # Stato del progetto, limiti noti e roadmap
    ├── API_SMOKE_TEST.md           # Smoke test dell'API con curl (Bash)
    └── API_SMOKE_TEST_WINDOWS.ps1  # Smoke test dell'API per PowerShell
```

