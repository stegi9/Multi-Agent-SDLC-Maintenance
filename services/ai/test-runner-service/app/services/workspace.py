"""Preparazione dell'area di lavoro Git su cui eseguire i test.

Il servizio tiene una working copy PERSISTENTE per repository, non una per
esecuzione: è ciò che rende riutilizzabile `node_modules` fra un run e il
successivo, e quindi ciò che fa scendere il secondo run da minuti a secondi.

    /testspace/
    ├── repos/<owner_repo>/     working copy (con node_modules dentro)
    ├── pnpm-store/             store del gestore di pacchetti
    └── reports/<run_id>.json   report prodotto dal runner di test

Lo store deve stare sotto la stessa radice della working copy: il gestore di
pacchetti collega i file con hard link, e gli hard link non attraversano i
filesystem.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.services.process import CommandError, run_command

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceInfo:
    """Esito della preparazione: dove si è finiti e che cosa è cambiato."""

    path: str
    branch: str                       # branch effettivamente testato
    base_branch: str
    commit_sha: str = ""
    changed_files: set[str] = field(default_factory=set)
    fallback_reason: str = ""         # valorizzato se si è ripiegato sul branch base


def repo_dir_name(repo_url: str) -> str:
    """Nome di cartella prevedibile a partire dall'URL: `owner_repo`.

    Stessa normalizzazione usata dall'Agente Mappatore, così le cartelle restano
    ispezionabili a mano e coerenti fra i servizi.
    """
    cleaned = re.sub(r"\.git$", "", (repo_url or "").rstrip("/"))
    parts = [p for p in re.split(r"[/:]", cleaned) if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    return re.sub(r"[^A-Za-z0-9_.-]", "_", "_".join(tail)) or "repo"


def workspace_root() -> Path:
    return Path(settings.test_workspace_path)


def store_dir() -> Path:
    return workspace_root() / "pnpm-store"


def reports_dir() -> Path:
    return workspace_root() / "reports"


async def branch_exists_on_remote(repo_url: str, branch: str) -> bool:
    """Verifica se un branch esiste su origin, senza clonare nulla.

    Serve a rispondere subito all'utente quando l'agente cloud non ha ancora
    pubblicato il codice: costa un secondo ed evita un'esecuzione destinata a
    fallire.
    """
    if not repo_url or not branch:
        return False
    try:
        code, output = await run_command(
            ["git", "ls-remote", "--heads", repo_url, branch],
            cwd=str(workspace_root()),
            timeout=15,
        )
    except CommandError:
        # Rete assente o URL non raggiungibile: non si può affermare che manchi.
        return False
    return code == 0 and bool(output.strip())


async def prepare(
    repo_url: str,
    base_branch: str,
    work_branch: str,
    log,
    *,
    allow_base_branch: bool = False,
) -> WorkspaceInfo:
    """Porta la working copy sul branch di lavoro e calcola i file modificati."""
    base_branch = base_branch or "main"
    root = workspace_root()
    (root / "repos").mkdir(parents=True, exist_ok=True)
    store_dir().mkdir(parents=True, exist_ok=True)
    reports_dir().mkdir(parents=True, exist_ok=True)

    dest = root / "repos" / repo_dir_name(repo_url)

    async def git(*args: str, timeout: int = 120, check: bool = True) -> tuple[int, str]:
        return await run_command(
            ["git", *args], cwd=str(dest), timeout=timeout, log=log, check=check
        )

    if not (dest / ".git").exists():
        await log.add(f"Clono il repository in {dest.name}")
        # Clone parziale invece di shallow: è altrettanto rapido ma conserva la
        # storia necessaria al diff base...HEAD, che è il segnale con cui si
        # distinguono i test nuovi da quelli preesistenti.
        await run_command(
            ["git", "clone", "--filter=blob:none", repo_url, str(dest)],
            cwd=str(root),
            timeout=settings.clone_timeout_seconds,
            log=log,
        )
    else:
        await git("remote", "set-url", "origin", repo_url, check=False)

    await log.add("Aggiorno i riferimenti remoti")
    await git("fetch", "origin", "--prune", timeout=settings.clone_timeout_seconds)

    branch = work_branch or base_branch
    fallback_reason = ""
    code, _ = await git("rev-parse", "--verify", f"origin/{branch}", check=False)
    if code != 0:
        if not allow_base_branch:
            raise CommandError(
                f"Il branch di lavoro «{branch}» non esiste su origin: "
                "l'agente cloud non ha ancora pubblicato il codice."
            )
        await log.add(
            f"Il branch di lavoro «{branch}» non esiste su origin: l'agente cloud non ha "
            f"ancora pubblicato il codice. Eseguo la sola suite di regressione su {base_branch}"
        )
        branch = base_branch
        fallback_reason = "work_branch_missing"

    await log.add(f"Passo al branch {branch}")
    await git("checkout", "-f", "-B", branch, f"origin/{branch}")
    await git("reset", "--hard", f"origin/{branch}")
    # Rimuove gli artefatti del run precedente ma PRESERVA node_modules: è la
    # cache che tiene il secondo run sotto il minuto.
    await git("clean", "-fdx", "-e", "node_modules", "-e", ".turbo", check=False)

    _, sha = await git("rev-parse", "HEAD", check=False)
    commit_sha = sha.strip().splitlines()[0][:40] if sha.strip() else ""

    changed: set[str] = set()
    if branch != base_branch:
        # Tre punti: il diff parte dal merge-base, così i file toccati da altri sul
        # branch base nel frattempo non risultano introdotti da questo Work Order.
        code, diff = await git(
            "diff", "--name-only", f"origin/{base_branch}...HEAD", check=False
        )
        if code == 0:
            changed = {line.strip() for line in diff.splitlines() if line.strip()}
            await log.add(f"File modificati rispetto a {base_branch}: {len(changed)}")

    return WorkspaceInfo(
        path=str(dest),
        branch=branch,
        base_branch=base_branch,
        commit_sha=commit_sha,
        changed_files=changed,
        fallback_reason=fallback_reason,
    )
