"""Entry point del microservizio Auth & RBAC Service."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
    title="Auth & RBAC Service",
    description="Gestisce login, sessioni JWT e permessi RBAC (Product Manager, Scrum Master, Sviluppatore).",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")
# Metriche Prometheus su /metrics
from shared.common.observability import setup_metrics

setup_metrics(app)



@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "service": settings.service_name}
