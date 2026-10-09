"""Creazione e gestione dei Work Order.

Un Work Order può raggruppare più ticket: l'Agente di Pianificazione analizza
i ticket approvati e li raggruppa (stesso modulo/file impattati, dominio,
dipendenze). La membership resta modificabile dallo Scrum Master.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import bindparam, text

from app.core.config import settings
from app.core.db import SessionLocal
from app.kafka.producer import publish
from shared.common import llm

logger = logging.getLogger(__name__)

# stati dei ticket che possono entrare in un Work Order
ELIGIBLE_STATUSES = ("approved", "analyzed")

GROUPING_PROMPT = """Sei l'Agente di Pianificazione dei Work Order di una piattaforma SDLC.
Ricevi una lista di ticket approvati (con dominio e file impattati stimati).
Raggruppa in Work Order i ticket che conviene sviluppare INSIEME, secondo questi criteri:
- toccano gli stessi file o lo stesso modulo del codice;
- appartengono allo stesso dominio funzionale;
- hanno dipendenze reciproche.
Ogni ticket deve stare in ESATTAMENTE un gruppo. Ticket non correlati vanno in gruppi separati.

Rispondi SOLO con JSON:
{"groups": [{"ticket_ids": ["<id>", ...], "rationale": "<perché insieme, in italiano>"}]}"""


class TicketNotFound(Exception):
    pass


class WorkOrderExists(Exception):
    def __init__(self, wo_id: str) -> None:
        super().__init__(wo_id)
        self.wo_id = wo_id


class WorkOrderNotFound(Exception):
    pass


class TicketInOtherWorkOrder(Exception):
    """Il ticket è già in un altro Work Order: per aggiungerlo serve confermare lo spostamento."""

    def __init__(self, wo_id: str) -> None:
        super().__init__(wo_id)
        self.wo_id = wo_id


async def _load_tickets(session, ticket_ids: list[str]) -> list[dict]:
    if not ticket_ids:
        return []
    rows = (
        await session.execute(
            text(
                "SELECT id, type, title, description, severity, domain, repo_url, status "
                "FROM tickets WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ticket_ids},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _ticket_payload(t: dict) -> dict:
    return {
        "id": t["id"],
        "type": t["type"],
        "title": t["title"],
        "description": (t["description"] or "")[:1500],
        "severity": t["severity"],
        "domain": t["domain"],
    }


async def _publish_generated(payload: dict) -> None:
    try:
        await publish(
            "workorders.generated",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                **payload,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento workorders.generated non pubblicato")


async def create_workorder(ticket_ids: list[str], instructions: str = "", rationale: str = "") -> dict:
    """Crea un Work Order che raggruppa uno o più ticket."""
    if isinstance(ticket_ids, str):  # comodità: accetta anche un singolo id
        ticket_ids = [ticket_ids]
    async with SessionLocal() as session:
        tickets = await _load_tickets(session, ticket_ids)
        if not tickets:
            raise TicketNotFound(", ".join(ticket_ids))

        # un ticket non può stare in due WO attivi contemporaneamente
        for tid in [t["id"] for t in tickets]:
            existing = (
                await session.execute(
                    text(
                        "SELECT wot.wo_id FROM work_order_tickets wot "
                        "JOIN work_orders wo ON wo.id = wot.wo_id "
                        "WHERE wot.ticket_id = :tid AND wo.status NOT IN ('failed')"
                    ),
                    {"tid": tid},
                )
            ).first()
            if existing:
                raise WorkOrderExists(existing[0])

        wo_id = str(uuid.uuid4())
        payload = {
            "wo_id": wo_id,
            "tickets": [_ticket_payload(t) for t in tickets],
            "instructions": instructions,
            "rationale": rationale,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Il WO eredita il progetto (repository) dai suoi ticket; lo Scrum Master
        # può poi assegnarlo/cambiarlo manualmente nel dettaglio del WO.
        wo_repo = next((t.get("repo_url") for t in tickets if t.get("repo_url")), None)
        await session.execute(
            text(
                "INSERT INTO work_orders (id, ticket_id, status, payload, repo_url) "
                "VALUES (:id, :tid, 'generated', :payload, :repo)"
            ),
            {"id": wo_id, "tid": tickets[0]["id"],
             "payload": json.dumps(payload, ensure_ascii=False), "repo": wo_repo},
        )
        for t in tickets:
            await session.execute(
                text("INSERT INTO work_order_tickets (wo_id, ticket_id) VALUES (:w, :t)"),
                {"w": wo_id, "t": t["id"]},
            )
            await session.execute(
                text("UPDATE tickets SET status = 'in_progress' WHERE id = :id"), {"id": t["id"]}
            )
        await session.commit()

    await _publish_generated(payload)
    logger.info("Work Order %s creato con %d ticket", wo_id, len(tickets))
    return {"wo_id": wo_id, "ticket_ids": [t["id"] for t in tickets], "payload": payload}


def _fallback_groups(tickets: list[dict]) -> list[dict]:
    """Senza LLM: raggruppa per dominio (i ticket senza dominio restano singoli)."""
    by_domain: dict[str, list[str]] = {}
    singles: list[dict] = []
    for t in tickets:
        dom = (t.get("domain") or "").strip().lower()
        if dom:
            by_domain.setdefault(dom, []).append(t["id"])
        else:
            singles.append({"ticket_ids": [t["id"]], "rationale": "Ticket senza dominio: WO singolo"})
    groups = [
        {"ticket_ids": ids, "rationale": f"Stesso dominio '{dom}'"} for dom, ids in by_domain.items()
    ]
    return groups + singles


async def plan_workorders(instructions: str = "") -> dict:
    """Agente di Pianificazione: analizza i ticket approvati e crea i Work Order
    raggruppando quelli correlati. È l'azione del bottone unico 'Crea Work Order'."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.id, t.type, t.title, t.description, t.severity, t.domain, "
                    "ia.affected_files "
                    "FROM tickets t "
                    "LEFT JOIN ( "
                    "  SELECT ticket_id, affected_files, "
                    "         ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY created_at DESC) rn "
                    "  FROM impact_analyses "
                    ") ia ON ia.ticket_id = t.id AND ia.rn = 1 "
                    "WHERE t.status IN :st "
                    "AND t.id NOT IN (SELECT ticket_id FROM work_order_tickets) "
                    "ORDER BY t.priority ASC, t.imported_at ASC"
                ).bindparams(bindparam("st", expanding=True)),
                {"st": list(ELIGIBLE_STATUSES)},
            )
        ).mappings().all()
    tickets = [dict(r) for r in rows]
    if not tickets:
        return {"status": "ok", "created": 0, "detail": "Nessun ticket approvato da raggruppare"}

    compact = [
        {
            "id": t["id"],
            "title": t["title"],
            "domain": t["domain"],
            "affected_files": json.loads(t["affected_files"]) if isinstance(t.get("affected_files"), str) else (t.get("affected_files") or []),
        }
        for t in tickets
    ]
    ticket_ids = {t["id"] for t in tickets}

    llm_used = True
    try:
        result = await llm.chat_json(
            GROUPING_PROMPT,
            json.dumps({"tickets": compact}, ensure_ascii=False),
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )
        groups = result.get("groups", []) if isinstance(result, dict) else []
        # difese: tieni solo id validi, niente duplicati tra gruppi, reintegra i mancanti
        seen: set[str] = set()
        clean: list[dict] = []
        for g in groups:
            ids = [i for i in (g.get("ticket_ids") or []) if i in ticket_ids and i not in seen]
            if ids:
                seen.update(ids)
                clean.append({"ticket_ids": ids, "rationale": str(g.get("rationale") or "")[:500]})
        missing = [t["id"] for t in tickets if t["id"] not in seen]
        if missing:
            clean.extend(_fallback_groups([t for t in tickets if t["id"] in missing]))
        groups = clean or _fallback_groups(tickets)
    except llm.LLMError as exc:
        logger.warning("LLM non disponibile per il raggruppamento, uso euristica: %s", exc)
        llm_used = False
        groups = _fallback_groups(tickets)

    created = []
    for g in groups:
        try:
            wo = await create_workorder(g["ticket_ids"], instructions, g.get("rationale", ""))
            created.append({"wo_id": wo["wo_id"], "ticket_ids": wo["ticket_ids"], "rationale": g.get("rationale", "")})
        except (TicketNotFound, WorkOrderExists) as exc:
            logger.warning("Gruppo saltato: %s", exc)

    return {"status": "ok", "llm": llm_used, "created": len(created), "work_orders": created}


