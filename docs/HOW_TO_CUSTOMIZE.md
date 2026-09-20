# Guida alla Personalizzazione: LangBrain

Questa guida spiega come estendere e personalizzare il boilerplate per adattarlo alla tua architettura IoT o a qualsiasi altro dominio gerarchico.

---

## 0. Installazione e Avvio del Server

Serve **Python 3.12 o superiore**. Le versioni delle dipendenze sono fissate in `requirements.txt` (dirette) e `requirements.lock` (elenco completo, usato dal Dockerfile); i test si lanciano con `python -m pytest tests`.

### Test e scansione di sicurezza

- `python -m pytest tests` esegue tutta la suite con database temporanei e senza chiamare LLM reali: nessun test può toccare `langbrain.db` o il database del `.env`. `tests/test_all.py` è la regressione generale (utilità, database, tool, MAO, agenti, API, HITL, gerarchia, agenti medici); l'unica prova con un modello vero è disattivata e si abilita con `RUN_LLM_TESTS=1 python -m pytest tests/test_all.py -k Reale` (usa le chiavi del `.env`).
- `scripts/scansione_sicurezza.sh` cerca segreti e vulnerabilità note prima di pubblicare: verifica che `.env` non sia tracciato, esegue `gitleaks` sulla cronologia git e sui file attuali (anche quelli non ancora committati, senza il `.env`) e `pip-audit` su `requirements.lock`. Esce con 0 se è tutto pulito, 1 se trova qualcosa, 2 se mancano gli strumenti. Servono [gitleaks](https://github.com/gitleaks/gitleaks) e `pip install pip-audit`.

```bash
bash scripts/scansione_sicurezza.sh
```


### Avvio locale su Windows PowerShell

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt   # dipendenze fissate + pytest (solo per eseguire i test: requirements.txt)
Copy-Item .env.example .env
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --reload
```

Configura almeno un provider LLM nel file `.env` (`google_studio`, `openrouter`, `mistral` o `local`, scelto con `DEFAULT_PROVIDER`) prima di eseguire cicli che invocano gli agenti. Per Mistral servono `MISTRAL_API_KEY` e, facoltativi, `MISTRAL_MODEL` (default `ministral-8b-latest`) e `MISTRAL_BASE_URL`; nessun modello Mistral è gratuito di per sé, il piano gratuito offre crediti mensili da cui vengono scalati i consumi. La documentazione interattiva sarà disponibile su `http://127.0.0.1:8000/docs`.

Per un modello OpenAI-compatible avviato direttamente su Windows usa, per esempio, `LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1`. Se LangBrain gira in Docker e il modello è sull'host Windows, imposta `LOCAL_MODEL_DOCKER_BASE_URL=http://host.docker.internal:8080/v1`; se il modello è su un'altra macchina della LAN, usa lo stesso URL LAN in entrambe le variabili. `LOCAL_MODEL` deve essere l'ID esatto restituito da `GET <base_url>/models`. Il server del modello deve essere già avviato e raggiungibile.

Il timeout delle richieste LLM è `40` secondi per default. Puoi modificarlo nel `.env`, poi riavviare o ricreare il server:

```dotenv
MAO_TIMEOUT_SECONDS=40
```

Nota (2026-09-04): il progetto contiene un'opzione di testing per evitare di chiamare provider esterni durante lo sviluppo: impostando `MAO_ENABLE_MOCK=1` l'oggetto `Mao` restituisce una risposta canned JSON utile per testare i flussi di `OVERRIDE` senza chiavi LLM.

### Token, errori dell'LLM e intervento dell'operatore

**Token.** La finestra di contesto è una proprietà del modello (es. `qwen/qwen3.8-27b:free` ha 262.144 token): ciò che si configura è il limite di token di **output** di ogni chiamata. I modelli con ragionamento consumano molti token prima di rispondere, quindi le chiamate degli agenti partono da `MAO_MAX_TOKENS_MINIMO` (default `8192`) e, se la risposta viene interrotta dal limite, la richiesta viene ripetuta raddoppiando i token fino a `MAO_MAX_TOKENS_LIMITE` (default `32768`). Se anche così resta troncata è un errore (`RISPOSTA_TRONCATA`), non una risposta parziale usata come decisione.

**Errori.** Quando il modello non è utilizzabile il grafo **non prosegue con decisioni inventate**: l'errore risale con un codice e un suggerimento per l'operatore, e il grafo si ferma (interrupt HITL `llm_failure_human_intervention`). Ogni errore è classificato:

| Codice | Causa tipica | Cosa fa l'operatore |
|---|---|---|
| `CREDITI_ESAURITI` | 402 del provider | Ricarica il piano o passa a un modello gratuito/altro provider |
| `CHIAVE_NON_VALIDA` / `CHIAVE_NON_CONFIGURATA` | 401/403, chiave assente | Sostituisce la chiave nel `.env` |
| `LIMITE_RICHIESTE` | 429 (anche upstream sui modelli `:free`) | Attende, usa una propria chiave o cambia modello |
| `MODELLO_NON_TROVATO` | 404 | Corregge il nome del modello nel `.env` |
| `CONTESTO_SUPERATO` | 400 per contesto | Sceglie un modello con contesto più ampio |
| `RISPOSTA_TRONCATA` / `RISPOSTA_VUOTA` | token finiti nel ragionamento | Aumenta `MAO_MAX_TOKENS_*` o cambia modello |
| `RISPOSTA_NON_UTILIZZABILE` | il modello ha risposto `LLM_ERRORE: ...` o un JSON di comandi non valido (override) | Verifica il contesto o cambia modello |
| `PROVIDER_NON_RAGGIUNGIBILE` / `ERRORE_PROVIDER` | rete, timeout, 5xx | Verifica rete e stato del servizio |

Il payload dell'interrupt contiene `errore` (`codice`, `messaggio`, `suggerimento`, `provider`, `modello`; le chiavi API sono oscurate) e un `prompt` leggibile. Dopo aver risolto (per esempio modificando `.env`: **le variabili cambiate nel file vengono ricaricate a caldo alla chiamata successiva, senza riavviare il server**) l'operatore riprende con `POST /graph/resume`: `APPROVA` o `RETRY` riprovano il nodo; `RESPINGI` annulla il ciclo senza eseguire nulla. Se il problema persiste il grafo si ferma di nuovo. Gli endpoint che chiamano direttamente il modello (`/llm/invoke`, `/graph/health-check`) rispondono `503` con lo stesso dettaglio.

**Nessun ripiego silenzioso.** Per default (`fallback = false` in `configurazione.toml`, oppure `MAO_FALLBACK=0`) un errore del provider o del modello scelto non fa provare in silenzio altri modelli o provider, che potrebbero essere a pagamento: l'operatore decide. Con `MAO_FALLBACK=1` (la variabile d'ambiente ha la precedenza sul file) o `fallback = true` la richiesta prosegue sui provider indicati in `ordine_provider` e, dentro ciascuno, sui `modelli_di_ripiego` (solo gratuiti o molto economici, scelti da te). Gli errori che riguardano l'intero provider (chiave errata, crediti finiti, provider irraggiungibile) passano subito al provider successivo senza provare altri suoi modelli. Ogni volta che il ripiego scatta il log riporta `RIPIEGO ATTIVO` con provider usato e causa.

