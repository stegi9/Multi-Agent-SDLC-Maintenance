"""Configurazione del servizio, letta da variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "coding-service"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "coding-service"
    kafka_consume_topics: list[str] = ["workorders.generated"]

    # MySQL
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "sdlc"
    mysql_password: str = "sdlc"
    mysql_database: str = "sdlc_platform"

    # Database vettoriale (RAG)
    qdrant_url: str = "http://qdrant:6333"

    # Endpoint LLM (es. Ollama on-premise o provider esterno)
    llm_base_url: str = "http://host.docker.internal:11434"
    llm_model: str = "qwen2.5:3b"

    # Clone locale del repository (condiviso con il topology-service)
    target_repo_path: str = "/workspace/repo"

    # Agente Mappatore (topologia e grafo del codice)
    topology_base_url: str = "http://topology-service:8000/api/v1"

    # Verifica anti-allucinazione: secondo passaggio LLM che controlla se le
    # modifiche generate sono supportate dai file forniti (LLM-as-judge).
    verifier_enabled: bool = True

    # Interruttori dei singoli meccanismi di selezione: disattivandoli si
    # isola il contributo del singolo meccanismo architetturale.
    #  - semantic_search:    ricerca semantica ibrida nella selezione dei file
    #  - graph_context:      grafo delle dipendenze (centralità + lettura dipendenze)
    #  - cve_reachability:   percorso deterministico per i ticket di vulnerabilità
    #                        (dal manifesto ai punti di chiamata)
    #  - ticket_normalization: query canonica al posto del testo grezzo nel ranking
    semantic_search: bool = True
    graph_context: bool = True
    cve_reachability: bool = True
    ticket_normalization: bool = True

    # Pianificazione dei test: chiede all'agente di prevedere, per ogni intervento,
    # i file di test che ne dimostrano la correttezza. Segue la convenzione degli
    # interruttori qui sopra; disattivandolo si usa il prompt base senza test.
    test_planning: bool = True

    # Convenzione di test del repository bersaglio, interpolata nel prompt.
    # La piattaforma è generica: la convenzione è un dato di configurazione, non
    # una costante di codice. Default riferito al repository attuale (monorepo
    # pnpm/TypeScript con vitest).
    test_convention_hint: str = (
        "file `*.test.ts` sotto `artifacts/api-server/tests/`, framework vitest con "
        "supertest; i test usano un PostgreSQL reale tramite `@workspace/db`"
    )

    # Agenti cloud esterni per la scrittura del codice:
    # webhook a cui inviare il JSON delle modifiche. Se vuoto, il JSON resta
    # pronto in piattaforma come artefatto di handoff (nessun invio).
    cloud_agent_url: str = ""

    log_level: str = "info"


settings = Settings()
