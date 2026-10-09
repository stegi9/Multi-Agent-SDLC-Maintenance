"""Endpoint del Generatore di Work Order (raggruppamento multi-ticket)."""
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import bindparam, text

from app.core.config import settings
from shared.common.auth import require_roles
from app.core.db import SessionLocal
from app.kafka.producer import publish
from app.services import generator

logger = logging.getLogger(__name__)
router = APIRouter()

# Colonne del Work Order restituite dalle query (incluse quelle di gestione Git).
# Le sottoquery correlate risolvono nome/handle dello sviluppatore assegnato e
# funzionano sia con la tabella non aliasata sia quando è aliasata ``wo``.
WO_SELECT_COLUMNS = (
    "id, status, payload, changes, repo_url, base_branch, work_branch, "
    "code_pr_url, code_pr_state, docs_pr_url, docs_pr_state, assignee_id, "
    "(SELECT username FROM users WHERE id = assignee_id) AS assignee_username, "
    "(SELECT github_username FROM users WHERE id = assignee_id) AS assignee_github, "
    "created_at, updated_at"
)


class PlanRequest(BaseModel):
    instructions: str = ""


class GenerateRequest(BaseModel):
    ticket_ids: list[str] = []
    ticket_id: str | None = None  # compatibilità: singolo ticket
    instructions: str = ""


class AddTicketRequest(BaseModel):
    ticket_id: str
    move: bool = False  # conferma spostamento se il ticket è in un altro Work Order


class WorkOrderTicket(BaseModel):
    id: str
    title: str | None = None
    status: str | None = None
    domain: str | None = None


class WorkOrder(BaseModel):
    id: str
    status: str
    tickets: list[WorkOrderTicket] = []
    payload: dict
    changes: list | dict | None = None
    rationale: str | None = None
    # Gestione Git
    repo_url: str | None = None
    base_branch: str | None = None
    work_branch: str | None = None
    code_pr_url: str | None = None
    code_pr_state: str = "none"
    docs_pr_url: str | None = None
    docs_pr_state: str = "none"
    # Assegnazione allo sviluppatore (reviewer della PR)
    assignee_id: int | None = None
    assignee_username: str | None = None
    assignee_github: str | None = None
    created_at: datetime
    updated_at: datetime


async def _wo_tickets(session, wo_id: str) -> list[WorkOrderTicket]:
    rows = (
        await session.execute(
            text(
                "SELECT t.id, t.title, t.status, t.domain FROM work_order_tickets wot "
                "JOIN tickets t ON t.id = wot.ticket_id WHERE wot.wo_id = :w ORDER BY t.priority ASC"
            ),
            {"w": wo_id},
        )
    ).mappings().all()
    return [WorkOrderTicket(**dict(r)) for r in rows]


async def _row_to_wo(session, row: dict) -> WorkOrder:
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    changes = json.loads(row["changes"]) if isinstance(row.get("changes"), str) else row.get("changes")
    return WorkOrder(
        id=row["id"],
        status=row["status"],
        tickets=await _wo_tickets(session, row["id"]),
        payload=payload,
        changes=changes,
        rationale=(payload or {}).get("rationale"),
        repo_url=row.get("repo_url"),
        base_branch=row.get("base_branch"),
        work_branch=row.get("work_branch"),
        code_pr_url=row.get("code_pr_url"),
        code_pr_state=row.get("code_pr_state") or "none",
        docs_pr_url=row.get("docs_pr_url"),
        docs_pr_state=row.get("docs_pr_state") or "none",
        assignee_id=row.get("assignee_id"),
        assignee_username=row.get("assignee_username"),
        assignee_github=row.get("assignee_github"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("/plan", dependencies=[Depends(require_roles("scrum_master"))], summary="Agente di Pianificazione: analizza i ticket approvati e crea i Work Order raggruppati")
async def plan(body: PlanRequest) -> dict:
    return await generator.plan_workorders(body.instructions)


@router.post(
    "/generate",
    response_model=WorkOrder,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Crea un Work Order da uno o più ticket (uso manuale/compatibilità)",
)
async def generate_workorder(body: GenerateRequest) -> WorkOrder:
    ticket_ids = body.ticket_ids or ([body.ticket_id] if body.ticket_id else [])
    if not ticket_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nessun ticket indicato")
    try:
        created = await generator.create_workorder(ticket_ids, body.instructions)
    except generator.TicketNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket non trovato")
    except generator.WorkOrderExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Un ticket è già in un Work Order attivo ({exc.wo_id})",
        )
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(f"SELECT {WO_SELECT_COLUMNS} FROM work_orders WHERE id = :id"),
                {"id": created["wo_id"]},
            )
        ).mappings().first()
        return await _row_to_wo(session, dict(row))


