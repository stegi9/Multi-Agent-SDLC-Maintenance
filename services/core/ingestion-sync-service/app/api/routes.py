"""Endpoint di gestione ticket e impostazioni di piattaforma.

- Importazione da Jira e GitHub (config dalla pagina Impostazioni)
- Creazione interna di Bug / Change Request
- Reverse Sync verso Jira
- CRUD impostazioni con test di connessione
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import settings
from shared.common.auth import internal_headers, require_roles
from app.core.db import SessionLocal
from app.kafka.producer import publish
from app.services import bugzilla_client, cve_client, github_client, jira_client, settings_store

logger = logging.getLogger(__name__)
router = APIRouter()

# Stato dell'ultima importazione (in memoria, per la dashboard)
_last_import: dict = {"status": "never_run"}


class TicketCreate(BaseModel):
    type: Literal["bug", "change_request"] = "bug"
    title: str = Field(min_length=3, max_length=500)
    description: str = ""
    severity: str | None = None
    domain: str | None = None  # dominio software (es. billing, hr)
    repo_url: str | None = None  # progetto di appartenenza (repository attivo)
    force: bool = False  # crea anche se esiste già una specifica simile nel RAG Futuro


class PrecheckRequest(BaseModel):
    title: str = ""
    description: str = ""


RAG_PRECHECK_URL = f"{settings.rag_base_url}/precheck"


async def _rag_precheck(title: str, description: str) -> dict | None:
    """Chiede al RAG Manager se esiste già una specifica simile nel RAG Futuro.
    Ritorna il match se duplicato, altrimenti None. Non blocca se il RAG è giù."""
    text = f"{title}\n{description}".strip()
    if not text:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(RAG_PRECHECK_URL, headers=internal_headers(), json={"text": text, "rag": "future"})
            if response.status_code != 200:
                return None
            data = response.json()
        return data.get("best_match") if data.get("duplicate") else None
    except httpx.HTTPError:
        return None


class Ticket(BaseModel):
    id: str
    external_id: str | None
    source: str
    type: str
    title: str
    description: str | None
    severity: str | None
    domain: str | None
    repo_url: str | None = None  # progetto di appartenenza
    status: str
    priority: int | None
    imported_at: datetime


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


async def _publish_ticket_raw(ticket: dict) -> None:
    """Pubblica l'evento tickets.raw; non blocca la richiesta se Kafka Ã¨ giÃ¹."""
    try:
        await publish(
            "tickets.raw",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "ticket_id": ticket["id"],
                "external_id": ticket.get("external_id"),
                "source": ticket["source"],
                "type": ticket["type"],
                "title": ticket["title"],
                "description": ticket.get("description") or "",
                "severity": ticket.get("severity"),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento tickets.raw non pubblicato")


async def _store_issues(source: str, issues: list[dict], repo_url: str | None = None) -> tuple[int, int]:
    """Inserisce le issue deduplicando per (source, external_id). Ritorna (importati, scartati).
    I ticket vengono stampati con il progetto (repo_url) del contesto d'importazione."""
    imported, skipped = 0, 0
    async with SessionLocal() as session:
        for issue in issues:
            exists = (
                await session.execute(
                    text("SELECT id FROM tickets WHERE source = :src AND external_id = :k"),
                    {"src": source, "k": issue["external_id"]},
                )
            ).first()
            if exists:
                skipped += 1
                continue
            ticket_id = str(uuid.uuid4())
            await session.execute(
                text(
                    "INSERT INTO tickets (id, external_id, source, type, title, description, severity, repo_url, status) "
                    "VALUES (:id, :external_id, :src, :type, :title, :description, :severity, :repo_url, 'raw')"
                ),
                {"id": ticket_id, "src": source, "repo_url": repo_url, **issue},
            )
            imported += 1
            await _publish_ticket_raw({"id": ticket_id, "source": source, **issue})
        await session.commit()
    return imported, skipped


# ---------------------------------------------------------------------------
# Ticket
# ---------------------------------------------------------------------------

@router.get("/tickets", response_model=list[Ticket], summary="Elenca i ticket della piattaforma")
async def list_tickets(
    ticket_status: str | None = None, domain: str | None = None,
    repo_url: str | None = None, limit: int = 200,
) -> list[Ticket]:
    query = (
        "SELECT id, external_id, source, type, title, description, severity, domain, repo_url, "
        "status, priority, imported_at FROM tickets "
    )
    params: dict = {"limit": min(limit, 500)}
    conditions = []
    if ticket_status:
        conditions.append("status = :st")
        params["st"] = ticket_status
    if domain:
        conditions.append("domain = :dom")
        params["dom"] = domain.strip().lower()
    if repo_url:
        # Filtro per progetto attivo (repository)
        conditions.append("repo_url = :repo")
        params["repo"] = repo_url.strip()
    if conditions:
        query += "WHERE " + " AND ".join(conditions) + " "
    query += "ORDER BY imported_at DESC LIMIT :limit"
    async with SessionLocal() as session:
        rows = (await session.execute(text(query), params)).mappings().all()
    return [Ticket(**dict(r)) for r in rows]


@router.post(
    "/tickets",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    summary="Crea un Bug o una Change Request direttamente in piattaforma",
)
async def create_ticket(body: TicketCreate) -> Ticket:
    # Verifica pre-ingestion: la richiesta esiste già nella roadmap (RAG Futuro)?
    if not body.force:
        match = await _rag_precheck(body.title, body.description)
        if match:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Esiste già una specifica simile nel RAG Futuro: possibile task duplicato. "
                    "Conferma per crearlo comunque.",
                    "roadmap_match": {
                        "title": match.get("title"),
                        "score": match.get("score"),
                        "document_id": match.get("document_id"),
                    },
                },
            )

    ticket_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO tickets (id, source, type, title, description, severity, domain, repo_url, status) "
                "VALUES (:id, 'internal', :type, :title, :description, :severity, :domain, :repo_url, 'raw')"
            ),
            {
                "id": ticket_id,
                "type": body.type,
                "title": body.title,
                "description": body.description,
                "severity": body.severity,
                "domain": (body.domain or "").strip().lower() or None,
                "repo_url": (body.repo_url or "").strip() or None,
            },
        )
        await session.commit()
        row = (
            await session.execute(
                text(
                    "SELECT id, external_id, source, type, title, description, severity, domain, repo_url, "
                    "status, priority, imported_at FROM tickets WHERE id = :id"
                ),
                {"id": ticket_id},
            )
        ).mappings().first()
    ticket = dict(row)
    await _publish_ticket_raw(ticket)
    return Ticket(**ticket)