**Risposta standard del modello.** A ogni system prompt viene aggiunta l'istruzione di rispondere sempre nel formato richiesto, scegliendo l'opzione più prudente se i dati sono scarsi, e di usare la riga `LLM_ERRORE: <motivo>` solo se non riesce a produrre alcuna risposta nel formato (richiesta troncata o incomprensibile): la risposta viene trattata come errore e attiva la stessa pausa HITL. L'istruzione è volutamente restrittiva: con i modelli piccoli una formulazione più larga li porta a dichiararsi incapaci troppo spesso.

### Configurazione delle scelte dell'utente (`configurazione.toml`)

Il `.env` resta per **segreti e parametri d'ambiente** (chiavi API, URL, percorso del database). Le **scelte strutturate** stanno in [`configurazione.toml`](../configurazione.toml): l'elenco dei dispositivi con i valori ammessi e la politica di ripiego dei provider LLM. Priorità: variabile d'ambiente > `configurazione.toml` > default nel codice. Il file viene riletto a caldo quando cambia (se la nuova versione è errata resta attiva la precedente e l'errore compare nel log); percorso alternativo con `LANGBRAIN_CONFIG`.

**Elenco esplicito dei dispositivi.** Ogni dispositivo che può ricevere comandi va dichiarato:

```toml
[politica]
dispositivi_non_elencati = "rifiuta"   # oppure "consenti"

[dispositivi.front_door_lock]
valori = ["LOCKED", "UNLOCKED"]
valore_attivo = "LOCKED"               # cosa invia un agente che decide di "attivare" il dispositivo

[dispositivi.ac_living_room]
valori = ["OFF", "ON"]
intervallo = [10.0, 35.0]              # per i numeri; "22.5°C" e "22.5" sono equivalenti
unita = "°C"
valore_attivo = "22.5°C"

[dispositivi."device_l*"]              # pattern con * e ?; il nome esatto ha la precedenza
valori = ["ON", "OFF"]
```

Il comando viene validato in **ogni** punto in cui raggiunge un dispositivo: attuazione degli agenti (`applica_stato`, esito `COMMAND_NOT_ALLOWED` ed evento `INVALID_COMMAND_<azione>`, con escalation al padre), `execute_tool_safely`, `force_execute_tool` e l'OVERRIDE (che bypassa le priorità ma non i limiti fisici: un dispositivo non elencato non viene né comandato né creato), e `POST /tools` (`422`). `GET /tools/{id}` e `POST /tools/{id}/fault` rispondono `404` per un dispositivo non elencato invece di crearlo. Il valore ammesso viene inviato nella forma canonica (`unlocked` → `UNLOCKED`); i flag interni (`REJECTED`, `RECONCILED_*`, ...) non sono valori fisici e non vengono validati. I valori ammessi sono anche quelli mostrati all'LLM nei suoi prompt. Se il file manca vale la politica `consenti` con un avviso nel log.

**Politica dei provider LLM.**

```toml
[llm]
fallback = false                                                       # ripiego automatico su altri provider/modelli
ordine_provider = ["google_studio", "mistral", "openrouter", "local"]  # dopo il provider scelto con DEFAULT_PROVIDER

[llm.modelli_di_ripiego]                                               # solo gratuiti o molto economici
openrouter = ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free"]
mistral = ["ministral-3b-latest"]
```

### Protezione dell'API con chiavi e ruoli

Tutti gli endpoint, tranne `GET /` (usato dall'healthcheck di Docker), la pagina statica `GET /demo` e la documentazione Swagger, richiedono l'header `X-API-Key`: `401` se la chiave manca o è errata, `403` se il suo ruolo non basta per quell'endpoint. Ci sono tre ruoli, con nomi dalla nomenclatura medica, ciascuno con la propria chiave nel `.env`. Sono i ruoli di chi **chiama l'API** (persone o servizi): non c'entrano con i poteri degli agenti, che si regolano con `priority_weight` e con l'elenco dei dispositivi in `configurazione.toml`.

| Ruolo | Variabile | Cosa può fare |
|---|---|---|
| `tirocinante` | `API_KEY_TIROCINANTE` | Sola lettura: stato del grafo, tool, eventi, agenti, gerarchia, configurazione HITL |
| `medico_di_guardia` | `API_KEY_MEDICO_DI_GUARDIA` | In più le procedure ordinarie: `POST /graph/run` e `POST /graph/run/stream`, `POST /graph/health-check`, `POST /tools`, `POST /events/unblock`, `DELETE /events/reset-conflicts/{target}` e `POST /graph/resume` (o `/graph/resume/stream`) con `APPROVA`/`RESPINGI`/`RETRY` |
| `primario` | `API_KEY_PRIMARIO` (o la chiave unica `API_KEY`) | In più le decisioni critiche e la configurazione: `POST /graph/resume` con `OVERRIDE`, `POST /hitl/config`, `POST /agents/create`, `DELETE /agents/{nome}`, `POST /tools/{id}/fault`, `POST /events/seed-conflict`, `POST /llm/invoke`, `DELETE /system/reset` |

Ogni ruolo include quelli sotto di sé. La matrice sta in un solo punto (`app/core/ruoli.py`, tabella `PERMESSI`): un endpoint non classificato richiede il primario, e un test verifica che ogni rotta lo sia. `API_KEY` è la chiave unica storica e vale come `primario`, quindi le configurazioni esistenti continuano a funzionare. Se nessuna chiave è impostata l'API resta aperta a chiunque raggiunga il server (con avviso all'avvio: impostale sempre fuori da un ambiente di sviluppo locale); se manca la chiave di un ruolo, quel ruolo non può accedere e l'avvio lo segnala.

```bash
curl -H "X-API-Key: $API_KEY_MEDICO_DI_GUARDIA" -X POST http://127.0.0.1:8000/graph/run -H 'Content-Type: application/json' -d '{}'
```

