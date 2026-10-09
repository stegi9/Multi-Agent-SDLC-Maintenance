"""Test del parser del report di vitest.

Il parser è l'unico punto in cui una versione futura del runner di test potrebbe
rompere la funzionalità: questi test fissano il comportamento atteso, compresi i
casi degenerati (file esploso in import, campi mancanti, report vuoto).
"""
from app.services.vitest_report import (
    TestCase,
    classify,
    parse_report,
    split_counts,
)

REPO = "/testspace/repos/owner_repo"


def _report(test_results: list) -> dict:
    return {"numTotalTests": 0, "success": False, "testResults": test_results}


def test_report_nominale() -> None:
    raw = _report(
        [
            {
                "name": f"{REPO}/artifacts/api-server/tests/auth.test.ts",
                "status": "failed",
                "assertionResults": [
                    {
                        "ancestorTitles": ["POST /login"],
                        "title": "accetta credenziali valide",
                        "status": "passed",
                        "duration": 12,
                        "failureMessages": [],
                    },
                    {
                        "ancestorTitles": ["POST /login", "errori"],
                        "title": "rifiuta password errata",
                        "status": "failed",
                        "duration": 42,
                        "failureMessages": ["AssertionError: expected 401 to equal 200"],
                    },
                ],
            }
        ]
    )
    cases, summary = parse_report(raw, REPO)

    assert summary.total == 2
    assert summary.passed == 1
    assert summary.failed == 1
    # Il percorso è relativo alla radice del repository, con separatori POSIX.
    assert cases[0].file == "artifacts/api-server/tests/auth.test.ts"
    # I describe annidati sono uniti con il separatore usato anche dalla UI.
    assert cases[1].suite == "POST /login › errori"
    assert cases[1].message.startswith("AssertionError")


def test_file_esploso_in_import_resta_visibile() -> None:
    """Un file fallito senza singoli test deve produrre comunque un caso.

    È il caso più frequente quando manca DATABASE_URL: senza questa regola un
    intero file di test sparirebbe dall'interfaccia.
    """
    raw = _report(
        [
            {
                "name": f"{REPO}/artifacts/api-server/tests/db.test.ts",
                "status": "failed",
                "message": "Error: DATABASE_URL must be set",
                "assertionResults": [],
            }
        ]
    )
    cases, summary = parse_report(raw, REPO)

    assert summary.total == 1
    assert summary.failed == 1
    assert cases[0].name == "(errore di caricamento del file)"
    assert "DATABASE_URL" in cases[0].message


def test_stati_pending_e_todo_diventano_skipped() -> None:
    raw = _report(
        [
            {
                "name": f"{REPO}/tests/a.test.ts",
                "status": "passed",
                "assertionResults": [
                    {"title": "rinviato", "status": "pending"},
                    {"title": "da scrivere", "status": "todo"},
                ],
            }
        ]
    )
    cases, summary = parse_report(raw, REPO)

    assert summary.skipped == 2
    assert all(c.status == "skipped" for c in cases)


def test_durata_mancante_non_rompe() -> None:
    raw = _report(
        [
            {
                "name": f"{REPO}/tests/a.test.ts",
                "status": "passed",
                "assertionResults": [{"title": "senza durata", "status": "passed"}],
            }
        ]
    )
    cases, _ = parse_report(raw, REPO)

    assert cases[0].duration_ms is None


def test_report_vuoto() -> None:
    cases, summary = parse_report({}, REPO)

    assert cases == []
    assert summary.total == 0


def test_file_senza_assertion_ma_passato_viene_ignorato() -> None:
    """Un file passato senza test non deve generare casi fantasma."""
    raw = _report([{"name": f"{REPO}/tests/a.test.ts", "status": "passed", "assertionResults": []}])
    cases, summary = parse_report(raw, REPO)

    assert summary.total == 0


def test_classificazione_da_diff_git() -> None:
    cases = [
        TestCase(file="tests/nuovo.test.ts", suite="", name="a", status="passed"),
        TestCase(file="tests/vecchio.test.ts", suite="", name="b", status="failed"),
    ]
    classify(cases, changed_files={"tests/nuovo.test.ts"}, planned_test_files=set())

    assert cases[0].origin == "new"
    assert cases[1].origin == "regression"


def test_classificazione_dal_piano_quando_il_diff_e_vuoto() -> None:
    """Dopo il merge del Work Order il diff è vuoto: deve bastare il piano.

    È la ragione per cui l'Agente Sviluppatore marca i file con kind='test'.
    """
    cases = [TestCase(file="tests/nuovo.test.ts", suite="", name="a", status="failed")]
    classify(cases, changed_files=set(), planned_test_files={"tests/nuovo.test.ts"})

    assert cases[0].origin == "new"


def test_conteggi_separati() -> None:
    cases = [
        TestCase(file="a", suite="", name="1", status="passed", origin="new"),
        TestCase(file="a", suite="", name="2", status="failed", origin="new"),
        TestCase(file="b", suite="", name="3", status="passed", origin="regression"),
        TestCase(file="b", suite="", name="4", status="failed", origin="regression"),
        TestCase(file="b", suite="", name="5", status="failed", origin="regression"),
    ]
    counts = split_counts(cases)

    assert counts == {
        "new_total": 2,
        "new_failed": 1,
        "regression_total": 3,
        "regression_failed": 2,
    }