# ---------------------------------------------------------------------------
# Importazione
# ---------------------------------------------------------------------------

@router.post("/tickets/precheck", summary="Verifica se la richiesta esiste già nel RAG Futuro (anti-duplicati)")
async def ticket_precheck(body: PrecheckRequest) -> dict:
    match = await _rag_precheck(body.title, body.description)
    return {"duplicate": bool(match), "roadmap_match": match}


@router.post("/import/run", summary="Avvia l'importazione dei ticket dalle sorgenti configurate")
async def run_import(full: bool = False, repo_url: str = "") -> dict:
    """Importa dalle sorgenti configurate: aging zero di default, full=true per lo storico.
    Con `repo_url` l'import è scoped su un progetto: i ticket vengono stampati con quel
    repository e, per GitHub, owner/repo sono dedotti dall'URL del progetto."""
    global _last_import
    project = repo_url.strip() or None
    jira_cfg = await jira_client.load_config()
    github_cfg = await github_client.load_config()
    bugzilla_cfg = await bugzilla_client.load_config()
    cve_cfg = await cve_client.load_config()

    # Config sorgenti per-progetto: se è indicato un repo GitHub, importa da quello.
    if project:
        scoped_gh = await github_client.config_for_repo(project)
        if scoped_gh and scoped_gh.configured:
            github_cfg = scoped_gh

    if not any((jira_cfg.configured, github_cfg.configured, bugzilla_cfg.configured, cve_cfg.configured)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nessuna sorgente configurata: aggiungere le credenziali nella pagina Impostazioni",
        )

    results: list[dict] = []
    errors: list[str] = []

    if jira_cfg.configured:
        # Jira (/search/jql) rifiuta le query non delimitate: serve sempre una clausola di filtro
        if full:
            jql = (
                f"project = {jira_cfg.project_key} ORDER BY created DESC"
                if jira_cfg.project_key
                else "created >= -30d ORDER BY created DESC"
            )
        else:
            jql = settings.jira_jql
        try:
            issues = await jira_client.search_issues(jira_cfg, jql)
            imported, skipped = await _store_issues("jira", issues, project)
            results.append({"source": "jira", "found": len(issues), "imported": imported, "skipped_duplicates": skipped})
        except httpx.HTTPError as exc:
            detail = getattr(getattr(exc, "response", None), "status_code", None) or str(exc)
            errors.append(f"jira: {detail}")

    if github_cfg.configured:
        try:
            issues = await github_client.search_issues(github_cfg, since_days=None if full else 1)
            imported, skipped = await _store_issues("github", issues, project)
            results.append({"source": "github", "found": len(issues), "imported": imported, "skipped_duplicates": skipped})
        except httpx.HTTPError as exc:
            detail = getattr(getattr(exc, "response", None), "status_code", None) or str(exc)
            errors.append(f"github: {detail}")

    if bugzilla_cfg.configured:
        try:
            issues = await bugzilla_client.search_bugs(bugzilla_cfg, since_days=None if full else 1)
            imported, skipped = await _store_issues("bugzilla", issues, project)
            results.append({"source": "bugzilla", "found": len(issues), "imported": imported, "skipped_duplicates": skipped})
        except httpx.HTTPError as exc:
            detail = getattr(getattr(exc, "response", None), "status_code", None) or str(exc)
            errors.append(f"bugzilla: {detail}")

    if cve_cfg.configured:
        try:
            # NVD: aging zero = ultime 24h; full = ultimi 30 giorni
            issues = await cve_client.search_cves(cve_cfg, since_days=30 if full else 1)
            imported, skipped = await _store_issues("cve", issues, project)
            results.append({"source": "cve", "found": len(issues), "imported": imported, "skipped_duplicates": skipped})
        except httpx.HTTPError as exc:
            detail = getattr(getattr(exc, "response", None), "status_code", None) or str(exc)
            errors.append(f"cve: {detail}")

    if not results and errors:
        _last_import = {"status": "error", "detail": "; ".join(errors), "run_at": datetime.now(timezone.utc).isoformat()}
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="; ".join(errors))

    _last_import = {
        "status": "ok" if not errors else "partial",
        "results": results,
        "errors": errors,
        "found": sum(r["found"] for r in results),
        "imported": sum(r["imported"] for r in results),
        "skipped_duplicates": sum(r["skipped_duplicates"] for r in results),
        "run_at": datetime.now(timezone.utc).isoformat(),
    }
    return _last_import


