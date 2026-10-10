# Customisation guide: LangBrain

🇬🇧 English | 🇮🇹 [Italiano](HOW_TO_CUSTOMIZE.md)

This guide explains how to extend and customise the boilerplate for your IoT architecture or any other hierarchical
domain. Configuration names are given in English; the Italian names of `configurazione.toml` mean the same (see
section 0).

---

## 0. Install and start the server

You need **Python 3.12 or later**. Dependency versions are pinned in `requirements.txt` (direct) and
`requirements.lock` (complete list, used by the Dockerfile); run the tests with `python -m pytest tests`.

### Language

One setting chooses the language of the default prompts of the Brain and the agents, of the API messages and of the
web pages:

```toml
[general]
language = "en"     # or "it"
```

The answers of the model are read in both languages whatever the setting (`DECISION: APPROVE` and
`DECISIONE: APPROVA` mean the same), and so are the decisions of the operator (`APPROVE`/`APPROVA`,
`REJECT`/`RESPINGI`). JSON keys of the API are English identifiers in both languages.

The web pages (`/demo`, `/config` and those of the installed nodes) take their texts from one small file per language,
`app/static/i18n/it.json` and `en.json`, with one section per page. The language of a page is, in order: `?lang=it`
or `?lang=en` in the address (remembered in a cookie; the link at the top of each page switches it), the language of
the browser, then `[general] language`. To change a label, edit the file: the page picks it up at the next load.

### Tests and security scan

- `python -m pytest tests` runs the whole suite on temporary databases and without calling real LLMs: no test can touch
  `langbrain.db` or the database of `.env`. `tests/test_all.py` is the general regression (utilities, database, tools,
  MAO, agents, API, HITL, hierarchy, medical agents); the only test with a real model is off and is enabled with
  `RUN_LLM_TESTS=1 python -m pytest tests/test_all.py -k Reale` (it uses the keys of `.env`).
