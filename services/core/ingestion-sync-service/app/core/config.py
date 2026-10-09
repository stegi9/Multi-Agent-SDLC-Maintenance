"""Configurazione del servizio, letta da variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "ingestion-sync-service"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "ingestion-sync-service"
    # Orchestrazione Git: reagisce ai due gate Human-in-the-Loop
    #  - code.validated      -> apre/fonde la PR del codice
    #  - documents.validated -> commit del documento + apre/fonde la PR della doc
    kafka_consume_topics: list[str] = ["code.validated", "code.rejected", "documents.validated"]

    # MySQL
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "sdlc"
    mysql_password: str = "sdlc"
    mysql_database: str = "sdlc_platform"

    # Jira (Reverse Sync e importazione)
    jira_base_url: str = ""        # es. https://mycompany.atlassian.net
    jira_email: str = ""           # email dell'account Atlassian (basic auth)
    jira_api: str = ""             # API token Atlassian
    jira_project_key: str = ""     # progetto di destinazione per la Reverse Sync
    jira_jql: str = "created >= -1d ORDER BY created DESC"  # ticket con aging zero

    # GitHub: token per import issue e orchestrazione Git (branch, commit, PR).
    # Fallback dal .env quando non impostato dalla pagina Impostazioni.
    github_token: str = ""

    # Comportamento dell'orchestrazione Git (override dalla pagina Impostazioni).
    git_merge_method: str = "merge"          # merge | squash | rebase
    git_require_ci: bool = True              # non fondere se la CI non è verde
    git_delete_branch_on_merge: bool = True  # elimina il branch di lavoro dopo il merge

    # Chiave per cifrare i segreti a riposo (token per-repository). Passphrase
    # qualsiasi: viene derivata in una chiave Fernet. Impostala in produzione.
    settings_enc_key: str = ""

    # Endpoint degli altri servizi (rete docker-compose)
    topology_base_url: str = "http://topology-service:8000/api/v1"
    rag_base_url: str = "http://rag-db-manager:8000/api/v1"

    log_level: str = "info"


settings = Settings()
