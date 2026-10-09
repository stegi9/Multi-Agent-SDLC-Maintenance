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


async def handle_message(topic: str, payload: dict) -> None:
    """Autonomia a eventi: verifica di coerenza automatica sui documenti caricati
    (documents.uploaded) e su quelli prodotti dall'Agente Documentatore (docs.generated).
    La decisione finale resta allo Scrum Master (Human-in-the-Loop)."""
    document_id = payload.get("document_id")
    if topic in ("documents.uploaded", "docs.generated") and document_id:
        from fastapi import HTTPException

        from app.api.routes import ValidateRequest, validate_document

        try:
            result = await validate_document(ValidateRequest(document_id=document_id))
            logger.info(
                "Coerenza di %s: coherent=%s confidence=%s suggerimento=%s",
                document_id, result.get("coherent"), result.get("confidence"),
                result.get("suggested_decision"),
            )
        except HTTPException as exc:
            # topologia assente o LLM giù: la verifica resta disponibile via POST /validate
            logger.warning("Verifica di coerenza non eseguita per %s: %s", document_id, exc.detail)
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