- `scripts/scansione_sicurezza.sh` looks for secrets and known vulnerabilities before publishing: it checks that `.env`
  is not tracked, runs `gitleaks` on the git history and on the current files (also those not committed yet, without
  `.env`) and `pip-audit` on `requirements.lock`. Exit code 0 if all is clean, 1 if it finds something, 2 if tools are
  missing. It needs [gitleaks](https://github.com/gitleaks/gitleaks) and `pip install pip-audit`.

```bash
bash scripts/scansione_sicurezza.sh
```

### Local start on Windows PowerShell

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt   # pinned dependencies + pytest (to run the tests only: requirements.txt)
Copy-Item .env.example .env
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --reload
```

Configure at least one LLM provider in `.env` (`google_studio`, `openrouter`, `mistral` or `local`, chosen with
`DEFAULT_PROVIDER`) before running cycles that call the agents. Mistral needs `MISTRAL_API_KEY` and, optionally,
`MISTRAL_MODEL` (default `ministral-8b-latest`) and `MISTRAL_BASE_URL`; no Mistral model is free by itself, the free
plan gives monthly credits that consumption is taken from. Interactive documentation is at
`http://127.0.0.1:8000/docs`.

For an OpenAI-compatible model started directly on Windows use, for example,
`LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1`. If LangBrain runs in Docker and the model is on the Windows host, set
`LOCAL_MODEL_DOCKER_BASE_URL=http://host.docker.internal:8080/v1`; if the model is on another machine of the LAN, use
the same LAN URL in both variables. `LOCAL_MODEL` must be the exact ID returned by `GET <base_url>/models`. The model
server must already be running and reachable.

The timeout of LLM requests is `40` seconds by default. Change it in `.env`, then restart or recreate the server:

```dotenv
MAO_TIMEOUT_SECONDS=40
```

For development without external providers, `MAO_ENABLE_MOCK=1` makes the `Mao` object return a canned JSON answer,
useful to test `OVERRIDE` flows without LLM keys.

### Tokens, LLM errors and the operator

**Tokens.** The context window is a property of the model (e.g. `qwen/qwen3.8-27b:free` has 262,144 tokens): what you
configure is the **output** token limit of each call. Reasoning models use many tokens before answering, so agent calls
start from `MAO_MAX_TOKENS_MINIMO` (default `8192`) and, if the answer is cut by the limit, the request is repeated
doubling the tokens up to `MAO_MAX_TOKENS_LIMITE` (default `32768`). If it is still cut it is an error
(`RISPOSTA_TRONCATA`), never a partial answer used as a decision.

**Errors.** When the model cannot be used the graph **does not go on with invented decisions**: the error goes up with
a code and a hint for the operator, and the graph stops (HITL interrupt `llm_failure_human_intervention`). Every error
is classified:

| Code | Typical cause | What the operator does |
|---|---|---|
| `CREDITI_ESAURITI` | 402 from the provider | Tops up the plan or switches to a free model / another provider |
| `CHIAVE_NON_VALIDA` / `CHIAVE_NON_CONFIGURATA` | 401/403, missing key | Replaces the key in `.env` |
| `LIMITE_RICHIESTE` | 429 (also upstream on `:free` models) | Waits, uses an own key or changes model |
| `MODELLO_NON_TROVATO` | 404 | Fixes the model name in `.env` |
| `CONTESTO_SUPERATO` | 400 for context | Chooses a model with a larger context |
| `RISPOSTA_TRONCATA` / `RISPOSTA_VUOTA` | tokens used up by reasoning | Raises `MAO_MAX_TOKENS_*` or changes model |
| `RISPOSTA_NON_UTILIZZABILE` | the model answered `LLM_ERRORE: ...` or an invalid JSON of commands (override) | Checks the context or changes model |
| `PROVIDER_NON_RAGGIUNGIBILE` / `ERRORE_PROVIDER` | network, timeout, 5xx | Checks the network and the service status |

The codes are stable identifiers; the message and the hint follow the language. The interrupt payload holds `errore`
(`codice`, `messaggio`, `suggerimento`, `provider`, `modello`; API keys are hidden) and a readable `prompt`. Once the
problem is fixed (for example by editing `.env`: **variables changed in the file are reloaded at the next call,
without restarting the server**) the operator resumes with `POST /graph/resume`: `APPROVE` or `RETRY` run the node
again; `REJECT` cancels the cycle without running anything. If the problem persists the graph stops again. Endpoints
that call the model directly (`/llm/invoke`, `/graph/health-check`) answer `503` with the same detail.

**No silent fallback.** By default (`fallback = false` in the configuration, or `MAO_FALLBACK=0`) an error of the
chosen provider or model does not silently try other models or providers, which might be paid: the operator decides.
With `MAO_FALLBACK=1` (the environment variable prevails over the file) or `fallback = true` the request goes on with
the providers listed in `provider_order` and, within each, with the `fallback_models` (only free or very cheap ones,
chosen by you). Errors concerning a whole provider (wrong key, no credits, unreachable provider) move to the next
provider at once without trying its other models. Each time the fallback triggers, the log reports `RIPIEGO ATTIVO`
with the provider used and the cause.

**Standard answer of the model.** Every system prompt gets the instruction to always answer in the requested format,
choosing the most cautious option when data are scarce, and to use the line `LLM_ERRORE: <reason>` only if it cannot
produce any answer in the format (truncated or incomprehensible request): such an answer is treated as an error and
triggers the same HITL pause. The instruction is restrictive on purpose: with small models a looser wording makes them
declare themselves unable too often.

### The user's choices (`configurazione.toml` or `config.toml`)

`.env` keeps **secrets and environment parameters** (API keys, URLs, database path). **Structured choices** are in
the configuration file: the list of devices with their accepted values, the fallback policy of the LLM providers,
HITL, prompts, language. Priority: environment variable > configuration file > defaults in the code. The file is
reloaded when it changes (if the new version is wrong the previous one stays active and the error is in the log).

The file is `configurazione.toml` (Italian names) or `config.toml` (English names, used when `configurazione.toml`
does not exist); `LANGBRAIN_CONFIG` points to any other path. [`config.example.toml`](../config.example.toml) is the
complete English version, with every setting explained: copy it as `config.toml`. Every section, key and value can be
written in either language; one entry must not use both:

| English | Italian |
|---|---|
| `[general] language` | `[generale] lingua` |
| `[llm] provider_order`, `[llm.fallback_models]` | `ordine_provider`, `[llm.modelli_di_ripiego]` |
| `[hitl] timer_enabled`, `default_timer_seconds`, `on_timeout` (`human`/`system`/`reject`), `level` (`nodes`/`brain`/`both`), `brain_critical_targets` | `timer_attivo`, `timer_predefinito_secondi`, `azione_alla_scadenza` (`umano`/`sistema`/`respingi`), `livello` (`nodi`/`brain`/`entrambi`), `target_critici_brain` |
| `[demo] seed_conflict_at_start`, `web_page` | `conflitto_all_avvio`, `pagina_web` |
| `[policy] unlisted_devices` (`reject`/`allow`) | `[politica] dispositivi_non_elencati` (`rifiuta`/`consenti`) |
| `[effectors.<name>]` (or `[devices.<name>]`) `values`, `range`, `unit`, `active_value` | `[dispositivi.<nome>] valori`, `intervallo`, `unita`, `valore_attivo` |
| `[organism.external_actors.<name>] level`, `priority`, `description` | `[organismo.attori_esterni.<nome>] livello`, `priorita`, `descrizione` |
| `[prompts] brain_escalation_user`, `routine_check` | `[prompt] brain_escalation_utente`, `controllo_macro` |
| `[signals] window_hours` (hours of signals kept in the state of each run, default 4) | `[segnali] finestra_ore` |

**Explicit list of devices.** Every device that can receive commands must be declared:

```toml
[policy]
unlisted_devices = "reject"   # or "allow"

[devices.front_door_lock]
values = ["LOCKED", "UNLOCKED"]
active_value = "LOCKED"         # what an agent sends when it decides to "activate" the device

[devices.ac_living_room]
values = ["OFF", "ON"]
range = [10.0, 35.0]            # for numbers; "22.5°C" and "22.5" are the same
unit = "°C"
active_value = "22.5°C"

[devices."device_l*"]           # pattern with * and ?; the exact name prevails
values = ["ON", "OFF"]
```

The command is validated **wherever** it reaches a device: actuation by agents (`applica_stato`, outcome
`COMMAND_NOT_ALLOWED` and event `INVALID_COMMAND_<action>`, with escalation to the parent), `execute_tool_safely`,
`force_execute_tool` and OVERRIDE (which bypasses priorities but not physical limits: an unlisted device is neither
commanded nor created), and `POST /tools` (`422`). `GET /tools/{id}` and `POST /tools/{id}/fault` answer `404` for an
unlisted device instead of creating it. The accepted value is sent in canonical form (`unlocked` → `UNLOCKED`);
internal flags (`REJECTED`, `RECONCILED_*`, ...) are not physical values and are not validated. The accepted values
are also those shown to the LLM in its prompts. If the file is missing the policy is `allow`, with a warning in the
log.

**Policy of the LLM providers.**

```toml
[llm]
fallback = false                                                      # automatic fallback to other providers/models
provider_order = ["google_studio", "mistral", "openrouter", "local"]  # after the provider chosen with DEFAULT_PROVIDER

[llm.fallback_models]                                                 # only free or very cheap ones
openrouter = ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free"]
mistral = ["ministral-3b-latest"]
```

### Settings page (`/config`) and restart

The files stay the main way to configure LangBrain. When you prefer a form, open `/config`: the page itself is public like
`/demo`, reading the values needs the viewer key and saving needs the admin key (type it at the top of the page).

- It edits the **same files** (`.env`, `configurazione.toml` / `config.toml` and the file of any installed node):
  comments, order and the names you wrote (Italian or English) are kept, and the previous version of each file is saved as
  `<file>.bak` (**Restore previous version** puts it back).
- Nothing is written unless every value is valid and the real loader accepts the new file: all or nothing.
- Secrets (API keys, passwords) are write-only: the page only shows whether they are set.
- A setting that is read only at start is marked **needs restart**. The **Restart app** button (admin key; hidden for the
  other roles) calls `POST /config/api/restart`: the process stops the way Ctrl+C does and starts again with the same
  command. In Docker it only exits and `restart: unless-stopped` brings it back; with `uvicorn --reload` the reloader
  does it. The page waits for the application and reloads by itself.
- API: `GET /config/api/fields` (viewer; it also says `can_restart` for the key that asks), `POST /config/api/save` and
  `POST /config/api/restore` (admin), `POST /config/api/restart` (admin).

### API protection with keys and roles

Every endpoint except `GET /` (used by the Docker healthcheck), the static pages (`GET /demo`, `GET /config` and the pages
of the installed nodes) and the Swagger documentation requires the `X-API-Key` header: `401` if the key is missing or wrong,
`403` if its role is not enough for that endpoint. There are three roles, each with its own key in `.env`. They are
roles of whoever **calls the API** (people or services): they have nothing to do with the powers of the agents, which
are set with `priority_weight` and with the list of devices in the configuration.

| Role | Variable | What it can do |
|---|---|---|
| viewer (`tirocinante`) | `API_KEY_VIEWER` or `API_KEY_TIROCINANTE` | Read only: graph state, tools, events, agents, hierarchy, HITL configuration, statistics |
| operator (`medico_di_guardia`) | `API_KEY_OPERATOR` or `API_KEY_MEDICO_DI_GUARDIA` | Also the ordinary procedures: `POST /graph/run` and `POST /graph/run/stream`, `POST /graph/health-check`, `POST /tools`, `POST /events/unblock`, `DELETE /events/reset-conflicts/{target}` and `POST /graph/resume` (or `/graph/resume/stream`) with `APPROVE`/`REJECT`/`RETRY` |
| admin (`primario`) | `API_KEY_ADMIN`, `API_KEY_PRIMARIO` (or the single key `API_KEY`) | Also critical decisions and configuration: `POST /graph/resume` with `OVERRIDE`, `POST /hitl/config`, `POST /agents/create`, `DELETE /agents/{name}`, `POST /tools/{id}/fault`, `POST /events/seed-conflict`, `POST /llm/invoke`, `DELETE /system/reset` |

Each role includes those below it. The matrix is in one place (`app/core/ruoli.py`, table `PERMESSI`): an unclassified
endpoint requires the admin, and a test checks that every route is classified. `API_KEY` is the historic single key
and counts as admin, so existing configurations keep working. If no key is set the API is open to anyone who reaches
the server (with a warning at start: always set them outside a local development environment); if the key of a role
is missing, that role cannot access and the start reports it.

```bash
curl -H "X-API-Key: $API_KEY_OPERATOR" -X POST http://127.0.0.1:8000/graph/run -H 'Content-Type: application/json' -d '{}'
```

The smoke-test scripts send the header when the environment variable `API_KEY` is defined (so as admin). In Swagger
(`/docs`) use the **Authorize** button.

### Several people at once: workers

`python -m uvicorn app.api.main:app` runs one process. To serve several people at the same time start the API with
more processes (workers) that share the port and the SQLite database:

```bash
python -m app.serve                              # workers: LANGBRAIN_WORKERS, otherwise "auto" (one per CPU, at most 4)
python -m app.serve --workers 4 --host 0.0.0.0 --port 8000
```

With more than one worker (`app/core/multiprocesso.py`):

- **Background tasks run in one process at a time** (a lease on SQLite: if that process dies another takes over within a few
  seconds): the HITL timer sweeper and the tasks of the installed nodes, for example reading a mailbox.
- **What used to live only in the memory of one process is shared:** the runtime HITL configuration (`POST /hitl/config`),
  the value of the simulated effectors, the ON/OFF switches of a node's tools and the compiled graph (an agent created in
  one process is seen by the others at their next request).
