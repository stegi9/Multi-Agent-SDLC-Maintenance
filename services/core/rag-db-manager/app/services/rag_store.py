"""Store del Doppio RAG su Qdrant.

Due collezioni separate:
- rag_main   -> RAG Principale: documentazione validata e già implementata
- rag_future -> RAG Futuro / Roadmap: specifiche e feature accettate ma non sviluppate

Gli embedding sono calcolati on-premise via Ollama (nomic-embed-text, 768 dim).
"""
import logging
import uuid

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from shared.common import embeddings

logger = logging.getLogger(__name__)

COLLECTIONS = {"main": "rag_main", "future": "rag_future"}
VECTOR_SIZE = 768  # nomic-embed-text
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150

_client: AsyncQdrantClient | None = None


def client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=settings.qdrant_url)
    return _client


def collection_for(rag: str) -> str:
    if rag not in COLLECTIONS:
        raise ValueError(f"RAG sconosciuto: {rag!r} (ammessi: main, future)")
    return COLLECTIONS[rag]


async def ensure_collections() -> None:
    for name in COLLECTIONS.values():
        if not await client().collection_exists(name):
            await client().create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
            )
            logger.info("Collezione Qdrant creata: %s", name)


async def embed(texts: list[str]) -> list[list[float]]:
    """Calcola gli embedding via Ollama (/api/embed) usando l'helper condiviso."""
    return await embeddings.embed(
        texts, base_url=settings.llm_base_url, model=settings.embedding_model
    )


def chunk_text(content: str) -> list[str]:
    """Suddivide il testo in blocchi sovrapposti per l'indicizzazione."""
    content = content.strip()
    if not content:
        return []
    chunks = []
    start = 0
    while start < len(content):
        chunks.append(content[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


async def _load_document(document_id: str) -> dict | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, title, content, target_rag, status, wo_id FROM documents WHERE id = :id"),
                {"id": document_id},
            )
        ).mappings().first()
    return dict(row) if row else None


async def remove_vectors(document_id: str) -> None:
    """Rimuove i punti del documento da entrambe le collezioni."""
    for name in COLLECTIONS.values():
        await client().delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
                )
            ),
        )


async def index_document(document_id: str, target_rag: str | None = None) -> dict:
    """Indicizza (o re-indicizza) un documento nella collezione del suo RAG."""
    await ensure_collections()
    doc = await _load_document(document_id)
    if doc is None:
        return {"status": "not_found", "document_id": document_id}
    rag = target_rag or doc["target_rag"]
    collection = collection_for(rag)

    chunks = chunk_text(doc["content"] or "")
    if not chunks:
        return {"status": "skipped", "detail": "Documento senza contenuto testuale", "document_id": document_id}

    vectors = await embed(chunks)
    await remove_vectors(document_id)  # idempotenza: mai chunk duplicati
    await client().upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "document_id": document_id,
                    "title": doc["title"],
                    "chunk_index": i,
                    "text": chunk,
                    "wo_id": doc["wo_id"],
                },
            )
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ],
    )
    if rag != doc["target_rag"]:
        async with SessionLocal() as session:
            await session.execute(
                text("UPDATE documents SET target_rag = :rag WHERE id = :id"),
                {"rag": rag, "id": document_id},
            )
            await session.commit()
    logger.info("Documento %s indicizzato in %s (%d chunk)", document_id, collection, len(chunks))
    return {"status": "ok", "document_id": document_id, "rag": rag, "chunks": len(chunks)}


async def search(rag: str, query: str, limit: int = 5) -> list[dict]:
    await ensure_collections()
    vector = (await embed([query]))[0]
    hits = await client().query_points(
        collection_name=collection_for(rag), query=vector, limit=limit, with_payload=True
    )
    return [
        {
            "score": round(hit.score, 4),
            "document_id": hit.payload.get("document_id"),
            "title": hit.payload.get("title"),
            "chunk_index": hit.payload.get("chunk_index"),
            "text": (hit.payload.get("text") or "")[:500],
        }
        for hit in hits.points
    ]


async def stats() -> dict:
    await ensure_collections()
    result = {}
    for rag, name in COLLECTIONS.items():
        info = await client().get_collection(name)
        result[rag] = {"collection": name, "points": info.points_count}
    return result


NORMALIZE_MATCH_THRESHOLD = 0.55  # similarità minima per riconoscere la feature nel RAG Futuro


async def _move_to_main(document_id: str, title: str) -> bool:
    result = await index_document(document_id, target_rag="main")
    if result["status"] == "ok":
        logger.info("Normalizzazione RAG: '%s' (%s) spostato nel RAG Principale", title, document_id)
        return True
    return False


async def normalize_by_feature(feature_text: str) -> dict:
    """Normalizzazione guidata dall'Agente di Coerenza: a fronte di una feature appena
    sviluppata, individua per SIMILARITÀ SEMANTICA la specifica corrispondente nel
    RAG Futuro e la sposta nel RAG Principale (sistema autoconsistente).

    Funziona anche con le specifiche caricate a mano (senza wo_id), che il JOIN
    classico non potrebbe collegare.
    """
    await ensure_collections()
    if not (feature_text or "").strip():
        return {"status": "skipped", "detail": "Nessuna descrizione della feature"}

    # cerca nel RAG Futuro il documento semanticamente più vicino alla feature
    hits = await search("future", feature_text, limit=3)
    if not hits:
        return {"status": "no_match", "detail": "RAG Futuro vuoto o nessuna corrispondenza"}

    best = hits[0]
    if best["score"] < NORMALIZE_MATCH_THRESHOLD:
        return {
            "status": "no_match",
            "detail": f"Nessuna feature corrispondente sopra soglia (best {best['score']})",
            "best_candidate": best["title"],
            "best_score": best["score"],
        }

    document_id = best["document_id"]
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, title, status, target_rag FROM documents WHERE id = :id"),
                {"id": document_id},
            )
        ).mappings().first()
    if row is None or row["target_rag"] != "future":
        return {"status": "no_match", "detail": "Il candidato non è più nel RAG Futuro"}

    moved = await _move_to_main(document_id, row["title"])
    return {
        "status": "ok" if moved else "error",
        "moved": [{"document_id": document_id, "title": row["title"], "match_score": best["score"]}] if moved else [],
        "count": 1 if moved else 0,
    }


async def normalize() -> dict:
    """Normalizzazione batch: sposta nel RAG Principale le specifiche del RAG Futuro
    esplicitamente collegate (via wo_id) a un Work Order il cui ticket è 'completed'.
    Complementare alla normalizzazione semantica per le spec caricate a mano."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT d.id, d.title FROM documents d "
                    "JOIN work_orders wo ON wo.id = d.wo_id "
                    "JOIN tickets t ON t.id = wo.ticket_id "
                    "WHERE d.target_rag = 'future' "
                    "AND d.status IN ('accepted', 'forced') "
                    "AND t.status = 'completed'"
                )
            )
        ).mappings().all()

    moved = []
    for row in rows:
        if await _move_to_main(row["id"], row["title"]):
            moved.append({"document_id": row["id"], "title": row["title"]})

    return {"status": "ok", "moved": moved, "count": len(moved)}