Gli script di smoke test inviano l'header quando la variabile d'ambiente `API_KEY` è definita (quindi come `primario`). In Swagger (`/docs`) usa il pulsante **Authorize**.

### Avvio con Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Il servizio viene pubblicato su `http://127.0.0.1:8000` per default. Puoi cambiare la porta host impostando `LANGBRAIN_PORT` in `.env`. SQLite viene conservato nel volume Docker `langbrain_data`. Il container usa intenzionalmente un solo worker perché registry dei tool e configurazione HITL sono ancora in memoria di processo (il checkpointer invece è su SQLite, nello stesso volume).

Per arrestare il servizio:

```powershell
docker compose down
```

Per eliminare anche il volume SQLite, solo quando vuoi cancellare definitivamente i dati:

```powershell
docker compose down --volumes
```

---

## 1. Architettura Gerarchica N-Livelli

Il sistema adotta una struttura ricorsiva ad albero **Padre-Figlio**:

$$\text{Cervello (Brain - Livello 0)} \longrightarrow \text{Organo (Livello 1)} \longrightarrow \text{Componente dell'Organo (Livello 2)} \longrightarrow \text{Sotto-Componente (Livello N)}$$

### Principi Chiave:
- **Cervello (Brain):** Orchestratore Supremo di Livello 0 con priorità massima (`1000.0`). Possiede la visione globale ed effettua la riconciliazione finale dei conflitti.
- **Organo (es. `organ_security`, `organ_climate`):** Agente di Livello 1 responsabile di una macro-area. Può gestire direttamente dei tool oppure coordinare dei componenti figli.
- **Componente dell'Organo (es. `component_door_lock`, `component_alarm`):** Agente di Livello 2 o superiore dedicato ad una specifica periferica o compito.
- **Escalation Ricorsiva:** Se un componente o organo rileva un conflitto non risolvibile localmente, genera un'`EscalationItem` verso il proprio `parent_agent_name`.
- **Priorità (`priority_weight`):** un agente non può agire su un target su cui un altro attore ha un blocco attivo con priorità **strettamente maggiore** (a parità di peso il blocco non prevale). L'azione respinta viene registrata come `REJECTED_<azione>` e, negli agenti dinamici, diventa un'escalation verso il padre. Il Brain prevale sempre; un attore con peso sconosciuto (es. un operatore umano) prevale per prudenza. Le regole sono in `app/core/priorita.py`.
- **Catena di comando:** come in un'organizzazione reale, il junior chiede al senior e il senior, se non sa rispondere, chiede al proprio superiore, fino al Brain. La routine si esaurisce ai livelli bassi; al Brain arrivano solo le situazioni anomale.
- **`parent_agent_name` è l'unica fonte di verità:** `level` e `sub_agent_names` sono sempre derivati dal registro. Si crea dall'alto verso il basso (prima il padre, poi i figli) e ogni scrittura verifica l'intero albero: padre esistente, nessun ciclo, nomi univoci ignorando maiuscole/minuscole (`brain` e `end` sono riservati) e target condivisi solo tra antenato e discendente (due rami diversi non possono agire sullo stesso dispositivo). L'agente nativo `agent_climate` non delega ai figli e non può essere usato come padre.

---

## 2. Mappatura Completa Endpoints API REST

Tutti gli endpoint disponibili in FastAPI ([`app/api/main.py`](../app/api/main.py)):

### 📌 1. Sistema & Grafo Agenti

#### `GET /`
- **Descrizione:** Health check root dell'API.
- **Risposta (200):**
  ```json
  { "status": "ok", "version": "2.1.0", "architecture": "Hierarchical N-Level (Brain -> Organs -> Components)" }
  ```

#### `POST /graph/run`
- **Descrizione:** Esegue un singolo ciclo del grafo agenti LangGraph per uno specifico `thread_id`.
- **Request Body:**
  ```json
  {
    "sensor_readings": [
      { "sensor_id": "temp_living_room", "agent_owner": "api", "value": "30.0", "unit": "°C" }
    ],
    "force_next_agent": "brain",
    "thread_id": "test-thread-123"
  }
  ```
- **Risposta (200):**
  ```json
  {
    "next_agent": "END",
    "last_message": "[agent_climate] AC attivata su ac_living_room: OFF -> 22.5°C.",
    "pending_escalations": []
  }
  ```

#### `POST /graph/run/stream` e `POST /graph/resume/stream`
- **Descrizione:** Come `POST /graph/run` e `POST /graph/resume` (stessi body e stessi permessi), ma rispondono con un flusso di eventi `text/event-stream`: un evento per ogni nodo che completa, così un client può mostrare l'avanzamento del grafo invece di attendere la fine del ciclo. Il thread resta bloccato per tutta l'esecuzione, come nelle rotte non in streaming; `/graph/resume/stream` risponde `409` se non c'è una richiesta HITL in attesa e richiede il primario per `OVERRIDE`.
- **Formato:** righe `data: {json}` separate da una riga vuota. Il campo `tipo` vale:

  | `tipo` | Contenuto |
  |---|---|
  | `inizio` | `thread_id` |
  | `nodo` | `nodo` eseguito, `prossimo` (a chi passa la mano, `END` a fine ciclo), `messaggio` (ultimo messaggio del nodo), `escalation_pendenti` |
  | `pausa` | `richiesta`: il payload dell'interrupt (`type`, `prompt`, ...): il grafo aspetta l'operatore |
  | `errore` | `codice` e `messaggio` (chiavi oscurate) di un errore che ha interrotto il ciclo |
  | `fine` | `in_pausa`, `next_agent` e `hitl_timer` (stato e secondi rimanenti del timer) |

  `EventSource` del browser non può inviare l'header `X-API-Key`: usa `fetch` e leggi `response.body` (vedi `app/static/demo_grafo.html`).

#### `GET /graph/state`
- **Descrizione:** Restituisce lo stato attuale del grafo dal checkpointer, indicando eventuali `interrupt` pendenti.
- **Query Params:** `thread_id` (opzionale)
- **Risposta (200):**
  ```json
  {
    "next": ["brain"],
    "values": { "next_agent": "brain", "pending_escalations": [] },
    "tasks": [],
    "is_interrupted": false
  }
  ```

#### `POST /graph/resume`
- **Descrizione:** Riprende l'esecuzione del grafo sospeso da un interrupt **Human-in-the-Loop (HITL)**.
- **Request Body:**
  ```json
  {
    "decision": "APPROVA",
    "reasoning": "Approvato dall'utente tramite dashboard",
    "thread_id": "api_session"
  }
  ```
- **Risposta (200):**
  ```json
  {
    "status": "resumed",
    "decision_applied": "APPROVA",
    "last_message": "[Brain] Escalation APPROVATA per front_door_lock.",
    "next_agent": "END"
  }
  ```

#### `POST /graph/health-check`
- **Descrizione:** Invoca l'analisi macro di routine dell'Orchestratore Supremo (`check_body_status`).
- **Risposta (200):**
  ```json
  { "result": "[Brain] Macro Check Completato: STATUS: OK" }
  ```

---

### 🤖 2. Gestione Sotto-Agenti Dinamici (N-Livelli)

#### `GET /agents`
- **Descrizione:** Elenca tutti i sotto-agenti registrati nel sistema.
- **Risposta (200):**
  ```json
  {
    "count": 2,
    "agents": [
      { "name": "organ_climate", "level": 1, "parent_agent_name": "Brain", "managed_targets": ["ac_living_room"] }
    ]
  }
  ```

#### `GET /agents/{agent_name}/model` · `PUT /agents/{agent_name}/model` · `DELETE /agents/{agent_name}/model`
- **Descrizione:** Modello LLM specifico di un agente (il Brain incluso, con `Brain` come nome). Se non è impostato l'agente **eredita quello del padre**, poi del nonno e così via fino al Brain; se nessuno lo imposta vale il provider predefinito del sistema (`DEFAULT_PROVIDER` e il modello di quel provider nel `.env`). Ha effetto dalla chiamata LLM successiva, senza ricompilare il grafo. `GET` è per il `tirocinante`, `PUT`/`DELETE` per il `primario`.
- **PUT Request Body:**
  ```json
  { "provider": "mistral", "model": "ministral-8b-latest" }
  ```
  `provider` è uno tra `google_studio`, `openrouter`, `mistral`, `local` (e deve avere la chiave configurata, altrimenti `422`); `model` è facoltativo e, se omesso, si usa il modello di default di quel provider. Un'impostazione è un'unità: il modello di un padre non viene mai applicato a un provider diverso.
- **Risposta (200):**
  ```json
  {
    "agent": "component_lights",
    "impostazione": null,
    "effettivo": { "provider": "mistral", "model": "ministral-8b-latest", "abilitato": true, "origine": "organ_home", "ereditato": true }
  }
  ```
  `origine` è l'agente da cui deriva l'impostazione (o `predefinito`). `DELETE` toglie l'impostazione propria: l'agente torna a ereditare. `GET /agents` riporta lo stesso campo `llm` per ogni agente; `POST /agents/create` accetta `provider` e `model` nella definizione.

#### `GET /agents/hierarchy`
- **Descrizione:** Restituisce l'albero gerarchico completo: `Cervello (Brain) -> Organi -> Componenti`.
- **Risposta (200):**
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
- **Descrizione:** Registra a runtime un nuovo Organo o Componente dell'Organo e ricompila il grafo. Il padre deve essere già registrato; `level` è facoltativo (derivato come livello del padre + 1) e, se indicato, deve coincidere. `sub_agent_names` è solo informativo: i figli si collegano registrandoli con il proprio `parent_agent_name`, e un figlio non ancora registrato non può essere elencato nel padre.
- **Request Body:**
  ```json
  {
    "agent_definition": "{\"name\": \"organ_security\", \"parent_agent_name\": \"Brain\", \"managed_targets\": [\"alarm_system\"], \"system_prompt_template\": \"Sei l'organo di sicurezza...\", \"priority_weight\": 500.0}"
  }
  ```
- **Risposta (200):**
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
- **Errore (422):** gerarchia incoerente (padre inesistente, ciclo, livello non coerente, nome non valido, riservato o duplicato, target condiviso tra rami diversi). Il registro resta invariato e il messaggio in `detail` indica la regola violata.

#### `DELETE /agents/{agent_name}`
- **Descrizione:** Rimuove un sotto-agente dal registry e ricompila la topologia del grafo. Un agente con figli registrati non può essere eliminato: vanno rimossi prima i figli (dal basso verso l'alto).
- **Risposta (200):**
  ```json
  { "status": "deleted", "agent_name": "component_door_lock" }
  ```
- **Errore (404):** agente non trovato. **Errore (409):** l'agente ha ancora figli; `detail` li elenca.

---

### 🛠️ 3. Gestione Tool IoT (Hardware/Simulati)

#### `GET /tools`
- **Descrizione:** Elenca tutti i tool IoT registrati ed il loro valore attuale.
- **Risposta (200):**
  ```json
  {
    "ac_living_room": { "value": "22.5°C", "unit": "°C" },
    "front_door_lock": { "value": "LOCKED", "unit": "" }
  }
  ```
  Un dispositivo guasto non fa fallire l'elenco: la sua voce riporta `"value": null` più `error` ed `error_type`.

#### `GET /tools/{device_id}`
- **Descrizione:** Legge il valore corrente di uno specifico tool.
- **Risposta (200):**
  ```json
  { "device_id": "ac_living_room", "value": "22.5°C", "unit": "°C" }
  ```
- **Errore (502):** il dispositivo non risponde. Il corpo è il risultato strutturato `{ "device_name", "success": false, "response", "error_type" }` (vedi sezione 4).

#### `POST /tools`
- **Descrizione:** Scrive direttamente il valore di un tool (bypassando la decisione degli agenti).
- **Request Body:**
  ```json
  { "target": "ac_living_room", "value": "OFF" }
  ```
- **Risposta (200):**
  ```json
  { "device_id": "ac_living_room", "new_value": "OFF" }
  ```
- **Errore (422):** il dispositivo non è elencato in `configurazione.toml` o il valore non è tra quelli ammessi; `detail` riporta i valori ammessi. Il valore ammesso viene inviato in forma canonica.
- **Errore (502):** il dispositivo ha rifiutato il comando o non risponde; stesso corpo strutturato di `GET /tools/{device_id}`.

#### `POST /tools/{device_id}/fault`
- **Descrizione:** Simula (o rimuove) un guasto di un dispositivo mock, per provare troubleshooting ed escalation senza hardware reale.
- **Request Body:**
  ```json
  { "fault": "timeout controller Zigbee", "operations": 1, "commands_only": true }
  ```
  `fault` è il messaggio d'errore (`null` rimuove il guasto); `operations` è il numero di operazioni che falliscono prima che il guasto rientri (`null` = finché non viene rimosso); con `commands_only` le letture funzionano e falliscono solo i comandi (sensore attivo, attuatore bloccato).
- **Risposta (200):**
  ```json
  { "device_id": "living_room_lights", "fault": "timeout controller Zigbee", "operations": 1, "commands_only": true }
  ```

---

### 🗄️ 4. Database, Conflitti & Event-Driven Unblock

#### `GET /events`
- **Descrizione:** Recupera lo storico degli eventi dal DB audit log nella finestra specificata.
- **Query Params:** `window_minutes` (default: 240)
- **Risposta (200):**
  ```json
  { "count": 5, "events": [...] }
  ```

#### `POST /events/seed-conflict`
- **Descrizione:** Inserisce un evento di conflitto nel DB per simulare uno scenario di escalation.
- **Request Body:**
  ```json
  {
    "actor": "agent_security",
    "action": "FORCE_SHUTDOWN",
    "target": "ac_living_room",
    "old_value": "22.5°C",
    "new_value": "OFF",
    "reasoning": "Simulazione conflitto via API"
  }
  ```

#### `DELETE /events/reset-conflicts/{target}`
- **Descrizione:** Marca come risolti (`RESOLVED_`) gli eventi di escalation pendenti per il target.

#### `POST /events/unblock`
- **Descrizione:** Sblocca un dispositivo precedentemente bloccato da un flag (`REJECTED`/`BLOCKED`).
- **Request Body:**
  ```json
  { "target": "ac_living_room", "reasoning": "Finestra chiusa: sblocco manuale via API" }
  ```
- **Risposta (200):**
  ```json
  { "unblocked": true, "target": "ac_living_room", "reasoning": "Finestra chiusa: sblocco manuale via API" }
  ```

#### `DELETE /system/reset`
- **Descrizione:** Esegue un reset totale dell'ambiente: svuota il DB degli eventi/readings, rimuove gli agenti dinamici registrati, ripristina la configurazione HITL ai default e ricompila la topologia del grafo.
- **Risposta (200):**
  ```json
  { "status": "reset_complete", "message": "Database svuotato, registro agenti resettato e grafo ricompilato." }
  ```

---

### 🧠 5. Proxy LLM Centralizzato (MAO)

#### `POST /llm/invoke`
- **Descrizione:** Invoca direttamente il MAO (Model Access Object) con provider e parametri a scelta.
- **Request Body:**
  ```json
  {
    "system_prompt": "Sei un assistente domotico.",
    "user_prompt": "Qual è la temperatura ideale per dormire?",
    "provider": "openrouter",
    "model": "deepseek/deepseek-r1:free",
    "temperature": 0.0,
    "max_tokens": 512,
    "enable_reasoning": true,
    "fallback_on_error": false
  }
  ```
- **Risposta (200):**
  ```json
  { "response": "La temperatura consigliata è tra 18°C e 20°C.", "provider": "openrouter" }
  ```
- **Errori provider:** se nessun provider disponibile completa la richiesta, l'API restituisce `503 Service Unavailable` senza traceback ASGI. I provider cloud con chiavi mancanti/placeholder non vengono tentati. `fallback_on_error` vale `false` per default sul proxy diretto.

---

## 3. Creare un Nuovo Agente in Codice Python

Per creare un agente personalizzato in Python:

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
        # 1. Analisi dello stato e dei sensori
        # 2. Invocazione modello via self.ask_brain()
        # 3. Azionamento tool via self.apply_status() o Escalation verso il Padre
        return {"next_agent": "Brain"}
```

---

## 4. Creare e Registrare Nuovi Tool IoT

Tutti i tool ereditano da `BaseTool`:

```python
from app.tools.baseTool import BaseTool

class SmartBlindTool(BaseTool):
    def __init__(self, target_device: str = "living_room_blinds"):
        super().__init__(target_device=target_device)
        self.position = 0  # 0% chiuso, 100% aperto

    async def get_tool_value(self):
        return f"{self.position}%"

    async def set_tool_value(self, value):
        self.position = int(str(value).replace("%", ""))
        return True
```

Registra il tool con `registra_tool` (in `app/tools/sensor_tools.py`) per renderlo disponibile a tutti gli agenti, Brain compreso, tramite il registry condiviso del processo. Senza registrazione lo trovano solo gli agenti a cui lo passi nella loro mappa `tools`, e il Brain non potrebbe applicare un'escalation approvata su quel dispositivo (`TOOL_MISSING`). Ricordati anche di elencare il dispositivo e i valori ammessi in `configurazione.toml`.

```python
from app.tools.sensor_tools import registra_tool

registra_tool("living_room_blinds", SmartBlindTool())
```

### Gestione dei guasti: errori come dati, non come eccezioni

**Raccomandazione:** in `get_tool_value` e `set_tool_value` non nascondere gli errori e non lasciare che facciano crollare il ciclo. Solleva un'eccezione (meglio `ErroreTool`, che porta un codice e dei dettagli) o restituisci `False` per un comando rifiutato: chi usa il tool passa da `leggi_stato()` / `esegui_comando()` (o dagli agenti, tramite `applica_stato()`), che non sollevano mai e restituiscono un dizionario con il nome del dispositivo e il **motivo in chiaro**:

```python
from app.tools.baseTool import BaseTool, ErroreTool

class SmartBlindTool(BaseTool):
    async def set_tool_value(self, value):
        try:
            await self._bus.invia(int(str(value).replace("%", "")))
        except TimeoutError:
            raise ErroreTool("il bus non risponde entro 5s", codice="TIMEOUT", dettagli={"retry_dopo": 30})
        return True
```

Il risultato ha questa forma (formato completo in `app/core/risultati.py`):

```json
{
  "device_name": "living_room_lights",
  "success": false,
  "status": "TOOL_ERROR",
  "response": "timeout controller Zigbee",
  "error_type": "TIMEOUT",
  "actor": "component_lights", "action": "DYNAMIC_ACTION",
  "old_value": "OFF", "requested_value": "ON",
  "audit_logged": true,
  "attempts": [{ "agent": "component_lights", "phase": "actuation", "status": "TOOL_ERROR", "response": "timeout controller Zigbee" }]
}
```

`status` vale `APPLIED`, `ALREADY_SET`, `REJECTED_PRIORITY`, `TOOL_MISSING` o `TOOL_ERROR`. L'esito dell'attuazione (`success`) è separato da quello della registrazione su audit log (`audit_logged`): un audit non riuscito non annulla un comando andato a buon fine. Un guasto non viene mai registrato sull'audit log come azione eseguita, ma come `TOOL_ERROR_<azione>`.

**Cosa succede a un guasto** (esempio con `component_lights` → `organ_home` → Brain):

1. Il sotto-agente non riesce ad agire: il motivo sale al padre nell'escalation (campo `tool_result`).
2. Un padre con `priority_weight` almeno pari a quello di chi ha segnalato il guasto (il Brain sempre) fa una **diagnosi con il modello**, a partire dall'errore, dai tentativi già fatti e dallo storico recente del dispositivo (non c'è una ricerca web). Se il guasto sembra transitorio ritenta il comando **una volta**, sottoposto al normale controllo di priorità; se riesce, l'escalation si chiude.
3. Se non basta, l'escalation prosegue verso l'alto con la diagnosi e la lista dei tentativi. Ogni agente interviene al massimo una volta.
4. Il Brain, ultima istanza, mette il grafo in pausa con un interrupt `tool_failure_human_intervention` (payload con dispositivo, esito, diagnosi e tentativi). L'operatore risponde con `POST /graph/resume`: `OVERRIDE` con una direttiva in linguaggio naturale nel campo `reasoning`, oppure qualsiasi altra decisione per prendere atto. L'interrupt sopravvive ai riavvii perché il checkpointer è su SQLite.

Per provare il flusso senza hardware usa `POST /tools/{device_id}/fault`.

---

## 5. Personalizzazione dei Prompt (Brain & Sotto-Agenti)

Puoi personalizzare sia le istruzioni di sistema (System Prompt) che la struttura dei dati inviati al modello (User Prompt) a qualsiasi livello della gerarchia.

### A. Personalizzare i Prompt dell'Orchestratore Supremo (Brain) via `.env`

Nel file `.env` puoi sovrascrivere direttamente i prompt del `BrainAgent`:

```env
# System Prompt dell'Orchestratore
BRAIN_SYSTEM_PROMPT="Sei l'Orchestratore Supremo della Smart Home. Hai ricevuto un'escalation da un sotto-agente per un conflitto o un'anomalia. Valuta il contesto e decidi se APPROVARE o RESPINGERE l'azione.\nFormato Risposta:\nDECISIONE: [APPROVA|RESPINGI]\nMOTIVAZIONE: [spiegazione]"

# Template del User Prompt con segnaposto dinamici
BRAIN_USER_PROMPT_TEMPLATE="Agente Richiedente: {source}\nDispositivo Target: {target}\nAzione Proposta: {action}\nMotivo Escalation: {reason}\nLetture Sensori Reali: {readings}\nStorico Eventi Recenti: {recent_events}\nQual è la risoluzione corretta?"
```

#### Segnaposto disponibili per `BRAIN_USER_PROMPT_TEMPLATE`:
| Segnaposto | Descrizione |
|---|---|
| `{source}` | Nome dell'agente che ha inviato l'escalation (es. `component_door_lock`) |
| `{target}` | Dispositivo/target interessato (es. `front_door_lock`) |
| `{action}` | Azione proposta dal sotto-agente (es. `22.5°C`, `LOCKED`) |
| `{reason}` | Motivazione/dettagli forniti dal sotto-agente |
| `{readings}` | Mappa delle letture in tempo reale per quel dispositivo |
| `{recent_events}` | Lista degli eventi dal DB audit log nella finestra temporale |

---

### B. Personalizzare i Prompt dei Sotto-Agenti via API (`POST /agents/create`)

Quando crei o aggiorni un sotto-agente via API REST o nel file di configurazione JSON, puoi specificare `system_prompt_template` e `user_prompt_template`:

```json
{
  "agent_definition": "{\"name\": \"organ_respiratory\", \"level\": 1, \"parent_agent_name\": \"Brain\", \"managed_targets\": [\"oxygen_regulator\"], \"system_prompt_template\": \"Sei l'Organo Respiratorio. Monitora la SpO2 ed aziona i regolatori di ossigeno.\\nFormato: DECISIONE: [ACTION|ESCALATE|NONE]\", \"user_prompt_template\": \"Target: {target}\\nStato Attuale: {current_status}\\nConflitto DB: {has_conflict}\\nLetture: {relevant_readings}\\nQual è la decisione?\"}"
}
```

Se il `system_prompt_template` personalizzato non descrive il formato della risposta (`DECISIONE: [ACTION|ESCALATE|NONE]`), il sistema lo aggiunge da solo insieme ai valori ammessi dei target: il formato è un contratto del codice, non una scelta dell'utente. Una risposta senza decisione riconoscibile non vale mai come "nessuna azione": ferma il grafo per l'operatore.

#### Segnaposto disponibili per `user_prompt_template` nei Sotto-Agenti:
| Segnaposto | Descrizione |
|---|---|
| `{target}` | Target primario controllato dall'agente (es. `ac_living_room`) |
| `{current_status}` | Stato letto in tempo reale dal tool IoT (es. `OFF`, `28.5°C`) |
| `{has_conflict}` | `True` se il DB rileva un conflitto non ancora risolto |
| `{recently_reconciled}` | `True` se il target è stato riconciliato recentemente |
| `{relevant_readings}` | Letture dei sensori filtrate per questo specifico agente |
| `{recent_events}` | Eventi recenti di audit log per i target gestiti |

---

## 6. Configurazione Dinamica Human-in-the-Loop (HITL) e TTL

Gli interrupt HITL possono essere collocati **ovunque nel flusso del grafo** tramite il wrapper universale in [`app/graph/builder.py`](../app/graph/builder.py) e gestiti dinamicamente via API senza riavviare il sistema.

### A. Configurare i punti di Interrupt HITL ed Attesa Massima via API

#### `GET /hitl/config`
- **Descrizione:** Legge la configurazione HITL attiva.
- **Risposta (200):**
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
- **Descrizione:** Imposta o aggiorna a runtime le regole di intercettazione e l'attesa massima (`max_wait_seconds`).
- **Request Body:**
  ```json
  {
    "hitl_all": false,
    "hitl_nodes": ["organ_security"],
    "hitl_targets": ["cardiac_pacemaker"],
    "hitl_actions": ["CRITICAL_ARHYTHMIA_ESCALATION"],
    "max_wait_seconds": 300
  }
  ```

### Dove si applica l'HITL: nodi, Brain o entrambi

L'HITL ha **due flussi separati**, e in `configurazione.toml` scegli quale usare:

```toml
[hitl]
livello = "entrambi"                                   # "nodi" | "brain" | "entrambi"
target_critici_brain = ["alarm_system", "front_door_lock"]
```

| Flusso | Chi si ferma | Regole | Tipi di pausa |
|---|---|---|---|
| `nodi` | I nodi del grafo, **prima** di eseguire e **dopo** aver proposto un'azione su un dispositivo protetto | `POST /hitl/config`: `hitl_all`, `hitl_nodes`, `hitl_targets`, `hitl_actions`. Il Brain decide da solo le escalation | `hitl_node_entry_interrupt`, `hitl_action_proposal_interrupt` |
| `brain` | Il Brain, quando valuta un'escalation su un dispositivo protetto, chiede prima all'operatore. I nodi non si fermano mai | `hitl_targets` e `hitl_all` di `POST /hitl/config`, più `target_critici_brain` del file (lista vuota = nessun dispositivo protetto per default) | `escalation_approval_request` |
| `entrambi` | Tutti e due (una stessa richiesta può essere chiesta due volte) | Le regole di entrambi | Tutti |

`hitl_nodes` e `hitl_actions` riguardano solo il flusso `nodi`; `hitl_targets` e `hitl_all` valgono per entrambi. Le **pause di emergenza** non dipendono dal livello e restano sempre attive: LLM non utilizzabile (`llm_failure_human_intervention`) e guasto di un dispositivo non risolto (`tool_failure_human_intervention`). `GET /hitl/config` mostra le scelte lette dal file (`configurazione_file`).

### Timer di attesa dell'operatore

Il timer si accende in [`configurazione.toml`](../configurazione.toml), con voci `0/1`:

```toml
[hitl]
timer_attivo = 1                  # 0: nessun timer, il grafo attende senza limite
timer_predefinito_secondi = 300   # durata predefinita, la sceglie l'utente
azione_alla_scadenza = "umano"    # "umano" | "sistema" | "respingi"
```

Con `timer_attivo = 1`, ogni volta che il grafo si ferma in attesa dell'operatore (di qualunque tipo) parte un timer. La durata è `max_wait_seconds` di `POST /hitl/config` se impostato (`null` lo azzera e si torna al predefinito), altrimenti `timer_predefinito_secondi`. Il tempo rimanente è esposto via API:

- `GET /graph/state?thread_id=...` → campo `hitl_timer`;
- `POST /graph/run` e `POST /graph/resume` riportano lo stesso `hitl_timer` nella risposta;
- `GET /hitl/scadenze` elenca tutte le richieste in attesa, dalla più urgente.

```json
"hitl_timer": { "attivo": true, "in_pausa": true, "tipo": "hitl_node_entry_interrupt",
                "secondi_totali": 300, "secondi_rimanenti": 212, "scade_il": "2026-09-20T18:05:00+00:00",
                "scaduto": false, "azione_alla_scadenza": "umano" }
```

**Alla scadenza** l'utente sceglie tra:

- **`umano`** (predefinito): il grafo resta in pausa e aspetta l'operatore; l'API segnala solo `scaduto: true`.
- **`sistema`**: decide il sistema. La richiesta torna al grafo con la decisione `SISTEMA` e prosegue **come se l'HITL non fosse configurato**, cioè il Brain la valuta con il suo modello. Cosa significa per ogni tipo di pausa:

  | Pausa | Con `SISTEMA` |
  |---|---|
  | `hitl_node_entry_interrupt` | il nodo viene eseguito normalmente |
  | `hitl_action_proposal_interrupt` | l'escalation resta e la valuta il Brain |
  | `escalation_approval_request` | il Brain decide con il suo modello (approva o respinge) |
  | `tool_failure_human_intervention` | il guasto viene registrato come non risolto, senza altre azioni |
  | `llm_failure_human_intervention` | il ciclo viene annullato (senza modello non c'è nulla da decidere) |

- **`respingi`**: la richiesta viene respinta in automatico (nessuna azione fisica viene eseguita).

Una decisione `SISTEMA` può anche essere inviata a mano con `POST /graph/resume`. Se l'operatore risponde in tempo il timer si chiude; una ripresa dopo la scadenza risponde `409`. Le scadenze sono su SQLite (tabella `hitl_scadenze`) e sopravvivono ai riavvii insieme agli interrupt. Con il timer spento `max_wait_seconds` è solo un metadato e si vede nel payload come attesa massima.

### B. Gestione dell'Interrupt e Resume del Grafo

Quando l'esecuzione del grafo incontra un punto protetto:
1. LangGraph sospende il ciclo invocando `interrupt()`.
2. Puoi rilevare lo stato di pausa con `GET /graph/state`.
3. Ripristina l'esecuzione con `POST /graph/resume`, scegliendo una delle tre modalità (per gli interrupt `llm_failure_human_intervention` e `tool_failure_human_intervention` valgono le regole descritte nelle rispettive sezioni):

Nota implementativa (2026-09-04):
- Il checkpointer è persistente su SQLite (`AsyncSqliteSaver`, tabelle `checkpoints`/`writes` nel file `DB_PATH`): thread e interrupt HITL pendenti sopravvivono a riavvii e ricompilazioni. Viene aperto nel lifespan (`apri_checkpointer()`) e passato a `build_graph(..., checkpointer=...)`; senza checkpointer `build_graph` usa un `MemorySaver` volatile (solo test/demo). `DELETE /system/reset` elimina anche i thread persistiti. La configurazione HITL (`/hitl/config`) invece non è persistita e torna ai valori predefiniti al riavvio.
- La ricompilazione del grafo e la sostituzione di `_shared_tools` avvengono sotto un lock asincrono per ridurre race condition in scenari di richieste concorrenti.

#### Tre modalità di decisione

| `decision` | Effetto |
|---|---|
| `APPROVA` | Il Brain applica al dispositivo l'azione proposta dall'agente, purché sia un comando ammesso da `configurazione.toml` (altrimenti il dispositivo non cambia e il messaggio lo dice), e scrive `RECONCILED_<action>` nel DB. |
| `RESPINGI` | Il Brain scrive `REJECTED_<action>` nel DB e marca il dispositivo `REJECTED`: resta bloccato (gli agenti con priorità inferiore non possono forzarlo) fino a TTL o sblocco manuale con `POST /events/unblock`. Sui dispositivi numerici, come i tool medici, il marcatore resta solo nell'audit log. |
| `OVERRIDE` | **God Mode Semantico**: il campo `reasoning` viene inviato al MAO con il prompt di Arbitrato Semantico. Il MAO traduce la frase in linguaggio naturale in un array JSON di comandi, eseguiti fisicamente via `force_execute_tool` che bypassa tutti i lock di priorità. |

#### Esempio — APPROVA
```http
POST /graph/resume
Content-Type: application/json

{"decision": "APPROVA", "reasoning": "Intervento approvato via Dashboard", "thread_id": "api_session"}
```

#### Esempio — RESPINGI
```http
POST /graph/resume
Content-Type: application/json

{"decision": "RESPINGI", "reasoning": "Sicurezza prioritaria, non modificare", "thread_id": "api_session"}
```

#### Esempio — OVERRIDE (God Mode Semantico)
Puoi scrivere la direttiva interamente in linguaggio naturale; il MAO si occupa di tradurla:
```http
POST /graph/resume
Content-Type: application/json

{
  "decision": "OVERRIDE",
  "reasoning": "Ignora il blocco dell'energia: mia nonna ha freddo. Accendi la stufa a 22 gradi e spegni la pompa della piscina.",
  "thread_id": "api_session"
}
```

Il sistema risponde con il log delle azioni fisicamente eseguite:
```json
{
  "status": "resumed",
  "decision_applied": "OVERRIDE",
  "last_message": "[Brain_Override] ✓ ESEGUITO — UNBLOCK_AND_SET su 'heater_bedroom' → '22'\n[Brain_Override] ✓ ESEGUITO — TURN_OFF su 'pool_pump' → 'OFF'"
}
```

> [!NOTE]
> Il System Prompt di Arbitrato Semantico usato dal MAO è definito in [`app/graph/orchestrator.py`](../app/graph/orchestrator.py) come `_OVERRIDE_SYSTEM_PROMPT`.
> Ogni azione OVERRIDE viene auditata nel DB con `actor: "Brain_Override"` e l'azione originale (es. `UNBLOCK_AND_SET`) per tracciabilità completa.

---


## 7. Gestione TTL (Time-To-Live) per i Blocchi
I flag di controllo (es. `REJECTED`, `BLOCKED`) scadono automaticamente dopo `FLAG_TTL_MINUTES` (default: 60 min).
Per sbloccare manualmente un dispositivo congelato:
```http
POST /events/unblock
Content-Type: application/json

{
  "target": "ac_living_room",
  "reasoning": "Finestra chiusa: sblocco manuale via API"
}
```

---

## 8. Demo, Smoke Test e Scenari

### Pagina "grafo in azione"

```bash
python examples/avvia_demo.py [--porta 8765] [--scenario conflitto_porta] [--modello Brain=mistral:ministral-8b-latest]
```

Crea (azzerandolo) il database dedicato `demo.db`, vi mette lo scenario scelto e avvia il server su `http://127.0.0.1:8765/demo`. La pagina, servita da `GET /demo` (si disattiva con `[demo] pagina_web = 0` in `configurazione.toml`), mostra l'albero degli agenti che si illumina nodo dopo nodo, i dispositivi, il registro eventi, i passi del grafo e, quando il grafo si ferma, la richiesta all'operatore con i pulsanti Approva, Respingi e Override (quest'ultimo richiede la chiave del `primario`) e il timer. Ogni nodo è una chiamata reale al modello configurato nel `.env`. La chiave API si inserisce in alto nella pagina (il lanciatore la passa al browser dal `.env`). È lo stesso flusso che un tuo client può ottenere con `POST /graph/run/stream` e `POST /graph/resume/stream`.

### Smoke test dell'API

Su un server in esecuzione: [`API_SMOKE_TEST.md`](API_SMOKE_TEST.md) (Bash con `curl` e `jq`) oppure [`API_SMOKE_TEST_WINDOWS.ps1`](API_SMOKE_TEST_WINDOWS.ps1) (PowerShell). Creano una gerarchia, verificano registro, tool, eventi, HITL, stato e reset. **Eseguono `DELETE /system/reset`**: puntali a un'istanza di prova (per esempio quella di `examples/avvia_demo.py`), non al server con i tuoi dati. Se `API_KEY` è impostata la esportano come header `X-API-Key`.

### Scenari di dati riproducibili

Per ripartire da un database pulito con dati coerenti (agenti, storico, conflitti) usa lo script [`examples/crea_scenario.py`](../examples/crea_scenario.py), che **elimina e ricrea tutte le tabelle** del database indicato dopo aver fatto una copia di sicurezza (`<nome>.backup-<data>.db`, ignorata da git):

```bash
python examples/crea_scenario.py --scenario base --si                 # database di DB_PATH (dal .env)
python examples/crea_scenario.py --db /tmp/prova.db --scenario conflitto_porta --si
python examples/crea_scenario.py --scenario base --modello Brain=mistral --modello organ_security=mistral:ministral-8b-latest --si
```

| Scenario | Contenuto |
|---|---|
| `vuoto` | tutte le tabelle ricreate e vuote (nemmeno l'agente clima di default) |
| `base` | gerarchia `agent_climate`, `organ_security` → `component_door_lock`/`component_alarm`, `organ_home` → `component_lights` e uno storico di 6 eventi di 5-26 ore fa (fuori dalle finestre degli agenti, nessun conflitto attivo) |
| `conflitto_porta` | `base` + blocco manuale recente su `front_door_lock` (fa fare escalation a `component_door_lock`) |
| `finestra_aperta` | `base` + `FORCE_SHUTDOWN` recente su `ac_living_room` (il vecchio conflitto dimostrativo) |

`--modello AGENTE=PROVIDER[:MODELLO]` (ripetibile) assegna un modello specifico a un agente. Lo stesso codice è usabile dai test: `await crea_scenario(percorso, "base")` da `app/db/scenario.py`. Gli stati dei tool non stanno nel database: partono dai valori predefiniti. Il vecchio conflitto dimostrativo all'avvio del server ora è spento (`[demo] conflitto_all_avvio = 0` in `configurazione.toml`).

---

## 9. Contratto degli Stati e delle Azioni del Dominio

Il core conserva `action`, `old_value` e `new_value` come valori estensibili e non impone una normalizzazione universale. Ogni sviluppatore che implementa un tool o un agente deve:

- definire gli stati fisici ammessi dal proprio dispositivo;
- tradurre le azioni simboliche del proprio dominio negli stati fisici corretti;
- rifiutare valori sconosciuti prima dell'attuazione (il core lo fa per te se dichiari i valori ammessi del dispositivo in `configurazione.toml`);
- aggiungere test per transizioni valide, invalide e idempotenti;
- decidere come risolvere flag interni quali `REJECTED` e `BLOCKED`.

L'health check segnala questi flag come `MACRO_ADJUSTMENT_REQUIRED`, ma intenzionalmente non sceglie né applica un valore fisico sostitutivo.