- **Operations on the same resource exclude each other across processes** (the HITL operations on one graph thread; a node can
  use `multiprocesso.blocco(name)` for its own resources).
- SQLite runs in WAL mode (readers do not block the writer) with a 30 s wait on a busy file; the startup migrations are
  done by one process at a time.
- **Restart app** from `/config` works with workers: the process that receives the request leaves a file, the supervisor stops
  all the processes and starts again (in Docker it exits and the container policy restarts it).
- With one worker (the default of `python -m uvicorn`) nothing of this touches the database: behaviour and tests are as before.

Limits: the graph of the demo uses one conversation thread (`api_session`), so two people running `POST /graph/run` at the
same time take turns; simulated faults (`POST /effectors/{id}/fault`) belong to the process that received them.

**The access log.** uvicorn writes one INFO line per request, which with pages that poll the API is a flood. Successful
requests (status below 400) are no longer written; errors and refusals still are. `LANGBRAIN_ACCESS_LOG=1` brings back
the full log.

### Start with Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The service is published on `http://127.0.0.1:8000` by default. Change the host port with `LANGBRAIN_PORT` in `.env`.
SQLite is kept in the Docker volume `langbrain_data`. The container starts `python -m app.serve`: `LANGBRAIN_WORKERS`
(default `auto`, at most 4) sets the number of processes, see "Several people at once: workers" above.

To stop the service:

```powershell
docker compose down
```

To also delete the SQLite volume, only when you want to delete the data for good:

```powershell
docker compose down --volumes
```

---

## 1. Hierarchical N-level architecture

The system uses a recursive **parent-child** tree:

$$\text{Brain (level 0)} \longrightarrow \text{Organ (level 1)} \longrightarrow \text{Component of the organ (level 2)} \longrightarrow \text{Sub-component (level N)}$$

### Key principles

- **Brain:** level-0 orchestrator with the highest priority (`1000.0`). It has the global view and reconciles conflicts
  in the end.
- **Organ (e.g. `organ_security`, `organ_climate`):** level-1 agent responsible for a macro-area. It can control tools
  directly or coordinate child components.
- **Component of the organ (e.g. `component_door_lock`, `component_alarm`):** agent of level 2 or more, dedicated to a
  specific device or task.
- **Recursive escalation:** if a component or organ finds a conflict it cannot solve locally, it creates an
  `EscalationItem` for its `parent_agent_name`.
- **Priority (`priority_weight`):** an agent cannot act on a target on which another actor has an active lock with
  **strictly higher** priority (with equal weights the lock does not prevail). The rejected action is recorded as
  `REJECTED_<action>` and, in dynamic agents, becomes an escalation to the parent. The Brain always prevails; an actor
  with unknown weight (e.g. a human operator) prevails to be safe. The rules are in `app/core/priorita.py`.
- **Chain of command:** as in a real organisation, the junior asks the senior and the senior, if they cannot answer,
  asks their own superior, up to the Brain. Routine ends at the low levels; only anomalies reach the Brain.
- **`parent_agent_name` is the only source of truth:** `level` and `sub_agent_names` are always derived from the
  registry. Create top-down (the parent first, then the children); every write checks the whole tree: existing parent,
  no cycles, unique names ignoring case (`brain` and `end` are reserved) and targets shared only between ancestor and
  descendant (two different branches cannot act on the same device). The native agent `agent_climate` does not
  delegate to children and cannot be a parent.

---

### The vocabulary of the body

The framework names its parts after the human body, so the same core fits any domain (a home, a plant, a clinic, an office):

| Part | Meaning | In the code |
|---|---|---|
| **Brain** · **Organ** · **Tissue** · **Cell** | the agents that use a model, by level of the hierarchy (0, 1, 2, 3+) | `nome_livello(n)` in `app/core/constants.py` |
| **Reflex** | a deterministic action: no model, no decision (a rule, a redaction, a conversion) | `app/tools/reflex.py` |
| **Receptor** | turns an outside stimulus into a signal; it may use a model but decides nothing | `Receptor`, a subclass of `Reflex` |
| **Effector** | the thing that acts on the world; `SimulatedEffector` is the in-memory one | `app/tools/effectors.py`, `[effectors.*]`, `/effectors` |
| **Signal** | a reading of a receptor (numeric ones are stored) | table `signals`, field `signals` of `POST /graph/run`, `GET /signals` |
| **Nervous system** | the event log: audit trail, locks, escalations | `NervousSystem` in `app/tools/nervous_system.py` |
| **Motor control** | the validated way a command reaches an effector | `app/tools/motor_control.py` |

The old names keep working as aliases (with a `DeprecationWarning` for the Python modules):

| Old | New |
|---|---|
| `BaseTool` (`app/tools/baseTool.py`) | `Reflex` (`app/tools/reflex.py`) |
| `IoTDeviceTool`, `get_default_iot_tools` (`app/tools/sensor_tools.py`) | `SimulatedEffector`, `default_effectors` (`app/tools/effectors.py`) |
| `EventLog` (`app/tools/event_log.py`) | `NervousSystem` (`app/tools/nervous_system.py`) |
| `execute_tool_safely`, `force_execute_tool` (`app/tools/tool_wrapper.py`) | same functions in `app/tools/motor_control.py` |
| `sensor_readings` in `POST /graph/run`, table `readings` | `signals`, table `signals` |
| `GET/POST /tools`, `/tools/{id}`, `/tools/{id}/fault` | `/effectors`, `/effectors/{id}`, `/effectors/{id}/fault` |
| `[devices.*]` / `[dispositivi.*]`, `unlisted_devices` | `[effectors.*]`, `unlisted_effectors` |
| `app/agents/agent_climate.py`, `medical_agents.py`, `app/tools/medical_tools.py` | `examples/smart_home/`, `examples/medical_homeostasis/` (domain examples live outside the core) |

---

## 2. Complete map of the REST API

Every endpoint of FastAPI ([`app/api/main.py`](../app/api/main.py)). The nodes installed as extensions (section 10) add their own endpoints.

### 📌 1. System and agent graph

#### `GET /`
- **Description:** health check of the API.
- **Response (200):**
  ```json
  { "status": "ok", "version": "2.1.0", "architecture": "Hierarchical N-Level (Brain -> Organs -> Components)" }
  ```

#### `POST /graph/run`
- **Description:** runs one cycle of the LangGraph agent graph for a `thread_id`.
- **Request body:**
  ```json
  {
    "sensor_readings": [
      { "sensor_id": "temp_living_room", "agent_owner": "api", "value": "30.0", "unit": "°C" }
    ],
    "force_next_agent": "brain",
    "thread_id": "test-thread-123"
  }
  ```
- **Response (200):**
  ```json
  {
    "next_agent": "END",
    "last_message": "[agent_climate] AC turned on for ac_living_room: OFF -> 22.5°C.",
    "pending_escalations": []
  }
  ```

#### `POST /graph/run/stream` and `POST /graph/resume/stream`
- **Description:** like `POST /graph/run` and `POST /graph/resume` (same bodies and permissions), but they answer with
  a `text/event-stream`: one event per completed node, so a client can show the progress of the graph instead of
  waiting for the end of the cycle. The thread stays locked for the whole run, as with the non-streaming routes;
  `/graph/resume/stream` answers `409` if no HITL request is waiting and requires the admin for `OVERRIDE`.
- **Format:** `data: {json}` lines separated by an empty line. The field `tipo` is:

  | `tipo` | Content |
  |---|---|
  | `inizio` | `thread_id` |
  | `nodo` | `nodo` run, `prossimo` (who gets the hand, `END` at the end of the cycle), `messaggio` (last message of the node), `escalation_pendenti` |
  | `pausa` | `richiesta`: the interrupt payload (`type`, `prompt`, ...): the graph waits for the operator |
  | `errore` | `codice` and `messaggio` (keys hidden) of an error that stopped the cycle |
  | `fine` | `in_pausa`, `next_agent` and `hitl_timer` (state and seconds left of the timer) |

  The browser `EventSource` cannot send the `X-API-Key` header: use `fetch` and read `response.body` (see
  `app/static/demo_grafo.html`).

