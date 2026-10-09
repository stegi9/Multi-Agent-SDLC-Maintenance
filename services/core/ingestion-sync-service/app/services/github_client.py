"""Client per le API REST di GitHub (importazione issue e orchestrazione Git).

Il token è opzionale per i repository pubblici (rate limit ridotto); per le
operazioni di scrittura (branch, commit, Pull Request) è invece obbligatorio.

Tutte le chiamate passano da ``_request``, che gestisce retry con backoff sugli
errori transitori (5xx) e sul rate limit (403/429 con ``Retry-After`` o
``X-RateLimit-Reset``), così le operazioni Git non falliscono per problemi
temporanei di rete o di quota.
"""
import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.services import settings_store

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"

# Retry sugli errori transitori
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.5  # secondi: 1.5, 3, 4.5...
_RATELIMIT_MAX_WAIT = 30.0  # non attendere oltre questo per un reset del rate limit


@dataclass
class GitHubConfig:
    owner: str
    repo: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.owner and self.repo)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


async def load_config() -> GitHubConfig:
    values = await settings_store.get_all()
    return GitHubConfig(
        owner=values.get("github.owner", ""),
        repo=values.get("github.repo", ""),
        token=values.get("github.token", ""),
    )


def _client(cfg: GitHubConfig) -> httpx.AsyncClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    return httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=30.0)


def _ratelimited(response: httpx.Response) -> bool:
    """True se la risposta indica un rate limit GitHub esaurito."""
    if response.status_code == 429:
        return True
    if response.status_code == 403:
        remaining = response.headers.get("X-RateLimit-Remaining")
        return remaining == "0" or "rate limit" in response.text.lower()
    return False


def _retry_after(response: httpx.Response) -> float:
    """Secondi di attesa suggeriti dagli header (Retry-After o X-RateLimit-Reset)."""
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), _RATELIMIT_MAX_WAIT)
    reset = response.headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        delta = int(reset) - datetime.now(timezone.utc).timestamp()
        return min(max(delta, 0.0), _RATELIMIT_MAX_WAIT)
    return _BACKOFF_BASE


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Esegue una richiesta con retry su errori transitori e rate limit.

    Non solleva su 4xx applicativi (es. 404/422): il chiamante li interpreta.
    Ritenta solo su 5xx e rate limit, fino a ``_MAX_RETRIES`` volte.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:  # errore di rete: ritenta con backoff
            last_exc = exc
            if attempt == _MAX_RETRIES - 1:
                raise
            await asyncio.sleep(_BACKOFF_BASE * (attempt + 1))
            continue

        if _ratelimited(response) and attempt < _MAX_RETRIES - 1:
            wait = _retry_after(response)
            logger.warning("GitHub rate limit su %s: attendo %.1fs e ritento", url, wait)
            await asyncio.sleep(wait)
            continue
        if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
            logger.warning("GitHub %s su %s: ritento", response.status_code, url)
            await asyncio.sleep(_BACKOFF_BASE * (attempt + 1))
            continue
        return response
    # non raggiungibile, ma per sicurezza
    if last_exc:
        raise last_exc
    raise GitError("Richiesta GitHub fallita dopo i retry")


def _map_issue(cfg: GitHubConfig, issue: dict) -> dict:
    labels = {(l.get("name") or "").lower() for l in issue.get("labels", [])}
    severity = None
    for candidate in ("critical", "high", "medium", "low"):
        if candidate in labels:
            severity = candidate.capitalize()
            break
    return {
        "external_id": f"{cfg.owner}/{cfg.repo}#{issue['number']}",
        "type": "bug" if "bug" in labels else "change_request",
        "title": issue.get("title", "(senza titolo)"),
        "description": (issue.get("body") or "")[:5000],
        "severity": severity,
    }