@router.get("/tickets/eligible", summary="Ticket approvati non ancora assegnati a un Work Order")
async def eligible_tickets() -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, title, severity, domain, status, priority FROM tickets "
                    "WHERE status IN ('approved', 'analyzed') "
                    "AND id NOT IN (SELECT ticket_id FROM work_order_tickets) "
                    "ORDER BY priority ASC, imported_at ASC"
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


@router.get("/workorders/{wo_id}/assignable", summary="Ticket assegnabili a questo Work Order (liberi o in altri WO)")
async def assignable_tickets(wo_id: str) -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.id, t.title, t.domain, t.status, "
                    "(SELECT wot2.wo_id FROM work_order_tickets wot2 WHERE wot2.ticket_id = t.id LIMIT 1) AS current_wo_id "
                    "FROM tickets t "
                    "WHERE t.status NOT IN ('completed', 'rejected', 'postponed', 'deduplicated') "
                    "AND t.id NOT IN (SELECT ticket_id FROM work_order_tickets WHERE wo_id = :w) "
                    "ORDER BY t.priority ASC, t.imported_at ASC"
                ),
                {"w": wo_id},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


@router.post("/workorders/{wo_id}/tickets", summary="Aggiunge (o sposta) un ticket nel Work Order")
async def add_ticket(wo_id: str, body: AddTicketRequest) -> dict:
    try:
        return await generator.add_ticket(wo_id, body.ticket_id, move=body.move)
    except generator.WorkOrderNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
    except generator.TicketNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket non trovato")
    except generator.TicketInOtherWorkOrder as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Il ticket è nel Work Order {exc.wo_id[:8]}: confermare lo spostamento",
        )


@router.delete("/workorders/{wo_id}/tickets/{ticket_id}", summary="Rimuove un ticket dal Work Order")
async def remove_ticket(wo_id: str, ticket_id: str) -> dict:
    try:
        return await generator.remove_ticket(wo_id, ticket_id)
    except generator.WorkOrderNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")


@router.delete("/workorders/{wo_id}", dependencies=[Depends(require_roles("scrum_master"))], summary="Elimina un Work Order (i ticket tornano disponibili)")
async def delete_workorder(wo_id: str) -> dict:
    try:
        return await generator.delete_workorder(wo_id)
    except generator.WorkOrderNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")


# ---------------------------------------------------------------------------
# Gestione Git: assegnazione manuale del repository al Work Order
# ---------------------------------------------------------------------------

class RepoAssignRequest(BaseModel):
    repo_url: str
    base_branch: str = ""


def _repo_display_name(url: str) -> str:
    """Nome leggibile per un URL repo (owner/repo), senza .git finale."""
    u = (url or "").strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    segs = [s for s in re.split(r"[/:]+", u) if s]
    return "/".join(segs[-2:]) if len(segs) >= 2 else (segs[-1] if segs else url)


