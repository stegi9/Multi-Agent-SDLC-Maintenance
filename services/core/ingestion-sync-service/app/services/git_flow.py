"""Orchestrazione Git dei Work Order (Requisito: gestione dei repository).

Reagisce ai due gate Human-in-the-Loop già presenti nella piattaforma:

  - ``code.validated``      (lo Scrum Master approva il codice)
        -> apre la **Pull Request del codice** (work_branch -> base_branch),
           assegna il reviewer (lo sviluppatore del WO), verifica mergeabilità e
           CI, e solo allora la fonde. Il codice è scritto dagli agenti cloud
           esterni che pushano su work_branch; la piattaforma orchestra la PR.

  - ``documents.validated`` (l'Agente di Coerenza accetta la documentazione)
        -> committa il documento su un branch dedicato e apre/fonde la
           **Pull Request della documentazione** (PR separata da quella del codice).

Rispetto alla prima versione, il merge NON è più cieco: si controllano
mergeabilità (conflitti) e stato della CI. Se il merge non è possibile lo stato
della PR passa a ``conflict``/``error`` e viene pubblicato ``git.merge_failed``
per notificare lo Scrum Master; l'errore non interrompe il consumer.
"""
import logging
import re
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.kafka.producer import publish
from app.services import git_provider
from app.services.git_provider import GitError, MergeConflictError

logger = logging.getLogger(__name__)

# Numero massimo di poll della CI prima di lasciare la PR aperta (merge manuale
# quando i check passeranno). Tenuto basso per non bloccare il consumer Kafka.
_CI_POLL_ATTEMPTS = 3


def _bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


async def _git_options() -> dict:
    """Opzioni di comportamento Git (pagina Impostazioni con fallback dal .env)."""
    from app.services import settings_store

    values = await settings_store.get_all()
    method = (values.get("git.merge_method") or settings.git_merge_method or "merge").strip().lower()
    if method not in ("merge", "squash", "rebase"):
        method = "merge"
    return {
        "merge_method": method,
        "require_ci": _bool(values.get("git.require_ci"), settings.git_require_ci),
        "delete_branch": _bool(values.get("git.delete_branch_on_merge"), settings.git_delete_branch_on_merge),
    }


def _work_branch(wo: dict) -> str:
    return wo.get("work_branch") or f"wo/{wo['id'][:8]}"


def _docs_branch(wo: dict) -> str:
    return f"wo/{wo['id'][:8]}-docs"


async def _load_wo(wo_id: str) -> dict | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, ticket_id, repo_url, base_branch, work_branch, assignee_id, "
                    "code_pr_state, code_pr_url, docs_pr_state "
                    "FROM work_orders WHERE id = :id"
                ),
                {"id": wo_id},
            )
        ).mappings().first()
    return dict(row) if row else None


async def _update_pr(wo_id: str, prefix: str, *, url: str | None = None, state: str) -> None:
    async with SessionLocal() as session:
        if url is not None:
            await session.execute(
                text(f"UPDATE work_orders SET {prefix}_pr_url = :u, {prefix}_pr_state = :s WHERE id = :id"),
                {"u": url, "s": state, "id": wo_id},
            )
        else:
            await session.execute(
                text(f"UPDATE work_orders SET {prefix}_pr_state = :s WHERE id = :id"),
                {"s": state, "id": wo_id},
            )
        await session.commit()


async def _assignee_info(wo: dict) -> dict | None:
    """Dati dello sviluppatore assegnato: handle GitHub (reviewer/assignee) e
    nome/email (author del commit della documentazione)."""
    if not wo.get("assignee_id"):
        return None
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT username, email, github_username FROM users WHERE id = :id"),
                {"id": wo["assignee_id"]},
            )
        ).mappings().first()
    if not row:
        return None
    return {
        "github": row["github_username"] or None,
        "name": row["username"],
        "email": row["email"],
    }