@router.get("/import/status", summary="Stato dell'ultima importazione")
async def import_status() -> dict:
    return _last_import


# ---------------------------------------------------------------------------
# Reverse Sync
# ---------------------------------------------------------------------------

@router.post("/reverse-sync/{ticket_id}", summary="Sincronizza un ticket interno verso il sistema esterno (es. Jira)")
async def reverse_sync(ticket_id: str) -> dict:
    jira_cfg = await jira_client.load_config()
    if not jira_cfg.configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira non configurato: aggiungere le credenziali nella pagina Impostazioni",
        )
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT id, external_id, source, type, title, description FROM tickets WHERE id = :id"),
                {"id": ticket_id},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket non trovato")
        if row["source"] != "internal":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Solo i ticket creati internamente possono essere sincronizzati",
            )
        if row["external_id"]:
            return {"status": "already_synced", "external_id": row["external_id"]}

        try:
            key = await jira_client.create_issue(
                jira_cfg,
                title=row["title"],
                description=row["description"] or "",
                ticket_type=row["type"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Errore Jira ({exc.response.status_code}): {exc.response.text[:200]}",
            )

        await session.execute(
            text("UPDATE tickets SET external_id = :k WHERE id = :id"),
            {"k": key, "id": ticket_id},
        )
        await session.commit()
    return {"status": "synced", "external_id": key}


# ---------------------------------------------------------------------------
# Impostazioni e sorgenti
# ---------------------------------------------------------------------------

@router.get("/settings", summary="Impostazioni di piattaforma (segreti mascherati)")
async def get_settings() -> list[dict]:
    return await settings_store.get_masked()


@router.put("/settings", dependencies=[Depends(require_roles("scrum_master"))], summary="Salva le impostazioni di piattaforma")
async def update_settings(body: SettingsUpdate) -> dict:
    before = await settings_store.get_all()
    updated = await settings_store.set_many(body.settings)
    after = await settings_store.get_all()

    # se il repository (o il branch) è cambiato, ridisegna automaticamente
    # topologia e grafo del codice: l'azienda incolla il link e basta.
    repo_changed = (
        before.get("repo.git_url") != after.get("repo.git_url")
        or before.get("repo.branch") != after.get("repo.branch")
    )
    rescan = False
    if repo_changed and after.get("repo.git_url"):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(f"{settings.topology_base_url}/scan", headers=internal_headers())
            rescan = True
        except httpx.HTTPError:
            logger.warning("Repo cambiato ma scan del Mappatore non avviato (topology non raggiungibile)")

    return {"status": "ok", "updated": updated, "repo_rescan": rescan}


# ---------------------------------------------------------------------------
# Callback dell'agente di coding: PR aperta su GitHub (gate Human-in-the-Loop)
# ---------------------------------------------------------------------------

class CodePushedRequest(BaseModel):
    wo_id: str


@router.post(
    "/workorders/code-pushed",
    summary="L'agente di coding notifica che ha pushato il codice: la piattaforma apre la PR",
)
async def code_pushed(body: CodePushedRequest) -> dict:
    """Chiamato dall'agente esterno (n8n) dopo aver committato il codice su
    work_branch. La piattaforma apre la Pull Request (che resta OPEN per la
    revisione) e assegna il reviewer: solo ora "Valida codice" si abilita."""
    from app.services import git_flow

    result = await git_flow.open_and_register_code_pr(body.wo_id)
    return {"wo_id": body.wo_id, **result}


@router.post(
    "/workorders/{wo_id}/sync-pr",
    summary="Allinea lo stato del WO alla PR reale su GitHub (merge/chiusura fatti a mano)",
)
async def sync_pr(wo_id: str) -> dict:
    """Se la PR è stata fusa o chiusa direttamente su GitHub, aggiorna il Work Order
    (stato, ticket, documentazione) di conseguenza."""
    from app.services import git_flow

    result = await git_flow.sync_code_pr(wo_id)
    return {"wo_id": wo_id, **result}


# ---------------------------------------------------------------------------
# Credenziali Git per-repository (token dedicato, cifrato a riposo)
# ---------------------------------------------------------------------------

class RepoCredentialUpdate(BaseModel):
    repo_url: str
    token: str = ""  # vuoto = rimuove il token dedicato (torna al token globale)


@router.get(
    "/repo-credentials",
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Repository con token Git dedicato (il token non è mai restituito)",
)
async def list_repo_credentials() -> list[dict]:
    from app.services import repo_credentials

    return await repo_credentials.list_configured()


@router.put(
    "/repo-credentials",
    dependencies=[Depends(require_roles("scrum_master"))],
    summary="Imposta o rimuove il token Git dedicato di un repository",
)
async def set_repo_credential(body: RepoCredentialUpdate) -> dict:
    from app.services import repo_credentials

    try:
        configured = await repo_credentials.set_token(body.repo_url, body.token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"repo_url": body.repo_url, "configured": configured}


@router.post("/settings/test/{source}", dependencies=[Depends(require_roles("scrum_master"))], summary="Testa la connessione di una sorgente (jira | github | bugzilla | cve)")
async def test_source(source: Literal["jira", "github", "bugzilla", "cve"]) -> dict:
    if source == "jira":
        return await jira_client.test_connection(await jira_client.load_config())
    if source == "bugzilla":
        return await bugzilla_client.test_connection(await bugzilla_client.load_config())
    if source == "cve":
        return await cve_client.test_connection(await cve_client.load_config())
    return await github_client.test_connection(await github_client.load_config())


@router.get("/sources", summary="Elenca le sorgenti esterne configurate")
async def list_sources() -> list[dict]:
    jira_cfg = await jira_client.load_config()
    github_cfg = await github_client.load_config()
    bugzilla_cfg = await bugzilla_client.load_config()
    cve_cfg = await cve_client.load_config()
    return [
        {
            "name": "jira",
            "configured": jira_cfg.configured,
            "base_url": jira_cfg.base_url or None,
            "reverse_sync": bool(jira_cfg.project_key),
        },
        {
            "name": "github",
            "configured": github_cfg.configured,
            "base_url": f"https://github.com/{github_cfg.owner}/{github_cfg.repo}" if github_cfg.configured else None,
        },
        {
            "name": "bugzilla",
            "configured": bugzilla_cfg.configured,
            "base_url": bugzilla_cfg.base_url or None,
        },
        {
            "name": "cve",
            "configured": cve_cfg.configured,
            "base_url": (cve_cfg.base_url if cve_cfg.configured else None),
        },
    ]
