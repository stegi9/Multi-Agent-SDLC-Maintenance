"""Endpoint dell'Agente Valutatore (triage: deduplicazione, dipendenze, priorità).

La logica del triage vive in `app.services.triage`, condivisa con il consumer
Kafka (autonomia a eventi su `tickets.raw`). Se il modello LLM non è
disponibile viene applicata una euristica di fallback basata sulla severity,
così la pipeline non si blocca mai.
"""
import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.triage import run_triage
from shared.common import llm

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/evaluate/run", summary="Avvia deduplicazione, risoluzione dipendenze e prioritizzazione")
async def run_evaluation(limit: int = 30) -> dict:
    return await run_triage(limit)


@router.get("/tickets/prioritized", summary="Restituisce la lista dei ticket prioritizzati")
async def list_prioritized() -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, external_id, source, type, title, severity, priority, status "
                    "FROM tickets WHERE status = 'prioritized' "
                    "ORDER BY priority ASC, imported_at ASC LIMIT 200"
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/tickets/duplicates", summary="Elenca i gruppi di ticket duplicati individuati")
async def list_duplicates() -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT d.id, d.title, d.duplicate_of, m.title AS master_title "
                    "FROM tickets d JOIN tickets m ON m.id = d.duplicate_of "
                    "WHERE d.status = 'deduplicated' ORDER BY d.duplicate_of"
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/llm/health", summary="Verifica la disponibilità del modello LLM")
async def llm_health() -> dict:
    return await llm.is_available(settings.llm_base_url, settings.llm_model)
