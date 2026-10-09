"""Lettura del report JSON prodotto dal runner di test del repository bersaglio.

Il reporter `json` di vitest produce un documento in formato jest-like:

    {"numTotalTests": 128, "success": false,
     "testResults": [
       {"name": "/percorso/assoluto/tests/auth.test.ts", "status": "failed",
        "assertionResults": [
          {"ancestorTitles": ["POST /login"], "title": "rifiuta password errata",
           "status": "failed", "duration": 42,
           "failureMessages": ["AssertionError: expected 401 ..."]}]}]}

Il modulo è puro (nessuna dipendenza da rete o database), quindi è verificabile
con dei semplici test unitari su un report di esempio.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Stati che vitest usa per un test non eseguito, ricondotti a un unico valore.
_SKIPPED_ALIASES = {"pending", "todo", "skipped", "disabled"}
_MAX_MESSAGE_CHARS = 4000


@dataclass
class TestCase:
    """Esito di un singolo test, già normalizzato per la persistenza."""

    # Il nome inizia per "Test": senza questo, pytest proverebbe a raccogliere la
    # dataclass come classe di test.
    __test__ = False

    file: str          # percorso relativo alla radice del repository, separatori POSIX
    suite: str         # describe annidati, uniti da " › "
    name: str
    status: str        # passed | failed | skipped
    duration_ms: int | None = None
    message: str = ""  # primo messaggio d'errore, troncato
    origin: str = "regression"  # new | regression, assegnato dalla classificazione


@dataclass
class ReportSummary:
    """Conteggi complessivi, ricalcolati dai singoli casi."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    files: list[str] = field(default_factory=list)


def _relative(path: str, repo_root: str) -> str:
    """Percorso relativo alla radice del repository, con separatori POSIX."""
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, repo_root)
    except ValueError:
        # Percorsi su volumi diversi (capita solo su Windows): si tiene l'originale.
        rel = path
    return rel.replace("\\", "/")


def _normalize_status(raw: str) -> str:
    status = (raw or "").lower()
    if status in _SKIPPED_ALIASES:
        return "skipped"
    if status in ("passed", "failed"):
        return status
    # Uno stato sconosciuto non deve far sparire il test: lo si tratta come fallito,
    # così resta visibile e verificabile a mano.
    return "failed" if status else "skipped"


def _first_message(messages: object, fallback: str = "") -> str:
    if isinstance(messages, list) and messages:
        text = str(messages[0])
    elif isinstance(messages, str) and messages:
        text = messages
    else:
        text = fallback
    return text[:_MAX_MESSAGE_CHARS]


def parse_report(raw: dict, repo_root: str) -> tuple[list[TestCase], ReportSummary]:
    """Normalizza il report di vitest in una lista piatta di casi.

    Difensivo per costruzione: si accede a tutti i campi con `.get()`, perché il
    formato del reporter può cambiare fra versioni. Un campo mancante impoverisce
    il dettaglio, non fa fallire l'esecuzione.
    """
    cases: list[TestCase] = []
    files: list[str] = []

    for file_result in raw.get("testResults") or []:
        if not isinstance(file_result, dict):
            continue
        file_path = _relative(str(file_result.get("name") or ""), repo_root)
        if file_path and file_path not in files:
            files.append(file_path)

        assertions = file_result.get("assertionResults") or []

        # Caso critico: il file è fallito ma non ha singoli test. Succede quando
        # il file esplode in fase di import (per esempio manca DATABASE_URL).
        # Senza questa sintesi un intero file di test sarebbe invisibile nella UI.
        if not assertions:
            if _normalize_status(str(file_result.get("status") or "")) == "failed":
                cases.append(
                    TestCase(
                        file=file_path,
                        suite="",
                        name="(errore di caricamento del file)",
                        status="failed",
                        duration_ms=None,
                        message=_first_message(
                            file_result.get("message"),
                            "Il file di test non è stato caricato correttamente.",
                        ),
                    )
                )
            continue

        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            ancestors = [str(a) for a in (assertion.get("ancestorTitles") or []) if a]
            duration = assertion.get("duration")
            cases.append(
                TestCase(
                    file=file_path,
                    suite=" › ".join(ancestors)[:500],
                    name=str(assertion.get("title") or assertion.get("fullName") or "(senza nome)")[:500],
                    status=_normalize_status(str(assertion.get("status") or "")),
                    duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
                    message=_first_message(assertion.get("failureMessages")),
                )
            )

    # I conteggi si ricalcolano dai casi e NON si leggono da numPassedTests: unica
    # fonte di verità, così il riepilogo non può divergere dall'elenco mostrato.
    summary = ReportSummary(
        total=len(cases),
        passed=sum(1 for c in cases if c.status == "passed"),
        failed=sum(1 for c in cases if c.status == "failed"),
        skipped=sum(1 for c in cases if c.status == "skipped"),
        files=files,
    )
    return cases, summary


def classify(
    cases: list[TestCase],
    changed_files: set[str],
    planned_test_files: set[str],
) -> None:
    """Marca ogni caso come 'new' (introdotto dal Work Order) o 'regression'.

    Due segnali in unione, perché nessuno dei due basta da solo:

    1. il diff Git rispetto al branch base, autoritativo finché il branch di
       lavoro esiste;
    2. i file marcati `kind='test'` nel piano dell'Agente Sviluppatore, che
       coprono il caso in cui il Work Order è già stato fuso e il diff è vuoto.

    Senza il secondo segnale, dopo il merge ogni test nuovo diventerebbe
    silenziosamente «regressione» e la distinzione sparirebbe dallo storico.
    """
    marked = {f for f in changed_files if f} | {f for f in planned_test_files if f}
    for case in cases:
        case.origin = "new" if case.file and case.file in marked else "regression"


def split_counts(cases: list[TestCase]) -> dict[str, int]:
    """Conteggi separati per test nuovi e suite di regressione."""
    new = [c for c in cases if c.origin == "new"]
    regression = [c for c in cases if c.origin == "regression"]
    return {
        "new_total": len(new),
        "new_failed": sum(1 for c in new if c.status == "failed"),
        "regression_total": len(regression),
        "regression_failed": sum(1 for c in regression if c.status == "failed"),
    }
