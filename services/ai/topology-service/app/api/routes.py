"""Endpoint dell'Agente Mappatore (topologia del codice).

Clona/aggiorna il repository configurato nella pagina Impostazioni
(repo.git_url) e genera la topologia: l'indice strutturale che gli altri
agenti interrogano per individuare i file su cui intervenire senza
passare l'intero repository all'LLM.
"""
import asyncio
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from git import Repo
from git.exc import GitError

from app.core.config import settings
from app.services import code_index
from shared.common import llm
from shared.common.auth import internal_headers

logger = logging.getLogger(__name__)
router = APIRouter()

SETTINGS_URL = "http://ingestion-sync-service:8000/api/v1/settings"

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".angular", ".idea", ".vscode", "graphify-out"}

LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript-react", ".js": "javascript",
    ".jsx": "javascript-react", ".mjs": "javascript", ".cjs": "javascript", ".vue": "vue",
    ".html": "html", ".css": "css",
    ".scss": "scss", ".java": "java", ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "c++",
    ".cs": "c#", ".rb": "ruby", ".php": "php", ".sql": "sql", ".sh": "shell", ".ps1": "powershell",
    ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".md": "markdown", ".txt": "testo",
    ".dockerfile": "docker", ".toml": "toml", ".ini": "ini", ".xml": "xml", ".kt": "kotlin",
}

QUERY_PROMPT = """Sei l'Agente Mappatore di una piattaforma SDLC.
Ricevi la descrizione di una modifica e l'elenco dei file del repository.
Individua i file su cui intervenire per realizzare la modifica.
Rispondi SOLO con JSON: {"files": ["path1", "path2", ...], "rationale": "breve motivazione in italiano"}
Indica al massimo 10 file, in ordine di rilevanza, scegliendo solo tra quelli elencati."""


def _topology_path() -> Path:
    return Path(settings.target_repo_path).parent / "topology.json"


def _code_graph_path() -> Path:
    return Path(settings.target_repo_path).parent / "code_graph.json"


def _build_code_graph() -> dict | None:
    """Genera il grafo di conoscenza del codice con graphify (estrazione AST) e lo
    riduce a una mappa di dipendenze file->file. Se graphify non è disponibile o
    fallisce, ritorna None senza bloccare la scansione della topologia."""
    root = Path(settings.target_repo_path)
    try:
        # 'update' = estrazione AST solo-codice, nessun LLM/chiave richiesti
        proc = subprocess.run(
            ["graphify", "update", str(root)],
            cwd=str(root.parent),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            logger.warning("graphify uscito con codice %s: %s", proc.returncode, proc.stderr[:300])
            return None
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("graphify non eseguibile: %s", exc)
        return None

    gpath = root / "graphify-out" / "graph.json"
    if not gpath.exists():
        logger.warning("graphify non ha prodotto graph.json")
        return None

    raw = json.loads(gpath.read_text(encoding="utf-8"))
    id_file = {n["id"]: n.get("source_file") for n in raw.get("nodes", []) if n.get("source_file")}
    # nomi dei simboli (funzioni/classi) definiti in ciascun file: arricchiscono
    # l'indice semantico del codice così l'embedding sa "cosa" contiene un file.
    symbols: dict[str, list[str]] = {}
    for n in raw.get("nodes", []):
        sf = n.get("source_file")
        name = n.get("name") or n.get("label")
        if sf and name:
            bucket = symbols.setdefault(sf, [])
            if name not in bucket and len(bucket) < 30:
                bucket.append(name)
    deps: dict[str, set] = {}
    rdeps: dict[str, set] = {}
    edge_count = 0
    for e in raw.get("links", []):
        sf = id_file.get(e.get("source"))
        tf = id_file.get(e.get("target"))
        if not sf or not tf or sf == tf:
            continue  # tengo solo le relazioni TRA file diversi (dipendenze reali)
        deps.setdefault(sf, set()).add(tf)
        rdeps.setdefault(tf, set()).add(sf)
        edge_count += 1

    all_files = set(deps) | set(rdeps) | set(id_file.values())
    file_map = {
        f: {
            "depends_on": sorted(deps.get(f, set())),
            "depended_by": sorted(rdeps.get(f, set())),
            "degree": len(deps.get(f, set())) + len(rdeps.get(f, set())),
            "symbols": symbols.get(f, []),
        }
        for f in sorted(x for x in all_files if x)
    }
    god_nodes = sorted(
        ({"file": f, "degree": m["degree"]} for f, m in file_map.items() if m["degree"] > 0),
        key=lambda x: -x["degree"],
    )[:15]
    graph = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "graphify",
        "node_count": len(raw.get("nodes", [])),
        "file_count": len(file_map),
        "edge_count": edge_count,
        "god_nodes": god_nodes,
        "files": file_map,
    }
    _code_graph_path().write_text(json.dumps(graph), encoding="utf-8")
    logger.info("Grafo del codice generato: %d file, %d dipendenze", len(file_map), edge_count)
    return graph


