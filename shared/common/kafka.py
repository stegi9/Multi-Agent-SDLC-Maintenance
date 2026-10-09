"""Infrastruttura Kafka condivisa: producer resiliente e consumer affidabile.

Tutti i microservizi usano questi due componenti al posto del boilerplate
duplicato (Fase 2.3 della roadmap — resilienza degli eventi):

- `ResilientProducer`: riconnessione pigra e retry con backoff sulla publish,
  così un broker momentaneamente giù non fa perdere gli eventi di dominio.
- `ConsumerRunner`: commit manuale DOPO l'elaborazione (at-least-once),
  retry con backoff sul singolo messaggio, dead-letter queue (`events.dlq`)
  per i messaggi non processabili e deduplicazione per `event_id` (idempotenza
  best-effort a livello di processo).
"""
import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

logger = logging.getLogger(__name__)

DLQ_TOPIC = "events.dlq"

Handler = Callable[[str, dict], Awaitable[None]]


class ResilientProducer:
    """Producer con riconnessione pigra e retry con backoff esponenziale."""

    def __init__(self, bootstrap_servers: str, *, retries: int = 3, backoff_seconds: float = 1.0) -> None:
        self._bootstrap = bootstrap_servers
        self._retries = retries
        self._backoff = backoff_seconds
        self._producer: AIOKafkaProducer | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            await self._ensure_started()
        except Exception:  # noqa: BLE001 - il servizio deve partire anche senza broker
            logger.warning("Kafka non raggiungibile (%s): producer in attesa di riconnessione", self._bootstrap)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def _ensure_started(self) -> AIOKafkaProducer:
        async with self._lock:
            if self._producer is None:
                producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap)
                await producer.start()
                self._producer = producer
            return self._producer

    async def publish(self, topic: str, payload: dict) -> None:
        """Pubblica un evento JSON. Riprova con backoff; rilancia l'ultimo errore."""
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                producer = await self._ensure_started()
                await producer.send_and_wait(topic, data)
                logger.info("Pubblicato evento su %s", topic)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                # connessione compromessa: si ricrea al prossimo tentativo
                await self.stop()
                if attempt < self._retries - 1:
                    await asyncio.sleep(self._backoff * (2**attempt))
        raise RuntimeError(f"Publish su {topic} fallita dopo {self._retries} tentativi") from last_error


class ConsumerRunner:
    """Loop di consumo affidabile: at-least-once, retry, DLQ, dedupe per event_id."""

    def __init__(
        self,
        *,
        topics: list[str],
        bootstrap_servers: str,
        group_id: str,
        handler: Handler,
        dlq_publish: Callable[[str, dict], Awaitable[None]] | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 2.0,
        dedupe_size: int = 2048,
    ) -> None:
        self._topics = topics
        self._bootstrap = bootstrap_servers
        self._group_id = group_id
        self._handler = handler
        self._dlq_publish = dlq_publish
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._consumer: AIOKafkaConsumer | None = None
        self._task: asyncio.Task | None = None
        self._seen_ids: deque[str] = deque(maxlen=dedupe_size)
        self._seen_set: set[str] = set()

    # -- idempotenza best-effort -------------------------------------------------
    def _already_processed(self, payload: dict) -> bool:
        event_id = payload.get("event_id")
        if not event_id:
            return False
        if event_id in self._seen_set:
            return True
        if len(self._seen_ids) == self._seen_ids.maxlen:
            self._seen_set.discard(self._seen_ids[0])
        self._seen_ids.append(event_id)
        self._seen_set.add(event_id)
        return False

    async def _to_dlq(self, topic: str, payload: dict, error: str) -> None:
        if self._dlq_publish is None:
            logger.error("Messaggio non processabile da %s (nessuna DLQ configurata): %s", topic, error)
            return
        try:
            await self._dlq_publish(
                DLQ_TOPIC,
                {
                    "original_topic": topic,
                    "consumer_group": self._group_id,
                    "error": error[:2000],
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "payload": payload,
                },
            )
            logger.warning("Messaggio da %s spostato in %s: %s", topic, DLQ_TOPIC, error[:200])
        except Exception:  # noqa: BLE001
            logger.exception("Impossibile pubblicare sulla DLQ il messaggio da %s", topic)

    async def _handle_with_retry(self, topic: str, payload: dict) -> None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._handler(topic, payload)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Errore nella gestione del messaggio da %s (tentativo %d/%d)",
                    topic, attempt, self._max_attempts,
                )
                if attempt < self._max_attempts:
                    await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))
                else:
                    await self._to_dlq(topic, payload, f"{type(exc).__name__}: {exc}")

    async def _loop(self) -> None:
        assert self._consumer is not None
        async for msg in self._consumer:
            try:
                try:
                    payload = json.loads(msg.value.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    await self._to_dlq(msg.topic, {"raw": repr(msg.value[:1000])}, f"payload non JSON: {exc}")
                else:
                    if self._already_processed(payload):
                        logger.info("Evento %s già processato: ignorato", payload.get("event_id"))
                    else:
                        await self._handle_with_retry(msg.topic, payload)
                # commit SOLO dopo l'elaborazione (o la DLQ): at-least-once
                await self._consumer.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - il loop non deve mai morire
                logger.exception("Errore imprevisto nel loop di consumo (%s)", msg.topic)

    async def start(self) -> None:
        if not self._topics:
            logger.info("Nessun topic da consumare: consumer non avviato")
            return
        consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=self._bootstrap,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        try:
            await consumer.start()
        except Exception:  # noqa: BLE001 - il servizio deve partire anche senza broker
            logger.warning("Kafka non raggiungibile (%s): consumer disabilitato", self._bootstrap)
            return
        self._consumer = consumer
        self._task = asyncio.create_task(self._loop())
        logger.info("Consumer avviato sui topic %s (commit manuale, retry, DLQ)", self._topics)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None


__all__ = ["ResilientProducer", "ConsumerRunner", "DLQ_TOPIC"]