async def _closes_clause(wo_id: str, fallback_ticket_id: str | None) -> str:
    """Riga 'Closes #n, #m' per le issue GitHub collegate al WO (auto-chiusura al merge)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.external_id FROM tickets t "
                    "LEFT JOIN work_order_tickets wt ON wt.ticket_id = t.id "
                    "WHERE (wt.wo_id = :wo OR t.id = :tid) AND t.source = 'github' "
                    "AND t.external_id IS NOT NULL"
                ),
                {"wo": wo_id, "tid": fallback_ticket_id},
            )
        ).all()
    numbers = []
    for (external_id,) in rows:
        match = re.search(r"#(\d+)$", external_id or "")
        if match:
            numbers.append(f"#{match.group(1)}")
    return "Closes " + ", ".join(dict.fromkeys(numbers)) if numbers else ""


async def _publish(topic: str, wo_id: str, kind: str, **extra) -> None:
    try:
        await publish(
            topic,
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "wo_id": wo_id,
                "kind": kind,
                **extra,
            },
        )
    except Exception:  # noqa: BLE001 - la notifica non deve bloccare il flusso Git
        logger.warning("Kafka non disponibile: evento %s non pubblicato", topic)


async def _review_and_merge(
    provider: git_provider.GitProvider, wo: dict, kind: str, pr: dict, opts: dict, head: str
) -> None:
    """Assegna reviewer, controlla mergeabilità e CI, poi fonde la PR.

    Aggiorna lo stato della PR e pubblica gli eventi git.merged/git.merge_failed.
    Non solleva: gli errori diventano stato + evento.
    """
    wo_id = wo["id"]
    number = pr["number"]
    prefix = "code" if kind == "code" else "docs"

    # Reviewer/assignee = sviluppatore assegnato dallo Scrum Master
    info = await _assignee_info(wo)
    reviewer = info["github"] if info else None
    if reviewer:
        await provider.request_reviewers(number, [reviewer])
        await provider.add_assignees(number, [reviewer])

    try:
        # 1. Mergeabilità (GitHub la calcola in modo asincrono: si attende)
        merge_state = await provider.pr_mergeability(number)
        if merge_state.get("merged"):
            await _update_pr(wo_id, prefix, url=pr["url"], state="merged")
            return
        if merge_state.get("mergeable") is False:
            await _update_pr(wo_id, prefix, url=pr["url"], state="conflict")
            await _publish("git.merge_failed", wo_id, kind, reason="conflict",
                           detail="Conflitti da risolvere sul branch di lavoro", pr_url=pr["url"])
            logger.warning("PR %s del WO %s non fondibile (conflitti): %s", kind, wo_id, pr["url"])
            return

        head_sha = merge_state.get("head_sha") or pr.get("head_sha")

        # 2. Gate CI
        if opts["require_ci"]:
            status = await provider.ci_status(head_sha)
            for _ in range(_CI_POLL_ATTEMPTS - 1):
                if status != "pending":
                    break
                status = await provider.ci_status(head_sha)
            if status == "failure":
                await _update_pr(wo_id, prefix, url=pr["url"], state="error")
                await _publish("git.merge_failed", wo_id, kind, reason="ci_failed",
                               detail="La CI è fallita sul branch di lavoro", pr_url=pr["url"])
                logger.warning("PR %s del WO %s: CI rossa, merge non eseguito", kind, wo_id)
                return
            if status == "pending":
                # PR lasciata aperta: verrà fusa quando i check passeranno
                # (branch protection / auto-merge di GitHub). Nessun merge cieco.
                await _update_pr(wo_id, prefix, url=pr["url"], state="open")
                logger.info("PR %s del WO %s: CI in corso, PR lasciata aperta", kind, wo_id)
                return

        # 3. Merge (sha atteso = protezione contro codice cambiato sotto i piedi)
        await provider.merge_pr(number, method=opts["merge_method"], sha=head_sha)
        await _update_pr(wo_id, prefix, url=pr["url"], state="merged")
        await _publish("git.merged", wo_id, kind, pr_url=pr["url"], branch=head)
        logger.info("PR %s del WO %s fusa: %s", kind, wo_id, pr["url"])

        # 4. Cleanup del branch di lavoro
        if opts["delete_branch"]:
            try:
                await provider.delete_branch(head)
            except (GitError, httpx.HTTPError) as exc:
                logger.info("Cleanup del branch %s non riuscito: %s", head, exc)

    except MergeConflictError as exc:
        await _update_pr(wo_id, prefix, url=pr["url"], state="conflict")
        await _publish("git.merge_failed", wo_id, kind, reason="conflict", detail=str(exc), pr_url=pr["url"])
        logger.warning("Merge PR %s del WO %s non riuscito (conflitto): %s", kind, wo_id, exc)
    except (GitError, httpx.HTTPError) as exc:
        await _update_pr(wo_id, prefix, url=pr["url"], state="error")
        await _publish("git.merge_failed", wo_id, kind, reason="error", detail=str(exc), pr_url=pr["url"])
        logger.warning("Orchestrazione PR %s del WO %s fallita: %s", kind, wo_id, exc)


def _pr_number_from_url(url: str | None) -> int | None:
    match = re.search(r"/pull/(\d+)", url or "")
    return int(match.group(1)) if match else None


async def handle_code_validated(payload: dict) -> None:
    """Codice APPROVATO dallo Scrum Master -> fonde la PR già aperta dall'agente.

    Gate Human-in-the-Loop: la Pull Request è stata aperta dall'agente di coding
    al termine della scrittura e resta OPEN per la revisione umana su GitHub. Qui
    NON si apre nulla: si controllano mergeabilità/CI e si fonde la PR esistente.
    """
    wo_id = payload.get("wo_id")
    if not wo_id:
        return
    wo = await _load_wo(wo_id)
    if not wo or not wo.get("repo_url"):
        logger.info("code.validated per %s: nessun repository assegnato, git saltato", wo_id)
        return
    if wo.get("code_pr_state") == "merged":  # idempotenza: evento riprocessato
        logger.info("code.validated per %s: PR del codice già fusa, salto", wo_id)
        return

    provider = await git_provider.get_provider(wo["repo_url"])
    if provider is None:
        logger.info("Repo %s non gestito da alcun provider Git: git saltato", wo["repo_url"])
        return

    number = _pr_number_from_url(wo.get("code_pr_url"))
    if wo.get("code_pr_state") != "open" or number is None:
        # Non dovrebbe accadere: il bottone "Valida codice" è abilitato solo con PR aperta.
        logger.warning(
            "code.validated per %s: nessuna PR aperta da fondere (stato=%s)",
            wo_id, wo.get("code_pr_state"),
        )
        return

    opts = await _git_options()
    pr = {"number": number, "url": wo["code_pr_url"], "head_sha": None}
    await _review_and_merge(provider, wo, "code", pr, opts, head=_work_branch(wo))


async def _apply_wo_outcome(wo_id: str, *, wo_status: str, ticket_status: str) -> list[str]:
    """Aggiorna lo stato del WO e dei suoi ticket (esito della PR)."""
    async with SessionLocal() as session:
        await session.execute(
            text("UPDATE work_orders SET status = :s WHERE id = :id"),
            {"s": wo_status, "id": wo_id},
        )
        tids = (
            await session.execute(
                text("SELECT ticket_id FROM work_order_tickets WHERE wo_id = :w"), {"w": wo_id}
            )
        ).scalars().all()
        for tid in tids:
            await session.execute(
                text("UPDATE tickets SET status = :s WHERE id = :id"),
                {"s": ticket_status, "id": tid},
            )
        await session.commit()
    return list(tids)


async def sync_code_pr(wo_id: str) -> dict:
    """Allinea lo stato del WO alla PR reale su GitHub.

    Serve quando la PR viene **fusa o chiusa direttamente su GitHub** (fuori dalla
    piattaforma): qui la piattaforma se ne accorge e aggiorna tutto di conseguenza.
      - PR fusa    -> code_pr_state='merged', WO 'validated', ticket 'completed',
                      e parte la documentazione (evento code.validated).
      - PR chiusa  -> code_pr_state='closed', WO 'failed', ticket 'analyzed'.
    """
    wo = await _load_wo(wo_id)
    if not wo or not wo.get("repo_url"):
        return {"synced": False, "code_pr_state": (wo or {}).get("code_pr_state")}
    state = wo.get("code_pr_state")
    if state != "open":  # si sincronizza solo una PR aperta
        return {"synced": False, "code_pr_state": state}

    provider = await git_provider.get_provider(wo["repo_url"])
    number = _pr_number_from_url(wo.get("code_pr_url"))
    if provider is None or number is None:
        return {"synced": False, "code_pr_state": state}

    try:
        pr = await provider.pr_mergeability(number)
    except (GitError, httpx.HTTPError) as exc:
        logger.info("Sync PR del WO %s non riuscito: %s", wo_id, exc)
        return {"synced": False, "code_pr_state": state, "error": str(exc)}

    if pr.get("merged"):
        await _update_pr(wo_id, "code", url=wo["code_pr_url"], state="merged")
        tids = await _apply_wo_outcome(wo_id, wo_status="validated", ticket_status="completed")
        # fusa su GitHub = codice accettato: fa partire l'Agente Documentatore
        await _publish(
            "code.validated", wo_id, "code",
            ticket_ids=tids, summary="", note="merge effettuato su GitHub", external=True,
        )
        logger.info("WO %s: PR fusa su GitHub (sync) -> WO validato + docs", wo_id)
        return {"synced": True, "code_pr_state": "merged", "wo_status": "validated"}

    if pr.get("state") == "closed":
        await _update_pr(wo_id, "code", url=wo["code_pr_url"], state="closed")
        await _apply_wo_outcome(wo_id, wo_status="failed", ticket_status="analyzed")
        logger.info("WO %s: PR chiusa su GitHub (sync) -> WO rifiutato", wo_id)
        return {"synced": True, "code_pr_state": "closed", "wo_status": "failed"}

    return {"synced": False, "code_pr_state": "open"}


async def handle_code_rejected(payload: dict) -> None:
    """Codice RIFIUTATO dallo Scrum Master -> chiude la PR aperta (senza fondere)."""
    wo_id = payload.get("wo_id")
    if not wo_id:
        return
    wo = await _load_wo(wo_id)
    if not wo or not wo.get("repo_url"):
        return
    number = _pr_number_from_url(wo.get("code_pr_url"))
    if wo.get("code_pr_state") != "open" or number is None:
        return
    provider = await git_provider.get_provider(wo["repo_url"])
    if provider is None:
        return
    try:
        await provider.close_pr(number)
        await _update_pr(wo_id, "code", url=wo["code_pr_url"], state="closed")
        logger.info("PR del codice del WO %s chiusa (rifiutata dallo Scrum Master)", wo_id)
    except (GitError, httpx.HTTPError) as exc:
        logger.warning("Chiusura PR del WO %s fallita: %s", wo_id, exc)


async def open_and_register_code_pr(wo_id: str) -> dict:
    """Apre la PR del codice e la registra sul WO (gate Human-in-the-Loop).

    Chiamata dall'endpoint di callback quando l'agente di coding ha finito di
    pushare il codice su work_branch. Apre la Pull Request (che resta OPEN per la
    revisione umana), la porta in stato 'open' — così "Valida codice" si abilita —
    e assegna il reviewer. Ritorna un esito per l'API.
    """
    wo = await _load_wo(wo_id)
    if not wo or not wo.get("repo_url"):
        return {"status": "skipped", "detail": "WO senza repository"}
    if wo.get("code_pr_state") in ("open", "merged"):
        return {"status": "skipped", "detail": f"PR già in stato {wo['code_pr_state']}"}

    provider = await git_provider.get_provider(wo["repo_url"])
    if provider is None:
        return {"status": "skipped", "detail": "provider Git non gestito"}

    try:
        base = wo.get("base_branch") or await provider.default_branch()
        head = _work_branch(wo)
        if not await provider.branch_exists(head):
            logger.warning("code-pushed per %s: branch %s assente", wo_id, head)
            return {"status": "no_branch", "detail": f"branch {head} assente"}
        closes = await _closes_clause(wo_id, wo.get("ticket_id"))
        body = (
            "Pull Request aperta dall'agente di coding. In attesa di revisione e "
            "validazione dello Scrum Master." + (f"\n\n{closes}" if closes else "")
        )
        pr = await provider.open_pr(
            head=head, base=base, title=f"[WO {wo_id[:8]}] {head}", body=body,
        )
        await _update_pr(wo_id, "code", url=pr["url"], state="open")
    except (GitError, httpx.HTTPError) as exc:
        await _update_pr(wo_id, "code", url=None, state="error")
        logger.warning("Apertura PR codice fallita per %s: %s", wo_id, exc)
        return {"status": "error", "detail": str(exc)}

    # reviewer/assignee = sviluppatore assegnato dallo Scrum Master
    info = await _assignee_info(wo)
    reviewer = info["github"] if info else None
    if reviewer:
        try:
            await provider.request_reviewers(pr["number"], [reviewer])
            await provider.add_assignees(pr["number"], [reviewer])
        except (GitError, httpx.HTTPError) as exc:
            logger.info("Reviewer non impostato sulla PR del WO %s: %s", wo_id, exc)

    await _publish("git.pr_opened", wo_id, "code", pr_url=pr["url"])
    logger.info("PR del codice del WO %s aperta per revisione: %s", wo_id, pr["url"])
    return {"status": "open", "pr_url": pr["url"], "pr_number": pr["number"]}


async def handle_document_validated(payload: dict) -> None:
    """Documento accettato dall'Agente di Coerenza -> commit + PR della documentazione."""
    document_id = payload.get("document_id")
    if not document_id:
        return
    async with SessionLocal() as session:
        doc = (
            await session.execute(
                text("SELECT id, wo_id, title, file_name, content FROM documents WHERE id = :id"),
                {"id": document_id},
            )
        ).mappings().first()
    if doc is None or not doc["wo_id"]:
        # documento caricato a mano (senza Work Order): nessun flusso git
        return

    wo = await _load_wo(doc["wo_id"])
    if not wo or not wo.get("repo_url"):
        logger.info("documents.validated per doc %s: WO senza repository, git saltato", document_id)
        return
    if wo.get("docs_pr_state") == "merged":  # idempotenza
        logger.info("documents.validated per doc %s: PR docs già fusa, salto", document_id)
        return

    provider = await git_provider.get_provider(wo["repo_url"])
    if provider is None:
        return

    opts = await _git_options()
    try:
        base = wo.get("base_branch") or await provider.default_branch()
        head = _docs_branch(wo)
        await provider.ensure_branch(head, base)
        path = f"docs/work-orders/{doc['file_name'] or (doc['id'][:8] + '.md')}"
        info = await _assignee_info(wo)
        author = {"name": info["name"], "email": info["email"]} if info else None
        await provider.commit_file(
            head, path=path, content=doc["content"] or "",
            message=f"docs: {doc['title']}", author=author,
        )
        pr = await provider.open_pr(
            head=head, base=base, title=f"[WO {wo['id'][:8]}] Documentazione",
            body="Pull Request della documentazione aperta dalla piattaforma dopo l'accettazione dell'Agente di Coerenza.",
        )
        await _update_pr(wo["id"], "docs", url=pr["url"], state="open")
    except (GitError, httpx.HTTPError) as exc:
        await _update_pr(wo["id"], "docs", url=None, state="error")
        await _publish("git.merge_failed", wo["id"], "docs", reason="error", detail=str(exc))
        logger.warning("Apertura PR docs fallita per doc %s: %s", document_id, exc)
        return

    await _review_and_merge(provider, wo, "docs", pr, opts, head)
