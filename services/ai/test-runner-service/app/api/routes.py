"""Endpoint dell'Agente Tester (Test Runner).

L'avvio è un'azione umana, riservata a Sviluppatore e Scrum Master: è il gate
Human-in-the-Loop che chiude il ciclo fra la scrittura del codice e la sua
validazione. La lettura degli esiti è invece aperta a chiunque sia autenticato,
perché è evidenza da consultare, non un'azione.
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import SessionLocal
from app.services import persistence, runner, workspace
from shared.common.auth import require_roles

logger = logging.getLogger(__name__)
router = APIRouter()

# Stati in cui il Work Order non ha ancora prodotto un piano di modifiche.
_NOT_READY_STATES = ("generated", "running")


class RunTestsRequest(BaseModel):
    wo_id: str
    # Consente di eseguire la sola suite di regressione sul branch base quando il
    # branch di lavoro non esiste ancora, perché l'agente cloud non ha pubblicato.
    allow_base_branch: bool = False


async def _run_and_log(run_id: str, wo_id: str, allow_base_branch: bool) -> None:
    try:
        result = await runner.execute_test_run(
            run_id, wo_id, allow_base_branch=allow_base_branch
        )
        logger.info("Esecuzione test %s: %s", run_id, result.get("status"))
    except Exception:  # noqa: BLE001 - il task in background non deve propagare
        logger.exception("Errore nell'esecuzione dei test del Work Order %s", wo_id)


@router.post(
    "/test-runs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Avvia l'esecuzione dei test di un Work Order",
)
async def start_test_run(
    body: RunTestsRequest,
    principal: dict = Depends(require_roles("developer", "scrum_master")),
) -> dict:
    """Avvio manuale. L'esecuzione parte in background e si segue dal terminale."""
    wo = await persistence.load_workorder(body.wo_id)
    if wo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")

    if not wo.get("repo_url"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Assegna prima un repository Git al Work Order (sezione «Repository Git»)",
        )
    if wo.get("status") in _NOT_READY_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Il Work Order non è ancora stato elaborato: non c'è codice da testare",
        )
    if await persistence.has_running_run(body.wo_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un'esecuzione dei test è già in corso per questo Work Order",
        )

    base_branch = wo.get("base_branch") or "main"
    work_branch = wo.get("work_branch") or f"wo/{body.wo_id[:8]}"

    # Verifica preventiva sul remoto: costa un secondo e trasforma il fallimento
    # più probabile della funzionalità (codice non ancora pubblicato dall'agente
    # cloud) in un messaggio comprensibile invece che in un'esecuzione fallita.
    if not body.allow_base_branch:
        exists = await workspace.branch_exists_on_remote(wo["repo_url"], work_branch)
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Il branch «{work_branch}» non esiste ancora su GitHub: l'agente cloud "
                    "non ha ancora pubblicato il codice. Riprova fra qualche minuto oppure "
                    "esegui la sola suite di regressione sul branch base."
                ),
            )

    run_id = str(uuid.uuid4())
    await persistence.create_run(
        run_id,
        body.wo_id,
        repo_url=wo["repo_url"],
        base_branch=base_branch,
        branch=base_branch if body.allow_base_branch else work_branch,
        triggered_by=principal.get("uid"),
    )
    await _audit(principal.get("uid"), body.wo_id, run_id)

    asyncio.create_task(_run_and_log(run_id, body.wo_id, body.allow_base_branch))
    return {
        "status": "started",
        "run_id": run_id,
        "wo_id": body.wo_id,
        "detail": "Esecuzione dei test avviata: segui il terminale",
    }


@router.get(
    "/workorders/{wo_id}/test-runs/latest",
    summary="Ultima esecuzione dei test di un Work Order, con i singoli casi",
)
async def latest_test_run(
    wo_id: str,
    with_log: bool = Query(default=False, description="Include il log del terminale"),
    only_failed: bool = Query(
        default=False, description="Restituisce i soli test non superati"
    ),
) -> dict:
    """Endpoint del pannello. Se non ci sono esecuzioni ritorna `run: null` con
    codice 200: il pannello mostra lo stato vuoto, non un errore."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT * FROM test_runs WHERE wo_id = :wo "
                    "ORDER BY started_at DESC, id DESC LIMIT 1"
                ),
                {"wo": wo_id},
            )
        ).mappings().first()

        if row is None:
            return {"run": None, "cases": [], "lines": []}

        run = dict(row)
        log_text = run.pop("log", None) or ""

        query = "SELECT file, suite, name, status, origin, duration_ms, message FROM test_case_results WHERE run_id = :run"
        if only_failed:
            query += " AND status = 'failed'"
        query += " ORDER BY (status = 'failed') DESC, origin, file, id"
        cases = (
            await session.execute(text(query), {"run": run["id"]})
        ).mappings().all()

    return {
        "run": run,
        "cases": [dict(c) for c in cases],
        "lines": log_text.splitlines() if with_log else [],
    }


@router.get(
    "/workorders/{wo_id}/test-runs",
    summary="Storico sintetico delle esecuzioni di un Work Order",
)
async def list_test_runs(wo_id: str, limit: int = Query(default=10, ge=1, le=50)) -> dict:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, status, branch, commit_sha, total_tests, passed_tests, "
                    "failed_tests, skipped_tests, new_total, new_failed, regression_total, "
                    "regression_failed, duration_ms, error_stage, fallback_reason, "
                    "started_at, finished_at FROM test_runs WHERE wo_id = :wo "
                    "ORDER BY started_at DESC, id DESC LIMIT :lim"
                ),
                {"wo": wo_id, "lim": limit},
            )
        ).mappings().all()
    return {"wo_id": wo_id, "runs": [dict(r) for r in rows]}


@router.get("/test-runs/{run_id}", summary="Dettaglio di un'esecuzione")
async def get_test_run(run_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT * FROM test_runs WHERE id = :id"), {"id": run_id}
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Esecuzione non trovata"
            )
        run = dict(row)
        log_text = run.pop("log", None) or ""
        cases = (
            await session.execute(
                text(
                    "SELECT file, suite, name, status, origin, duration_ms, message "
                    "FROM test_case_results WHERE run_id = :run "
                    "ORDER BY (status = 'failed') DESC, origin, file, id"
                ),
                {"run": run_id},
            )
        ).mappings().all()
    return {"run": run, "cases": [dict(c) for c in cases], "lines": log_text.splitlines()}


@router.get("/test-runs/{run_id}/log", summary="Log di esecuzione per il terminale virtuale")
async def test_run_log(run_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status, log FROM test_runs WHERE id = :id"), {"id": run_id}
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Esecuzione non trovata"
        )
    return {
        "run_id": row["id"],
        "status": row["status"],
        "lines": (row["log"] or "").splitlines(),
    }


async def _audit(user_id: int | None, wo_id: str, run_id: str) -> None:
    """Traccia l'avvio nel registro delle azioni. Non deve mai bloccare l'avvio."""
    try:
        async with SessionLocal() as session:
            await session.execute(
                text(
                    "INSERT INTO audit_log (user_id, action, entity_type, entity_id, details) "
                    "VALUES (:uid, 'tests_started', 'work_order', :wo, :det)"
                ),
                {"uid": user_id, "wo": wo_id, "det": json.dumps({"run_id": run_id})},
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Registro azioni non aggiornato per il run %s", run_id)
