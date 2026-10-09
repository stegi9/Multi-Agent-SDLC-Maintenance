"""Scritture su MySQL dell'Agente Tester: esecuzioni e singoli casi di test."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.vitest_report import TestCase

logger = logging.getLogger(__name__)

# Intervallo minimo fra due scritture del log su database. L'output del gestore
# di pacchetti è molto più verboso del ragionamento dell'Agente Sviluppatore:
# riscrivere l'intera colonna a ogni riga sarebbe sprecato.
_FLUSH_INTERVAL_SECONDS = 0.8


class TestLog:
    """Log testuale dell'esecuzione, mostrato nel terminale del pannello.

    Stessa strategia del log di ragionamento dell'Agente Sviluppatore: lo stato
    autoritativo vive in memoria e il database ne è la proiezione, riscritta per
    intero a ogni flush con una sessione nuova, così l'avanzamento è visibile
    mentre l'esecuzione è ancora in corso. Il log non può mai far fallire il run.

    Unica differenza voluta: le scritture sono raggruppate nel tempo, tranne le
    righe di fase (che iniziano con «»») che vengono scritte subito.
    """

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id
        self.lines: list[str] = []
        self._last_flush = 0.0

    async def add(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.lines.append(f"[{stamp}] {message}")
        logger.info("[test] %s", message)
        immediate = message.startswith("»")
        if immediate or (time.monotonic() - self._last_flush) >= _FLUSH_INTERVAL_SECONDS:
            await self.flush()

    async def flush(self) -> None:
        if not self.run_id:
            return
        self._last_flush = time.monotonic()
        try:
            async with SessionLocal() as session:
                await session.execute(
                    text("UPDATE test_runs SET log = :lg WHERE id = :id"),
                    {"lg": self.text(), "id": self.run_id},
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - il log non è mai critico
            logger.warning("Impossibile persistere il log del run %s", self.run_id)

    def text(self) -> str:
        return "\n".join(self.lines)


async def load_workorder(wo_id: str) -> dict | None:
    """Dati del Work Order necessari all'esecuzione (repository e branch)."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, ticket_id, status, repo_url, base_branch, work_branch, changes "
                    "FROM work_orders WHERE id = :id"
                ),
                {"id": wo_id},
            )
        ).mappings().first()
    return dict(row) if row else None


async def create_run(
    run_id: str,
    wo_id: str,
    *,
    repo_url: str,
    base_branch: str,
    branch: str,
    triggered_by: int | None,
    trigger_kind: str = "manual",
) -> None:
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO test_runs (id, wo_id, status, trigger_kind, triggered_by, "
                "repo_url, branch, base_branch) "
                "VALUES (:id, :wo, 'running', :tk, :by, :url, :br, :base)"
            ),
            {
                "id": run_id,
                "wo": wo_id,
                "tk": trigger_kind,
                "by": triggered_by,
                "url": repo_url,
                "br": branch,
                "base": base_branch,
            },
        )
        await session.commit()


async def has_running_run(wo_id: str) -> bool:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id FROM test_runs WHERE wo_id = :wo AND status = 'running' LIMIT 1"),
                {"wo": wo_id},
            )
        ).mappings().first()
    return row is not None


async def finish_run(
    run_id: str,
    *,
    status: str,
    log: TestLog,
    branch: str = "",
    commit_sha: str = "",
    fallback_reason: str = "",
    error_stage: str = "",
    error_detail: str = "",
    typecheck_status: str = "skipped",
    typecheck_output: str = "",
    build_status: str = "skipped",
    build_output: str = "",
    counts: dict[str, int] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Chiude un'esecuzione scrivendone l'esito completo."""
    counts = counts or {}
    async with SessionLocal() as session:
        await session.execute(
            text(
                "UPDATE test_runs SET status = :st, log = :lg, branch = COALESCE(NULLIF(:br,''), branch), "
                "commit_sha = :sha, fallback_reason = NULLIF(:fb,''), "
                "error_stage = NULLIF(:es,''), error_detail = NULLIF(:ed,''), "
                "typecheck_status = :tcs, typecheck_output = NULLIF(:tco,''), "
                "build_status = :bs, build_output = NULLIF(:bo,''), "
                "total_tests = :tot, passed_tests = :pas, failed_tests = :fai, skipped_tests = :ski, "
                "new_total = :nt, new_failed = :nf, regression_total = :rt, regression_failed = :rf, "
                "duration_ms = :dur, finished_at = CURRENT_TIMESTAMP WHERE id = :id"
            ),
            {
                "st": status,
                "lg": log.text(),
                "br": branch,
                "sha": commit_sha,
                "fb": fallback_reason,
                "es": error_stage,
                "ed": (error_detail or "")[:60000],
                "tcs": typecheck_status,
                "tco": (typecheck_output or "")[:60000],
                "bs": build_status,
                "bo": (build_output or "")[:60000],
                "tot": counts.get("total", 0),
                "pas": counts.get("passed", 0),
                "fai": counts.get("failed", 0),
                "ski": counts.get("skipped", 0),
                "nt": counts.get("new_total", 0),
                "nf": counts.get("new_failed", 0),
                "rt": counts.get("regression_total", 0),
                "rf": counts.get("regression_failed", 0),
                "dur": duration_ms,
                "id": run_id,
            },
        )
        await session.commit()


async def save_cases(run_id: str, cases: list[TestCase]) -> None:
    """Sostituisce i casi del run con quelli appena misurati."""
    if not cases:
        return
    rows = [
        {
            "run": run_id,
            "file": case.file[:500],
            "suite": (case.suite or "")[:500] or None,
            "name": case.name[:500],
            "status": case.status,
            "origin": case.origin,
            "dur": case.duration_ms,
            "msg": (case.message or "") or None,
        }
        for case in cases
    ]
    async with SessionLocal() as session:
        await session.execute(
            text("DELETE FROM test_case_results WHERE run_id = :run"), {"run": run_id}
        )
        # Inserimento in blocchi: una suite può contenere centinaia di casi.
        for start in range(0, len(rows), 200):
            await session.execute(
                text(
                    "INSERT INTO test_case_results "
                    "(run_id, file, suite, name, status, origin, duration_ms, message) "
                    "VALUES (:run, :file, :suite, :name, :status, :origin, :dur, :msg)"
                ),
                rows[start : start + 200],
            )
        await session.commit()