#### `GET /graph/state`
- **Description:** the current state of the graph from the checkpointer, with any pending `interrupt`.
- **Query params:** `thread_id` (optional)
- **Response (200):**
  ```json
  {
    "next": ["brain"],
    "values": { "next_agent": "brain", "pending_escalations": [] },
    "tasks": [],
    "is_interrupted": false
  }
  ```

#### `POST /graph/resume`
- **Description:** resumes the graph suspended by a **Human-in-the-Loop (HITL)** interrupt.
- **Request body:**
  ```json
  {
    "decision": "APPROVE",
    "reasoning": "Approved by the user from the dashboard",
    "thread_id": "api_session"
  }
  ```
- **Response (200):**
  ```json
  {
    "status": "resumed",
    "decision_applied": "APPROVE",
    "last_message": "[Brain] Escalation APPROVED for front_door_lock.",
    "next_agent": "END"
  }
  ```

#### `POST /graph/health-check`
- **Description:** runs the routine macro check of the Brain (`check_body_status`).
- **Response (200):**
  ```json
  { "result": "[Brain] Macro Check Completato: STATUS: OK" }
  ```

#### `GET /events/stats`
- **Description:** indicators of the nervous system over `window_hours` (default 24): events by layer (`brain`,
  `organs`, `components`, `other`) and by outcome (`action`, `escalation_open`, `approved_by_brain`,
  `rejected_by_brain`, `escalation_resolved`, `expired`, `unblocked`, `override`, `forced`, `fault_handled`), share of
  cases handled without escalating (`autonomy_rate`), escalations, Brain decisions, most active agents and devices,
  events over time, pending human approval. Used by dashboards (see section 6 for the other statistics).

---

### 🤖 2. Dynamic sub-agents (N levels)

#### `GET /agents`
- **Description:** every sub-agent registered in the system.
- **Response (200):**
  ```json
  {
    "count": 2,
    "agents": [
      { "name": "organ_climate", "level": 1, "parent_agent_name": "Brain", "managed_targets": ["ac_living_room"] }
    ]
  }
  ```

#### `GET /agents/{agent_name}/model` · `PUT /agents/{agent_name}/model` · `DELETE /agents/{agent_name}/model`
- **Description:** the LLM model of one agent (the Brain included, with `Brain` as name). If it is not set the agent
  **inherits the parent's**, then the grandparent's and so on up to the Brain; if nobody sets it, the default provider
  of the system applies (`DEFAULT_PROVIDER` and the model of that provider in `.env`). It applies from the next LLM
  call, without recompiling the graph. `GET` is for the viewer, `PUT`/`DELETE` for the admin.
- **PUT request body:**
  ```json
  { "provider": "mistral", "model": "ministral-8b-latest" }
  ```
  `provider` is one of `google_studio`, `openrouter`, `mistral`, `local` (and must have its key configured, otherwise
  `422`); `model` is optional and, if omitted, the default model of that provider is used. A setting is one unit: the
  model of a parent is never applied to a different provider.
- **Response (200):**
  ```json
  {
    "agent": "component_lights",
    "impostazione": null,
    "effettivo": { "provider": "mistral", "model": "ministral-8b-latest", "abilitato": true, "origine": "organ_home", "ereditato": true }
  }
  ```
  `origine` is the agent the setting comes from (or `predefinito`, the default). `DELETE` removes the own setting: the
  agent inherits again. `GET /agents` reports the same `llm` field for every agent; `POST /agents/create` accepts
  `provider` and `model` in the definition.

#### `GET /agents/hierarchy`
- **Description:** the whole tree: Brain → organs → components.
- **Response (200):**
  ```json
  {
    "root": "Brain",
    "title": "Gerarchia IoT: Cervello -> Organi -> Componenti dell'Organo",
    "tree": {
      "name": "Brain",
      "level": 0,
      "children": [
        {
          "name": "organ_security",
          "level": 1,
          "children": [
            { "name": "component_door_lock", "level": 2, "children": [] }
          ]
        }
      ]
    }
  }
  ```

