"""Consumer Kafka del servizio.

Delegato al runner condiviso (`shared.common.kafka.ConsumerRunner`):
commit manuale dopo l'elaborazione (at-least-once), retry con backoff,
dead-letter queue `events.dlq` e deduplicazione per event_id.
"""
import logging

from app.core.config import settings
from app.kafka.producer import publish
from shared.common.kafka import ConsumerRunner

logger = logging.getLogger(__name__)


async def _feature_text(payload: dict) -> str:
    """Ricostruisce la descrizione della feature dal ticket completato (per il match semantico)."""
    from sqlalchemy import text as sql_text

    from app.core.db import SessionLocal

    parts: list[str] = []
    ticket_id = payload.get("ticket_id")
    if ticket_id:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    sql_text("SELECT title, description FROM tickets WHERE id = :id"),
                    {"id": ticket_id},
                )
            ).mappings().first()
        if row:
            parts += [row["title"] or "", row["description"] or ""]
    parts.append(payload.get("summary") or "")
    return "\n".join(p for p in parts if p).strip()


async def handle_message(topic: str, payload: dict) -> None:
    """Indicizzazione dei documenti validati e Normalizzazione del RAG (a fine ciclo)."""
    from app.services import rag_store

    if topic == "documents.validated" and payload.get("document_id"):
        result = await rag_store.index_document(payload["document_id"])
        logger.info("Indicizzazione %s: %s", payload["document_id"], result.get("status"))
    elif topic == "code.validated":
        # ciclo concluso: l'Agente di Coerenza individua la feature nel RAG Futuro
        # per similarità semantica e la sposta nel RAG Principale
        feature = await _feature_text(payload)
        result = await rag_store.normalize_by_feature(feature)
        logger.info("Normalizzazione semantica (code.validated): %s", result.get("status"))
    elif topic == "rag.normalize":
        result = await rag_store.normalize()
        logger.info("Normalizzazione RAG: %d documenti spostati", result.get("count", 0))
    else:
        logger.info("Ricevuto messaggio su %s: %s", topic, payload)


_runner = ConsumerRunner(
    topics=settings.kafka_consume_topics,
    bootstrap_servers=settings.kafka_bootstrap_servers,
    group_id=settings.kafka_consumer_group,
    handler=handle_message,
    dlq_publish=publish,
)


async def start_consumer() -> None:
    await _runner.start()


async def stop_consumer() -> None:
    await _runner.stop()
