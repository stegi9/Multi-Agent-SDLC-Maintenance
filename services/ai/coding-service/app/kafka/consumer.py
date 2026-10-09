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
    """L'avvio dell'elaborazione è un'azione manuale dello Sviluppatore (POST /execute):
    il Work Order resta in stato 'generated' finché non viene avviato dalla dashboard.
    Qui registriamo solo la ricezione dell'evento."""
    if topic == "workorders.generated" and payload.get("wo_id"):
        logger.info("Work Order %s pronto: in attesa di avvio manuale dello Sviluppatore", payload["wo_id"])
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
