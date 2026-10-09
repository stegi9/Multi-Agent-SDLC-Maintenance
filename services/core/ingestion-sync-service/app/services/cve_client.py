"""Client per l'importazione delle vulnerabilità CVE dal NVD (NIST).

API pubblica NVD 2.0 (https://services.nvd.nist.gov/rest/json/cves/2.0).
L'API key è opzionale (aumenta solo il rate limit). Per evitare di importare
l'intero database, l'import è sempre scopato da una parola chiave (es. la
tecnologia/prodotto aziendale) e dalla finestra temporale (aging zero).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.services import settings_store

logger = logging.getLogger(__name__)

DEFAULT_NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"


@dataclass
class CveConfig:
    base_url: str
    api_key: str
    keyword: str

    @property
    def configured(self) -> bool:
        # serve almeno una parola chiave per scopare la ricerca (no import totale)
        return bool(self.keyword)


async def load_config() -> CveConfig:
    values = await settings_store.get_all()
    return CveConfig(
        base_url=values.get("cve.base_url", "") or DEFAULT_NVD_URL,
        api_key=values.get("cve.api_key", ""),
        keyword=values.get("cve.keyword", ""),
    )


def _client(cfg: CveConfig) -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    if cfg.api_key:
        headers["apiKey"] = cfg.api_key
    return httpx.AsyncClient(headers=headers, timeout=40.0)


def _severity(cve: dict) -> str | None:
    metrics = cve.get("metrics", {})
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key) or []
        if entries:
            data = entries[0].get("cvssData", {})
            return data.get("baseSeverity") or entries[0].get("baseSeverity")
    return None


def _map_cve(item: dict) -> dict:
    cve = item.get("cve", {})
    cid = cve.get("id", "")
    desc = next(
        (d["value"] for d in cve.get("descriptions", []) if d.get("lang") == "en"),
        "",
    )
    return {
        "external_id": cid,
        "type": "cve",
        "title": f"{cid}: {desc[:120]}" if desc else cid,
        "description": desc[:2000],
        "severity": _severity(cve),
    }


async def test_connection(cfg: CveConfig) -> dict:
    if not cfg.configured:
        return {"ok": False, "detail": "Configurazione incompleta: serve una parola chiave (cve.keyword)"}
    try:
        async with _client(cfg) as client:
            response = await client.get(
                cfg.base_url, params={"keywordSearch": cfg.keyword, "resultsPerPage": 1}
            )
            response.raise_for_status()
            total = response.json().get("totalResults", 0)
        return {"ok": True, "detail": f"NVD raggiungibile: {total} CVE totali per '{cfg.keyword}'"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"NVD ha risposto {exc.response.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"NVD non raggiungibile: {exc}"}


async def search_cves(cfg: CveConfig, since_days: int | None = 1, max_results: int = 50) -> list[dict]:
    """Importa le CVE per parola chiave. since_days=1 -> aging zero (ultime 24 ore)."""
    params: dict[str, Any] = {
        "keywordSearch": cfg.keyword,
        "resultsPerPage": min(max_results, 2000),
    }
    if since_days is not None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=since_days)
        # NVD vuole il formato ISO-8601 esteso con millisecondi
        params["pubStartDate"] = start.strftime("%Y-%m-%dT%H:%M:%S.000")
        params["pubEndDate"] = end.strftime("%Y-%m-%dT%H:%M:%S.000")

    async with _client(cfg) as client:
        response = await client.get(cfg.base_url, params=params)
        response.raise_for_status()
        vulns = response.json().get("vulnerabilities", [])
    logger.info("NVD: trovate %d CVE per '%s'", len(vulns), cfg.keyword)
    return [_map_cve(v) for v in vulns if v.get("cve", {}).get("id")]
