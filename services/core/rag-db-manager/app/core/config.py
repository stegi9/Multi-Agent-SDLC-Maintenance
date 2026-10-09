"""Configurazione del servizio, letta da variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "rag-db-manager"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "rag-db-manager"
    kafka_consume_topics: list[str] = ["documents.validated", "rag.normalize", "code.validated"]

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
    embedding_model: str = "nomic-embed-text"

    log_level: str = "info"


settings = Settings()
