"""Entry point del microservizio Agente Tester (Test Runner)."""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from shared.common.auth import require_auth

from app.api.routes import router
from app.core.config import settings
from app.kafka.consumer import start_consumer, stop_consumer
from app.kafka.producer import start_producer, stop_producer
from app.services import testdb

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_producer()
    await start_consumer()
    # Elimina i database effimeri rimasti da esecuzioni interrotte da un riavvio
    # del container: senza questa pulizia si accumulerebbero senza limite.
    removed = await testdb.cleanup_orphan_databases()
    if removed:
        logger.info("Database di test orfani rimossi: %s", removed)
    yield
    await stop_consumer()
    await stop_producer()


app = FastAPI(
    title="Agente Tester (Test Runner)",
    description=(
        "Esegue la suite di test del repository bersaglio sul branch di lavoro di un "
        "Work Order e ne riporta l'esito per singolo test, distinguendo i test "
        "introdotti dall'intervento dalla suite di regressione."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Tutte le API richiedono un JWT valido o la chiave interna (X-Internal-Api-Key)
app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_auth)])
# Metriche Prometheus su /metrics
from shared.common.observability import setup_metrics

setup_metrics(app)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "service": settings.service_name}
