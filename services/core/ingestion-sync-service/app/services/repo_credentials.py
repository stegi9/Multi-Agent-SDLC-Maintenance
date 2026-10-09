"""Credenziali Git per-repository (token dedicato, cifrato a riposo).

Ogni repository può avere un proprio token GitHub, con permessi minimi e
indipendente dal token globale di piattaforma. Il token è memorizzato cifrato
(Fernet) nella tabella ``repo_credentials`` e non viene mai restituito in chiaro
dalle API: la UI vede solo se è configurato o meno.

La chiave è lo *slug* ``owner/repo`` minuscolo, così URL diversi che puntano
allo stesso repository (con/senza ``.git``, http/ssh) condividono il token.
"""
import re

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services import crypto


def repo_slug(repo_url: str) -> str | None:
    """owner/repo minuscolo estratto dall'URL, o None se non è GitHub."""
    match = re.search(r"github\.com[/:]+([^/]+)/([^/]+?)(?:\.git)?/?$", (repo_url or "").strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}".lower()


async def set_token(repo_url: str, token: str) -> bool:
    """Salva/aggiorna (cifrato) il token del repo. Token vuoto = rimozione."""
    slug = repo_slug(repo_url)
    if slug is None:
        raise ValueError("URL non è un repository GitHub valido")
    async with SessionLocal() as session:
        if not (token or "").strip():
            await session.execute(
                text("DELETE FROM repo_credentials WHERE repo_slug = :s"), {"s": slug}
            )
            await session.commit()
            return False
        enc = crypto.encrypt(token.strip())
        await session.execute(
            text(
                "INSERT INTO repo_credentials (repo_slug, repo_url, token_encrypted) "
                "VALUES (:s, :u, :t) "
                "ON DUPLICATE KEY UPDATE repo_url = :u, token_encrypted = :t"
            ),
            {"s": slug, "u": repo_url.strip(), "t": enc},
        )
        await session.commit()
        return True


async def get_token(repo_url: str) -> str | None:
    """Token in chiaro del repo (decifrato), o None se non configurato."""
    slug = repo_slug(repo_url)
    if slug is None:
        return None
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT token_encrypted FROM repo_credentials WHERE repo_slug = :s"),
                {"s": slug},
            )
        ).first()
    return crypto.decrypt(row[0]) if row else None


async def list_configured() -> list[dict]:
    """Repository con token dedicato (mai il token, solo lo stato)."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT repo_slug, repo_url, updated_at FROM repo_credentials ORDER BY repo_slug")
            )
        ).mappings().all()
    return [
        {"repo_slug": r["repo_slug"], "repo_url": r["repo_url"], "configured": True, "updated_at": r["updated_at"]}
        for r in rows
    ]