async def test_connection(cfg: GitHubConfig) -> dict:
    """Verifica l'accesso al repository configurato."""
    if not cfg.configured:
        return {"ok": False, "detail": "Configurazione incompleta: servono owner e repository"}
    try:
        async with _client(cfg) as client:
            response = await _request(client, "GET", f"/repos/{cfg.slug}")
            response.raise_for_status()
            repo = response.json()
        visibility = "privato" if repo.get("private") else "pubblico"
        return {"ok": True, "detail": f"Repository {repo['full_name']} raggiungibile ({visibility})"}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {"ok": False, "detail": "Repository non trovato (o token senza permessi)"}
        return {"ok": False, "detail": f"GitHub ha risposto {exc.response.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"GitHub non raggiungibile: {exc}"}


async def search_issues(cfg: GitHubConfig, since_days: int | None = 1, max_results: int = 50) -> list[dict]:
    """Issue aperte del repository; con since_days limita alle più recenti (aging zero)."""
    params: dict = {"state": "open", "per_page": min(max_results, 100), "sort": "created", "direction": "desc"}
    if since_days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=since_days)
        params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    async with _client(cfg) as client:
        response = await _request(client, "GET", f"/repos/{cfg.slug}/issues", params=params)
        response.raise_for_status()
        items = response.json()
    # L'endpoint issues include anche le pull request: vanno escluse
    issues = [i for i in items if "pull_request" not in i]
    logger.info("GitHub: trovate %d issue su %s", len(issues), cfg.slug)
    return [_map_issue(cfg, i) for i in issues]


# ---------------------------------------------------------------------------
# Orchestrazione Git: branch, commit, Pull Request
# ---------------------------------------------------------------------------

class GitError(Exception):
    """Errore in un'operazione Git su GitHub."""


class MergeConflictError(GitError):
    """La Pull Request non è fondibile (conflitti, base cambiata o check falliti)."""


async def config_for_repo(repo_url: str) -> GitHubConfig | None:
    """Costruisce una GitHubConfig da un URL repo arbitrario (owner/repo dedotti
    dall'URL). Usa il **token dedicato del repository** se configurato (cifrato a
    riposo), altrimenti ripiega sul token globale di piattaforma. Ritorna None se
    l'URL non è un repository GitHub riconoscibile."""
    match = re.search(r"github\.com[/:]+([^/]+)/([^/]+?)(?:\.git)?/?$", (repo_url or "").strip())
    if not match:
        return None
    # import locale per evitare un ciclo di import a livello di modulo
    from app.services import repo_credentials

    token = await repo_credentials.get_token(repo_url)
    if not token:
        values = await settings_store.get_all()
        token = values.get("github.token", "")
    return GitHubConfig(owner=match.group(1), repo=match.group(2), token=token)


async def get_default_branch(cfg: GitHubConfig) -> str:
    async with _client(cfg) as client:
        response = await _request(client, "GET", f"/repos/{cfg.slug}")
        response.raise_for_status()
        return response.json().get("default_branch", "main")


