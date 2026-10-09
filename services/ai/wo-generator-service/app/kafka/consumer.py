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
    """Approvazione (umana o per policy) -> generazione automatica del Work Order."""
    if topic == "workorders.approved" and payload.get("ticket_id"):
        from app.services import generator

        try:
            created = await generator.create_workorder(
                [payload["ticket_id"]], payload.get("instructions", "")
            )
            logger.info("WO %s generato da evento workorders.approved", created["wo_id"])
        except generator.TicketNotFound:
            logger.warning("Ticket %s non trovato: WO non generato", payload["ticket_id"])
        except generator.WorkOrderExists as exc:
            logger.info("WO già esistente per il ticket %s (%s)", payload["ticket_id"], exc.wo_id)
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
