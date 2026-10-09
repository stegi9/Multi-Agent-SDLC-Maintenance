"""Generazione della documentazione tecnica dalle modifiche validate.

Dopo la validazione dello Scrum Master (code.validated), l'agente legge il
JSON delle modifiche del Work Order e produce la documentazione aggiornata,
che entra nel normale flusso di ingestion documentale (Agente di Coerenza).
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.kafka.producer import publish
from shared.common import llm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Sei l'Agente Documentatore di una piattaforma SDLC aziendale.
Ricevi il ticket risolto e il JSON delle modifiche al codice validate dallo Scrum Master.
Scrivi la documentazione tecnica aggiornata in italiano, formato Markdown, con queste sezioni:
# <titolo sintetico>
## Contesto (il problema o la richiesta)
## Intervento (cosa è stato modificato e perché)
## File modificati (elenco con spiegazione di ogni modifica)
## Note operative (eventuali impatti su deploy, configurazione o utilizzo)
Sii preciso, professionale e conciso. Rispondi SOLO con il documento Markdown."""


async def generate_documentation(wo_id: str) -> dict:
    """Genera il documento per il WO indicato e lo inserisce tra i documenti della piattaforma."""
    async with SessionLocal() as session:
        wo = (
            await session.execute(
                text(
                    "SELECT wo.id, wo.ticket_id, wo.changes, wo.status, t.title AS ticket_title, "
                    "t.type AS ticket_type, t.description AS ticket_description "
                    "FROM work_orders wo JOIN tickets t ON t.id = wo.ticket_id WHERE wo.id = :id"
                ),
                {"id": wo_id},
            )
        ).mappings().first()
    if wo is None:
        return {"status": "not_found", "wo_id": wo_id}

    changes = wo["changes"]
    if isinstance(changes, str):
        changes = json.loads(changes)
    if not changes:
        return {"status": "skipped", "detail": "Il Work Order non ha modifiche da documentare", "wo_id": wo_id}

    # evita doppioni se l'evento viene riconsegnato
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT id FROM documents WHERE wo_id = :wid"), {"wid": wo_id}
            )
        ).first()
    if existing:
        return {"status": "skipped", "detail": "Documentazione già generata", "document_id": existing[0]}

    user_prompt = json.dumps(
        {
            "ticket": {
                "title": wo["ticket_title"],
                "type": wo["ticket_type"],
                "description": (wo["ticket_description"] or "")[:1000],
            },
            "changes": changes,
        },
        ensure_ascii=False,
    )
    try:
        markdown = await llm.chat(
            SYSTEM_PROMPT, user_prompt, base_url=settings.llm_base_url, model=settings.llm_model
        )
    except llm.LLMError as exc:
        logger.warning("LLM non disponibile: documentazione non generata per %s (%s)", wo_id, exc)
        return {"status": "failed", "detail": str(exc), "wo_id": wo_id}

    document_id = str(uuid.uuid4())
    title = f"Documentazione — {wo['ticket_title']}"[:500]
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO documents (id, title, file_name, content, target_rag, status, wo_id) "
                "VALUES (:id, :title, :fname, :content, 'main', 'uploaded', :wid)"
            ),
            {
                "id": document_id,
                "title": title,
                "fname": f"doc-{wo_id[:8]}.md",
                "content": markdown,
                "wid": wo_id,
            },
        )
        await session.execute(
            text("UPDATE work_orders SET status = 'merged' WHERE id = :id"), {"id": wo_id}
        )
        await session.commit()

    try:
        await publish(
            "docs.generated",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "document_id": document_id,
                "wo_id": wo_id,
                "ticket_id": wo["ticket_id"],
                "title": title,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento docs.generated non pubblicato")

    logger.info("Documentazione %s generata per il WO %s", document_id, wo_id)
    return {"status": "ok", "document_id": document_id, "wo_id": wo_id, "title": title}