async def _detach_ticket(session, wo_id: str, ticket_id: str, restore_status: str) -> int | None:
    """Stacca un ticket da un WO dentro la transazione corrente: aggiorna payload e
    rappresentante, elimina il WO se resta vuoto. Ritorna i ticket rimanenti (o None)."""
    wo = (
        await session.execute(
            text("SELECT id, payload FROM work_orders WHERE id = :id"), {"id": wo_id}
        )
    ).mappings().first()
    if wo is None:
        return None
    await session.execute(
        text("DELETE FROM work_order_tickets WHERE wo_id = :w AND ticket_id = :t"),
        {"w": wo_id, "t": ticket_id},
    )
    await session.execute(
        text("UPDATE tickets SET status = :s WHERE id = :id"), {"s": restore_status, "id": ticket_id}
    )
    payload = wo["payload"] if isinstance(wo["payload"], dict) else json.loads(wo["payload"])
    payload["tickets"] = [t for t in payload.get("tickets", []) if t.get("id") != ticket_id]
    remaining = len(payload["tickets"])
    await session.execute(
        text("UPDATE work_orders SET payload = :p WHERE id = :id"),
        {"p": json.dumps(payload, ensure_ascii=False), "id": wo_id},
    )
    if remaining:
        await session.execute(
            text("UPDATE work_orders SET ticket_id = :t WHERE id = :id"),
            {"t": payload["tickets"][0]["id"], "id": wo_id},
        )
    else:
        await session.execute(text("DELETE FROM work_orders WHERE id = :id"), {"id": wo_id})
    return remaining