@router.get("/repos", summary="Repository Git configurati (per l'assegnazione manuale al Work Order)")
async def list_repos() -> list[dict]:
    """Elenca i repository disponibili, letti da `repo.git_url` delle impostazioni
    di piattaforma (un progetto può dichiarare più repository)."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT setting_value FROM platform_settings WHERE setting_key = 'repo.git_url'")
            )
        ).first()
    raw = row[0] if row else ""
    urls = [u for u in re.split(r"[\s,]+", (raw or "").strip()) if u]
    return [{"url": u, "name": _repo_display_name(u)} for u in urls]


@router.put(
    "/workorders/{wo_id}/repo",
    response_model=WorkOrder,
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Assegna manualmente un repository Git al Work Order (Scrum Master)",
)
async def assign_repo(wo_id: str, body: RepoAssignRequest) -> WorkOrder:
    repo_url = body.repo_url.strip()
    if not repo_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="repo_url mancante")
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status FROM work_orders WHERE id = :id"), {"id": wo_id}
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
        # Il repo si assegna finché il codice non è già stato validato/unito: dopo
        # non avrebbe senso cambiare il target delle Pull Request.
        if row["status"] in ("validated", "merged"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Work Order in stato '{row['status']}': repository non più modificabile",
            )
        # Branch di lavoro deterministico: gli agenti cloud esterni ci pushano il codice.
        work_branch = f"wo/{wo_id[:8]}"
        await session.execute(
            text("UPDATE work_orders SET repo_url = :r, base_branch = :b, work_branch = :w WHERE id = :id"),
            {"r": repo_url, "b": body.base_branch.strip() or None, "w": work_branch, "id": wo_id},
        )
        await session.commit()
        updated = (
            await session.execute(
                text(f"SELECT {WO_SELECT_COLUMNS} FROM work_orders WHERE id = :id"), {"id": wo_id}
            )
        ).mappings().first()
        return await _row_to_wo(session, dict(updated))


# ---------------------------------------------------------------------------
# Assegnazione dello sviluppatore al Work Order (reviewer della Pull Request)
# ---------------------------------------------------------------------------

class DeveloperAssignRequest(BaseModel):
    assignee_id: int | None = None  # None per rimuovere l'assegnazione


@router.get("/developers", summary="Sviluppatori assegnabili a un Work Order")
async def list_developers() -> list[dict]:
    """Utenti con ruolo 'developer' attivi (id, username, dominio, handle GitHub)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT u.id, u.username, u.domain, u.github_username "
                    "FROM users u "
                    "JOIN user_roles ur ON ur.user_id = u.id "
                    "JOIN roles r ON r.id = ur.role_id "
                    "WHERE r.name = 'developer' AND u.is_active = TRUE "
                    "ORDER BY u.username"
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


@router.put(
    "/workorders/{wo_id}/developer",
    response_model=WorkOrder,
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Assegna (o rimuove) lo sviluppatore responsabile del Work Order (Scrum Master)",
)
async def assign_developer(wo_id: str, body: DeveloperAssignRequest) -> WorkOrder:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status FROM work_orders WHERE id = :id"), {"id": wo_id}
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
        if row["status"] == "merged":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Work Order già unito: assegnazione non più modificabile",
            )
        if body.assignee_id is not None:
            dev = (
                await session.execute(
                    text(
                        "SELECT u.id FROM users u "
                        "JOIN user_roles ur ON ur.user_id = u.id "
                        "JOIN roles r ON r.id = ur.role_id "
                        "WHERE u.id = :id AND r.name = 'developer' AND u.is_active = TRUE"
                    ),
                    {"id": body.assignee_id},
                )
            ).first()
            if dev is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="L'utente indicato non è uno sviluppatore attivo",
                )
        await session.execute(
            text("UPDATE work_orders SET assignee_id = :a WHERE id = :id"),
            {"a": body.assignee_id, "id": wo_id},
        )
        await session.commit()
        updated = (
            await session.execute(
                text(f"SELECT {WO_SELECT_COLUMNS} FROM work_orders WHERE id = :id"), {"id": wo_id}
            )
        ).mappings().first()
        return await _row_to_wo(session, dict(updated))


class ValidateRequest(BaseModel):
    approve: bool
    note: str = ""


