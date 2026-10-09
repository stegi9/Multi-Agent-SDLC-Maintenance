"""Test del runner Kafka condiviso: retry, DLQ e deduplicazione."""
import pytest

from shared.common.kafka import DLQ_TOPIC, ConsumerRunner


def _runner(handler, dlq=None, max_attempts=3):
    return ConsumerRunner(
        topics=["t"],
        bootstrap_servers="kafka:9092",
        group_id="g",
        handler=handler,
        dlq_publish=dlq,
        max_attempts=max_attempts,
        backoff_seconds=0.0,  # niente attese nei test
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_handler_ok_nessun_retry():
    chiamate = []

    async def handler(topic, payload):
        chiamate.append(payload)

    runner = _runner(handler)
    await runner._handle_with_retry("t", {"event_id": "e1"})
    assert len(chiamate) == 1


@pytest.mark.anyio
async def test_retry_poi_successo():
    tentativi = []

    async def handler(topic, payload):
        tentativi.append(1)
        if len(tentativi) < 2:
            raise RuntimeError("transitorio")

    runner = _runner(handler)
    await runner._handle_with_retry("t", {"event_id": "e1"})
    assert len(tentativi) == 2


@pytest.mark.anyio
async def test_esaurimento_retry_finisce_in_dlq():
    dlq_messaggi = []

    async def handler(topic, payload):
        raise RuntimeError("sempre rotto")

    async def dlq(topic, payload):
        dlq_messaggi.append((topic, payload))

    runner = _runner(handler, dlq=dlq, max_attempts=2)
    await runner._handle_with_retry("t", {"event_id": "e1", "dato": 42})
    assert len(dlq_messaggi) == 1
    topic, msg = dlq_messaggi[0]
    assert topic == DLQ_TOPIC
    assert msg["original_topic"] == "t"
    assert msg["payload"] == {"event_id": "e1", "dato": 42}
    assert "sempre rotto" in msg["error"]


def test_dedupe_per_event_id():
    runner = _runner(None)
    assert runner._already_processed({"event_id": "a"}) is False
    assert runner._already_processed({"event_id": "a"}) is True
    assert runner._already_processed({"event_id": "b"}) is False
    # senza event_id non si deduplica mai
    assert runner._already_processed({}) is False
    assert runner._already_processed({}) is False
