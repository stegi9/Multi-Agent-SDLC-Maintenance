"""Endpoint dell'Agente Documentatore."""
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.doc_writer import generate_documentation

router = APIRouter()


class GenerateRequest(BaseModel):
    wo_id: str


@router.post("/generate", summary="Genera la documentazione aggiornata a partire dalle modifiche validate")
async def generate_docs(body: GenerateRequest) -> dict:
    result = await generate_documentation(body.wo_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
    if result["status"] == "failed":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result["detail"])
    return result


@router.get("/documents/{wo_id}", summary="Documentazione generata per un Work Order")
async def get_generated_docs(wo_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, title, file_name, content, target_rag, status, created_at "
                    "FROM documents WHERE wo_id = :wid ORDER BY created_at DESC LIMIT 1"
                ),
                {"wid": wo_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nessuna documentazione per questo Work Order")
    return dict(row)