async def _load_repo_settings() -> tuple[str, str]:
    """Legge repo.git_url e repo.branch dalle impostazioni di piattaforma."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(SETTINGS_URL, headers=internal_headers())
            response.raise_for_status()
            items = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Impossibile leggere le impostazioni di piattaforma: {exc}",
        )
    values = {i["key"]: i["value"] for i in items}
    return values.get("repo.git_url", ""), values.get("repo.branch", "")


def _parse_repo_urls(raw: str) -> list[str]:
    """Un progetto può stare su più repository: accetta più URL separati da
    a-capo, virgola o spazio."""
    return [p for p in re.split(r"[\s,]+", (raw or "").strip()) if p]


def _repo_name(url: str) -> str:
    """Nome di cartella stabile per un URL repo (owner_repo, sanitizzato)."""
    u = url.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    segs = [s for s in re.split(r"[/:]+", u) if s]
    name = "_".join(segs[-2:]) if len(segs) >= 2 else (segs[-1] if segs else "repo")
    return re.sub(r"[^A-Za-z0-9._-]", "-", name) or "repo"


def _sync_repos(urls: list[str], branch: str) -> str:
    """Clona/aggiorna uno o più repository in sottocartelle di /workspace/repo.
    Rimuove le sottocartelle non più richieste. Multi-repo nativo."""
    base = Path(settings.target_repo_path)
    base.mkdir(parents=True, exist_ok=True)

    # migrazione dal vecchio layout (repo singolo clonato nella root di base)
    if (base / ".git").exists():
        for child in list(base.iterdir()):
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)

    # nomi cartella univoci per ogni URL
    wanted: dict[str, str] = {}
    for url in urls:
        name = _repo_name(url)
        unique, i = name, 1
        while unique in wanted:
            i += 1
            unique = f"{name}-{i}"
        wanted[unique] = url

    notes: list[str] = []
    # rimuovi sottocartelle di repo non più in elenco (preserva graphify-out)
    for child in list(base.iterdir()):
        if child.is_dir() and child.name not in wanted and child.name != "graphify-out":
            shutil.rmtree(child, ignore_errors=True)
            notes.append(f"{child.name}: rimosso")

    for name, url in wanted.items():
        dest = base / name
        try:
            if (dest / ".git").exists():
                repo = Repo(dest)
                repo.remotes.origin.set_url(url)
                repo.remotes.origin.pull()
                notes.append(f"{name}: aggiornato")
            else:
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                kwargs = {"depth": 1}
                if branch:
                    kwargs["branch"] = branch
                Repo.clone_from(url, dest, **kwargs)
                notes.append(f"{name}: clonato")
        except GitError as exc:
            notes.append(f"{name}: ERRORE git ({str(exc)[:120]})")

    return f"{len(wanted)} repository → " + "; ".join(notes)


def _count_loc(path: Path) -> int | None:
    try:
        if path.stat().st_size > 512 * 1024:
            return None
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _build_topology() -> dict:
    root = Path(settings.target_repo_path)
    if not root.exists() or not any(root.iterdir()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nessun repository da scansionare: configurare repo.git_url nella pagina Impostazioni",
        )
    files = []
    languages: dict[str, int] = {}
    total_loc = 0
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower() if path.suffix else path.name.lower()
        language = LANGUAGES.get(suffix)
        if language is None and path.name.lower() == "dockerfile":
            language = "docker"
        if language is None:
            continue  # file binari o non rilevanti per la topologia
        loc = _count_loc(path)
        rel = path.relative_to(root).as_posix()
        files.append({"path": rel, "language": language, "loc": loc})
        languages[language] = languages.get(language, 0) + 1
        total_loc += loc or 0
        if len(files) >= 2000:
            break
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "file_count": len(files),
        "total_loc": total_loc,
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "files": files,
    }


# stato della scansione (non bloccante): la ricostruzione del grafo può richiedere minuti
_scan_state: dict = {"state": "idle", "detail": "", "result": None, "started_at": None, "finished_at": None}


def _do_scan_blocking(urls: list[str], branch: str) -> dict:
    note = _sync_repos(urls, branch) if urls else "Scansione della directory locale"
    topology = _build_topology()
    _topology_path().write_text(json.dumps(topology), encoding="utf-8")
    logger.info("Topologia rigenerata: %d file", topology["file_count"])
    graph = _build_code_graph()
    return {
        "note": note,
        "file_count": topology["file_count"],
        "total_loc": topology["total_loc"],
        "languages": topology["languages"],
        "generated_at": topology["generated_at"],
        "code_graph": (
            {"file_count": graph["file_count"], "edge_count": graph["edge_count"], "god_nodes": graph["god_nodes"][:5]}
            if graph else "non disponibile (graphify non eseguito)"
        ),
    }


async def _run_scan(urls: list[str], branch: str) -> None:
    global _scan_state
    _scan_state = {"state": "running", "detail": f"{len(urls)} repository in elaborazione",
                   "result": None, "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
    try:
        # il lavoro è bloccante (git + graphify): lo eseguo in un thread
        result = await asyncio.to_thread(_do_scan_blocking, urls, branch)
        # indice semantico del codice (async, richiede embedding via Ollama):
        # rileggo topologia e grafo appena scritti su disco. Fallisce in modo
        # silenzioso (la selezione dei file ricade sul ranking lessicale).
        try:
            topology = json.loads(_topology_path().read_text(encoding="utf-8"))
            graph = (
                json.loads(_code_graph_path().read_text(encoding="utf-8"))
                if _code_graph_path().exists() else None
            )
            index_info = await code_index.build_index(topology, graph)
            if index_info:
                result["semantic_index"] = index_info
        except Exception:  # noqa: BLE001 - l'indice non deve far fallire la scansione
            logger.warning("Costruzione dell'indice semantico saltata", exc_info=True)
        _scan_state = {"state": "done", "detail": result["note"], "result": result,
                       "started_at": _scan_state["started_at"], "finished_at": datetime.now(timezone.utc).isoformat()}
    except HTTPException as exc:
        _scan_state = {"state": "error", "detail": str(exc.detail), "result": None,
                       "started_at": _scan_state["started_at"], "finished_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Scansione fallita")
        _scan_state = {"state": "error", "detail": str(exc), "result": None,
                       "started_at": _scan_state["started_at"], "finished_at": datetime.now(timezone.utc).isoformat()}


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED,
             summary="Avvia (in background) clone, topologia e ricostruzione del grafo")
async def scan_repository() -> dict:
    if _scan_state.get("state") == "running":
        return {"status": "already_running", **_scan_state}
    git_url, branch = await _load_repo_settings()
    urls = _parse_repo_urls(git_url)
    asyncio.create_task(_run_scan(urls, branch))
    return {"status": "started", "repos": len(urls),
            "detail": "Ricostruzione di topologia e grafo avviata: segui GET /scan/status"}


@router.get("/scan/status", summary="Stato dell'ultima scansione/ricostruzione del grafo")
async def scan_status() -> dict:
    return _scan_state


@router.get("/topology", summary="Restituisce la topologia del codice corrente")
async def get_topology() -> dict:
    path = _topology_path()
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Topologia non ancora generata: eseguire prima POST /scan",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/graph", summary="Grafo di conoscenza del codice (dipendenze tra file)")
async def get_code_graph() -> dict:
    path = _code_graph_path()
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Grafo del codice non ancora generato: eseguire prima POST /scan",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/graph/impact", summary="File a valle impattati dalla modifica di un file (dipendenti)")
async def graph_impact(file: str = Query(min_length=1)) -> dict:
    graph = await get_code_graph()
    info = graph["files"].get(file)
    if info is None:
        return {"file": file, "found": False, "depends_on": [], "depended_by": [], "impact_set": [file]}
    impact_set = sorted({file, *info["depended_by"]})
    return {
        "file": file,
        "found": True,
        "depends_on": info["depends_on"],
        "depended_by": info["depended_by"],
        "impact_set": impact_set,
    }


@router.get("/topology/search", summary="Ricerca semantica dei file più pertinenti a una descrizione")
async def topology_search(
    q: str = Query(min_length=3, max_length=2000),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Retrieval denso (embedding) dei file più vicini alla descrizione `q`.

    A differenza di /topology/query non usa l'LLM: restituisce i candidati per
    similarità semantica, che l'Agente Sviluppatore fonde con il segnale
    lessicale e la centralità nel grafo (ranking ibrido)."""
    results = await code_index.search(q, limit=limit)
    return {"query": q, "results": results, "model": settings.embedding_model}


@router.get("/topology/query", summary="Individua i file impattati da una modifica descritta")
async def query_topology(q: str = Query(min_length=5, max_length=1000)) -> dict:
    topology = await get_topology()
    file_list = "\n".join(f["path"] for f in topology["files"][:400])
    try:
        result = await llm.chat_json(
            QUERY_PROMPT,
            f"MODIFICA RICHIESTA:\n{q}\n\nFILE DEL REPOSITORY:\n{file_list}",
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agente LLM non disponibile: {exc}")
    known = {f["path"] for f in topology["files"]}
    files = [f for f in (result.get("files") or []) if f in known][:10]
    return {"query": q, "files": files, "rationale": result.get("rationale", ""), "model": settings.llm_model}