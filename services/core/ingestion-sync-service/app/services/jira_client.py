"""Client per le API REST di Jira Cloud (importazione e Reverse Sync).

La configurazione viene letta dalle impostazioni di piattaforma (DB),
con fallback sulle variabili d'ambiente.
"""
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.services import settings_store

logger = logging.getLogger(__name__)


@dataclass
class JiraConfig:
    base_url: str
    email: str
    token: str
    project_key: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.token)


async def load_config() -> JiraConfig:
    values = await settings_store.get_all()
    return JiraConfig(
        base_url=values.get("jira.base_url", ""),
        email=values.get("jira.email", ""),
        token=values.get("jira.api_token", ""),
        project_key=values.get("jira.project_key", ""),
    )


def _client(cfg: JiraConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/"),
        auth=(cfg.email, cfg.token),
        headers={"Accept": "application/json"},
        timeout=30.0,
    )


def _adf_to_text(adf: Any) -> str:
    """Estrae il testo semplice da un documento Atlassian Document Format."""
    if not isinstance(adf, dict):
        return str(adf or "")
    parts: list[str] = []

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            walk(child)

    walk(adf)
    return " ".join(parts).strip()


def _text_to_adf(text: str) -> dict:
    """Costruisce un documento ADF minimale da testo semplice."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text or "-"}]}
        ],
    }


def _map_issue(issue: dict) -> dict:
    fields = issue.get("fields", {})
    issuetype = (fields.get("issuetype") or {}).get("name", "").lower()
    return {
        "external_id": issue.get("key", ""),
        "type": "bug" if "bug" in issuetype else "change_request",
        "title": fields.get("summary", "(senza titolo)"),
        "description": _adf_to_text(fields.get("description")),
        "severity": (fields.get("priority") or {}).get("name"),
    }


async def test_connection(cfg: JiraConfig) -> dict:
    """Verifica le credenziali chiamando /myself."""
    if not cfg.configured:
        return {"ok": False, "detail": "Credenziali incomplete: servono base URL, email e API token"}
    try:
        async with _client(cfg) as client:
            response = await client.get("/rest/api/3/myself")
            response.raise_for_status()
            me = response.json()
        return {"ok": True, "detail": f"Autenticato come {me.get('displayName', cfg.email)}"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"Jira ha risposto {exc.response.status_code}: credenziali non valide"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Jira non raggiungibile: {exc}"}


async def search_issues(cfg: JiraConfig, jql: str, max_results: int = 50) -> list[dict]:
    """Cerca issue su Jira. Prova il nuovo endpoint /search/jql con fallback su /search."""
    fields = "summary,description,priority,issuetype,created"
    async with _client(cfg) as client:
        response = await client.get(
            "/rest/api/3/search/jql",
            params={"jql": jql, "maxResults": max_results, "fields": fields},
        )
        if response.status_code in (404, 410):
            response = await client.get(
                "/rest/api/3/search",
                params={"jql": jql, "maxResults": max_results, "fields": fields},
            )
        response.raise_for_status()
        issues = response.json().get("issues", [])
    logger.info("Jira: trovate %d issue per JQL %r", len(issues), jql)
    return [_map_issue(i) for i in issues]


async def create_issue(cfg: JiraConfig, *, title: str, description: str, ticket_type: str) -> str:
    """Crea una issue su Jira (Reverse Sync) e restituisce la key ufficiale."""
    if not cfg.project_key:
        raise ValueError("Project key Jira non configurata (pagina Impostazioni)")
    issue_type = "Bug" if ticket_type == "bug" else "Task"
    payload = {
        "fields": {
            "project": {"key": cfg.project_key},
            "summary": title,
            "description": _text_to_adf(description),
            "issuetype": {"name": issue_type},
        }
    }
    async with _client(cfg) as client:
        response = await client.post("/rest/api/3/issue", json=payload)
        response.raise_for_status()
        key = response.json()["key"]
    logger.info("Jira: creata issue %s (Reverse Sync)", key)
    return key