@router.post(
    "/workorders/{wo_id}/validate",
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Validazione Human-in-the-Loop dello Scrum Master sul codice prodotto",
)
async def validate_workorder(wo_id: str, body: ValidateRequest) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, status, changes, code_pr_state FROM work_orders WHERE id = :id"),
                {"id": wo_id},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
        if row["status"] != "code_ready":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Il Work Order è in stato '{row['status']}': si valida solo da 'code_ready'",
            )
        # Gate Human-in-the-Loop: si valida solo dopo che l'agente ha aperto la PR
        # su GitHub (code_pr_state='open'), così lo Scrum Master rivede il codice reale.
        if row["code_pr_state"] != "open":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La Pull Request non è ancora aperta su GitHub: attendi che l'agente pubblichi il codice",
            )
        ticket_ids = (
            await session.execute(
                text("SELECT ticket_id FROM work_order_tickets WHERE wo_id = :w"), {"w": wo_id}
            )
        ).scalars().all()

        if body.approve:
            await session.execute(
                text("UPDATE work_orders SET status = 'validated' WHERE id = :id"), {"id": wo_id}
            )
            new_ticket_status = "completed"
        else:
            await session.execute(
                text("UPDATE work_orders SET status = 'failed' WHERE id = :id"), {"id": wo_id}
            )
            new_ticket_status = "analyzed"
        for tid in ticket_ids:
            await session.execute(
                text("UPDATE tickets SET status = :s WHERE id = :id"),
                {"s": new_ticket_status, "id": tid},
            )
        await session.commit()

    if body.approve:
        changes = row["changes"]
        if isinstance(changes, str):
            changes = json.loads(changes)
        try:
            await publish(
                "code.validated",
                {
                    "event_id": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "produced_by": settings.service_name,
                    "wo_id": wo_id,
                    "ticket_ids": list(ticket_ids),
                    "summary": (changes or {}).get("summary", ""),
                    "note": body.note,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("Kafka non disponibile: evento code.validated non pubblicato")
    else:
        # Rifiuto: la PR aperta va chiusa (git_flow reagisce a code.rejected).
        try:
            await publish(
                "code.rejected",
                {
                    "event_id": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "produced_by": settings.service_name,
                    "wo_id": wo_id,
                    "ticket_ids": list(ticket_ids),
                    "note": body.note,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("Kafka non disponibile: evento code.rejected non pubblicato")

    return {
        "wo_id": wo_id,
        "status": "validated" if body.approve else "failed",
        "tickets_updated": len(ticket_ids),
        "detail": "Codice approvato: la PR viene fusa e parte la documentazione"
        if body.approve
        else "Codice rifiutato: la PR viene chiusa e i ticket tornano in stato 'analyzed'",
    }


@router.get("/workorders", response_model=list[WorkOrder], summary="Elenca i Work Order generati")
async def list_workorders(
    domain: str | None = None, repo_url: str | None = None, limit: int = 200
) -> list[WorkOrder]:
    query = (
        "SELECT DISTINCT wo.id, wo.status, wo.payload, wo.changes, wo.repo_url, wo.base_branch, "
        "wo.work_branch, wo.code_pr_url, wo.code_pr_state, wo.docs_pr_url, wo.docs_pr_state, "
        "wo.assignee_id, "
        "(SELECT username FROM users WHERE id = wo.assignee_id) AS assignee_username, "
        "(SELECT github_username FROM users WHERE id = wo.assignee_id) AS assignee_github, "
        "wo.created_at, wo.updated_at FROM work_orders wo "
    )
    params: dict = {"limit": min(limit, 500)}
    if domain:
        query += (
            "JOIN work_order_tickets wot ON wot.wo_id = wo.id "
            "JOIN tickets t ON t.id = wot.ticket_id AND t.domain = :dom "
        )
        params["dom"] = domain.strip().lower()
    if repo_url:
        # Filtro per progetto attivo (repository)
        query += "WHERE wo.repo_url = :repo "
        params["repo"] = repo_url.strip()
    query += "ORDER BY wo.created_at DESC LIMIT :limit"
    async with SessionLocal() as session:
        rows = (await session.execute(text(query), params)).mappings().all()
        return [await _row_to_wo(session, dict(r)) for r in rows]


def _as_list(value) -> list:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


@router.get(
    "/workorders/{wo_id}/handoff",
    summary="JSON completo del Work Order da inviare agli agenti cloud esterni per la scrittura del codice",
)
async def workorder_handoff(wo_id: str) -> dict:
    async with SessionLocal() as session:
        wo = (
            await session.execute(
                text(
                    "SELECT id, status, payload, changes, repo_url, base_branch, work_branch, "
                    "created_at, updated_at FROM work_orders WHERE id = :id"
                ),
                {"id": wo_id},
            )
        ).mappings().first()
        if wo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")

        tickets = (
            await session.execute(
                text(
                    "SELECT t.id, t.external_id, t.source, t.type, t.title, t.description, "
                    "t.severity, t.domain, t.priority, t.status "
                    "FROM work_order_tickets wot JOIN tickets t ON t.id = wot.ticket_id "
                    "WHERE wot.wo_id = :w ORDER BY t.priority ASC, t.imported_at ASC"
                ),
                {"w": wo_id},
            )
        ).mappings().all()
        ticket_ids = [t["id"] for t in tickets]

        impacts: dict[str, dict] = {}
        if ticket_ids:
            rows = (
                await session.execute(
                    text(
                        "SELECT ia.ticket_id, ia.impact_percentage, ia.decision, ia.affected_files, ia.risk_notes "
                        "FROM impact_analyses ia "
                        "JOIN (SELECT ticket_id, MAX(created_at) mc FROM impact_analyses "
                        "      WHERE ticket_id IN :ids GROUP BY ticket_id) last "
                        "ON last.ticket_id = ia.ticket_id AND last.mc = ia.created_at"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": ticket_ids},
                )
            ).mappings().all()
            for r in rows:
                impacts[r["ticket_id"]] = {
                    "impact_percentage": float(r["impact_percentage"]),
                    "decision": r["decision"],
                    "affected_files": _as_list(r["affected_files"]),
                    "risk_notes": r["risk_notes"],
                }

        repo_rows = (
            await session.execute(
                text(
                    "SELECT setting_key, setting_value FROM platform_settings "
                    "WHERE setting_key IN ('repo.git_url', 'repo.branch')"
                )
            )
        ).all()
    repo = {k.split(".")[1]: v for k, v in repo_rows}
    # Il repository del WO (assegnazione manuale) ha la precedenza su quello globale.
    target_git_url = wo["repo_url"] or repo.get("git_url")
    target_branch = wo["base_branch"] or repo.get("branch")

    payload = wo["payload"] if isinstance(wo["payload"], dict) else json.loads(wo["payload"])
    changes = wo["changes"]
    if isinstance(changes, str):
        changes = json.loads(changes)
    changes = changes or {}

    # unione di tutti i file coinvolti (da impatto + da modifiche generate)
    affected: set[str] = set()
    for imp in impacts.values():
        affected.update(imp.get("affected_files") or [])
    for ch in changes.get("changes", []):
        if ch.get("file"):
            affected.add(ch["file"])

    return {
        "work_order_id": wo["id"],
        "status": wo["status"],
        "created_at": wo["created_at"].isoformat() if wo["created_at"] else None,
        "updated_at": wo["updated_at"].isoformat() if wo["updated_at"] else None,
        "rationale": payload.get("rationale", ""),
        "instructions": payload.get("instructions", ""),
        "target_repository": {"git_url": target_git_url, "branch": target_branch, "work_branch": wo["work_branch"]},
        "tickets": [
            {
                "id": t["id"],
                "external_id": t["external_id"],
                "source": t["source"],
                "type": t["type"],
                "title": t["title"],
                "description": t["description"],
                "severity": t["severity"],
                "domain": t["domain"],
                "priority": t["priority"],
                "status": t["status"],
                "impact_analysis": impacts.get(t["id"]),
            }
            for t in tickets
        ],
        "affected_files": sorted(affected),
        "code_changes": {
            "summary": changes.get("summary", ""),
            "model": changes.get("model"),
            "generated_by": changes.get("generated_by"),
            "files_consulted": changes.get("files_consulted", []),
            "generated_at": changes.get("generated_at"),
            "dispatch_status": changes.get("dispatch_status"),
            "changes": changes.get("changes", []),
        },
        "handoff": {
            "target": "external cloud coding agents",
            "note": "JSON di consegna: contiene contesto dei ticket, analisi di impatto e modifiche "
            "proposte (con bozze di codice). Gli agenti esterni scrivono il codice nel repository indicato.",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/workorders/{wo_id}", response_model=WorkOrder, summary="Dettaglio di un Work Order")
async def get_workorder(wo_id: str) -> WorkOrder:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(f"SELECT {WO_SELECT_COLUMNS} FROM work_orders WHERE id = :id"),
                {"id": wo_id},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work Order non trovato")
        return await _row_to_wo(session, dict(row))
