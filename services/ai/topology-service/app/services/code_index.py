"""Indice semantico del codice (retrieval denso per la selezione dei file).

Affianca la ricerca lessicale (keyword sul path) con una ricerca per SIMILARITÀ
SEMANTICA. A differenza di una semplice "impronta" delle prime righe, qui ogni
file di CODICE viene spezzato in blocchi sovrapposti (chunk) e ogni blocco è
indicizzato in Qdrant: così è coperto TUTTO il contenuto del file, non solo la
testa. A fronte della descrizione di un Work Order (in italiano), l'Agente
Sviluppatore recupera i file più pertinenti anche quando le parole del ticket non
compaiono nei nomi dei file — cosa che il dizionario di sinonimi non copriva.

Scelte da prodotto:
- CHUNKING: recupera la logica sepolta in fondo ai file grandi (non solo import/firme).
- FILTRO NON-CODICE: markdown, documentazione, config, dati e test NON sono file da
  modificare per risolvere un ticket → esclusi dall'indice per non fare rumore.

Tutto è degradabile: se il modello di embedding o Qdrant non sono disponibili,
build_index() logga e ritorna senza bloccare la scansione, e la selezione dei
file ricade sul ranking lessicale esistente.
"""
import logging
from pathlib import Path

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings
from shared.common import embeddings

logger = logging.getLogger(__name__)

# Linguaggi NON indicizzati: non sono codice sorgente da modificare per un ticket
# (documentazione, config, dati). Riduce drasticamente il rumore nei risultati.
EXCLUDE_LANGUAGES = {"markdown", "testo", "json", "yaml", "toml", "ini", "xml"}
# Segmenti di path che indicano test/fixture: esclusi (non sono l'obiettivo del fix).
TEST_MARKERS = ("/tests/", "/test/", "/__tests__/", ".test.", ".spec.")

MAX_FILE_CHARS = 50_000   # cap per file: evita di embeddare file generati enormi
CHUNK_SIZE = 1_000
CHUNK_OVERLAP = 150
EMBED_BATCH = 64
# Al recupero interroghiamo più blocchi del numero di file voluto, perché più
# chunk possono appartenere allo stesso file: poi aggreghiamo per path.
SEARCH_FANOUT = 6

_client: AsyncQdrantClient | None = None


def _qdrant() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=settings.qdrant_url)
    return _client


def _symbols_by_file(graph: dict | None) -> dict[str, list[str]]:
    """Nomi dei simboli (funzioni/classi) definiti in ogni file, dal grafo."""
    if not graph:
        return {}
    return {
        path: info.get("symbols", [])
        for path, info in graph.get("files", {}).items()
        if info.get("symbols")
    }


def _should_index(file: dict) -> bool:
    """Vero se il file è codice sorgente rilevante per un intervento."""
    if file.get("language") in EXCLUDE_LANGUAGES:
        return False
    path = file["path"].lower()
    return not any(marker in path for marker in TEST_MARKERS)


