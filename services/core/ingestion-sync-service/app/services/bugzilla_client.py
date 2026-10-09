"""Client per le API REST di Bugzilla (importazione bug).

Compatibile con Bugzilla 5.x (endpoint /rest). L'API key è opzionale:
le istanze pubbliche (es. bugzilla.mozilla.org) espongono i bug pubblici
senza autenticazione.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.services import settings_store

logger = logging.getLogger(__name__)


@dataclass
class BugzillaConfig:
    base_url: str
    api_key: str
    product: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.product)


async def load_config() -> BugzillaConfig:
    values = await settings_store.get_all()
    return BugzillaConfig(
        base_url=values.get("bugzilla.base_url", ""),
        api_key=values.get("bugzilla.api_key", ""),
        product=values.get("bugzilla.product", ""),
    )


def _client(cfg: BugzillaConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/"),
        headers={"Accept": "application/json"},
        timeout=30.0,
    )


def _map_bug(bug: dict) -> dict:
    severity = bug.get("severity") or bug.get("priority") or None
    return {
        "external_id": str(bug.get("id", "")),
        "type": "bug",
        "title": bug.get("summary", "(senza titolo)"),
        "description": f"[Bugzilla] {bug.get('product', '')} / {bug.get('component', '')} — {bug.get('summary', '')}",
        "severity": severity if severity not in ("--", "N/A") else None,
    }


async def test_connection(cfg: BugzillaConfig) -> dict:
    """Verifica il server chiamando /rest/version e l'esistenza del prodotto."""
    if not cfg.configured:
        return {"ok": False, "detail": "Configurazione incompleta: servono base URL e prodotto"}
    try:
        async with _client(cfg) as client:
            response = await client.get("/rest/version")
            response.raise_for_status()
            version = response.json().get("version", "?")
        return {"ok": True, "detail": f"Bugzilla {version} raggiungibile (prodotto: {cfg.product})"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"Bugzilla ha risposto {exc.response.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Bugzilla non raggiungibile: {exc}"}


async def search_bugs(cfg: BugzillaConfig, since_days: int | None = 1, max_results: int = 50) -> list[dict]:
    """Cerca i bug del prodotto configurato. since_days=1 -> aging zero (ultime 24 ore)."""
    params: dict = {
        "product": cfg.product,
        "limit": max_results,
        "include_fields": "id,summary,status,severity,priority,product,component,creation_time",
        "order": "creation_time DESC",
    }
    if since_days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=since_days)
        params["creation_time"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    if cfg.api_key:
        params["api_key"] = cfg.api_key

    async with _client(cfg) as client:
        response = await client.get("/rest/bug", params=params)
        response.raise_for_status()
        bugs = response.json().get("bugs", [])
    logger.info("Bugzilla: trovati %d bug per il prodotto %s", len(bugs), cfg.product)
    return [_map_bug(b) for b in bugs]
