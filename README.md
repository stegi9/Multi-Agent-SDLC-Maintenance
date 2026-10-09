# Agentic Software Maintenance

Piattaforma a microservizi che automatizza la manutenzione del software lungo il
ciclo di vita (SDLC) tramite agenti basati su LLM, secondo un approccio
**Human-in-the-Loop**: gli agenti svolgono le attività ripetitive (triage dei
ticket, analisi d'impatto, sviluppo, test, documentazione) mentre le decisioni
critiche restano a Product Manager, Scrum Master e Sviluppatori.

I modelli girano on-premise tramite [Ollama](https://ollama.com); in alternativa
è possibile usare un modello di frontiera via API senza modificare il codice
degli agenti.

## Indice

- [Architettura](#architettura)
- [Flusso operativo](#flusso-operativo)
- [Requisiti](#requisiti)
- [Avvio rapido](#avvio-rapido)
- [Prima configurazione](#prima-configurazione)
- [Agente di coding cloud (n8n)](#agente-di-coding-cloud-n8n)
- [Configurazione](#configurazione)
- [Sviluppo e test](#sviluppo-e-test)
- [Database e migrazioni](#database-e-migrazioni)
- [Deploy su Kubernetes](#deploy-su-kubernetes)
- [Osservabilità](#osservabilità)
- [Risoluzione dei problemi](#risoluzione-dei-problemi)
- [Struttura del repository](#struttura-del-repository)

## Architettura

I servizi comunicano in modo asincrono su **Apache Kafka** (architettura
event-driven) ed espongono API REST con **FastAPI**. Il frontend Angular
raggiunge i servizi attraverso il reverse proxy nginx (`/api/<servizio>/`).

| Componente | Tecnologia |
|---|---|
| Microservizi | Python 3.12, FastAPI |
| Messaggistica | Apache Kafka 3.8 (KRaft) |
| Database relazionale | MySQL 8.4 (migrazioni con Alembic) |
| Database vettoriale | Qdrant |
| LLM ed embedding | Ollama (default `qwen2.5:3b`, `nomic-embed-text`, `bge-m3`) |
| Orchestrazione dei workflow | n8n |
| Frontend | Angular 18 servito da nginx |
| Osservabilità | Prometheus, Grafana |
| Deploy | Docker Compose, Kubernetes |

### Microservizi

| Servizio | Ruolo | Porta host |
|---|---|---|
| `auth-rbac-service` | Login, JWT, ruoli e permessi (RBAC) | 8001 |
| `ingestion-sync-service` | Import dei ticket (Jira, GitHub, Bugzilla, CVE), sincronizzazione inversa, orchestrazione Git delle PR | 8002 |
| `rag-db-manager` | Doppio RAG su Qdrant: *RAG Principale* (documentazione validata) e *RAG Futuro* (specifiche non ancora implementate) | 8003 |
| `document-validator-service` | **Agente di Coerenza**: valida i documenti e normalizza il RAG | 8004 |
| `evaluation-service` | **Agente Valutatore**: deduplica, dipendenze e priorità dei ticket | 8005 |
| `impact-analysis-service` | **Agente di Planning**: analisi d'impatto e simulazioni *what-if* sul grafo delle dipendenze | 8006 |
| `topology-service` | **Agente Mappatore**: clona i repository, costruisce topologia, grafo del codice e indice semantico | 8007 |
| `wo-generator-service` | Generazione dei Work Order dai ticket approvati | 8008 |
| `coding-service` | **Agente Sviluppatore**: selezione del contesto, piano delle modifiche e dei test, handoff all'agente cloud | interna (scalabile) |
| `documentation-service` | **Agente Documentatore**: aggiorna la documentazione dopo il merge | 8010 |
| `test-runner-service` | **Agente Tester**: esegue la suite del repository bersaglio sul branch del Work Order | 8011 |

Ogni servizio è autonomo: ha il proprio `Dockerfile`, `requirements.txt`, l'app
FastAPI in `app/` e i test in `tests/`. Il codice comune (client Kafka e LLM,
autenticazione, telemetria, ranking) si trova in `shared/common/`.

## Flusso operativo

1. I ticket (Bug, CVE, Change Request) vengono importati da Jira, GitHub o
   Bugzilla, oppure creati dalla dashboard: topic `tickets.raw`.
2. L'**Agente Valutatore** deduplica, risolve le dipendenze e assegna la
   priorità: `tickets.prioritized`.
3. L'**Agente di Planning** stima l'impatto sul grafo delle dipendenze e assegna
   una fascia decisionale (`impact.analyzed`):
   - impatto **basso**: approvazione rapida;
   - impatto **medio**: serve un'autorizzazione esplicita;
   - impatto **alto** (architetturale): bloccato e rinviato a una major release.
4. Lo Scrum Master approva (`workorders.approved`) e il **WO Generator** produce
   il Work Order (`workorders.generated`), a cui vengono assegnati un repository
   e uno sviluppatore.
5. L'**Agente Sviluppatore** seleziona i file pertinenti, produce il piano delle
   modifiche e dei test (`code.generated`) e consegna il JSON all'agente cloud
   (n8n), che scrive il codice e lo pusha sul branch `wo/<id>`. La piattaforma
   apre la Pull Request.
6. L'**Agente Tester** esegue la suite del repository sul branch di lavoro, su
   richiesta dalla dashboard o automaticamente se `AUTO_RUN_ON_CODE_GENERATED=true`
   (`tests.executed`).
7. Lo Scrum Master rivede la PR su GitHub: se approva, la piattaforma esegue il
   merge (`code.validated`); se rifiuta, la PR viene chiusa (`code.rejected`).
   I merge eseguiti direttamente su GitHub vengono sincronizzati.
8. L'**Agente Documentatore** aggiorna la documentazione (`docs.generated`) e
   l'**Agente di Coerenza** la valida, spostando le feature completate dal RAG
   Futuro al RAG Principale.

## Requisiti

- **Docker** con Docker Compose v2 (Docker Desktop su Windows e macOS).
- **Ollama** in esecuzione sull'host, con i modelli scaricati:

  ```bash
  ollama pull qwen2.5:3b         # modello di ragionamento degli agenti
  ollama pull nomic-embed-text   # embedding del Doppio RAG
  ollama pull bge-m3             # embedding della ricerca semantica sul codice
  ```

- Circa 8 GB di RAM liberi per lo stack completo (escluso il modello LLM).
- Opzionali: un token GitHub con permessi di scrittura sul repository bersaglio
  e una chiave API Google Gemini per l'agente di coding cloud.

## Avvio rapido

```bash
git clone <url-del-repository>
cd Agentic-software-maintenance
cp .env.example .env              # modificare almeno JWT_SECRET, INTERNAL_API_KEY, SETTINGS_ENC_KEY
docker compose up -d --build
docker compose ps                 # attendere che mysql e kafka risultino "healthy"
```

| Risorsa | URL |
|---|---|
| Dashboard | http://localhost:4200 |
| Swagger di ogni servizio | `http://localhost:<porta>/docs` (porte nella tabella dei microservizi) |
| n8n | http://localhost:5678 |
| Qdrant | http://localhost:6333/dashboard |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (`admin` / `admin` di default) |
| MySQL | `localhost:3307`, database `sdlc_platform`, utente `sdlc` |
| Kafka (dall'host) | `localhost:29092` |

### Utenti demo

Il database viene inizializzato con tre utenti, tutti con password `password123`:

| Utente | Ruolo |
|---|---|
| `pm` | Product Manager |
| `sm` | Scrum Master |
| `dev` | Sviluppatore |

Cambiare le password prima di qualunque uso al di fuori dell'ambiente di sviluppo.

### Autenticazione delle API

Tutte le API richiedono un JWT (ottenuto con il login) oppure, per le chiamate
tra servizi, l'header `X-Internal-Api-Key` con il valore di `INTERNAL_API_KEY`:

```bash
TOKEN=$(curl -s -X POST http://localhost:4200/api/auth/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"sm","password":"password123"}' | jq -r .access_token)

curl -H "Authorization: Bearer $TOKEN" http://localhost:4200/api/ingestion/tickets
```

### Esecuzione parallela dei Work Order

L'Agente Sviluppatore scala orizzontalmente: ogni replica consuma Work Order
distinti dallo stesso consumer group Kafka.

```bash
docker compose up -d --scale coding-service=3
```

## Prima configurazione

1. Accedere alla dashboard come `sm`.
2. In **Impostazioni** inserire le credenziali delle sorgenti dei ticket
   (Jira, GitHub, Bugzilla, CVE).
3. Nella sezione **Repository del software** inserire l'URL Git del repository
   bersaglio (uno per riga per i progetti multi-repo) ed eventualmente il branch.
   Al salvataggio l'Agente Mappatore clona i repository e ricostruisce topologia,
   grafo e indice semantico in background (stato su
   `GET /api/topology/scan/status`).
4. Per l'orchestrazione delle Pull Request impostare un token GitHub globale
   oppure un token per singolo repository (cifrato a riposo con
   `SETTINGS_ENC_KEY`, ha la precedenza su quello globale).
5. Nella sezione **Utenti, domini e GitHub** associare a ogni sviluppatore il proprio handle
   GitHub, così da poterlo indicare come reviewer delle PR.

Senza un repository configurato, l'analisi d'impatto e la verifica di coerenza
rispondono `409`.

## Agente di coding cloud (n8n)

La scrittura del codice è delegata a un workflow n8n che riceve il JSON di
handoff del Work Order, genera i file con Google Gemini, crea il branch
`wo/<id>` e vi committa le modifiche. I segreti non sono salvati nel workflow:
n8n li legge dall'ambiente del container.

1. Nel file `.env`:

   ```dotenv
   GEMINI_API_KEY=<chiave-google-gemini>
   GITHUB_TOKEN=<token-con-permessi-di-scrittura>
   CLOUD_AGENT_URL=http://n8n:5678/webhook/cloud-coding-agent
   ```

2. Importare i workflow e riavviare n8n:

   ```bash
   docker compose up -d n8n coding-service
   docker compose exec n8n n8n import:workflow --input=/workflows/cloud-coding-agent.json
   docker compose exec n8n n8n import:workflow --input=/workflows/sdlc-pipeline.json
   docker compose restart n8n
   ```

3. Su http://localhost:5678 creare l'account owner (solo al primo avvio), aprire
   i workflow **Agente di Coding cloud (Gemini)** e **SDLC: Pipeline agentica
   giornaliera** e attivarli con il toggle *Active*.

La pipeline giornaliera esegue ogni giorno alle 07:00 la scansione del
repository, l'import dei ticket, il triage e l'analisi d'impatto; da lì la
pipeline prosegue via Kafka. Il nodo Gemini usa `gemini-2.5-flash`, disponibile
nel piano gratuito.

Se `CLOUD_AGENT_URL` è vuoto, il JSON di handoff resta disponibile in piattaforma
ma non viene inviato e nessuna PR viene aperta.

## Configurazione

Tutte le variabili si impostano in `.env` (vedi `.env.example`) e vengono
propagate ai servizi da `docker-compose.yml`.

### Principali

| Variabile | Default | Descrizione |
|---|---|---|
| `JWT_SECRET` | `change-me-in-production` | Segreto di firma dei JWT, condiviso da tutti i servizi |
| `INTERNAL_API_KEY` | `dev-internal-key` | Chiave per le chiamate tra servizi e da n8n |
| `SETTINGS_ENC_KEY` | `change-me-in-production` | Passphrase di cifratura dei token Git per repository |
| `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD` | `root`, `sdlc` | Credenziali MySQL |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Endpoint Ollama |
| `LLM_MODEL` | `qwen2.5:3b` | Modello usato dagli agenti |
| `LLM_PROVIDER` | dedotto | `ollama` o `anthropic`; se vuoto è dedotto dal nome del modello (`claude-*` usa `anthropic`) |
| `ANTHROPIC_API_KEY` | vuoto | Necessaria solo con il provider `anthropic` |
| `CLOUD_AGENT_URL` | vuoto | Webhook dell'agente di coding cloud |
| `GEMINI_API_KEY`, `GITHUB_TOKEN` | vuoto | Segreti del workflow n8n |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API`, `JIRA_PROJECT_KEY` | vuoto, `SCRUM` | Integrazione Jira |
| `TELEMETRY_ENABLED` | `true` | Registra token, latenza ed esito di ogni chiamata LLM in `data/telemetry/llm_calls.jsonl` |

### Interruttori degli agenti

Tutti attivi di default; disattivarli serve a isolare il contributo di un singolo
meccanismo.

| Variabile | Effetto |
|---|---|
| `STRUCTURED_OUTPUT` | Decoding vincolato a uno schema JSON per le risposte degli agenti |
| `VERIFIER_ENABLED` | Verifica che le modifiche proposte siano supportate dai file letti |
| `SEMANTIC_SEARCH` | Ricerca semantica ibrida nella selezione dei file |
| `GRAPH_CONTEXT` | Uso del grafo delle dipendenze (centralità e lettura delle dipendenze) |
| `CVE_REACHABILITY` | Percorso deterministico per i ticket di vulnerabilità, dal manifesto ai punti di chiamata |
| `TICKET_NORMALIZATION` | Query canonica del ticket al posto del testo grezzo nel ranking |
| `TEST_PLANNING` | L'Agente Sviluppatore pianifica anche i test per ogni modifica |
| `STRUCTURAL_IMPACT` | Fascia d'impatto calcolata sul grafo invece che sulla stima dell'LLM |

### Agente Tester

L'Agente Tester esegue la suite del repository bersaglio contro un PostgreSQL
effimero (`test-postgres`, dati in `tmpfs`). I default presuppongono un monorepo
pnpm/TypeScript con vitest e vanno adattati al proprio repository:

| Variabile | Default |
|---|---|
| `TEST_PACKAGE_PATH` | `artifacts/api-server` |
| `TEST_DB_PACKAGE_PATH` | `lib/db` |
| `TEST_PG_USER`, `TEST_PG_PASSWORD` | `magazzino` |
| `AUTO_RUN_ON_CODE_GENERATED` | `false` |

## Sviluppo e test

### Un singolo servizio in locale

I servizi partono anche senza Kafka o MySQL attivi (i consumer si disattivano con
un warning), quindi si può lavorare su un servizio isolato. La root del
repository deve essere nel `PYTHONPATH` per importare `shared`:

```bash
cd services/ai/evaluation-service
pip install -r requirements.txt
PYTHONPATH=../../..:. uvicorn app.main:app --reload --port 8005
```

### Test

```bash
# libreria condivisa (dalla root del repository)
pip install fastapi httpx aiokafka "python-jose[cryptography]" pydantic pytest anyio
python -m pytest shared/tests -q

# un servizio
cd services/ai/coding-service
pip install -r requirements.txt pytest anyio
PYTHONPATH=../../..:. python -m pytest tests -q
```

Su Windows il separatore del `PYTHONPATH` è `;` e i percorsi vanno indicati in
forma Windows.

La pipeline di CI (`.github/workflows/ci.yml`) esegue a ogni push e pull request
i test della libreria condivisa, quelli di ogni microservizio e la build del
frontend.

### Frontend

```bash
cd frontend
npm install
npm start          # http://localhost:4200, proxy verso i servizi in proxy.conf.json
npm run build
```

## Database e migrazioni

- `infrastructure/mysql/init/`: schema completo, utenti demo e impostazioni,
  eseguiti da MySQL **solo al primo avvio** del container.
- `infrastructure/mysql/alembic/`: migrazioni versionate con Alembic per i
  database già esistenti.
- `infrastructure/mysql/migrations/`: migrazioni SQL storiche, mantenute solo
  per i database creati prima di Alembic.

```bash
pip install alembic pymysql cryptography
cd infrastructure/mysql
alembic stamp 0001        # solo per un database creato prima di Alembic
alembic upgrade head
```

La connessione usa `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD` e
`MYSQL_DATABASE` (default `localhost:3307`, utente `sdlc`). Ogni modifica di
schema va riportata sia in una nuova revisione Alembic sia in
`init/01-schema.sql`.

Per ripartire da un database vuoto: `docker compose down -v` (elimina anche i
volumi di Kafka, Qdrant, n8n e Grafana).

## Deploy su Kubernetes

I manifest in `infrastructure/kubernetes/` creano la piattaforma nel namespace
`sdlc-platform`. Servono un cluster (anche minikube, kind o Docker Desktop), le
immagini già costruite e caricate nel cluster e un Ingress Controller.

```bash
cd infrastructure/kubernetes
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-config.yaml
kubectl -n sdlc-platform create configmap mysql-initdb \
  --from-file=../mysql/init/01-schema.sql \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f 02-mysql.yaml
kubectl apply -f 03-kafka.yaml
kubectl apply -f 04-qdrant.yaml
kubectl apply -f services/
kubectl apply -f 05-frontend.yaml
kubectl apply -f 06-ingress.yaml

# repliche dell'Agente Sviluppatore
kubectl -n sdlc-platform scale deployment coding-service --replicas=5
```

Kafka è configurato a nodo singolo, adatto a sviluppo e dimostrazioni. I valori
di `platform-secrets` sono esempi: in produzione vanno gestiti con un sistema
dedicato (Sealed Secrets, External Secrets, Vault).

## Osservabilità

Ogni servizio espone metriche Prometheus su `/metrics`. Prometheus le raccoglie
secondo `infrastructure/observability/prometheus.yml` e Grafana carica
automaticamente la dashboard **SDLC: Metriche dei microservizi** (servizi attivi,
richieste, latenze, errori).

## Risoluzione dei problemi

| Sintomo | Causa probabile e soluzione |
|---|---|
| Container in stato `Exited` dopo un riavvio di Docker o una sospensione del PC | Rilanciare `docker compose up -d` |
| Errori 404 o 502 diffusi dalla dashboard | Stack non avviato o upstream nginx non aggiornato: `docker compose restart frontend` |
| Ticket fermi nello stato iniziale dopo un riavvio di Kafka | Consumer Kafka non più validi: riavviare i servizi applicativi senza toccare i volumi (`docker compose restart`) |
| Timeout dell'LLM durante il coding | Il modello locale è lento su prompt grandi: il Work Order passa in `failed` e può essere riavviato |
| `409` su analisi d'impatto o verifica di coerenza | Nessun repository configurato nelle Impostazioni |
| La ricerca semantica sul codice non restituisce risultati | Modello di embedding mancante: `ollama pull bge-m3` |

## Struttura del repository

```
├── docker-compose.yml          ambiente completo
├── .env.example                variabili d'ambiente
├── frontend/                   dashboard Angular con RBAC
├── services/
│   ├── core/                   auth-rbac, ingestion-sync, rag-db-manager
│   └── ai/                     agenti: evaluation, impact-analysis, topology,
│                               wo-generator, coding, test-runner,
│                               documentation, document-validator
├── shared/common/              libreria comune (Kafka, LLM, auth, telemetria, ranking)
├── infrastructure/
│   ├── kafka/                  creazione dei topic
│   ├── mysql/                  schema iniziale e migrazioni
│   ├── n8n/workflows/          workflow di orchestrazione e agente di coding cloud
│   ├── observability/          Prometheus e Grafana
│   └── kubernetes/             manifest per il deploy su cluster
└── data/telemetry/             telemetria delle chiamate LLM (generata a runtime)
```
