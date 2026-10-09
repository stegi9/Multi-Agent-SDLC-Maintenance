"""Helper condiviso per il calcolo degli embedding via Ollama.

Un'unica implementazione usata sia dal RAG documentale (rag-db-manager) sia
dall'indice semantico del codice (topology-service): entrambi calcolano gli
embedding on-premise tramite l'endpoint /api/embed del modello configurato.
"""
import httpx

# Il calcolo degli embedding su GPU locale è veloce, ma un batch grande di
# chunk può richiedere qualche secondo: timeout generoso ma non infinito.
EMBED_TIMEOUT = 120.0


class EmbeddingError(RuntimeError):
    """L'endpoint di embedding non è raggiungibile o ha risposto male."""


async def embed(texts: list[str], *, base_url: str, model: str) -> list[list[float]]:
    """Calcola gli embedding di una lista di testi via Ollama (/api/embed).

    Ritorna una lista di vettori (uno per testo, stesso ordine dell'input).
    Solleva EmbeddingError se il servizio non risponde o l'output è vuoto.
    """
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as http:
            response = await http.post(
                f"{base_url.rstrip('/')}/api/embed",
                json={"model": model, "input": texts},
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise EmbeddingError(
            f"Servizio di embedding non raggiungibile su {base_url} (modello {model}): {exc}"
        ) from exc
    vectors = data.get("embeddings")
    if not vectors or len(vectors) != len(texts):
        raise EmbeddingError(
            f"Il modello di embedding {model!r} ha restituito {len(vectors or [])} vettori "
            f"per {len(texts)} testi: risposta inutilizzabile"
        )
    return vectors


async def embedding_dimension(*, base_url: str, model: str) -> int:
    """Ricava la dimensione dei vettori del modello embeddando una stringa sonda.

    Evita di cablare a mano la dimensione (768 per nomic-embed-text, 1024 per
    bge-m3, ...): l'indice si adatta a qualunque modello configurato.
    """
    probe = await embed(["dimension probe"], base_url=base_url, model=model)
    return len(probe[0])
