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
    """Codice validato dallo Scrum Master -> generazione automatica della documentazione."""
    if topic == "code.validated" and payload.get("wo_id"):
        from app.services.doc_writer import generate_documentation

        logger.info("Codice validato per il WO %s: genero la documentazione", payload["wo_id"])
        result = await generate_documentation(payload["wo_id"])
        logger.info("Documentazione WO %s: %s", payload["wo_id"], result.get("status"))
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
