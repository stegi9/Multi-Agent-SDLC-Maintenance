"""Test del chunking per l'indicizzazione nel Doppio RAG."""
from app.services.rag_store import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text, collection_for

import pytest


def test_testo_vuoto_nessun_chunk():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_testo_corto_un_solo_chunk():
    assert chunk_text("breve") == ["breve"]


def test_testo_lungo_chunk_sovrapposti():
    testo = "x" * (CHUNK_SIZE * 2)
    chunks = chunk_text(testo)
    assert len(chunks) >= 2
    assert all(len(c) <= CHUNK_SIZE for c in chunks)
    # l'overlap garantisce continuità: la fine di un chunk riappare nel successivo
    step = CHUNK_SIZE - CHUNK_OVERLAP
    ricostruito = chunks[0] + "".join(c[CHUNK_OVERLAP:] for c in chunks[1:])
    assert ricostruito == testo


def test_collection_for():
    assert collection_for("main") == "rag_main"
    assert collection_for("future") == "rag_future"
    with pytest.raises(ValueError):
        collection_for("altro")