#### `POST /agents/create`
- **Description:** registers a new organ or component at runtime and recompiles the graph. The parent must already be
  registered; `level` is optional (derived as the parent's level + 1) and, if given, must match. `sub_agent_names` is
  informative only: children are linked by registering them with their own `parent_agent_name`, and a child not yet
  registered cannot be listed in the parent.
- **Request body:**
  ```json
  {
    "agent_definition": "{\"name\": \"organ_security\", \"parent_agent_name\": \"Brain\", \"managed_targets\": [\"alarm_system\"], \"system_prompt_template\": \"You are the security organ...\", \"priority_weight\": 500.0}"
  }
  ```
- **Response (200):**
  ```json
  {
    "status": "registered_and_compiled",
    "agent_name": "organ_security",
    "level": 1,
    "parent_agent_name": "Brain",
    "sub_agent_names": [],
    "managed_targets": ["alarm_system"],
    "graph_node_active": true
  }
  ```
- **Error (422):** inconsistent hierarchy (missing parent, cycle, wrong level, invalid, reserved or duplicate name,
  target shared between different branches). The registry is unchanged and `detail` names the rule broken.

#### `DELETE /agents/{agent_name}`
- **Description:** removes a sub-agent from the registry and recompiles the graph. An agent with registered children
  cannot be deleted: remove the children first (bottom-up).
- **Response (200):**
  ```json
  { "status": "deleted", "agent_name": "component_door_lock" }
  ```
- **Error (404):** agent not found. **Error (409):** the agent still has children; `detail` lists them.

---

### 🛠️ 3. IoT tools (hardware or simulated)

In the vocabulary of the body this family is `/effectors` (`GET/POST /effectors`, `/effectors/{id}`, `/effectors/{id}/fault`); `/tools` keeps working as an alias.

#### `GET /tools`
- **Description:** every registered tool (of the graph and of the process registry) with its current value.
- **Response (200):**
  ```json
  {
    "ac_living_room": { "value": "22.5°C", "unit": "°C" },
    "front_door_lock": { "value": "LOCKED", "unit": "" }
  }
  ```
  A faulty device does not make the list fail: its entry reports `"value": null` plus `error` and `error_type`.

#### `GET /tools/{device_id}`
- **Description:** the current value of one tool.
- **Response (200):**
  ```json
  { "device_id": "ac_living_room", "value": "22.5°C", "unit": "°C" }
  ```
- **Error (502):** the device does not respond. The body is the structured result
  `{ "device_name", "success": false, "response", "error_type" }` (see section 4).

#### `POST /tools`
- **Description:** writes the value of a tool directly (bypassing the agents' decision).
- **Request body:**
  ```json
  { "target": "ac_living_room", "value": "OFF" }
  ```
- **Response (200):**
  ```json
  { "device_id": "ac_living_room", "new_value": "OFF" }
  ```
- **Error (422):** the device is not listed in the configuration or the value is not accepted; `detail` reports the
  accepted values. The accepted value is sent in canonical form.
- **Error (502):** the device refused the command or does not respond; same structured body as `GET /tools/{device_id}`.

#### `POST /tools/{device_id}/fault`
- **Description:** simulates (or removes) a fault of a mock device, to try troubleshooting and escalation without real
  hardware.
- **Request body:**
  ```json
  { "fault": "Zigbee controller timeout", "operations": 1, "commands_only": true }
  ```
  `fault` is the error message (`null` removes the fault); `operations` is how many operations fail before the fault
  goes away (`null` = until it is removed); with `commands_only` reads work and only commands fail (sensor alive,
  actuator stuck).
- **Response (200):**
  ```json
  { "device_id": "living_room_lights", "fault": "Zigbee controller timeout", "operations": 1, "commands_only": true }
  ```

---

### 🗄️ 4. Database, conflicts and event-driven unblock

#### `GET /events`
- **Description:** the events of the audit log in the given window.
- **Query params:** `window_minutes` (default: 240)
- **Response (200):**
  ```json
  { "count": 5, "events": [...] }
  ```

#### `POST /events/seed-conflict`
- **Description:** inserts a conflict event in the DB to simulate an escalation scenario.
- **Request body:**
  ```json
  {
    "actor": "agent_security",
    "action": "FORCE_SHUTDOWN",
    "target": "ac_living_room",
    "old_value": "22.5°C",
    "new_value": "OFF",
    "reasoning": "Conflict simulated through the API"
  }
  ```

#### `DELETE /events/reset-conflicts/{target}`
- **Description:** marks the pending escalation events of the target as resolved (`RESOLVED_`).

#### `POST /events/unblock`
- **Description:** unblocks a device blocked by a flag (`REJECTED`/`BLOCKED`).
- **Request body:**
  ```json
  { "target": "ac_living_room", "reasoning": "Window closed: manual unblock through the API" }
  ```
- **Response (200):**
  ```json
  { "unblocked": true, "target": "ac_living_room", "reasoning": "Window closed: manual unblock through the API" }
  ```

#### `DELETE /system/reset`
- **Description:** full reset: empties the events/readings DB, removes the registered dynamic agents, restores the
  default HITL configuration and recompiles the graph.
- **Response (200):**
  ```json
  { "status": "reset_complete", "message": "Database svuotato, registro agenti resettato e grafo ricompilato." }
  ```

---

### 🧠 5. Central LLM proxy (MAO)

#### `POST /llm/invoke`
- **Description:** calls the MAO (Model Access Object) directly with the provider and parameters of your choice.
- **Request body:**
  ```json
  {
    "system_prompt": "You are a home-automation assistant.",
    "user_prompt": "What is the ideal temperature for sleeping?",
    "provider": "openrouter",
    "model": "deepseek/deepseek-r1:free",
    "temperature": 0.0,
    "max_tokens": 512,
    "enable_reasoning": true,
    "fallback_on_error": false
  }
  ```
- **Response (200):**
  ```json
  { "response": "The recommended temperature is between 18°C and 20°C.", "provider": "openrouter" }
  ```
- **Provider errors:** if no available provider completes the request, the API answers `503 Service Unavailable`
  without an ASGI traceback. Cloud providers with missing or placeholder keys are not tried. `fallback_on_error` is
  `false` by default on the direct proxy.

---

### 📈 6. Statistics for charts

Read-only (viewer key) and with generic parameters, so a dashboard of any domain can draw from them: the window in
hours, the size of the intervals, a filter by receptor or by owner agent. `GET /events/stats` (above) is the first of them.

#### `GET /graph/stats?window_hours=24`
- **Description:** how the graph ran its nodes (Brain, organs, components) in the runs that started in the window. Every
  execution of a node is recorded (`node_runs` table) with its duration and outcome: `ok`, `escalated`, `waiting` (the
  graph stopped for a person) or `error`.
- **Response (200):**
  ```json
  {
    "generated_at": "2026-10-10T12:00:00+00:00", "window_hours": 24, "runs": 12, "waiting": 1,
    "nodes": [{"node": "brain", "runs": 15, "outcomes": {"ok": 13, "waiting": 2}, "median_ms": 410.5, "p90_ms": 1260.0}],
    "edges": [{"from": "__start__", "to": "brain", "count": 12}, {"from": "brain", "to": "organ_security", "count": 5}],
    "wait_hours_median": 0.4,
    "topology": {"nodes": ["brain", "organ_security"], "edges": [{"from": "brain", "to": "organ_security", "conditional": true}]}
  }
  ```
  The waits for a person are not counted in the durations. A run is identified by `config.run_id`, assigned by
  `POST /graph/run`; a resume keeps the same run.

#### `GET /graph/runs?window_hours=24&run_id=&limit=50`
- **Description:** the runs of the window, newest first, each with its steps (`node`, `started_at`, `duration_ms`, `outcome`,
  `detail`) and whether it is waiting; or one run by `run_id`.

#### `GET /signals?window_hours=4&receptor_id=&limit=500`
- **Description:** the numeric signals recorded in the nervous system (newest first), optionally of one receptor.

#### `GET /signals/stats?window_hours=24&receptor_id=&agent_owner=&bucket_minutes=`
- **Description:** per receptor: `count`, `min`, `max`, `mean`, `last` and a `series` over time. The window is split in
  intervals of `bucket_minutes` (default: 48 intervals); an interval without signals stays in the series with `count: 0`
  and no values, so a chart never invents a point.
- **Response (200):**
  ```json
  {
    "window_hours": 24, "bucket_minutes": 30.0, "signals": 96,
    "receptors": [{"receptor_id": "temp_living", "owners": ["agent_climate"], "unit": "°C", "count": 96, "min": 19.5, "max": 24.1,
                   "mean": 21.7, "last": {"value": 22.0, "timestamp": "2026-10-10T11:58:00+00:00"},
                   "series": [{"start": "2026-10-09T12:00:00+00:00", "count": 2, "min": 20.1, "max": 20.4, "mean": 20.25}]}]
  }
  ```
- The window the graph keeps in its state is `[signals] window_hours` of the configuration (default 4).

---

## 3. A new agent in Python

```python
from app.agents.base_agent import BaseAgent
from app.graph.state import GraphState
from typing import Any

class MyCustomAgent(BaseAgent):
    def __init__(self, tools: dict[str, Any] | None = None):
        super().__init__(
            name="agent_lighting",
            managed_targets=["living_room_lights"],
            conflict_window_minutes=15,
            priority_weight=50.0
        )
        self.tools = tools or {}

    async def process(
        self,
        state: GraphState,
        recent_events: list[dict],
        relevant_readings: list[dict],
        agent_escalations: list[dict]
    ) -> dict[str, Any]:
        # 1. Analyse the state and the sensors
        # 2. Ask the model with self.ask_brain()
        # 3. Act with self.apply_status() or escalate to the parent
        return {"next_agent": "Brain"}
```

To read the decision of the model, use `self.estrai_decisione(answer, ("ACTION", "ESCALATE", "NONE"))`: it accepts
`DECISION:` and `DECISIONE:` and returns the canonical value. For texts in the language of the configuration use
`scegli(italian, english)` from `app.core.lingua`.

---

## 4. New IoT tools

Every tool inherits from `BaseTool`:

```python
from app.tools.baseTool import BaseTool

class SmartBlindTool(BaseTool):
    def __init__(self, target_device: str = "living_room_blinds"):
        super().__init__(target_device=target_device)
        self.position = 0  # 0% closed, 100% open

    async def get_tool_value(self):
        return f"{self.position}%"

    async def set_tool_value(self, value):
        self.position = int(str(value).replace("%", ""))
        return True
```

Register the tool with `registra_tool` (in `app/tools/sensor_tools.py`) to make it available to every agent, the Brain
included, through the shared registry of the process. Without registration only the agents you pass it to in their
`tools` map find it, and the Brain could not apply an approved escalation on that device (`TOOL_MISSING`). Remember to
list the device and its accepted values in the configuration too.

```python
from app.tools.sensor_tools import registra_tool

registra_tool("living_room_blinds", SmartBlindTool())
```

A tool does not have to be a device: a deterministic rule, a redaction or a conversion is a `Reflex` (see the vocabulary in section 1) and is
switched ON/OFF through `POST /effectors` like any other.

### Faults: errors as data, not as exceptions

**Recommendation:** in `get_tool_value` and `set_tool_value` do not hide errors and do not let them break the cycle.
Raise an exception (preferably `ErroreTool`, which carries a code and details) or return `False` for a refused command:
whoever uses the tool goes through `leggi_stato()` / `esegui_comando()` (or, for agents, `applica_stato()`), which
never raise and return a dictionary with the device name and the **reason in plain words**:

```python
from app.tools.baseTool import BaseTool, ErroreTool

class SmartBlindTool(BaseTool):
    async def set_tool_value(self, value):
        try:
            await self._bus.send(int(str(value).replace("%", "")))
        except TimeoutError:
            raise ErroreTool("the bus does not answer within 5s", codice="TIMEOUT", dettagli={"retry_after": 30})
        return True
```

The result looks like this (full format in `app/core/risultati.py`):

```json
{
  "device_name": "living_room_lights",
  "success": false,
  "status": "TOOL_ERROR",
  "response": "Zigbee controller timeout",
  "error_type": "TIMEOUT",
  "actor": "component_lights", "action": "DYNAMIC_ACTION",
  "old_value": "OFF", "requested_value": "ON",
  "audit_logged": true,
  "attempts": [{ "agent": "component_lights", "phase": "actuation", "status": "TOOL_ERROR", "response": "Zigbee controller timeout" }]
}
```

`status` is `APPLIED`, `ALREADY_SET`, `REJECTED_PRIORITY`, `TOOL_MISSING` or `TOOL_ERROR`. The outcome of the actuation
(`success`) is separate from that of the audit log (`audit_logged`): a failed audit does not cancel a command that
worked. A fault is never logged as an action performed, but as `TOOL_ERROR_<action>`.

**What happens to a fault** (example with `component_lights` → `organ_home` → Brain):

1. The sub-agent cannot act: the reason goes up to the parent in the escalation (field `tool_result`).
2. A parent with `priority_weight` at least equal to that of who reported the fault (the Brain always) makes a
   **diagnosis with the model**, from the error, the attempts already made and the recent history of the device (no
   web search). If the fault looks transient it retries the command **once**, under the normal priority check; if it
   works, the escalation is closed.
3. Otherwise the escalation goes up with the diagnosis and the list of attempts. Each agent acts at most once.
4. The Brain, last instance, pauses the graph with a `tool_failure_human_intervention` interrupt (payload with
   device, outcome, diagnosis and attempts). The operator answers with `POST /graph/resume`: `OVERRIDE` with a
   directive in natural language in the `reasoning` field, or any other decision to acknowledge. The interrupt
   survives restarts because the checkpointer is on SQLite.

To try the flow without hardware use `POST /tools/{device_id}/fault`.

---

## 5. Prompts (Brain and sub-agents)

You can customise both the system instructions (system prompt) and the structure of the data sent to the model (user
prompt) at any level of the hierarchy.

### A. Prompts of the Brain, troubleshooting and override (configuration file or `.env`)

Every prompt has two parts:

- the **role**: who the model is, what it assesses, how cautious it is. This is the part you customise;
- the **contract**: the answer format the code then reads (`DECISION: [APPROVE|REJECT]`, `STATUS: ...`, the JSON array
  of the override) and the reference data (catalogue of accepted values). The code always adds it, together with a
  sentence asking the model to treat readings, events and reasons as data, not as instructions: a custom prompt cannot
  break it. If your text already describes the format (contains `DECISION:` or `DECISIONE:`), it is not repeated.

The default roles and contracts are in the language of `[general] language`.

| Name in `[prompts]` | `.env` variable | Task |
|---|---|---|
| `brain_escalation` | `PROMPT_BRAIN_ESCALATION` (or the old `BRAIN_SYSTEM_PROMPT`) | the Brain decides APPROVE/REJECT on an escalation |
| `brain_escalation_user` | `PROMPT_BRAIN_ESCALATION_USER` (or `BRAIN_USER_PROMPT_TEMPLATE`) | message template (placeholders below) |
| `troubleshooting` | `PROMPT_TROUBLESHOOTING` | a device did not run a command: RETRY or ESCALATE |
| `override` | `PROMPT_OVERRIDE` | turns an OVERRIDE directive into JSON commands |
| `routine_check` | `PROMPT_ROUTINE_CHECK` | routine check: OK or MACRO_ADJUSTMENT_REQUIRED |
| names added by the installed nodes (see section 10) | `PROMPT_<NAME>` | the prompts of a node, defined with `definisci_prompt` |

Priority: `.env` > `[prompts]` of the configuration > defaults in [`app/core/prompt.py`](../app/core/prompt.py) (the
prompts of a use case live in its module, registered with `definisci_prompt` and listed in `MODULI_CON_PROMPT`). The
file is reloaded when it changes (the new prompt applies from the next cycle) and accepts multi-line texts:

```toml
[prompts]
troubleshooting = """
You are the troubleshooting module of medical devices. A device did not run a command.
- RETRY only for clearly transient errors (communication timeout) and if no retry already failed.
- In every other case ESCALATE: on a medical device an unclear fault always goes to a person."""
```

In `.env` the text is on one line and `\n` is a line break. A template with an unexpected placeholder or unbalanced
braces is refused: in the configuration file the new version does not apply (the previous one stays, error in the
log); from `.env` the default is used. For a literal brace write `{{` and `}}`.

#### How to write a good role

The defaults are written for any domain. Specialise them when the domain is known:

- **name the critical devices** and what cannot be undone (an unlocked door, a drug given);
- **give a numeric criterion** when there is one (physiological ranges, temperature thresholds): the model applies it
  better than "assess carefully";
- **say what to do when in doubt**: for the Brain REJECT (a rejection is reversible and goes to the operator), for
  troubleshooting ESCALATE;
- **do not repeat the format**: the code adds it; if you write it, use exactly the one of the contract;
- **do not put changing data in the role**: readings and events come in the user message.

A complete example (the Brain prompt adapted to a plant) is commented at the end of
`config.example.toml`.

#### Placeholders of `brain_escalation_user`

| Placeholder | Description |
|---|---|
| `{source}` | Name of the agent that sent the escalation (e.g. `component_door_lock`) |
| `{target}` | Device concerned (e.g. `front_door_lock`) |
| `{action}` | Action proposed by the sub-agent (e.g. `22.5°C`, `LOCKED`) |
| `{reason}` | Reason/details given by the sub-agent |
| `{readings}` | Map of the real-time readings of that device |
| `{recent_events}` | List of the events of the audit log in the time window |

**One call per cycle.** The Brain decides every escalation received in the same cycle with one model call: with several escalations the template is filled for each one, numbered (`ESCALATION: 1`, `ESCALATION: 2`...), the event history appears once at the end, and the model answers with one block per escalation in the format of the contract. With one escalation the message is the usual one. Before any call or command the Brain collects the decisions of the operator (`brain` HITL flow, faults): on resume after a pause nothing is repeated. Escalations decided by the operator do not go to the model; if the answer misses the decision of an escalation no device is commanded and the graph stops for the operator. So, at worst, each level of the hierarchy makes one call.

### External organs (`[organism.external_actors]`)

An actor that writes on the nervous system without being a node of the graph (a security panel, an external system,
an agent of another process) is declared in the configuration:

```toml
[organism.external_actors.agent_security]
level = 1          # 1 = organ, 2+ = component (statistics and dashboard)
priority = 500.0   # weight of its locks, like a registered agent
```

Without a declaration its locks always prevail (unknown priority, to be safe) and the dashboard lists it among "other
actors". A registered agent with the same name prevails over the declaration. It does not become a node of the graph:
the Brain does not call it and it costs no model call.

---

### B. Prompts of the sub-agents through the API (`POST /agents/create`)

When you create or update a sub-agent through the REST API or the JSON configuration, you can set
`system_prompt_template` and `user_prompt_template`:

```json
{
  "agent_definition": "{\"name\": \"organ_respiratory\", \"level\": 1, \"parent_agent_name\": \"Brain\", \"managed_targets\": [\"oxygen_regulator\"], \"system_prompt_template\": \"You are the respiratory organ. Watch SpO2 and drive the oxygen regulators.\\nFormat: DECISION: [ACTION|ESCALATE|NONE]\", \"user_prompt_template\": \"Target: {target}\\nCurrent state: {current_status}\\nDB conflict: {has_conflict}\\nReadings: {relevant_readings}\\nWhat is the decision?\"}"
}
```

If the custom `system_prompt_template` does not describe the answer format (`DECISION: [ACTION|ESCALATE|NONE]`), the
system adds it, with the accepted values of the targets: the format is a contract of the code, not a choice of the
user. An answer without a recognisable decision never counts as "no action": it stops the graph for the operator.

An agent with several `managed_targets` decides them all with **one model call per cycle**: the answer has one block per
target (`TARGET: <name>` followed by `DECISION:` and `REASON:`), and the system adds this format also to a custom prompt
that describes only the single-target one. Only the targets that need a decision go to the model: a locked one waits to
be unlocked, one with an unreconciled conflict goes up to the parent anyway; when none needs it no call is made. At best
the organ solves the problem alone; at worst each level makes its own call up to the Brain. If the decision of a target
is missing no device is commanded and the graph stops for the operator. In the placeholders of the user template, with
several targets, `{target}` lists the names and `{current_status}`, `{has_conflict}`, `{recently_reconciled}` give
`target: value` for each.

#### Placeholders of `user_prompt_template` for sub-agents

| Placeholder | Description |
|---|---|
| `{target}` | Main target controlled by the agent (e.g. `ac_living_room`) |
| `{current_status}` | State read in real time from the IoT tool (e.g. `OFF`, `28.5°C`) |
| `{has_conflict}` | `True` if the DB has an unresolved conflict |
| `{recently_reconciled}` | `True` if the target was reconciled recently |
| `{relevant_readings}` | Sensor readings filtered for this agent |
| `{recent_events}` | Recent audit-log events for the managed targets |

---

## 6. Human-in-the-loop (HITL) and TTL

HITL interrupts can be placed **anywhere in the flow of the graph** through the universal wrapper in
[`app/graph/builder.py`](../app/graph/builder.py) and are managed through the API without restarting the system.

### A. Interrupt points and maximum wait through the API

#### `GET /hitl/config`
- **Description:** the active HITL configuration.
- **Response (200):**
  ```json
  {
    "hitl_all": false,
    "hitl_nodes": ["organ_security", "brain"],
    "hitl_targets": ["front_door_lock", "cardiac_pacemaker"],
    "hitl_actions": ["FORCE_SHUTDOWN", "UNLOCK"],
    "max_wait_seconds": 120
  }
  ```

#### `POST /hitl/config`
- **Description:** sets or updates at runtime the interception rules and the maximum wait (`max_wait_seconds`).
- **Request body:**
  ```json
  {
    "hitl_all": false,
    "hitl_nodes": ["organ_security"],
    "hitl_targets": ["cardiac_pacemaker"],
    "hitl_actions": ["CRITICAL_ARHYTHMIA_ESCALATION"],
    "max_wait_seconds": 300
  }
  ```

### Where HITL applies: nodes, Brain or both

HITL has **two separate flows**; choose in the configuration which to use:

```toml
[hitl]
level = "both"                                          # "nodes" | "brain" | "both"
brain_critical_targets = ["alarm_system", "front_door_lock"]
```

| Flow | Who stops | Rules | Pause types |
|---|---|---|---|
| `nodes` | Graph nodes, **before** running and **after** proposing an action on a protected device | `POST /hitl/config`: `hitl_all`, `hitl_nodes`, `hitl_targets`, `hitl_actions`. The Brain decides escalations alone | `hitl_node_entry_interrupt`, `hitl_action_proposal_interrupt` |
| `brain` | The Brain, when it evaluates an escalation on a protected device, asks the operator first. Nodes never stop | `hitl_targets` and `hitl_all` of `POST /hitl/config`, plus `brain_critical_targets` of the file (empty list = no protected device by default) | `escalation_approval_request` |
| `both` | Both (the same request may be asked twice) | The rules of both | All |

`hitl_nodes` and `hitl_actions` concern only the `nodes` flow; `hitl_targets` and `hitl_all` apply to both.
**Emergency pauses** do not depend on the level and are always active: unusable LLM (`llm_failure_human_intervention`)
and unresolved device fault (`tool_failure_human_intervention`). `GET /hitl/config` shows the choices read from the
file (`configurazione_file`).

### Operator timer

The timer is switched on in the configuration, with `0/1` values:

```toml
[hitl]
timer_enabled = 1               # 0: no timer, the graph waits with no limit
default_timer_seconds = 300     # default length, chosen by the user
on_timeout = "human"            # "human" | "system" | "reject"
```

With `timer_enabled = 1`, each time the graph stops waiting for the operator (of any kind) a timer starts. Its length
is `max_wait_seconds` of `POST /hitl/config` if set (`null` clears it and the default applies again), otherwise
`default_timer_seconds`. The time left is exposed by the API:

- `GET /graph/state?thread_id=...` → field `hitl_timer`;
- `POST /graph/run` and `POST /graph/resume` report the same `hitl_timer` in the answer;
- `GET /hitl/scadenze` lists every waiting request, the most urgent first.

```json
"hitl_timer": { "attivo": true, "in_pausa": true, "tipo": "hitl_node_entry_interrupt",
                "secondi_totali": 300, "secondi_rimanenti": 212, "scade_il": "2026-09-20T18:05:00+00:00",
                "scaduto": false, "azione_alla_scadenza": "umano" }
```

**When it expires** the user chooses between:

- **`human`** (default): the graph stays paused and waits for the operator; the API only reports `scaduto: true`.
- **`system`**: the system decides. The request goes back to the graph with the decision `SISTEMA` and goes on **as if
  HITL were not configured**, i.e. the Brain evaluates it with its model. For each pause type:

  | Pause | With `SISTEMA` |
  |---|---|
  | `hitl_node_entry_interrupt` | the node runs normally |
  | `hitl_action_proposal_interrupt` | the escalation stays and the Brain evaluates it |
  | `escalation_approval_request` | the Brain decides with its model (approve or reject) |
  | `tool_failure_human_intervention` | the fault is logged as unresolved, with no other action |
  | `llm_failure_human_intervention` | the cycle is cancelled (without a model there is nothing to decide) |

- **`reject`**: the request is rejected automatically (no physical action is performed).

A `SISTEMA` decision can also be sent by hand with `POST /graph/resume`. If the operator answers in time the timer
closes; a resume after the expiry answers `409`. Deadlines are on SQLite (table `hitl_scadenze`) and survive restarts
together with the interrupts. With the timer off `max_wait_seconds` is only metadata and is shown in the payload as
maximum wait.

### B. Interrupts and resuming the graph

When the run reaches a protected point:
1. LangGraph suspends the cycle by calling `interrupt()`.
2. You see the pause with `GET /graph/state`.
3. Resume with `POST /graph/resume`, choosing one of three modes (for `llm_failure_human_intervention` and
   `tool_failure_human_intervention` interrupts the rules of their sections apply).

The checkpointer is persistent on SQLite (`AsyncSqliteSaver`, tables `checkpoints`/`writes` in the `DB_PATH` file):
threads and pending HITL interrupts survive restarts and recompilations. It is opened in the lifespan
(`apri_checkpointer()`) and passed to `build_graph(..., checkpointer=...)`; without a checkpointer `build_graph` uses a
volatile `MemorySaver` (tests and demos only). `DELETE /system/reset` also deletes the persisted threads. The HITL
configuration (`/hitl/config`) is not persisted and goes back to the defaults at restart. Recompiling the graph and
replacing `_shared_tools` happen under an asynchronous lock, to avoid races between concurrent requests.

#### Three decisions

| `decision` | Effect |
|---|---|
| `APPROVE` (`APPROVA`) | The Brain applies to the device the action proposed by the agent, as long as it is a command accepted by the configuration (otherwise the device does not change and the message says so), and writes `RECONCILED_<action>` in the DB. |
| `REJECT` (`RESPINGI`) | The Brain writes `REJECTED_<action>` in the DB and marks the device `REJECTED`: it stays blocked (agents with lower priority cannot force it) until the TTL or a manual unblock with `POST /events/unblock`. On numeric devices, like the medical tools, the marker stays only in the audit log. |
| `OVERRIDE` | **Semantic god mode**: the `reasoning` field is sent to the MAO with the Semantic Arbitration prompt. The MAO turns the sentence in natural language into a JSON array of commands, carried out physically by `force_execute_tool`, which bypasses every priority lock. |

#### Example: APPROVE
```http
POST /graph/resume
Content-Type: application/json

{"decision": "APPROVE", "reasoning": "Action approved from the dashboard", "thread_id": "api_session"}
```

#### Example: REJECT
```http
POST /graph/resume
Content-Type: application/json

{"decision": "REJECT", "reasoning": "Safety first, do not change it", "thread_id": "api_session"}
```

#### Example: OVERRIDE (semantic god mode)
Write the directive entirely in natural language; the MAO translates it:
```http
POST /graph/resume
Content-Type: application/json

{
  "decision": "OVERRIDE",
  "reasoning": "Ignore the energy lock: grandma is cold. Turn the heater on at 22 degrees and switch off the pool pump.",
  "thread_id": "api_session"
}
```

The system answers with the log of the actions performed:
```json
{
  "status": "resumed",
  "decision_applied": "OVERRIDE",
  "last_message": "[Brain_Override] ✓ ESEGUITO — UNBLOCK_AND_SET su 'heater_bedroom' → '22'\n[Brain_Override] ✓ ESEGUITO — TURN_OFF su 'pool_pump' → 'OFF'"
}
```

> [!NOTE]
> The role of the Semantic Arbitration prompt is customised like the others (`override` in `[prompts]` or
> `PROMPT_OVERRIDE`); the code always adds the JSON format and the catalogue of values
> ([`app/core/prompt.py`](../app/core/prompt.py)). Every OVERRIDE action is audited in the DB with
> `actor: "Brain_Override"` and the original action (e.g. `UNBLOCK_AND_SET`) for full traceability.

---

## 7. TTL (time-to-live) of locks

Control flags (e.g. `REJECTED`, `BLOCKED`) expire automatically after `FLAG_TTL_MINUTES` (default: 60 min). To unblock
a frozen device by hand:
```http
POST /events/unblock
Content-Type: application/json

{
  "target": "ac_living_room",
  "reasoning": "Window closed: manual unblock through the API"
}
```

---

## 8. Demo, smoke tests and scenarios

### The "graph at work" page

```bash
python examples/avvia_demo.py [--porta 8765] [--scenario conflitto_porta] [--modello Brain=mistral:ministral-8b-latest]
```

It creates (resetting it) the dedicated database `demo.db`, loads the chosen scenario and starts the server on
`http://127.0.0.1:8765/demo`. The page, served by `GET /demo` (switched off with `[demo] web_page = 0`), shows the tree
of agents lighting up node after node, the devices, the event log, the steps of the graph and, when the graph stops,
the request to the operator with the Approve, Reject and Override buttons (the latter needs the admin key) and the
timer. Every node is a real call to the model configured in `.env`. The API key goes at the top of the page (the
launcher passes it to the browser from `.env`). It is the same flow a client of yours gets with
`POST /graph/run/stream` and `POST /graph/resume/stream`. The page follows the language of the browser (Italian or
English).

### API smoke tests

On a running server: [`API_SMOKE_TEST.md`](API_SMOKE_TEST.md) (Bash with `curl` and `jq`) or
[`API_SMOKE_TEST_WINDOWS.ps1`](API_SMOKE_TEST_WINDOWS.ps1) (PowerShell). They create a hierarchy and check registry,
tools, events, HITL, state and reset. **They run `DELETE /system/reset`**: point them to a test instance (for example
the one of `examples/avvia_demo.py`), not to the server with your data. If `API_KEY` is set they send it as the
`X-API-Key` header.

### Reproducible data scenarios

To start again from a clean database with consistent data (agents, history, conflicts) use
[`examples/crea_scenario.py`](../examples/crea_scenario.py), which **drops and recreates every table** of the given
database after a backup copy (`<name>.backup-<date>.db`, ignored by git):

```bash
python examples/crea_scenario.py --scenario base --si                 # database of DB_PATH (from .env)
python examples/crea_scenario.py --db /tmp/test.db --scenario conflitto_porta --si
python examples/crea_scenario.py --scenario base --modello Brain=mistral --modello organ_security=mistral:ministral-8b-latest --si
```

| Scenario | Content |
|---|---|
| `vuoto` | every table recreated and empty (not even the default climate agent) |
| `base` | hierarchy `agent_climate`, `organ_security` → `component_door_lock`/`component_alarm`, `organ_home` → `component_lights` and a history of 6 events from 5-26 hours ago (outside the agents' windows, no active conflict) |
| `conflitto_porta` | `base` + a recent manual lock on `front_door_lock` (makes `component_door_lock` escalate) |
| `finestra_aperta` | `base` + a recent `FORCE_SHUTDOWN` on `ac_living_room` (the old demo conflict) |

`--modello AGENT=PROVIDER[:MODEL]` (repeatable) assigns a model to an agent. The same code can be used by tests:
`await crea_scenario(path, "base")` from `app/db/scenario.py`. Tool states are not in the database: they start from the
default values. The old demo conflict at server start is off (`[demo] seed_conflict_at_start = 0`).

---

## 9. Contract of the states and actions of the domain

The core keeps `action`, `old_value` and `new_value` as extensible values and does not impose a universal
normalisation. Every developer who writes a tool or an agent must:

- define the physical states accepted by their device;
- translate the symbolic actions of their domain into the right physical states;
- refuse unknown values before actuation (the core does it for you if you declare the accepted values of the device
  in the configuration);
- add tests for valid, invalid and idempotent transitions;
- decide how to resolve internal flags such as `REJECTED` and `BLOCKED`.

The health check reports these flags as `MACRO_ADJUSTMENT_REQUIRED`, but on purpose it does not choose or apply a
replacement physical value.

---

## 10. Extensions: a node that plugs into the API

A use case (a node: a helpdesk, a plant, a clinic...) is not part of the core. It is a module that lives in `app/nodes/`
and exposes `ESTENSIONE`, an `Estensione` ([`app/core/estensioni.py`](../app/core/estensioni.py)); the API loads every module
of that folder at start, and the core never imports a node by name. With nothing in `app/nodes/` the API runs as the
plain framework. An extension declares what it brings:

| Field | What it does |
|---|---|
| `registra_rotte(app)` | adds its routes and pages (a page can use `risposta_pagina` of `app/core/pagine_web.py`) |
| `permessi`, `rotte_pubbliche` | the minimum role of each of its routes `{(method, path): Ruolo}`, and its public pages: an unclassified route needs the admin key |
| `nav` | entries of the top bar shared by the pages (`VoceNav(page, href, text key, order)`; the core's settings entry is 100) |
| `compiti`, `chiusure` | async functions run in the background while the API runs, and called at shutdown |
| `moduli_prompt`, `alias_prompt` | modules that define their own prompts (`definisci_prompt`) and the English names of their keys in `[prompts]` |
| `testi` | a folder with `<language>.json`: its sections are merged with those of the core (`common` + one section per page) |
| `sorgenti`, `campi` | the TOML file of the node (path, header, loader that validates it, page that shows it as a tab) and its fields in the settings page |

```python
# app/nodes/my_node.py
from app.core.estensioni import Estensione, VoceNav
from app.core.ruoli import Ruolo

def register(app):
    app.add_api_route("/my-node/api/status", status, methods=["GET"], tags=["My node"])

ESTENSIONE = Estensione(
    nome="my_node", registra_rotte=register,
    permessi={("GET", "/my-node/api/status"): Ruolo.TIROCINANTE},   # viewer
    nav=(VoceNav("my_node", "/my-node", "nav_my_node", 20),),
)
```

A node should use the generic APIs of the core instead of its own copies: `GET /events/stats`, `/graph/stats`,
`/signals/stats` for charts, `/effectors` for the things it acts on, `/config/api/*` for its settings. Everything is an
API call: integration from the backend is the first-class way to use LangBrain.
