"""Entry point del microservizio Ingestion & Sync Service."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from shared.common.auth import require_auth

from app.api.routes import router
from app.core.config import settings
from app.kafka.consumer import start_consumer, stop_consumer
from app.kafka.producer import start_producer, stop_producer


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_producer()
    await start_consumer()
    yield
    await stop_consumer()
    await stop_producer()


app = FastAPI(
    title="Ingestion & Sync Service",
    description="Importa ticket (Bug, CVE, Change Request) da Jira, GitHub, Bugzilla e gestisce la Reverse Sync verso i sistemi esterni.",
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
