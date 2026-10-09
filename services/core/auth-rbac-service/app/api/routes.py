"""Endpoint di autenticazione e RBAC."""
import re

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.security import JWTError, create_access_token, decode_token, verify_password

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    id: int
    username: str
    email: str
    domain: str | None = None
    roles: list[str]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo


async def _load_user(username: str) -> dict | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, username, email, password_hash, domain, is_active "
                    "FROM users WHERE username = :u"
                ),
                {"u": username},
            )
        ).mappings().first()
        if row is None:
            return None
        roles = (
            await session.execute(
                text(
                    "SELECT r.name FROM roles r "
                    "JOIN user_roles ur ON ur.role_id = r.id WHERE ur.user_id = :uid"
                ),
                {"uid": row["id"]},
            )
        ).scalars().all()
    return {**row, "roles": list(roles)}


def _user_info(user: dict) -> UserInfo:
    return UserInfo(
        id=user["id"],
        username=user["username"],
        email=user["email"],
        domain=user["domain"],
        roles=user["roles"],
    )


def _current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token mancante")
    try:
        return decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token non valido o scaduto")


@router.post("/auth/login", response_model=LoginResponse, summary="Autentica un utente e restituisce un token JWT")
async def login(body: LoginRequest) -> LoginResponse:
    user = await _load_user(body.username)
    if user is None or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenziali non valide")
    if not user["is_active"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Utente disabilitato")

    token = create_access_token(user_id=user["id"], username=user["username"], roles=user["roles"])
    return LoginResponse(access_token=token, user=_user_info(user))


@router.post("/auth/refresh", response_model=LoginResponse, summary="Rinnova il token JWT")
async def refresh_token(claims: dict = Depends(_current_claims)) -> LoginResponse:
    user = await _load_user(claims["sub"])
    if user is None or not user["is_active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Utente non valido")
    token = create_access_token(user_id=user["id"], username=user["username"], roles=user["roles"])
    return LoginResponse(access_token=token, user=_user_info(user))


@router.get("/users/me", response_model=UserInfo, summary="Restituisce l'utente corrente e i suoi ruoli")
async def get_current_user(claims: dict = Depends(_current_claims)) -> UserInfo:
    user = await _load_user(claims["sub"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
    return _user_info(user)


@router.get("/roles", summary="Elenca i ruoli RBAC disponibili")
async def list_roles() -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(text("SELECT id, name, description FROM roles ORDER BY id"))
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Gestione utenti e domini (riservata allo Scrum Master)
# ---------------------------------------------------------------------------

def _require_scrum_master(claims: dict = Depends(_current_claims)) -> dict:
    if "scrum_master" not in (claims.get("roles") or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operazione riservata allo Scrum Master",
        )
    return claims


class DomainUpdate(BaseModel):
    domain: str | None = None  # None o stringa vuota per rimuovere l'assegnazione


class GithubUpdate(BaseModel):
    github_username: str | None = None  # None o stringa vuota per rimuovere l'handle


@router.get("/users", summary="Elenca gli utenti della piattaforma (solo Scrum Master)")
async def list_users(_: dict = Depends(_require_scrum_master)) -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT u.id, u.username, u.email, u.domain, u.github_username, u.is_active, "
                    "GROUP_CONCAT(r.name) AS roles "
                    "FROM users u "
                    "LEFT JOIN user_roles ur ON ur.user_id = u.id "
                    "LEFT JOIN roles r ON r.id = ur.role_id "
                    "GROUP BY u.id ORDER BY u.username"
                )
            )
        ).mappings().all()
    return [
        {**dict(r), "roles": (r["roles"] or "").split(",") if r["roles"] else []}
        for r in rows
    ]


@router.put(
    "/users/{user_id}/domain",
    summary="Assegna uno sviluppatore a un dominio software (es. billing, hr)",
)
async def assign_domain(
    user_id: int, body: DomainUpdate, claims: dict = Depends(_require_scrum_master)
) -> dict:
    domain = (body.domain or "").strip().lower() or None
    async with SessionLocal() as session:
        row = (
            await session.execute(text("SELECT id, username FROM users WHERE id = :id"), {"id": user_id})
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
        await session.execute(
            text("UPDATE users SET domain = :d WHERE id = :id"), {"d": domain, "id": user_id}
        )
        await session.execute(
            text(
                "INSERT INTO audit_log (user_id, action, entity_type, entity_id, details) "
                "VALUES (:uid, 'domain_assigned', 'user', :target, JSON_OBJECT('domain', :d, 'username', :uname))"
            ),
            {"uid": claims.get("uid"), "target": str(user_id), "d": domain, "uname": row["username"]},
        )
        await session.commit()
    return {"user_id": user_id, "username": row["username"], "domain": domain}


@router.put(
    "/users/{user_id}/github",
    summary="Imposta l'handle GitHub di uno sviluppatore (reviewer/assignee delle Pull Request)",
)
async def assign_github(
    user_id: int, body: GithubUpdate, claims: dict = Depends(_require_scrum_master)
) -> dict:
    # handle GitHub: lettere/cifre e trattini singoli, niente spazi
    handle = re.sub(r"[^A-Za-z0-9-]", "", (body.github_username or "").strip()) or None
    async with SessionLocal() as session:
        row = (
            await session.execute(text("SELECT id, username FROM users WHERE id = :id"), {"id": user_id})
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
        await session.execute(
            text("UPDATE users SET github_username = :g WHERE id = :id"), {"g": handle, "id": user_id}
        )
        await session.execute(
            text(
                "INSERT INTO audit_log (user_id, action, entity_type, entity_id, details) "
                "VALUES (:uid, 'github_assigned', 'user', :target, JSON_OBJECT('github_username', :g, 'username', :uname))"
            ),
            {"uid": claims.get("uid"), "target": str(user_id), "g": handle, "uname": row["username"]},
        )
        await session.commit()
    return {"user_id": user_id, "username": row["username"], "github_username": handle}