def _chunk(content: str) -> list[str]:
    """Spezza il testo in blocchi sovrapposti (copre tutto il file)."""
    content = content.strip()
    if not content:
        return []
    chunks, start = [], 0
    while start < len(content):
        chunks.append(content[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _file_chunks(file: dict, symbols: list[str], root: Path) -> list[str]:
    """Blocchi indicizzabili di un file: ogni blocco porta in testa il path (e, il
    primo, i simboli) così l'embedding conserva il contesto del file d'origine."""
    candidate = root / file["path"]
    try:
        if not candidate.is_file():
            return []
        content = candidate.read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_CHARS]
    except OSError:
        return []
    blocks = _chunk(content)
    if not blocks:
        return []
    header = f"path: {file['path']}\nlanguage: {file.get('language', '?')}\n"
    sym_line = ("symbols: " + ", ".join(symbols) + "\n") if symbols else ""
    out = []
    for i, block in enumerate(blocks):
        prefix = header + (sym_line if i == 0 else "")
        out.append(prefix + block)
    return out


async def build_index(topology: dict, graph: dict | None) -> dict | None:
    """(Ri)costruisce la collezione Qdrant con più vettori (chunk) per file.

    Ritorna un riepilogo, oppure None se l'indicizzazione è disattivata o gli
    embedding non sono calcolabili (fallback silenzioso sul ranking lessicale)."""
    if not settings.semantic_index_enabled:
        return None
    files = [f for f in topology.get("files", []) if _should_index(f)]
    if not files:
        return None

    root = Path(settings.target_repo_path)
    symbols_map = _symbols_by_file(graph)

    # costruisco (path, testo-del-chunk) per ogni blocco di ogni file di codice
    chunk_paths: list[str] = []
    chunk_texts: list[str] = []
    for f in files:
        for block in _file_chunks(f, symbols_map.get(f["path"], []), root):
            chunk_paths.append(f["path"])
            chunk_texts.append(block)
    if not chunk_texts:
        return None

    try:
        dim = await embeddings.embedding_dimension(
            base_url=settings.llm_base_url, model=settings.embedding_model
        )
        vectors: list[list[float]] = []
        for start in range(0, len(chunk_texts), EMBED_BATCH):
            batch = chunk_texts[start : start + EMBED_BATCH]
            vectors.extend(
                await embeddings.embed(
                    batch, base_url=settings.llm_base_url, model=settings.embedding_model
                )
            )
    except embeddings.EmbeddingError as exc:
        logger.warning(
            "Indice semantico non aggiornato (modello %s non disponibile): %s. "
            "La selezione dei file userà il solo ranking lessicale.",
            settings.embedding_model, exc,
        )
        return None

    client = _qdrant()
    collection = settings.code_index_collection
    # ricreo da zero: la dimensione può cambiare col modello ed evito chunk orfani
    await client.delete_collection(collection)
    await client.create_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
    )
    for start in range(0, len(vectors), 256):  # upsert a lotti per non fare payload enormi
        sl = slice(start, start + 256)
        await client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(
                    id=start + j,
                    vector=vec,
                    payload={"path": chunk_paths[start + j]},
                )
                for j, vec in enumerate(vectors[sl])
            ],
        )
    indexed_files = len(set(chunk_paths))
    logger.info(
        "Indice semantico aggiornato: %d file di codice, %d chunk (dim %d, modello %s)",
        indexed_files, len(chunk_texts), dim, settings.embedding_model,
    )
    return {
        "indexed_files": indexed_files,
        "chunks": len(chunk_texts),
        "dimension": dim,
        "model": settings.embedding_model,
    }


async def search(query: str, limit: int = 20) -> list[dict]:
    """Recupera i file semanticamente più vicini alla query.

    I punteggi sono aggregati a livello di FILE: per ogni path si tiene il blocco
    con similarità massima. Ritorna `[{path, score}]` decrescente. In caso di
    errore ritorna lista vuota (il chiamante ricade sul ranking lessicale)."""
    if not settings.semantic_index_enabled or not query.strip():
        return []
    try:
        vector = (
            await embeddings.embed(
                [query], base_url=settings.llm_base_url, model=settings.embedding_model
            )
        )[0]
        hits = await _qdrant().query_points(
            collection_name=settings.code_index_collection,
            query=vector,
            limit=limit * SEARCH_FANOUT,  # sovra-campiono i chunk, poi aggrego per file
            with_payload=True,
        )
    except (embeddings.EmbeddingError, Exception) as exc:  # noqa: BLE001
        logger.warning("Ricerca semantica non disponibile: %s", exc)
        return []

    best: dict[str, float] = {}
    for h in hits.points:
        path = h.payload.get("path")
        if path and h.score > best.get(path, -1.0):
            best[path] = h.score
    ranked = sorted(best.items(), key=lambda kv: -kv[1])[:limit]
    return [{"path": p, "score": round(s, 4)} for p, s in ranked]
