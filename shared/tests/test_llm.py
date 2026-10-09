"""Test del client LLM condiviso: estrazione JSON e output strutturato."""
import pytest
from pydantic import BaseModel

from shared.common import llm


# ---------------------------------------------------------------------------
# extract_json: difese contro le risposte sporche dei modelli piccoli
# ---------------------------------------------------------------------------

def test_extract_json_puro():
    assert llm.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_lista():
    assert llm.extract_json('[1, 2, 3]') == [1, 2, 3]


def test_extract_json_fenced():
    testo = 'Ecco la risposta:\n```json\n{"files": ["a.py"]}\n```\nSpero sia utile!'
    assert llm.extract_json(testo) == {"files": ["a.py"]}


def test_extract_json_annegato_nel_testo():
    testo = 'Il risultato è {"priority": 2, "id": "T1"} come richiesto.'
    assert llm.extract_json(testo) == {"priority": 2, "id": "T1"}


def test_extract_json_invalido_solleva_llmerror():
    with pytest.raises(llm.LLMError):
        llm.extract_json("non c'è nessun JSON qui")


# ---------------------------------------------------------------------------
# chat_structured: guardrail Pydantic con retry e feedback al modello
# ---------------------------------------------------------------------------

class _Schema(BaseModel):
    files: list[str]
    reasoning: str = ""


@pytest.mark.anyio
async def test_chat_structured_valida_al_primo_colpo(monkeypatch):
    async def fake_chat(system, user, **kwargs):
        return '{"files": ["a.py"], "reasoning": "ok"}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    result = await llm.chat_structured("s", "u", schema=_Schema, base_url="x", model="m")
    assert result.files == ["a.py"]


@pytest.mark.anyio
async def test_chat_structured_riprova_con_feedback(monkeypatch):
    calls = []

    async def fake_chat(system, user, **kwargs):
        calls.append(user)
        if len(calls) == 1:
            return '{"files": "non-una-lista"}'  # JSON valido ma fuori schema
        return '{"files": ["b.py"]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    result = await llm.chat_structured("s", "u", schema=_Schema, base_url="x", model="m", retries=1)
    assert result.files == ["b.py"]
    assert len(calls) == 2
    # il secondo prompt contiene il feedback sugli errori di validazione
    assert "ERRORI DI VALIDAZIONE" in calls[1]


@pytest.mark.anyio
async def test_chat_structured_riprova_su_risposta_non_json(monkeypatch):
    calls = []

    async def fake_chat(system, user, **kwargs):
        calls.append(user)
        if len(calls) == 1:
            return "chiacchiere senza JSON"  # non parsabile
        return '{"files": ["c.py"]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    result = await llm.chat_structured("s", "u", schema=_Schema, base_url="x", model="m", retries=1)
    assert result.files == ["c.py"]
    assert "NON ERA JSON VALIDO" in calls[1]


@pytest.mark.anyio
async def test_chat_structured_usa_structured_outputs(monkeypatch):
    """Ora passa il JSON Schema al campo `format` di Ollama (structured outputs)."""
    captured = {}

    async def fake_chat(system, user, **kwargs):
        captured.update(kwargs)
        return '{"files": ["a.py"]}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    await llm.chat_structured("s", "u", schema=_Schema, base_url="x", model="m")
    assert captured.get("fmt") is not None
    assert captured["fmt"]["properties"]["files"]  # è lo schema del modello
    assert captured.get("json_mode") is False  # non usa più il vecchio format="json"


@pytest.mark.anyio
async def test_chat_structured_fallisce_dopo_i_retry(monkeypatch):
    async def fake_chat(system, user, **kwargs):
        return '{"files": 42}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    with pytest.raises(llm.LLMError):
        await llm.chat_structured("s", "u", schema=_Schema, base_url="x", model="m", retries=1)


@pytest.fixture
def anyio_backend():
    return "asyncio"
