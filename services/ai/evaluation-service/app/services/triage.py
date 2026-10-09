"""Logica del triage dell'Agente Valutatore (deduplicazione, dipendenze, priorità).

Estratta dagli endpoint per essere riusabile sia dall'API (`/evaluate/run`)
sia dal consumer Kafka (autonomia a eventi su `tickets.raw`). Il triage usa
il modello LLM locale; se non disponibile, l'euristica di fallback basata
sulla severity garantisce che la pipeline non si blocchi mai.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.kafka.producer import publish
from shared.common import llm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Sei l'Agente Valutatore di una piattaforma SDLC aziendale.
Ricevi una lista di ticket grezzi (bug, CVE, change request) e devi:
1. DEDUPLICARE: individuare i ticket che segnalano lo stesso problema (stesso difetto, anche se descritto diversamente).
2. PRIORITIZZARE: assegnare una priorità da 1 (massima) a 5 (minima) pesando severity, impatto funzionale e dipendenze.
3. DIPENDENZE: se risolvere un ticket risolve a cascata anche altri, indicali in resolves_also.

Rispondi SOLO con JSON valido in questo formato:
{"tickets": [{"id": "<id>", "priority": <1-5>, "duplicate_of": "<id del ticket master oppure null>", "resolves_also": ["<id>", ...], "rationale": "<motivazione breve in italiano>"}]}
Ogni ticket della lista di input deve comparire esattamente una volta."""

SEVERITY_PRIORITY = {"critical": 1, "high": 2, "medium": 3, "low": 4}


async def _publish_prioritized(ticket: dict) -> None:
    try:
        await publish(
            "tickets.prioritized",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "ticket_id": ticket["id"],
                "priority": ticket["priority"],
                "duplicates": ticket.get("duplicates", []),
                "resolves_also": ticket.get("resolves_also", []),
                "rationale": ticket.get("rationale", ""),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento tickets.prioritized non pubblicato")


def _fallback_evaluation(tickets: list[dict]) -> list[dict]:
    """Euristica senza LLM: priorità dalla severity, nessuna deduplicazione."""
    return [
        {
            "id": t["id"],
            "priority": SEVERITY_PRIORITY.get((t["severity"] or "").lower(), 3),
            "duplicate_of": None,
            "resolves_also": [],
            "rationale": "Fallback euristico (LLM non disponibile): priorità derivata dalla severity",
        }
        for t in tickets
    ]


def sanitize_evaluations(evaluations: list[dict], tickets: list[dict]) -> list[dict]:
    """Difese contro le allucinazioni del modello: scarta gli id sconosciuti e
    completa con il fallback i ticket che il modello ha dimenticato."""
    ticket_ids = {t["id"] for t in tickets}
    cleaned = [e for e in evaluations if isinstance(e, dict) and e.get("id") in ticket_ids]
    evaluated_ids = {e["id"] for e in cleaned}
    missing = [t for t in tickets if t["id"] not in evaluated_ids]
    cleaned.extend(_fallback_evaluation(missing))
    return cleaned


def clamp_priority(value) -> int:
    if value is None:
        return 3
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


async def run_triage(limit: int = 30) -> dict:
    """Esegue il triage dei ticket in stato 'raw'. Ritorna il riepilogo per l'API."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, source, type, title, description, severity FROM tickets "
                    "WHERE status = 'raw' ORDER BY imported_at ASC LIMIT :limit"
                ),
                {"limit": min(limit, 50)},
            )
        ).mappings().all()
    tickets = [dict(r) for r in rows]
    if not tickets:
        return {"status": "ok", "evaluated": 0, "detail": "Nessun ticket grezzo da valutare"}

    ticket_ids = {t["id"] for t in tickets}
    compact = [
        {
            "id": t["id"],
            "type": t["type"],
            "severity": t["severity"],
            "title": t["title"],
            "description": (t["description"] or "")[:400],
        }
        for t in tickets
    ]

    llm_used = True
    try:
        result = await llm.chat_json(
            SYSTEM_PROMPT,
            json.dumps({"tickets": compact}, ensure_ascii=False),
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )
        evaluations = result.get("tickets", []) if isinstance(result, dict) else result
        evaluations = sanitize_evaluations(evaluations, tickets)
    except llm.LLMError as exc:
        logger.warning("LLM non disponibile, uso euristica: %s", exc)
        llm_used = False
        evaluations = _fallback_evaluation(tickets)

    duplicates, prioritized = 0, 0
    master_extra: dict[str, list[str]] = {}
    async with SessionLocal() as session:
        for ev in evaluations:
            dup_of = ev.get("duplicate_of")
            if dup_of and dup_of in ticket_ids and dup_of != ev["id"]:
                await session.execute(
                    text("UPDATE tickets SET status = 'deduplicated', duplicate_of = :m WHERE id = :id"),
                    {"m": dup_of, "id": ev["id"]},
                )
                duplicates += 1
                master_extra.setdefault(dup_of, []).append(ev["id"])
            else:
                priority = clamp_priority(ev.get("priority"))
                await session.execute(
                    text("UPDATE tickets SET status = 'prioritized', priority = :p WHERE id = :id"),
                    {"p": priority, "id": ev["id"]},
                )
                prioritized += 1
        await session.commit()

    for ev in evaluations:
        if not ev.get("duplicate_of"):
            await _publish_prioritized(
                {
                    "id": ev["id"],
                    "priority": clamp_priority(ev.get("priority")),
                    "duplicates": master_extra.get(ev["id"], []),
                    "resolves_also": ev.get("resolves_also", []),
                    "rationale": ev.get("rationale", ""),
                }
            )

    return {
        "status": "ok",
        "llm": llm_used,
        "model": settings.llm_model if llm_used else None,
        "evaluated": len(evaluations),
        "prioritized": prioritized,
        "duplicates": duplicates,
    }
