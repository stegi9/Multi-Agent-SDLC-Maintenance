"""Endpoint dell'Agente Sviluppatore (Coding Service)."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import SessionLocal
from shared.common.auth import require_roles
from app.services.executor import (
    _ReasoningLog,
    execute_workorder as run_workorder,
    select_context,
)
from shared.common.telemetry import new_run_id, run_context

logger = logging.getLogger(__name__)
router = APIRouter()


class ExecuteRequest(BaseModel):
    wo_id: str
    force: bool = False  # Rigenera: resetta un WO già elaborato e lo riesegue


async def _run_and_log(wo_id: str) -> None:
    try:
        result = await run_workorder(wo_id)
        logger.info("Esecuzione %s: %s", wo_id, result.get("status"))
    except Exception:  # noqa: BLE001 - il task in background non deve propagare
        logger.exception("Errore nell'esecuzione del Work Order %s", wo_id)


@router.post(
    "/execute",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_roles("developer", "scrum_master"))],
    summary="Avvia l'elaborazione di un Work Order (azione dello Sviluppatore)",
)
async def execute_workorder(body: ExecuteRequest) -> dict:
    """Avvio manuale dello Sviluppatore. L'elaborazione parte in background e si
    segue dal terminale del reasoning; la chiamata ritorna subito."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT status, repo_url, assignee_id FROM work_orders WHERE id = :id"),
                {"id": body.wo_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
    # Prerequisiti: senza repository non c'è dove pushare; senza sviluppatore la PR
    # non avrebbe un reviewer (gate Human-in-the-Loop). Si blocca con un messaggio chiaro.
    if not row["repo_url"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Assegna prima un repository Git al Work Order (sezione «Repository Git»)",
        )
    if not row["assignee_id"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Assegna prima uno sviluppatore al Work Order (sezione «Sviluppatore assegnato»)",
        )
    st = row["status"]
    if st == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Il Work Order è in esecuzione"
        )
    if st in ("validated", "merged"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Il Work Order è in stato '{st}': non rieseguibile",
        )
    # code_ready si riesegue solo con "Rigenera" (force): il risultato precedente
    # viene scartato e il WO torna a 'generated' prima di ripartire.
    if st == "code_ready":
        if not body.force:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Il Work Order è in stato '{st}': usa Rigenera per rieseguirlo",
            )
        async with SessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE work_orders SET status='generated', changes=NULL, reasoning_log=NULL "
                    "WHERE id = :id"
                ),
                {"id": body.wo_id},
            )
            await session.commit()
    asyncio.create_task(_run_and_log(body.wo_id))
    return {"status": "started", "wo_id": body.wo_id, "detail": "Elaborazione avviata: segui il terminale del reasoning"}


@router.get("/executions/{wo_id}/status", summary="Stato di esecuzione di un Work Order")
async def execution_status(wo_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status, changes, updated_at FROM work_orders WHERE id = :id"),
                {"id": wo_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
    data = dict(row)
    if isinstance(data.get("changes"), str):
        data["changes"] = json.loads(data["changes"])
    return data


@router.get("/executions/{wo_id}/log", summary="Log di reasoning dell'agente per il terminale virtuale")
async def execution_log(wo_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status, reasoning_log FROM work_orders WHERE id = :id"),
                {"id": wo_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
    return {
        "wo_id": row["id"],
        "status": row["status"],
        "lines": (row["reasoning_log"] or "").splitlines(),
    }

class SelectContextRequest(BaseModel):
    """Richiesta di sola selezione del contesto (diagnostica e valutazione)."""

    description: str
    ticket_id: str = ""
    experiment: str = ""
    run_id: str = ""
    config_id: str = ""
    repeat: int = 0


@router.post("/select-context", summary="Esegue la sola selezione del contesto (dry-run, nessun effetto di stato)")
async def select_context_dry_run(body: SelectContextRequest) -> dict:
    """Espone il passo di selezione dei file senza eseguire un Work Order.

    Permette di misurare precision/recall della selezione del contesto e il
    tasso di allucinazione sui path, senza
    creare Work Order ne' modificare lo stato della piattaforma. Percorre
    esattamente il codice di produzione (`select_context`).
    """
    reasoning = _ReasoningLog(None)
    with run_context(
        run_id=body.run_id or new_run_id(),
        experiment=body.experiment or "select-context",
        ticket_id=body.ticket_id,
        phase="select_context",
        config_id=body.config_id,
        repeat=body.repeat,
    ):
        try:
            ctx = await select_context(body.description, reasoning)
        except Exception as exc:  # noqa: BLE001 - l'harness registra il fallimento
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Selezione fallita: {exc}"
            ) from exc
    return {
        "ticket_id": body.ticket_id,
        "selected": ctx.selected,
        "more_candidates": ctx.more_candidates,
        "hallucinated": ctx.hallucinated,
        "graph_used": ctx.graph is not None,
        # Quale percorso ha prodotto la selezione ("hybrid" o "cve_reachability")
        # e, per il solo percorso CVE, se la dipendenza risulta raggiungibile dal
        # codice applicativo: permettono di analizzare i due percorsi separatamente.
        "path_used": ctx.path_used,
        "reachable": ctx.reachable,
        "reasoning": ctx.reasoning,
        "log": reasoning.text().splitlines(),
    }
