"""Test dell'orchestrazione Git: stato CI, merge robusto, riuso PR, opzioni.

Le chiamate HTTP a GitHub sono simulate con httpx.MockTransport, così i test
non toccano la rete. `github_client._client` viene sostituito con un client che
instrada le richieste sul transport di test.
"""
import httpx
import pytest

from app.services import github_client
from app.services.github_client import GitHubConfig, MergeConflictError

CFG = GitHubConfig(owner="octo", repo="demo", token="t0ken")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _patch_transport(monkeypatch, handler):
    def fake_client(cfg):
        return httpx.AsyncClient(
            base_url=github_client.API_BASE,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(github_client, "_client", fake_client)


# ---------------------------------------------------------------------------
# commit_ci_status: aggregazione di check-runs + legacy status
# ---------------------------------------------------------------------------

def _ci_handler(check_runs, legacy):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": check_runs})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=legacy)
        return httpx.Response(404)
    return handler


@pytest.mark.anyio
async def test_ci_status_success(monkeypatch):
    _patch_transport(monkeypatch, _ci_handler(
        [{"status": "completed", "conclusion": "success"}],
        {"state": "success", "total_count": 0},
    ))
    assert await github_client.commit_ci_status(CFG, "abc") == "success"


@pytest.mark.anyio
async def test_ci_status_pending(monkeypatch):
    _patch_transport(monkeypatch, _ci_handler(
        [{"status": "in_progress", "conclusion": None}],
        {"state": "pending", "total_count": 0},
    ))
    assert await github_client.commit_ci_status(CFG, "abc") == "pending"


@pytest.mark.anyio
async def test_ci_status_failure(monkeypatch):
    _patch_transport(monkeypatch, _ci_handler(
        [{"status": "completed", "conclusion": "failure"}],
        {"state": "success", "total_count": 0},
    ))
    assert await github_client.commit_ci_status(CFG, "abc") == "failure"


@pytest.mark.anyio
async def test_ci_status_none_quando_nessun_check(monkeypatch):
    _patch_transport(monkeypatch, _ci_handler([], {"state": "pending", "total_count": 0}))
    assert await github_client.commit_ci_status(CFG, "abc") == "none"


# ---------------------------------------------------------------------------
# merge_pull_request: gestione robusta di 405 (conflitto) e 409 (sha)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_merge_conflitto_405(monkeypatch):
    def handler(request):
        return httpx.Response(405, json={"message": "Pull Request is not mergeable"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(MergeConflictError):
        await github_client.merge_pull_request(CFG, 7)


@pytest.mark.anyio
async def test_merge_sha_cambiato_409(monkeypatch):
    def handler(request):
        return httpx.Response(409, json={"message": "Head branch was modified"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(MergeConflictError):
        await github_client.merge_pull_request(CFG, 7, sha="deadbeef")


@pytest.mark.anyio
async def test_merge_ok(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"merged": True})

    _patch_transport(monkeypatch, handler)
    assert await github_client.merge_pull_request(CFG, 7, method="squash") is True


# ---------------------------------------------------------------------------
# open_pull_request: riuso della PR esistente sul 422
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_open_pr_riusa_esistente_su_422(monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(422, json={"message": "A pull request already exists"})
        # GET /pulls?head=...
        return httpx.Response(200, json=[{
            "html_url": "https://github.com/octo/demo/pull/3",
            "number": 3, "state": "open", "head": {"sha": "cafe"},
        }])

    _patch_transport(monkeypatch, handler)
    pr = await github_client.open_pull_request(CFG, head="wo/x", base="main", title="t")
    assert pr["number"] == 3
    assert pr["head_sha"] == "cafe"


# ---------------------------------------------------------------------------
# delete_branch: idempotente (404/422 non sollevano)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_delete_branch_idempotente(monkeypatch):
    def handler(request):
        return httpx.Response(422, json={"message": "Reference does not exist"})

    _patch_transport(monkeypatch, handler)
    assert await github_client.delete_branch(CFG, "wo/x") is False


# ---------------------------------------------------------------------------
# git_flow._bool: coercizione delle opzioni booleane
# ---------------------------------------------------------------------------

def test_bool_coercion():
    from app.services.git_flow import _bool

    assert _bool(True) is True
    assert _bool("true") is True
    assert _bool("false") is False
    assert _bool("", default=True) is True
    assert _bool(None, default=False) is False
    assert _bool("off") is False


# ---------------------------------------------------------------------------
# crypto + repo_credentials: cifratura a riposo e derivazione dello slug
# ---------------------------------------------------------------------------

def test_crypto_round_trip():
    from app.services import crypto

    token = crypto.encrypt("ghp_supersecret")
    assert token != b"ghp_supersecret"          # cifrato, non in chiaro
    assert crypto.decrypt(token) == "ghp_supersecret"
    assert crypto.decrypt(None) is None


def test_repo_slug_normalizza_url_diversi():
    from app.services.repo_credentials import repo_slug

    assert repo_slug("https://github.com/Acme/Backend.git") == "acme/backend"
    assert repo_slug("git@github.com:Acme/Backend.git") == "acme/backend"
    assert repo_slug("https://gitlab.com/acme/backend") is None
