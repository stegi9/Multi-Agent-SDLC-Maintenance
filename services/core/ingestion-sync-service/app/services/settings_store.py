"""Store delle impostazioni di piattaforma (tabella platform_settings).

Le credenziali delle integrazioni si gestiscono dalla pagina Impostazioni
della dashboard. Le variabili d'ambiente del .env restano come fallback
per le chiavi non ancora salvate nel database.
"""
from sqlalchemy import text

from app.core.config import settings as env_settings
from app.core.db import SessionLocal

# Chiavi note: (chiave, è_segreta, fallback dalla variabile d'ambiente)
KNOWN_SETTINGS: list[tuple[str, bool, str]] = [
    ("jira.base_url", False, "jira_base_url"),
    ("jira.email", False, "jira_email"),
    ("jira.api_token", True, "jira_api"),
    ("jira.project_key", False, "jira_project_key"),
    ("github.owner", False, ""),
    ("github.repo", False, ""),
    ("github.token", True, "github_token"),
    # Comportamento dell'orchestrazione Git (branch/PR/merge)
    ("git.merge_method", False, "git_merge_method"),                    # merge | squash | rebase
    ("git.require_ci", False, "git_require_ci"),                        # "true"/"false"
    ("git.delete_branch_on_merge", False, "git_delete_branch_on_merge"),  # "true"/"false"
    ("bugzilla.base_url", False, ""),
    ("bugzilla.api_key", True, ""),
    ("bugzilla.product", False, ""),
    ("cve.keyword", False, ""),
    ("cve.api_key", True, ""),
    ("cve.base_url", False, ""),
    ("repo.git_url", False, ""),
    ("repo.branch", False, ""),
]

SECRET_KEYS = {k for k, secret, _ in KNOWN_SETTINGS if secret}
MASK = "••••••••"


def _env_fallback(env_attr: str) -> str:
    return getattr(env_settings, env_attr, "") if env_attr else ""


async def get_all() -> dict[str, str]:
    """Tutti i valori effettivi (DB con fallback su env). Uso interno, non mascherato."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(text("SELECT setting_key, setting_value FROM platform_settings"))
        ).all()
    stored = {k: v for k, v in rows}
    return {
        key: stored.get(key) or _env_fallback(env_attr)
        for key, _, env_attr in KNOWN_SETTINGS
    }


async def get_masked() -> list[dict]:
    """Vista per la UI: i segreti sono mascherati, mai restituiti in chiaro."""
    values = await get_all()
    result = []
    for key, secret, _ in KNOWN_SETTINGS:
        value = values.get(key, "")
        result.append(
            {
                "key": key,
                "value": (MASK if value else "") if secret else value,
                "secret": secret,
                "configured": bool(value),
            }
        )
    return result


async def set_many(pairs: dict[str, str]) -> int:
    """Upsert delle impostazioni. Ignora i segreti inviati come maschera."""
    updated = 0
    async with SessionLocal() as session:
        for key, value in pairs.items():
            if key not in {k for k, _, _ in KNOWN_SETTINGS}:
                continue
            if key in SECRET_KEYS and value == MASK:
                continue  # la UI rimanda la maschera quando il segreto non è cambiato
            value = (value or "").strip()
            if value:
                await session.execute(
                    text(
                        "INSERT INTO platform_settings (setting_key, setting_value, is_secret) "
                        "VALUES (:k, :v, :s) "
                        "ON DUPLICATE KEY UPDATE setting_value = :v"
                    ),
                    {"k": key, "v": value, "s": key in SECRET_KEYS},
                )
            else:
                await session.execute(
                    text("DELETE FROM platform_settings WHERE setting_key = :k"), {"k": key}
                )
            updated += 1
        await session.commit()
    return updated