async def add_ticket(wo_id: str, ticket_id: str, move: bool = False) -> dict:
    async with SessionLocal() as session:
        wo = (
            await session.execute(
                text("SELECT id, status, payload FROM work_orders WHERE id = :id"), {"id": wo_id}
            )
        ).mappings().first()
        if wo is None:
            raise WorkOrderNotFound(wo_id)
        ticket = (
            await session.execute(
                text("SELECT id, type, title, description, severity, domain, status FROM tickets WHERE id = :id"),
                {"id": ticket_id},
            )
        ).mappings().first()
        if ticket is None:
            raise TicketNotFound(ticket_id)

        # il ticket è già in un altro Work Order attivo?
        other = (
            await session.execute(
                text(
                    "SELECT wo.id FROM work_order_tickets wot JOIN work_orders wo ON wo.id = wot.wo_id "
                    "WHERE wot.ticket_id = :t AND wo.id <> :self AND wo.status NOT IN ('failed')"
                ),
                {"t": ticket_id, "self": wo_id},
            )
        ).first()
        if other:
            if not move:
                raise TicketInOtherWorkOrder(other[0])
            # spostamento confermato: stacca dall'altro WO
            await _detach_ticket(session, other[0], ticket_id, restore_status="approved")

        # già presente in questo WO? no-op
        already = (
            await session.execute(
                text("SELECT 1 FROM work_order_tickets WHERE wo_id = :w AND ticket_id = :t"),
                {"w": wo_id, "t": ticket_id},
            )
        ).first()
        if not already:
            payload = wo["payload"] if isinstance(wo["payload"], dict) else json.loads(wo["payload"])
            payload.setdefault("tickets", []).append(_ticket_payload(dict(ticket)))
            await session.execute(
                text("INSERT INTO work_order_tickets (wo_id, ticket_id) VALUES (:w, :t)"),
                {"w": wo_id, "t": ticket_id},
            )
            await session.execute(
                text("UPDATE work_orders SET payload = :p WHERE id = :id"),
                {"p": json.dumps(payload, ensure_ascii=False), "id": wo_id},
            )
        await session.execute(
            text("UPDATE tickets SET status = 'in_progress' WHERE id = :id"), {"id": ticket_id}
        )
        await session.commit()
    detail = "Ticket spostato in questo Work Order" if (other and move) else "Ticket aggiunto al Work Order"
    return {"wo_id": wo_id, "ticket_id": ticket_id, "moved_from": other[0] if (other and move) else None, "detail": detail}


async def remove_ticket(wo_id: str, ticket_id: str) -> dict:
    async with SessionLocal() as session:
        remaining = await _detach_ticket(session, wo_id, ticket_id, restore_status="approved")
        if remaining is None:
            raise WorkOrderNotFound(wo_id)
        await session.commit()
    return {"wo_id": wo_id, "ticket_id": ticket_id, "remaining": remaining,
            "detail": "Ticket rimosso" + ("" if remaining else "; Work Order eliminato (vuoto)")}


async def delete_workorder(wo_id: str) -> dict:
    async with SessionLocal() as session:
        members = (
            await session.execute(
                text("SELECT ticket_id FROM work_order_tickets WHERE wo_id = :w"), {"w": wo_id}
            )
        ).scalars().all()
        if not members:
            exists = (await session.execute(text("SELECT id FROM work_orders WHERE id = :id"), {"id": wo_id})).first()
            if not exists:
                raise WorkOrderNotFound(wo_id)
        for tid in members:
            await session.execute(
                text("UPDATE tickets SET status = 'approved' WHERE id = :id"), {"id": tid}
            )
        await session.execute(text("DELETE FROM work_orders WHERE id = :id"), {"id": wo_id})
        await session.commit()
    return {"wo_id": wo_id, "detail": f"Work Order eliminato, {len(members)} ticket tornati disponibili"}
