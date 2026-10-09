"""Configurazione del servizio, letta da variabili d'ambiente."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "impact-analysis-service"

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "impact-analysis-service"
    kafka_consume_topics: list[str] = ["tickets.prioritized"]

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
    rag_base_url: str = "http://rag-db-manager:8000/api/v1"

    # Governance graduata: fascia decisionale calcolata sul grafo delle
    # dipendenze invece che sulla percentuale generata dal modello.
    #
    # La regola vale quanto vale l'insieme di file che riceve in ingresso: per
    # questo i candidati sono ordinati per pertinenza al ticket e non per
    # centralita' (gli hub del grafo gonfierebbero la chiusura a valle).
    structural_impact: bool = True
    # Soglie sulla quota di codebase raggiunta a valle entro due salti.
    #  - band_high_pct:      oltre questa quota l'intervento e' architetturale (bloccato)
    #  - band_low_pct:       sotto questa quota, e con al massimo band_low_max_files
    #                        file toccati, l'intervento e' puntuale (approvazione rapida)
    # I default sono un punto di partenza: le soglie vanno ritarate sulla
    # distribuzione empirica di ciascun repository bersaglio.
    band_high_pct: float = 10.0
    band_low_pct: float = 2.0
    band_low_max_files: int = 1

    log_level: str = "info"


settings = Settings()
