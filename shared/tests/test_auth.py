"""Test dell'enforcement RBAC condiviso (JWT + chiave interna)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from shared.common.auth import INTERNAL_HEADER, require_auth, require_roles

SECRET = "segreto-di-test"


def _token(roles: list[str], *, expired: bool = False) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=-5 if expired else 5)
    return jwt.encode({"sub": "utente", "uid": 1, "roles": roles, "exp": exp}, SECRET, algorithm="HS256")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("INTERNAL_API_KEY", "chiave-interna-di-test")

    app = FastAPI()

    @app.get("/protetto", dependencies=[Depends(require_auth)])
    async def protetto():
        return {"ok": True}

    @app.post("/solo-sm", dependencies=[Depends(require_roles("scrum_master"))])
    async def solo_sm():
        return {"ok": True}

    return TestClient(app)


def test_senza_token_401(client):
    assert client.get("/protetto").status_code == 401


def test_token_valido_200(client):
    r = client.get("/protetto", headers={"Authorization": f"Bearer {_token(['developer'])}"})
    assert r.status_code == 200


def test_token_scaduto_401(client):
    r = client.get("/protetto", headers={"Authorization": f"Bearer {_token(['developer'], expired=True)}"})
    assert r.status_code == 401


def test_token_con_segreto_sbagliato_401(client):
    falso = jwt.encode({"sub": "x", "roles": [], "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                       "altro-segreto", algorithm="HS256")
    assert client.get("/protetto", headers={"Authorization": f"Bearer {falso}"}).status_code == 401


def test_ruolo_mancante_403(client):
    r = client.post("/solo-sm", headers={"Authorization": f"Bearer {_token(['developer'])}"})
    assert r.status_code == 403


def test_ruolo_corretto_200(client):
    r = client.post("/solo-sm", headers={"Authorization": f"Bearer {_token(['scrum_master'])}"})
    assert r.status_code == 200


def test_chiave_interna_valida_200(client):
    r = client.get("/protetto", headers={INTERNAL_HEADER: "chiave-interna-di-test"})
    assert r.status_code == 200


def test_chiave_interna_supera_i_ruoli(client):
    r = client.post("/solo-sm", headers={INTERNAL_HEADER: "chiave-interna-di-test"})
    assert r.status_code == 200


def test_chiave_interna_errata_401(client):
    r = client.get("/protetto", headers={INTERNAL_HEADER: "chiave-sbagliata"})
    assert r.status_code == 401
