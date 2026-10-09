"""Configurazione del servizio, letta da variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "topology-service"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "topology-service"
    kafka_consume_topics: list[str] = ["code.validated"]

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

    # Indice semantico del codice (retrieval ibrido nella selezione dei file).
    # embedding_model: modello di embedding on-premise; bge-m3 gestisce bene la
    # query in italiano contro path/simboli in inglese. Impostare a
    # "nomic-embed-text" per riusare il modello già presente senza download.
    embedding_model: str = "bge-m3"
    code_index_collection: str = "code_index"
    semantic_index_enabled: bool = True

    # Clone locale del repository su cui si genera la topologia
    target_repo_path: str = "/workspace/repo"

    log_level: str = "info"


settings = Settings()
