"""Verifica che gli endpoint del servizio siano protetti (RBAC backend)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt

from app.main import app


def _token(roles: list[str], secret: str = "change-me-in-production") -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    return jwt.encode({"sub": "u", "uid": 1, "roles": roles, "exp": exp}, secret, algorithm="HS256")


def test_health_resta_pubblico():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_api_senza_token_401():
    with TestClient(app) as client:
        response = client.post("/api/v1/execute", json={"wo_id": "x"})
    assert response.status_code == 401


def test_execute_ruolo_non_abilitato_403():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/execute",
            json={"wo_id": "x"},
            headers={"Authorization": f"Bearer {_token(['product_manager'])}"},
        )
    assert response.status_code == 403
