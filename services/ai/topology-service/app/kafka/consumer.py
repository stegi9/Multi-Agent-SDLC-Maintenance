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
    """Autonomia a eventi: dopo la validazione del codice la topologia e il grafo
    vengono rigenerati, così gli agenti lavorano sempre sullo stato aggiornato."""
    if topic == "code.validated":
        from app.api import routes

        if routes._scan_state.get("state") == "running":
            logger.info("Scansione già in corso: rigenerazione non riavviata")
            return
        git_url, branch = await routes._load_repo_settings()
        urls = routes._parse_repo_urls(git_url)
        if not urls:
            logger.warning("Nessun repository configurato: topologia non rigenerata")
            return
        logger.info("Codice validato (WO %s): rigenero topologia e grafo", payload.get("wo_id"))
        await routes._run_scan(urls, branch)
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
