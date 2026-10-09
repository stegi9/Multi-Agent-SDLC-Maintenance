"""Autenticazione e RBAC condivisi tra i microservizi.

Enforcement lato backend (Fase 1.1 della roadmap): ogni servizio protegge le
proprie API verificando il JWT emesso dall'auth-rbac-service (stesso segreto
HS256, letto da JWT_SECRET). Le chiamate machine-to-machine (n8n, chiamate
servizio→servizio) usano l'header `X-Internal-Api-Key`, confrontato con la
variabile d'ambiente INTERNAL_API_KEY: il principal interno supera anche i
controlli di ruolo.

Uso tipico:

    from shared.common.auth import require_auth, require_roles

    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_auth)])

    @router.post("/validate", dependencies=[Depends(require_roles("scrum_master"))])
    async def validate(...): ...
"""
import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

INTERNAL_HEADER = "X-Internal-Api-Key"

_bearer = HTTPBearer(auto_error=False)


def _jwt_secret() -> str:
    return os.getenv("JWT_SECRET", "change-me-in-production")


def _jwt_algorithm() -> str:
    return os.getenv("JWT_ALGORITHM", "HS256")


def _internal_key() -> str:
    return os.getenv("INTERNAL_API_KEY", "")


def internal_headers() -> dict[str, str]:
    """Header da allegare alle chiamate HTTP servizio→servizio."""
    key = _internal_key()
    return {INTERNAL_HEADER: key} if key else {}


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Dipendenza FastAPI: accetta un JWT utente valido o la chiave interna.

    Restituisce il principal: {"sub", "uid", "roles", "internal"}.
    """
    provided = request.headers.get(INTERNAL_HEADER, "")
    internal = _internal_key()
    if internal and provided and secrets.compare_digest(provided, internal):
        return {"sub": "internal", "uid": None, "roles": [], "internal": True}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token mancante",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = jwt.decode(credentials.credentials, _jwt_secret(), algorithms=[_jwt_algorithm()])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token non valido o scaduto",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "sub": claims.get("sub"),
        "uid": claims.get("uid"),
        "roles": list(claims.get("roles") or []),
        "internal": False,
    }


def require_roles(*roles: str):
    """Dipendenza FastAPI: richiede almeno uno dei ruoli indicati.

    Il principal interno (chiave di servizio) passa sempre: l'automazione
    (n8n, eventi) agisce per conto della piattaforma.
    """

    async def dependency(principal: dict = Depends(require_auth)) -> dict:
        if principal.get("internal") or set(roles) & set(principal.get("roles") or []):
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Operazione riservata ai ruoli: {', '.join(roles)}",
        )

    return dependency


__all__ = ["require_auth", "require_roles", "internal_headers", "INTERNAL_HEADER"]
