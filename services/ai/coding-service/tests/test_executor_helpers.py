"""Test unitari degli helper dell'Agente Sviluppatore."""
from app.services.executor import (
    ChangesResult,
    FileSelection,
    _dependencies_of,
    _verify_grounding,
    _wo_description,
)


def test_wo_description_multi_ticket():
    payload = {
        "tickets": [
            {"title": "Bug login", "type": "bug", "severity": "high", "description": "Crash al login"},
            {"title": "CVE-2026-1", "type": "cve", "severity": "critical", "description": "Libreria vulnerabile"},
        ],
        "instructions": "Priorità alla CVE",
        "rationale": "Stesso modulo auth",
    }
    titles, desc = _wo_description(payload)
    assert "Bug login" in titles and "CVE-2026-1" in titles
    assert "TICKET 1" in desc and "TICKET 2" in desc
    assert "RAGGRUPPAMENTO: Stesso modulo auth" in desc
    assert "Priorità alla CVE" in desc


def test_wo_description_compatibilita_ticket_singolo():
    payload = {"ticket": {"title": "Solo uno", "type": "bug"}}
    titles, desc = _wo_description(payload)
    assert titles == "Solo uno"
    assert "TICKET 1" in desc


def test_wo_description_senza_ticket():
    titles, _ = _wo_description({})
    assert titles == "(senza ticket)"


def test_dependencies_of_ordina_per_grado_ed_esclude_selezionati():
    graph = {
        "files": {
            "a.py": {"depends_on": ["b.py", "c.py"], "degree": 1},
            "b.py": {"depends_on": [], "degree": 5},
            "c.py": {"depends_on": [], "degree": 9},
        }
    }
    deps = _dependencies_of(["a.py"], graph)
    assert deps == ["c.py", "b.py"]  # più connesso prima
    # i file già selezionati non compaiono tra le dipendenze
    graph["files"]["a.py"]["depends_on"].append("a.py")
    assert "a.py" not in _dependencies_of(["a.py"], graph)


def test_dependencies_of_senza_grafo():
    assert _dependencies_of(["a.py"], None) == []


def test_schemi_llm_validano_e_normalizzano():
    sel = FileSelection.model_validate({"files": ["a.py"], "reasoning": "ok"})
    assert sel.files == ["a.py"]
    res = ChangesResult.model_validate(
        {"summary": "s", "changes": [{"file": "a.py", "action": "modify", "description": "d"}]}
    )
    assert res.changes[0].code_sketch == ""


def test_schema_changes_rifiuta_action_invalida():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ChangesResult.model_validate(
            {"summary": "s", "changes": [{"file": "a.py", "action": "esplodi"}]}
        )


def test_schema_changes_richiede_summary():
    import pytest
    from pydantic import ValidationError

    # il summary resta obbligatorio: una risposta senza summary non valida
    with pytest.raises(ValidationError):
        ChangesResult.model_validate({})


def test_schema_changes_permette_astensione():
    # ANTI-ALLUCINAZIONE: una risposta senza modifiche è LECITA se l'agente
    # dichiara contesto insufficiente, invece di inventare modifiche.
    res = ChangesResult.model_validate(
        {"summary": "non ho abbastanza contesto", "changes": [], "needs_more_context": True,
         "missing": "serve il file di routing"}
    )
    assert res.changes == []
    assert res.needs_more_context is True
    assert "routing" in res.missing


# ---------------------------------------------------------------------------
# _verify_grounding: verifica deterministica anti-allucinazione (senza LLM)
# ---------------------------------------------------------------------------

def test_grounding_ok_su_file_letti():
    changes = [{"file": "a.py", "action": "modify"}, {"file": "nuovo.py", "action": "create"}]
    g = _verify_grounding(changes, read_files={"a.py"}, known_files={"a.py", "b.py"})
    assert g["ok"] is True and g["warnings"] == []


def test_grounding_segnala_file_inesistente():
    changes = [{"file": "fantasma.py", "action": "modify"}]
    g = _verify_grounding(changes, read_files=set(), known_files={"a.py"})
    assert g["ok"] is False
    assert "non presente nella topologia" in g["warnings"][0]


def test_grounding_segnala_file_noto_ma_non_letto():
    # il file esiste nel repo ma l'agente non ne ha letto il contenuto:
    # ne "immagina" la struttura -> potenziale allucinazione
    changes = [{"file": "b.py", "action": "modify"}]
    g = _verify_grounding(changes, read_files={"a.py"}, known_files={"a.py", "b.py"})
    assert g["ok"] is False
    assert "senza averne letto il contenuto" in g["warnings"][0]