async def _branch_sha(client: httpx.AsyncClient, cfg: GitHubConfig, branch: str) -> str | None:
    """SHA della testa di un branch, o None se il branch non esiste."""
    response = await _request(client, "GET", f"/repos/{cfg.slug}/git/ref/heads/{branch}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["object"]["sha"]


async def branch_exists(cfg: GitHubConfig, branch: str) -> bool:
    async with _client(cfg) as client:
        return await _branch_sha(client, cfg, branch) is not None


async def ensure_branch(cfg: GitHubConfig, branch: str, base_branch: str) -> bool:
    """Crea `branch` a partire da `base_branch` se non esiste già. Ritorna True se
    il branch è presente (creato o preesistente)."""
    if not cfg.token:
        raise GitError("Token GitHub assente: impossibile creare branch")
    async with _client(cfg) as client:
        if await _branch_sha(client, cfg, branch) is not None:
            return True
        base_sha = await _branch_sha(client, cfg, base_branch)
        if base_sha is None:
            raise GitError(f"Branch base '{base_branch}' inesistente")
        response = await _request(
            client, "POST", f"/repos/{cfg.slug}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        response.raise_for_status()
        logger.info("Branch %s creato da %s su %s", branch, base_branch, cfg.slug)
        return True


async def delete_branch(cfg: GitHubConfig, branch: str) -> bool:
    """Elimina un branch (cleanup dopo il merge). Non solleva se il branch è già assente."""
    if not cfg.token:
        return False
    async with _client(cfg) as client:
        response = await _request(client, "DELETE", f"/repos/{cfg.slug}/git/refs/heads/{branch}")
        if response.status_code in (204, 422, 404):
            logger.info("Branch %s eliminato su %s (status %s)", branch, cfg.slug, response.status_code)
            return response.status_code == 204
        response.raise_for_status()
        return True


async def put_file(
    cfg: GitHubConfig, branch: str, path: str, content: str, message: str,
    author: dict | None = None,
) -> bool:
    """Crea o aggiorna un file di testo su un branch (commit singolo).

    `author` opzionale: {"name": ..., "email": ...} per attribuire il commit allo
    sviluppatore assegnato invece che al bot di piattaforma.
    """
    if not cfg.token:
        raise GitError("Token GitHub assente: impossibile committare")
    async with _client(cfg) as client:
        # se il file esiste già serve lo sha per l'update
        existing = await _request(
            client, "GET", f"/repos/{cfg.slug}/contents/{path}", params={"ref": branch}
        )
        sha = existing.json().get("sha") if existing.status_code == 200 else None
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        if author and author.get("name") and author.get("email"):
            body["author"] = {"name": author["name"], "email": author["email"]}
            body["committer"] = body["author"]
        response = await _request(client, "PUT", f"/repos/{cfg.slug}/contents/{path}", json=body)
        response.raise_for_status()
        logger.info("Commit di %s su %s (%s)", path, branch, cfg.slug)
        return True


async def open_pull_request(
    cfg: GitHubConfig, head: str, base: str, title: str, body: str = "", draft: bool = False
) -> dict:
    """Apre una Pull Request head->base. Se ne esiste già una aperta per lo stesso
    head, la riusa invece di fallire. Ritorna url, number e head_sha."""
    if not cfg.token:
        raise GitError("Token GitHub assente: impossibile aprire Pull Request")
    async with _client(cfg) as client:
        response = await _request(
            client, "POST", f"/repos/{cfg.slug}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        if response.status_code == 422:  # PR già esistente per questo head
            existing = await _request(
                client, "GET", f"/repos/{cfg.slug}/pulls",
                params={"head": f"{cfg.owner}:{head}", "state": "open"},
            )
            existing.raise_for_status()
            items = existing.json()
            if items:
                pr = items[0]
                return {
                    "url": pr["html_url"], "number": pr["number"], "state": pr.get("state", "open"),
                    "head_sha": pr["head"]["sha"],
                }
        response.raise_for_status()
        pr = response.json()
        return {
            "url": pr["html_url"], "number": pr["number"],
            "state": "draft" if draft else "open", "head_sha": pr["head"]["sha"],
        }


async def request_reviewers(cfg: GitHubConfig, number: int, reviewers: list[str]) -> None:
    """Richiede la review agli utenti GitHub indicati (best effort, non blocca il flusso)."""
    reviewers = [r for r in reviewers if r]
    if not cfg.token or not reviewers:
        return
    async with _client(cfg) as client:
        response = await _request(
            client, "POST", f"/repos/{cfg.slug}/pulls/{number}/requested_reviewers",
            json={"reviewers": reviewers},
        )
        if response.status_code >= 400:
            # reviewer inesistente o senza accesso: si registra ma non si interrompe
            logger.warning("Reviewer %s non impostati sulla PR #%s (%s)", reviewers, number, response.status_code)


async def add_assignees(cfg: GitHubConfig, number: int, assignees: list[str]) -> None:
    """Assegna la PR/issue agli utenti indicati (best effort)."""
    assignees = [a for a in assignees if a]
    if not cfg.token or not assignees:
        return
    async with _client(cfg) as client:
        response = await _request(
            client, "POST", f"/repos/{cfg.slug}/issues/{number}/assignees",
            json={"assignees": assignees},
        )
        if response.status_code >= 400:
            logger.warning("Assignee %s non impostati sulla PR #%s (%s)", assignees, number, response.status_code)


async def get_pull_request(cfg: GitHubConfig, number: int) -> dict:
    """Stato di una PR: mergeable, mergeable_state, head_sha, merged."""
    async with _client(cfg) as client:
        response = await _request(client, "GET", f"/repos/{cfg.slug}/pulls/{number}")
        response.raise_for_status()
        pr = response.json()
    return {
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "head_sha": pr["head"]["sha"],
        "merged": pr.get("merged", False),
        "state": pr.get("state"),
    }


async def wait_until_mergeable(cfg: GitHubConfig, number: int, attempts: int = 5) -> dict:
    """GitHub calcola `mergeable` in modo asincrono: appena aperta la PR il campo è
    null. Si fa polling finché il campo è valorizzato (o si esauriscono i tentativi)."""
    pr = await get_pull_request(cfg, number)
    for _ in range(attempts - 1):
        if pr["mergeable"] is not None:
            return pr
        await asyncio.sleep(_BACKOFF_BASE)
        pr = await get_pull_request(cfg, number)
    return pr


async def commit_ci_status(cfg: GitHubConfig, sha: str) -> str:
    """Esito complessivo della CI su un commit: 'success' | 'pending' | 'failure' | 'none'.

    Combina i Check Runs (GitHub Actions) e le legacy Commit Statuses. Se non
    esiste alcun controllo, ritorna 'none' (nessuna CI configurata)."""
    async with _client(cfg) as client:
        checks_resp = await _request(client, "GET", f"/repos/{cfg.slug}/commits/{sha}/check-runs")
        status_resp = await _request(client, "GET", f"/repos/{cfg.slug}/commits/{sha}/status")
    checks_resp.raise_for_status()
    status_resp.raise_for_status()
    runs = checks_resp.json().get("check_runs", [])
    legacy = status_resp.json()

    total = 0
    pending = False
    failure = False
    for run in runs:
        total += 1
        if run.get("status") != "completed":
            pending = True
        elif run.get("conclusion") not in ("success", "neutral", "skipped"):
            failure = True

    legacy_state = legacy.get("state")  # success | pending | failure | error
    legacy_total = legacy.get("total_count", 0)
    if legacy_total:
        total += legacy_total
        if legacy_state == "pending":
            pending = True
        elif legacy_state in ("failure", "error"):
            failure = True

    if total == 0:
        return "none"
    if failure:
        return "failure"
    if pending:
        return "pending"
    return "success"


async def close_pull_request(cfg: GitHubConfig, number: int) -> bool:
    """Chiude una Pull Request senza fonderla (codice rifiutato)."""
    if not cfg.token:
        raise GitError("Token GitHub assente: impossibile chiudere Pull Request")
    async with _client(cfg) as client:
        response = await _request(
            client, "PATCH", f"/repos/{cfg.slug}/pulls/{number}", json={"state": "closed"}
        )
        response.raise_for_status()
        logger.info("Pull Request #%s chiusa su %s", number, cfg.slug)
        return True


async def merge_pull_request(
    cfg: GitHubConfig, number: int, method: str = "merge", sha: str | None = None
) -> bool:
    """Fonde una Pull Request. Solleva MergeConflictError se non è fondibile.

    method: 'merge' | 'squash' | 'rebase'. `sha` opzionale = SHA atteso della testa
    (protezione contro merge di codice cambiato sotto i piedi: 409 se non combacia).
    """
    if not cfg.token:
        raise GitError("Token GitHub assente: impossibile fondere Pull Request")
    body: dict = {"merge_method": method}
    if sha:
        body["sha"] = sha
    async with _client(cfg) as client:
        response = await _request(client, "PUT", f"/repos/{cfg.slug}/pulls/{number}/merge", json=body)
        if response.status_code == 405:
            raise MergeConflictError(f"PR #{number} non fondibile: {response.json().get('message', 'conflitti')}")
        if response.status_code == 409:
            raise MergeConflictError(f"PR #{number}: la testa del branch è cambiata (SHA non coincide)")
        response.raise_for_status()
        logger.info("Pull Request #%s fusa su %s", number, cfg.slug)
        return True
