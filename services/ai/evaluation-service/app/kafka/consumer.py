"""Consumer Kafka del servizio.

Delegato al runner condiviso (`shared.common.kafka.ConsumerRunner`):
commit manuale dopo l'elaborazione (at-least-once), retry con backoff,
dead-letter queue `events.dlq` e deduplicazione per event_id.
"""
import asyncio
import logging

from app.core.config import settings
from app.kafka.producer import publish
from shared.common.kafka import ConsumerRunner

logger = logging.getLogger(__name__)


DEBOUNCE_SECONDS = 5.0

_pending_triage: asyncio.Task | None = None


async def _delayed_triage() -> None:
    """Attende la fine del burst di import, poi esegue un solo triage sul lotto."""
    await asyncio.sleep(DEBOUNCE_SECONDS)
    from app.services.triage import run_triage

    try:
        result = await run_triage()
        logger.info("Triage automatico da tickets.raw: %s", result)
    except Exception:  # noqa: BLE001 - il task in background non deve propagare
        logger.exception("Triage automatico fallito")


async def handle_message(topic: str, payload: dict) -> None:
    """Autonomia a eventi: ogni ticket importato fa partire il triage (con debounce)."""
    global _pending_triage
    if topic == "tickets.raw":
        if _pending_triage is None or _pending_triage.done():
            _pending_triage = asyncio.create_task(_delayed_triage())
            logger.info("Ticket grezzo ricevuto: triage schedulato tra %.0f secondi", DEBOUNCE_SECONDS)
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
