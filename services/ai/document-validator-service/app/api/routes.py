"""Endpoint dell'Agente di Coerenza Documentale.

Ingestion documentale (a cura dello Scrum Master): upload, elenco,
decisione Human-in-the-Loop (accetta / forza / rifiuta / revisione) e
verifica automatica di coerenza col repository (POST /validate, eseguita
anche in autonomia dal consumer Kafka su documents.uploaded e docs.generated).
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import settings
from shared.common.auth import internal_headers, require_roles
from app.core.db import SessionLocal
from app.kafka.producer import publish
from shared.common import llm

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_SIZE = 2 * 1024 * 1024  # 2 MB
TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".adoc"}

# decisione UI -> stato in tabella documents
DECISION_STATUS = {
    "accept": "accepted",
    "force": "forced",
    "reject": "rejected",
    "review": "in_review",
}


class DocumentMeta(BaseModel):
    id: str
    title: str
    file_name: str | None
    target_rag: str
    status: str
    created_at: datetime
    updated_at: datetime


class DecisionRequest(BaseModel):
    decision: Literal["accept", "force", "reject", "review"]
    note: str = ""


async def _publish_event(topic: str, payload: dict) -> None:
    try:
        await publish(topic, payload)
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento %s non pubblicato", topic)


def _meta_query() -> str:
    return (
        "SELECT id, title, file_name, target_rag, status, created_at, updated_at "
        "FROM documents "
    )


@router.post(
    "/documents",
    response_model=DocumentMeta,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Carica un documento (ingestion documentale dello Scrum Master)",
)
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(""),
    target_rag: Literal["main", "future"] = Form("main"),
) -> DocumentMeta:
    name = file.filename or "documento.txt"
    extension = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if extension not in TEXT_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Formato non supportato ({extension or 'sconosciuto'}): ammessi {', '.join(sorted(TEXT_EXTENSIONS))}",
        )
    raw = await file.read()
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File oltre i 2 MB")
    content = raw.decode("utf-8", errors="replace")

    document_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO documents (id, title, file_name, content, target_rag, status) "
                "VALUES (:id, :title, :file_name, :content, :rag, 'uploaded')"
            ),
            {
                "id": document_id,
                "title": title.strip() or name,
                "file_name": name,
                "content": content,
                "rag": target_rag,
            },
        )
        await session.commit()
        row = (
            await session.execute(text(_meta_query() + "WHERE id = :id"), {"id": document_id})
        ).mappings().first()

    await _publish_event(
        "documents.uploaded",
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "produced_by": settings.service_name,
            "document_id": document_id,
            "title": row["title"],
            "target_rag": target_rag,
        },
    )
    return DocumentMeta(**dict(row))


@router.get("/documents", response_model=list[DocumentMeta], summary="Elenca i documenti caricati")
async def list_documents(limit: int = 200) -> list[DocumentMeta]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(_meta_query() + "ORDER BY created_at DESC LIMIT :limit"),
                {"limit": min(limit, 500)},
            )
        ).mappings().all()
    return [DocumentMeta(**dict(r)) for r in rows]


@router.get("/documents/{document_id}/content", summary="Contenuto testuale di un documento")
async def get_document_content(document_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, title, content FROM documents WHERE id = :id"),
                {"id": document_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento non trovato")
    return dict(row)


@router.post(
    "/documents/{document_id}/decision",
    response_model=DocumentMeta,
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Decisione Human-in-the-Loop: accept | force | reject | review",
)
async def document_decision(document_id: str, body: DecisionRequest) -> DocumentMeta:
    new_status = DECISION_STATUS[body.decision]
    async with SessionLocal() as session:
        row = (
            await session.execute(text(_meta_query() + "WHERE id = :id"), {"id": document_id})
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento non trovato")
        await session.execute(
            text("UPDATE documents SET status = :st WHERE id = :id"),
            {"st": new_status, "id": document_id},
        )
        await session.commit()
        row = (
            await session.execute(text(_meta_query() + "WHERE id = :id"), {"id": document_id})
        ).mappings().first()

    # Documento accettato (o forzato): va indicizzato nel RAG di destinazione
    if new_status in ("accepted", "forced"):
        await _publish_event(
            "documents.validated",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "document_id": document_id,
                "title": row["title"],
                "target_rag": row["target_rag"],
                "forced": new_status == "forced",
                "note": body.note,
            },
        )
        # Chiusura del ciclo: il RAG DB Manager sposta le feature completate
        # dal RAG Futuro al RAG Principale (Normalizzazione del RAG)
        await _publish_event(
            "rag.normalize",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "triggered_by_document": document_id,
            },
        )
    return DocumentMeta(**dict(row))


VALIDATE_PROMPT = """Sei l'Agente di Coerenza Documentale di una piattaforma SDLC.
Ricevi un documento tecnico, la topologia del codice del repository (elenco dei file reali)
e, come MEMORIA, il Report degli Scarti: i ticket rifiutati o posticipati in passato.
Verifica se il documento è congruente con lo stato del software:
- cita file, moduli o tecnologie che esistono davvero nel repository?
- contraddice la struttura attuale (es. descrive componenti inesistenti)?
- descrive funzionalità che erano state SCARTATE o POSTICIPATE (vedi memoria)? In tal caso è incoerente.
- è abbastanza specifico da essere utile nella knowledge base?

