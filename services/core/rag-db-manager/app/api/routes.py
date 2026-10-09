"""Endpoint del RAG DB Manager (Doppio RAG su Qdrant)."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.services import rag_store

router = APIRouter()


class IndexRequest(BaseModel):
    document_id: str


@router.post(
    "/collections/{rag_name}/documents",
    summary="Indicizza un documento nel RAG indicato (main | future)",
)
async def add_document(rag_name: Literal["main", "future"], body: IndexRequest) -> dict:
    result = await rag_store.index_document(body.document_id, target_rag=rag_name)
    if result["status"] == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento non trovato")
    return result


@router.get("/collections/{rag_name}/search", summary="Ricerca semantica nel RAG indicato")
async def search(
    rag_name: Literal["main", "future"],
    q: str = Query(min_length=3, max_length=500),
    limit: int = 5,
) -> dict:
    hits = await rag_store.search(rag_name, q, limit=min(limit, 20))
    return {"rag": rag_name, "query": q, "hits": hits}


@router.get("/collections", summary="Statistiche delle collezioni del Doppio RAG")
async def collections_stats() -> dict:
    return await rag_store.stats()


class PrecheckRequest(BaseModel):
    text: str
    rag: Literal["main", "future"] = "future"
    threshold: float = 0.6


@router.post(
    "/precheck",
    summary="Verifica se una specifica simile esiste già nel RAG (evita task duplicati)",
)
async def precheck(body: PrecheckRequest) -> dict:
    hits = await rag_store.search(body.rag, body.text, limit=3)
    best = hits[0] if hits else None
    duplicate = bool(best and best["score"] >= body.threshold)
    return {
        "rag": body.rag,
        "duplicate": duplicate,
        "threshold": body.threshold,
        "best_match": best,
        "candidates": hits,
    }


@router.delete("/documents/{document_id}", summary="Rimuove un documento dal RAG")
async def delete_document(document_id: str) -> dict:
    await rag_store.remove_vectors(document_id)
    return {"status": "ok", "document_id": document_id, "detail": "Vettori rimossi da entrambe le collezioni"}


@router.post(
    "/normalize",
    summary="Normalizzazione del RAG: sposta le feature completate da RAG Futuro a RAG Principale",
)
async def normalize() -> dict:
    return await rag_store.normalize()
