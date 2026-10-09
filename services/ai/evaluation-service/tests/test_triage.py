"""Test unitari della logica di triage (sanitizzazione output LLM, fallback)."""
from app.services.triage import _fallback_evaluation, clamp_priority, sanitize_evaluations

TICKETS = [
    {"id": "T1", "severity": "critical"},
    {"id": "T2", "severity": "low"},
    {"id": "T3", "severity": None},
]


def test_fallback_priorita_dalla_severity():
    evals = _fallback_evaluation(TICKETS)
    by_id = {e["id"]: e for e in evals}
    assert by_id["T1"]["priority"] == 1
    assert by_id["T2"]["priority"] == 4
    assert by_id["T3"]["priority"] == 3  # severity assente -> priorità media
    assert all(e["duplicate_of"] is None for e in evals)


def test_sanitize_scarta_id_allucinati():
    evals = [
        {"id": "T1", "priority": 1},
        {"id": "INVENTATO", "priority": 1},  # id non presente nell'input
        "non-un-dict",
    ]
    cleaned = sanitize_evaluations(evals, TICKETS)
    ids = {e["id"] for e in cleaned}
    assert "INVENTATO" not in ids
    assert ids == {"T1", "T2", "T3"}  # i mancanti sono completati dal fallback


def test_sanitize_completa_i_ticket_dimenticati():
    cleaned = sanitize_evaluations([], TICKETS)
    assert {e["id"] for e in cleaned} == {"T1", "T2", "T3"}
    assert all("Fallback" in e["rationale"] for e in cleaned)


def test_clamp_priority():
    assert clamp_priority(0) == 1
    assert clamp_priority(99) == 5
    assert clamp_priority(None) == 3
    assert clamp_priority("2") == 2
    assert clamp_priority("boh") == 3