Rispondi SOLO con JSON:
{"coherent": true|false, "confidence": <0-100>, "issues": ["incongruenza 1", ...], "summary": "valutazione sintetica in italiano", "suggested_decision": "accept" | "review" | "reject"}"""


class CoherenceVerdict(BaseModel):
    """Schema atteso dal verdetto di coerenza (guardrail sull'output LLM)."""

    coherent: bool
    confidence: int = Field(ge=0, le=100)
    issues: list[str] = []
    summary: str = Field(min_length=1)
    suggested_decision: str = Field(pattern="^(accept|review|reject)$")


async def _load_discard_memory() -> str:
    """Memoria dell'Agente di Coerenza: i Report degli Scarti (ticket scartati/posticipati)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.title, dr.reason, dr.details FROM discard_reports dr "
                    "JOIN tickets t ON t.id = dr.ticket_id "
                    "ORDER BY dr.created_at DESC LIMIT 30"
                )
            )
        ).mappings().all()
    if not rows:
        return ""
    voci = "\n".join(
        f"- [{r['reason']}] {r['title']}: {(r['details'] or '')[:200]}" for r in rows
    )
    return f"\n\nMEMORIA — REPORT DEGLI SCARTI (ticket scartati/posticipati):\n{voci}"


class ValidateRequest(BaseModel):
    document_id: str


@router.post("/validate", summary="Valida un documento contro lo stato attuale del repository")
async def validate_document(body: ValidateRequest) -> dict:
    """Verifica automatica di coerenza con l'agente LLM. La decisione finale resta umana."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, title, content, target_rag FROM documents WHERE id = :id"),
                {"id": body.document_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento non trovato")

    # La verifica di coerenza è un confronto documento <-> codice: senza un
    # repository configurato e scansionato non ha senso e viene rifiutata.
    topology_block = ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.topology_base_url}/topology", headers=internal_headers())
            if response.status_code == 200:
                topo = response.json()
                paths = "\n".join(f["path"] for f in topo.get("files", [])[:300])
                if paths:
                    topology_block = f"\n\nTOPOLOGIA DEL CODICE ({topo.get('file_count')} file):\n{paths}"
    except httpx.HTTPError:
        pass
    if not topology_block:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Impossibile eseguire la verifica di coerenza: nessun repository o codice "
                "con cui confrontare la documentazione. Configura l'URL del repository nella "
                "pagina Impostazioni (sezione 'Repository del software') e lancia la scansione "
                "dell'Agente Mappatore (POST /scan del topology-service), poi riprova."
            ),
        )

    discard_memory = await _load_discard_memory()
    try:
        result = await llm.chat_structured(
            VALIDATE_PROMPT,
            f"DOCUMENTO: {row['title']} (RAG di destinazione: {row['target_rag']})\n\n"
            f"{(row['content'] or '')[:6000]}{topology_block}{discard_memory}",
            schema=CoherenceVerdict,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            retries=2,  # i modelli piccoli spesso sbagliano formato al primo colpo
        )
    except llm.LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"L'agente LLM non ha prodotto un verdetto valido: {exc}",
        )

    report = {
        "document_id": body.document_id,
        "model": settings.llm_model,
        "topology_used": bool(topology_block),
        "discard_memory_used": bool(discard_memory),
        "coherent": result.coherent,
        "confidence": result.confidence,
        "issues": result.issues[:10],
        "summary": result.summary,
        "suggested_decision": result.suggested_decision,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "detail": "La decisione finale resta allo Scrum Master (accept | force | reject | review)",
    }

    # il report resta sul documento, a supporto della decisione HITL
    async with SessionLocal() as session:
        await session.execute(
            text("UPDATE documents SET status = 'validating' WHERE id = :id AND status = 'uploaded'"),
            {"id": body.document_id},
        )
        await session.execute(
            text("UPDATE documents SET validation_report = :rep WHERE id = :id"),
            {"rep": json.dumps(report, ensure_ascii=False), "id": body.document_id},
        )
        await session.commit()
    return report


@router.get("/documents/{document_id}/validation", summary="Ultimo report di coerenza del documento")
async def get_validation_report(document_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status, validation_report FROM documents WHERE id = :id"),
                {"id": document_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento non trovato")
    report = row["validation_report"]
    if isinstance(report, str):
        report = json.loads(report)
    return {"document_id": row["id"], "status": row["status"], "report": report}


@router.get("/reports/discards", summary="Elenca i Report degli Scarti generati")
async def list_discard_reports() -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT dr.id, dr.ticket_id, t.title AS ticket_title, dr.reason, dr.details, dr.created_at "
                    "FROM discard_reports dr JOIN tickets t ON t.id = dr.ticket_id "
                    "ORDER BY dr.created_at DESC LIMIT 200"
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]
