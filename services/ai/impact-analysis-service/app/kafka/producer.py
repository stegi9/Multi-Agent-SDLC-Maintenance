"""Producer Kafka del servizio (delegato al producer resiliente condiviso).

Topic pubblicati da questo servizio: ["impact.analyzed"]
"""
from app.core.config import settings
from shared.common.kafka import ResilientProducer

_producer = ResilientProducer(settings.kafka_bootstrap_servers)


async def start_producer() -> None:
    await _producer.start()


async def stop_producer() -> None:
    await _producer.stop()


async def publish(topic: str, payload: dict) -> None:
    """Pubblica un evento JSON sul topic indicato (retry con backoff)."""
    await _producer.publish(topic, payload)
