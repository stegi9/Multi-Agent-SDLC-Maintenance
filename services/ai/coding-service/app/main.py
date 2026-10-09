"""Entry point del microservizio Agente Sviluppatore (Coding)."""
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
    title="Agente Sviluppatore (Coding)",
    description="Riceve il Work Order e la topologia, individua i file esatti e produce il JSON con le modifiche al codice. Scalabile in parallelo (un'istanza per WO).",
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
