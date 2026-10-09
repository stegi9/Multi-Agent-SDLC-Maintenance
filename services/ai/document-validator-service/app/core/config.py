"""Configurazione del servizio, letta da variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "document-validator-service"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "document-validator-service"
    kafka_consume_topics: list[str] = ["documents.uploaded", "docs.generated"]

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
    llm_model: str = "llama3.1"

    # Endpoint degli altri servizi (rete docker-compose)
    topology_base_url: str = "http://topology-service:8000/api/v1"

    log_level: str = "info"


settings = Settings()
