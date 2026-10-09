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
    """Autonomia a eventi: ticket prioritizzato -> analisi di impatto automatica."""
    if topic == "tickets.prioritized" and payload.get("ticket_id"):
        from fastapi import HTTPException

        from app.api.routes import AnalyzeRequest, analyze_ticket

        try:
            result = await analyze_ticket(AnalyzeRequest(ticket_id=payload["ticket_id"]))
            logger.info(
                "Analisi di impatto automatica per %s: %.1f%% (%s)",
                payload["ticket_id"], result.get("impact_percentage", 0.0), result.get("decision"),
            )
        except HTTPException as exc:
            # stato non analizzabile (es. topologia assente, LLM giù): non è un errore
            # transitorio del messaggio, quindi niente retry/DLQ
            logger.warning("Analisi di impatto non eseguita per %s: %s", payload["ticket_id"], exc.detail)
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
