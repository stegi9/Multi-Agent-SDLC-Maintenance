"""Esecuzione di un Work Order da parte dell'Agente Sviluppatore.

Flusso:
1. interroga la topologia del codice (Agente Mappatore);
2. individua i file esatti su cui intervenire;
3. legge SOLO quei file (ottimizzazione dei costi: niente repository intero);
4. produce il JSON con l'elenco delle modifiche e il log di reasoning;
5. pubblica code.generated e attende la validazione dello Scrum Master.
"""
import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.kafka.producer import publish
from app.services import dependency_index
from shared.common import llm, ranking
from shared.common.auth import internal_headers

logger = logging.getLogger(__name__)

# Indice delle dipendenze del repository bersaglio, condiviso fra le esecuzioni:
# la costruzione richiede una passata sui sorgenti, la cache si invalida da sola
# quando cambia un manifesto.
_dependency_index = dependency_index.DependencyIndex(settings.target_repo_path)


class FileSelection(BaseModel):
    """Schema atteso dalla selezione dei file (guardrail sull'output LLM)."""

    files: list[str] = []
    reasoning: str = ""


class ChangeItem(BaseModel):
    file: str
    action: str = Field(default="modify", pattern="^(modify|create|delete)$")
    # Distingue i file di PRODUZIONE dai file di TEST. Serve all'Agente Tester per
    # etichettare come "nuovi" i test nati da questo Work Order anche quando il diff
    # con il branch base non è più disponibile (WO già fuso).
    kind: str = Field(default="source", pattern="^(source|test)$")
    description: str = ""
    code_sketch: str = ""


class ChangesResult(BaseModel):
    """Schema atteso dal JSON delle modifiche (guardrail sull'output LLM).

    ANTI-ALLUCINAZIONE: `changes` NON è più obbligatorio. Se il contesto è
    insufficiente l'agente può astenersi (`needs_more_context=true`) invece di
    inventare modifiche: forzare almeno una modifica induce confabulazione."""

    summary: str = Field(min_length=1)
    changes: list[ChangeItem] = []
    # Cosa i test devono dimostrare, in italiano: viaggia nell'handoff verso
    # l'agente cloud, che è chi scrive materialmente il codice dei test.
    test_strategy: str = ""
    needs_more_context: bool = False
    missing: str = ""  # cosa manca / quali file servirebbero, se astenuto


class GroundingVerdict(BaseModel):
    """Esito del verificatore LLM (LLM-as-judge) sulle modifiche generate."""

    grounded: bool = True
    unsupported_claims: list[str] = []
    confidence: float = 0.0


SELECT_FILES_PROMPT = """Sei l'Agente Sviluppatore di una piattaforma SDLC.
Ricevi un Work Order e l'elenco dei file del repository (topologia del codice).
Scegli i file da leggere per realizzare l'intervento (massimo 4, solo tra quelli elencati).
Rispondi SOLO con JSON: {"files": ["path1", ...], "reasoning": "perché questi file, in italiano"}"""

CHANGES_PROMPT = """Sei l'Agente Sviluppatore di una piattaforma SDLC aziendale.
Ricevi un Work Order e il contenuto dei file rilevanti. Produci il JSON delle modifiche
architetturali e di codice necessarie a risolvere il ticket. Per ogni modifica indica:
- file: percorso del file (esistente o da creare)
- action: "modify" | "create" | "delete"
- description: cosa cambiare e perché, in italiano, preciso e attuabile
- code_sketch: bozza CONCISA del codice (solo le righe chiave, max ~15 righe)

REGOLE ANTI-ALLUCINAZIONE (IMPORTANTI):
- Basati SOLO sul contenuto dei file forniti. NON inventare funzioni, classi, API o file inesistenti.
- Per action "modify"/"delete" indica SOLO file di cui hai ricevuto il contenuto.
- Se il contesto è insufficiente per intervenire con certezza, NON inventare: rispondi con
  "needs_more_context": true e in "missing" indica quali file/informazioni ti servono.

Sii sintetico: al massimo 6 modifiche. Rispondi SOLO con JSON:
{"summary": "sintesi breve in italiano", "changes": [{"file": "...", "action": "...", "description": "...", "code_sketch": "..."}], "needs_more_context": false, "missing": ""}"""

# Variante del prompt che chiede anche la pianificazione dei test (feature flag
# `test_planning`). È una costante separata, e non un'aggiunta in coda a
# CHANGES_PROMPT, di proposito: con il flag disattivato il prompt inviato al
# modello resta identico byte per byte a quello base.
CHANGES_PROMPT_WITH_TESTS = """Sei l'Agente Sviluppatore di una piattaforma SDLC aziendale.
Ricevi un Work Order e il contenuto dei file rilevanti. Produci il JSON delle modifiche
architetturali e di codice necessarie a risolvere il ticket. Per ogni modifica indica:
- file: percorso del file (esistente o da creare)
- action: "modify" | "create" | "delete"
- kind: "source" per il codice di produzione, "test" per i file di test
- description: cosa cambiare e perché, in italiano, preciso e attuabile
- code_sketch: bozza CONCISA del codice (solo le righe chiave, max ~15 righe)

REGOLE ANTI-ALLUCINAZIONE (IMPORTANTI):
- Basati SOLO sul contenuto dei file forniti. NON inventare funzioni, classi, API o file inesistenti.
- Per action "modify"/"delete" indica SOLO file di cui hai ricevuto il contenuto.
- Se il contesto è insufficiente per intervenire con certezza, NON inventare: rispondi con
  "needs_more_context": true e in "missing" indica quali file/informazioni ti servono.

REGOLE SUI TEST (OBBLIGATORIE):
- Ogni intervento sul codice di produzione deve essere accompagnato da ALMENO UN file di
  test che ne dimostri la correttezza. Marca quei file con "kind": "test".
- In "test_strategy" spiega in italiano COSA i test devono dimostrare (caso nominale,
  caso di errore, regressione da evitare), non come sono scritti.
- Convenzione di test del repository: {test_convention_hint}
- Non proporre test su funzioni o API che non compaiono nei file forniti.

Sii sintetico: al massimo 6 modifiche. Rispondi SOLO con JSON:
{"summary": "sintesi breve in italiano", "test_strategy": "cosa i test devono dimostrare", "changes": [{"file": "...", "action": "...", "kind": "source|test", "description": "...", "code_sketch": "..."}], "needs_more_context": false, "missing": ""}"""


def _changes_prompt() -> str:
    """Prompt delle modifiche, con o senza la pianificazione dei test.

    Il flag `test_planning` segue la convenzione degli interruttori già presenti
    (semantic_search, graph_context, cve_reachability): disattivandolo si torna
    esattamente al prompt base.
    """
    if not settings.test_planning:
        return CHANGES_PROMPT
    return CHANGES_PROMPT_WITH_TESTS.replace(
        "{test_convention_hint}", settings.test_convention_hint
    )


VERIFY_PROMPT = """Sei un revisore critico dell'Agente Sviluppatore di una piattaforma SDLC.
Ricevi: il Work Order, il CONTENUTO dei file forniti all'agente e le MODIFICHE che ha proposto.
Verifica se ogni modifica è SUPPORTATA dal contenuto dei file forniti: segnala i riferimenti a
funzioni, classi, file o API che NON compaiono nei file forniti (possibili allucinazioni).
Non giudicare lo stile: solo il fondamento fattuale. Rispondi SOLO con JSON:
{"grounded": true/false, "unsupported_claims": ["..."], "confidence": 0.0-1.0}"""


class _ReasoningLog:
    """Log testuale del ragionamento, mostrato nel terminale virtuale dello Sviluppatore.

    Ogni riga viene persistita subito su `work_orders.reasoning_log`, così il
    terminale (che fa polling di GET /executions/{wo_id}/log) è davvero LIVE
    anche durante le fasi lunghe (chiamate al modello)."""

    def __init__(self, wo_id: str | None = None) -> None:
        self.wo_id = wo_id
        self.lines: list[str] = []

    async def add(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.lines.append(f"[{stamp}] {message}")
        logger.info("[reasoning] %s", message)
        await self._flush()

    async def _flush(self) -> None:
        if not self.wo_id:
            return
        try:
            async with SessionLocal() as session:
                await session.execute(
                    text("UPDATE work_orders SET reasoning_log = :lg WHERE id = :id"),
                    {"lg": self.text(), "id": self.wo_id},
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - il log non deve bloccare l'esecuzione
            logger.warning("Impossibile persistere il reasoning log del WO %s", self.wo_id)

    def text(self) -> str:
        return "\n".join(self.lines)


async def _set_status(wo_id: str, status: str, *, changes: dict | None = None, log: str | None = None) -> None:
    sets = ["status = :st"]
    params: dict = {"st": status, "id": wo_id}
    if changes is not None:
        sets.append("changes = :ch")
        params["ch"] = json.dumps(changes, ensure_ascii=False)
    if log is not None:
        sets.append("reasoning_log = :lg")
        params["lg"] = log
    async with SessionLocal() as session:
        await session.execute(text(f"UPDATE work_orders SET {', '.join(sets)} WHERE id = :id"), params)
        await session.commit()


async def _load_workorder(wo_id: str) -> dict | None:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                "SELECT id, ticket_id, status, payload, repo_url, base_branch, work_branch "
                "FROM work_orders WHERE id = :id"
            ),
                {"id": wo_id},
            )
        ).mappings().first()
    if row is None:
        return None
    wo = dict(row)
    if isinstance(wo["payload"], str):
        wo["payload"] = json.loads(wo["payload"])
    return wo


def _wo_description(payload: dict) -> tuple[str, str]:
    """Descrizione combinata dei ticket del Work Order (supporta più ticket)."""
    tickets = payload.get("tickets")
    if not tickets and payload.get("ticket"):  # compatibilità WO a ticket singolo
        tickets = [payload["ticket"]]
    tickets = tickets or []
    titles = ", ".join(t.get("title", "") for t in tickets) or "(senza ticket)"
    blocks = []
    for i, t in enumerate(tickets, 1):
        blocks.append(
            f"TICKET {i} ({t.get('type', '?')}, severity {t.get('severity', '?')}): {t.get('title', '')}\n"
            f"DESCRIZIONE: {t.get('description', '')}"
        )
    instructions = payload.get("instructions", "")
    rationale = payload.get("rationale", "")
    desc = "\n\n".join(blocks)
    if rationale:
        desc += f"\n\nRAGGRUPPAMENTO: {rationale}"
    desc += f"\nISTRUZIONI DELLO SCRUM MASTER: {instructions or '-'}"
    return titles, desc


async def _dispatch_to_cloud(wo: dict, changes: dict, reasoning: _ReasoningLog) -> str:
    """Invia il JSON completo di handoff agli agenti cloud esterni per la scrittura del codice.

    Se è configurato un webhook (cloud_agent_url) il JSON viene
    inviato davvero; altrimenti resta come artefatto di handoff pronto in piattaforma.

    Il payload include il **repository e il branch di destinazione** (`repository`):
    l'agente esterno deve creare `work_branch` da `base_branch` e pusharci il codice.
    """
    payload = wo["payload"]
    test_plan = changes.get("test_plan") or {}
    handoff = {
        "wo_id": wo["id"],
        "tickets": payload.get("tickets", []),
        "instructions": payload.get("instructions", ""),
        "rationale": payload.get("rationale", ""),
        "code_changes": changes,
        # L'agente cloud scrive materialmente il codice: senza questa sezione non
        # saprebbe che deve produrre anche i test, e l'Agente Tester non avrebbe
        # nulla di nuovo da eseguire.
        "test_requirements": {
            "strategy": test_plan.get("strategy", ""),
            "files": test_plan.get("files", []),
            "convention_hint": test_plan.get("convention_hint", settings.test_convention_hint),
            "mandatory": settings.test_planning,
        },
        "repository": {
            "repo_url": wo.get("repo_url"),
            "base_branch": wo.get("base_branch") or "main",
            "work_branch": wo.get("work_branch") or f"wo/{wo['id'][:8]}",
        },
        "target": "external cloud coding agents",
    }
    if not settings.cloud_agent_url:
        await reasoning.add(
            "JSON di handoff pronto in piattaforma (ticket + modifiche + bozze di codice). "
            "Nessun agente cloud configurato (cloud_agent_url vuoto): invio non eseguito."
        )
        return "ready_for_dispatch"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(settings.cloud_agent_url, json=handoff)
            response.raise_for_status()
        await reasoning.add(f"JSON inviato agli agenti cloud esterni ({settings.cloud_agent_url}): HTTP {response.status_code}")
        return "dispatched"
    except httpx.HTTPError as exc:
        await reasoning.add(f"Invio agli agenti cloud fallito ({exc}): il JSON resta come artefatto di handoff")
        return "dispatch_failed"


async def _get_topology() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.topology_base_url}/topology", headers=internal_headers())
            if response.status_code == 200:
                return response.json()
    except httpx.HTTPError:
        pass
    return None


async def _get_graph() -> dict | None:
    """Grafo delle dipendenze (dal Mappatore): serve a leggere anche le dipendenze
    dei file da modificare, così l'agente scrive codice conoscendo il vicinato."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.topology_base_url}/graph", headers=internal_headers())
            if response.status_code == 200:
                return response.json()
    except httpx.HTTPError:
        pass
    return None


async def _semantic_candidates(query: str, limit: int = 30) -> dict[str, float]:
    """Punteggi di similarità semantica per i file, dall'indice del Mappatore.

    Ritorna `{path: score}` (coseno, 0..1). In caso di indebolimento del servizio
    ritorna vuoto: il ranking ricade sul solo segnale lessicale + grafo."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{settings.topology_base_url}/topology/search",
                params={"q": query[:2000], "limit": limit},
                headers=internal_headers(),
            )
            if response.status_code == 200:
                return {r["path"]: r["score"] for r in response.json().get("results", [])}
    except httpx.HTTPError:
        pass
    return {}


def _dependencies_of(selected: list[str], graph: dict | None, limit: int = 4) -> list[str]:
    """File da cui dipendono quelli selezionati (`depends_on`): contesto tecnico
    che l'agente deve leggere per non rompere le interfacce usate."""
    if not graph:
        return []
    fmap = graph.get("files", {})
    deps: set[str] = set()
    for f in selected:
        deps.update(fmap.get(f, {}).get("depends_on", []))
    deps.difference_update(selected)
    # priorità alle dipendenze più connesse (più centrali nel grafo)
    ranked = sorted(deps, key=lambda p: -fmap.get(p, {}).get("degree", 0))
    return ranked[:limit]


async def _read_files(paths: list[str], reasoning: _ReasoningLog) -> dict[str, str]:
    """Accesso esclusivo ai soli file individuati (max 8 KB ciascuno)."""
    root = Path(settings.target_repo_path)
    contents: dict[str, str] = {}
    for rel in paths:
        candidate = (root / rel).resolve()
        if not str(candidate).startswith(str(root.resolve())):
            await reasoning.add(f"SCARTATO {rel}: percorso fuori dal repository")
            continue
        if not candidate.is_file():
            await reasoning.add(f"File {rel} non presente nel clone locale: verrà trattato come nuovo")
            continue
        try:
            text_content = candidate.read_text(encoding="utf-8", errors="ignore")[:5000]
            contents[rel] = text_content
            await reasoning.add(f"Letto {rel} ({len(text_content)} caratteri)")
        except OSError as exc:
            await reasoning.add(f"Impossibile leggere {rel}: {exc}")
    return contents


# La logica di ordinamento e di canonizzazione del ticket vive in
# `shared/common/ranking.py`: la usano sia l'Agente Sviluppatore sia l'Agente di
# Planning, così i due agenti vedono la stessa lista di candidati ordinata con
# lo stesso criterio. Qui si ri-espongono i nomi storici, per non spargere il
# cambiamento nel resto del modulo e nei test.
_STOPWORDS = ranking.STOPWORDS
_SYNONYMS = ranking.SYNONYMS
_FILLER = ranking.FILLER
_SUFFIXES = ranking.SUFFIXES
_ACTIONS = ranking.ACTIONS
_stem = ranking.stem
_ticket_keywords = ranking.ticket_keywords
_relevance = ranking.lexical_relevance


def normalize_ticket(text: str, *, stem: bool = True) -> str:
    """Query canonica del ticket. Si veda `shared.common.ranking.normalize_ticket`."""
    return ranking.normalize_ticket(text, stem_words=stem)



def _verify_grounding(changes: list[dict], read_files: set[str], known_files: set[str]) -> dict:
    """Verifica DETERMINISTICA (senza LLM) dell'ancoraggio delle modifiche.

    Non può eliminare tutte le allucinazioni semantiche, ma cattura quelle di
    *riferimento* con certezza: una modifica non è ancorata se tocca un file che
    non esiste nella topologia, o se modifica/elimina un file di cui l'agente non
    ha mai letto il contenuto (quindi ne "immagina" la struttura)."""
    warnings: list[str] = []
    for ch in changes:
        f = ch.get("file", "")
        action = ch.get("action", "modify")
        if action == "create":
            continue  # un file nuovo per definizione non è ancora nella topologia
        if f not in known_files:
            warnings.append(f"{f}: {action} di un file non presente nella topologia del repository")
        elif f not in read_files:
            warnings.append(f"{f}: {action} senza averne letto il contenuto (struttura non verificata)")
    return {"ok": not warnings, "warnings": warnings}


async def _verify_changes_llm(wo_description: str, files_block: str, changes: dict) -> GroundingVerdict | None:
    """Verificatore LLM (LLM-as-judge): un secondo passaggio del modello valuta
    se le modifiche sono supportate dai file forniti. Ritorna None se disattivato
    o se il verificatore stesso fallisce (non deve bloccare il flusso)."""
    if not settings.verifier_enabled:
        return None
    changes_json = json.dumps(changes.get("changes", []), ensure_ascii=False)[:4000]
    try:
        return await llm.chat_structured(
            VERIFY_PROMPT,
            f"WORK ORDER:\n{wo_description}\n\nFILE FORNITI:\n{files_block[:8000]}\n\n"
            f"MODIFICHE PROPOSTE:\n{changes_json}",
            schema=GroundingVerdict,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )
    except llm.LLMError:
        return None


@dataclass
class ContextSelection:
    """Esito della fase di selezione del contesto (passi 1-2 dell'agente)."""

    selected: list[str]
    more_candidates: list[str]
    known: set[str]
    graph: dict | None
    reasoning: str = ""
    hallucinated: list[str] = field(default_factory=list)
    # Quale percorso ha prodotto la selezione: "hybrid" (ranking + LLM) oppure
    # "cve_reachability" (analisi deterministica delle dipendenze).
    path_used: str = "hybrid"
    # Solo per il percorso CVE: la dipendenza vulnerabile e' raggiungibile dal
    # codice applicativo? None quando il percorso non e' stato imboccato.
    reachable: bool | None = None


async def _cve_selection(
    wo_description: str,
    degree: dict[str, int],
    known: set[str],
    graph: dict | None,
    reasoning: _ReasoningLog,
) -> ContextSelection | None:
    """Selezione deterministica per i ticket di vulnerabilità.

    Ritorna None quando il ticket non è una segnalazione di vulnerabilità o non
    cita alcun pacchetto dichiarato nel repository: in quel caso il chiamante
    prosegue con il ranking ibrido ordinario.

    Rispetto al percorso ordinario qui non c'è alcuna invocazione del modello,
    quindi la selezione è a varianza nulla e perfettamente invariante alla
    riformulazione del ticket.
    """
    try:
        finding = await asyncio.to_thread(
            dependency_index.analyse_ticket, wo_description, _dependency_index, degree
        )
    except OSError as exc:  # il clone locale potrebbe non essere montato
        await reasoning.add(f"Analisi delle dipendenze non disponibile ({exc}): uso il ranking ibrido")
        return None
    if finding is None:
        return None

    # I file dell'indice sono percorsi reali su disco, ma vengono comunque
    # confrontati con la topologia: se il Mappatore non li conosce non hanno
    # senso a valle (il grafo e la lettura si appoggiano alla topologia).
    proposed = finding.files
    selected = [f for f in proposed if f in known]
    if not selected:
        await reasoning.add(
            f"Percorso CVE: pacchetto '{finding.package}' riconosciuto ma i file risolti non "
            f"compaiono nella topologia: torno al ranking ibrido"
        )
        return None

    await reasoning.add(f"Percorso dedicato ai CVE (analisi di raggiungibilità): {finding.explain()}")
    await reasoning.add(f"File da modificare: {selected}")
    # Riserva per l'eventuale astensione: gli altri punti di chiamata noti.
    more = [f for f in finding.call_sites if f in known and f not in selected][:4]
    return ContextSelection(
        selected=selected,
        more_candidates=more,
        known=known,
        graph=graph,
        reasoning=finding.explain(),
        hallucinated=[],
        path_used="cve_reachability",
        reachable=finding.reachable,
    )


async def select_context(wo_description: str, reasoning: _ReasoningLog) -> ContextSelection:
    """Seleziona il sottoinsieme di file su cui l'agente lavorera'.

    Estratta da `execute_workorder` perche' e' il punto di misura della qualita'
    del contesto: l'endpoint `/select-context` invoca esattamente questo codice,
    non una sua replica.
    """
    topology = await _get_topology()
    graph = await _get_graph() if settings.graph_context else None
    degree = {p: i.get("degree", 0) for p, i in (graph or {}).get("files", {}).items()}
    if not topology:
        await reasoning.add("Topologia non disponibile: procedo senza contesto del repository")
        return ContextSelection([], [], set(), graph)

    await reasoning.add(
        f"Topologia: {topology['file_count']} file · grafo delle dipendenze: "
        f"{'disponibile' if graph else 'assente'}"
    )
    known = {f["path"] for f in topology["files"]}

    # PERCORSO DEDICATO AI CVE (analisi di raggiungibilità, Sez. 3.6.2).
    # Va tentato PRIMA del ranking ibrido perché su questa classe di ticket il
    # ranking è strutturalmente inadatto: la descrizione della vulnerabilità non
    # somiglia al manifesto che la risolve. Se il percorso riconosce il ticket,
    # la selezione è deterministica e non richiede il modello.
    if settings.cve_reachability:
        cve = await _cve_selection(wo_description, degree, known, graph, reasoning)
        if cve is not None:
            return cve
    # RANKING IBRIDO: fondo tre segnali per ordinare i candidati
    #  1. semantico (embedding del ticket vs indice del codice) → cattura
    #     i sinonimi e il ponte italiano→inglese senza dizionari a mano;
    #  2. lessicale (keyword sul path) → premia i match esatti su
    #     nomi/percorsi che l'embedding a volte sottopesa;
    #  3. centralità nel grafo → a parità, i file più connessi.
    # Lista compatta all'LLM: i modelli piccoli degenerano con liste lunghe.
    # Le query che alimentano il ranking sono la forma canonica del ticket, non
    # il testo grezzo: è ciò che rende l'ordinamento stabile fra riformulazioni.
    # I due canali usano varianti diverse (radici per il lessicale, parole intere
    # per il semantico). Al modello, più avanti, va comunque il testo integrale.
    if settings.ticket_normalization:
        lexical_query = normalize_ticket(wo_description, stem=True)
        semantic_query = normalize_ticket(wo_description, stem=False)
        await reasoning.add(f"Query canonica del ticket: {semantic_query[:300]}")
    else:
        lexical_query = semantic_query = wo_description
    keywords = _ticket_keywords(lexical_query)
    files = topology["files"]
    semantic = await _semantic_candidates(semantic_query) if settings.semantic_search else {}
    if semantic:
        await reasoning.add(f"Ricerca semantica: {len(semantic)} file candidati dall'indice del codice")
    else:
        await reasoning.add("Indice semantico non disponibile: uso il solo ranking lessicale")

    def _hybrid_score(path: str) -> float:
        # relevance lessicale: 0,1,2,... → normalizzo grossolanamente a ~[0,1]
        lex = min(_relevance(path, keywords), 4) / 4.0
        sem = semantic.get(path, 0.0)
        deg = degree.get(path, 0)
        deg_norm = min(deg, 20) / 20.0
        return 0.6 * sem + 0.3 * lex + 0.1 * deg_norm

    ranked = sorted(files, key=lambda f: -_hybrid_score(f["path"]))
    if keywords:
        await reasoning.add(f"Parole chiave del ticket: {', '.join(sorted(keywords))}")
    file_list = "\n".join(
        f["path"] + (f" ({degree[f['path']]} dipendenze)" if degree.get(f["path"]) else "")
        for f in ranked[:80]
    )
    selection = await llm.chat_structured(
        SELECT_FILES_PROMPT,
        f"WORK ORDER:\n{wo_description}\n\nFILE DEL REPOSITORY (i più pertinenti in cima):\n{file_list}",
        schema=FileSelection,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    llm_sel = [f for f in selection.files if f in known]
    # Path proposti dal modello che non esistono nel repository: e' la misura
    # diretta e oggettiva del tasso di allucinazione.
    hallucinated = [f for f in selection.files if f not in known]
    # Rete di sicurezza: i file in cima al ranking ibrido vengono letti
    # comunque, anche se il modello li ignora (come accadeva col bottone login).
    top_hits = [f["path"] for f in ranked if _hybrid_score(f["path"]) > 0][:2]
    selected = list(dict.fromkeys(top_hits + llm_sel))[:4]
    # candidati di riserva (per l'eventuale astensione: "servono più file")
    more_candidates = [f["path"] for f in ranked if f["path"] not in selected][:4]
    await reasoning.add(f"File da modificare: {selected or 'nessuno'}")
    if hallucinated:
        await reasoning.add(f"⚠ Path inesistenti proposti dal modello (scartati): {hallucinated}")
    if selection.reasoning:
        await reasoning.add(f"Motivazione: {selection.reasoning}")
    return ContextSelection(
        selected=selected,
        more_candidates=more_candidates,
        known=known,
        graph=graph,
        reasoning=selection.reasoning or "",
        hallucinated=hallucinated,
    )


async def execute_workorder(wo_id: str) -> dict:
    """Esegue il Work Order end-to-end. Ritorna un riepilogo per l'API."""
    wo = await _load_workorder(wo_id)
    if wo is None:
        return {"status": "not_found", "wo_id": wo_id}
    if wo["status"] in ("running", "code_ready", "validated", "merged"):
        return {"status": "skipped", "detail": f"Work Order già in stato {wo['status']}", "wo_id": wo_id}

    reasoning = _ReasoningLog(wo_id)
    titles, wo_description = _wo_description(wo["payload"])
    await reasoning.add(f"Avvio esecuzione Work Order {wo_id} — ticket: {titles}")
    await _set_status(wo_id, "running", log=reasoning.text())

    try:
        # 1-2. topologia + grafo + selezione dei file (ordinati per centralità nel grafo)
        ctx = await select_context(wo_description, reasoning)
        selected, more_candidates, known, graph = (
            ctx.selected, ctx.more_candidates, ctx.known, ctx.graph,
        )

        # 3. lettura dei file da modificare + delle loro DIPENDENZE (contesto dal grafo)
        contents = await _read_files(selected, reasoning) if selected else {}
        dep_files = _dependencies_of(selected, graph)
        if dep_files:
            await reasoning.add(f"Dipendenze lette come contesto (dal grafo): {dep_files}")
        dep_contents = await _read_files(dep_files, reasoning) if dep_files else {}

        # 4. generazione del JSON delle modifiche
        seed_block = "\n\n".join(f"=== {p} ===\n{c}" for p, c in contents.items()) or "(nessun file letto)"
        ctx_block = "\n\n".join(
            f"=== {p} (DIPENDENZA: contesto, non modificare senza necessità) ===\n{c}"
            for p, c in dep_contents.items()
        )
        files_block = seed_block + (f"\n\nCONTESTO DALLE DIPENDENZE (dal grafo):\n{ctx_block}" if ctx_block else "")
        await reasoning.add(f"Invoco il modello {settings.llm_model} per generare il JSON delle modifiche")
        result = await llm.chat_structured(
            _changes_prompt(),
            f"WORK ORDER:\n{wo_description}\n\nCONTENUTO DEI FILE RILEVANTI:\n{files_block}",
            schema=ChangesResult,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            retries=2,  # i modelli piccoli spesso sbagliano formato al primo colpo
        )

        # 4b. ASTENSIONE: se l'agente dichiara contesto insufficiente e non ha
        # prodotto modifiche, invece di forzare (=> allucinazioni) leggo altri
        # file candidati e rigenero UNA volta. Se persiste, lo dico allo Scrum Master.
        if (result.needs_more_context or not result.changes) and more_candidates:
            await reasoning.add(
                f"Contesto insufficiente ({result.missing or 'non specificato'}): "
                f"leggo altri file e riprovo → {more_candidates}"
            )
            extra = await _read_files(more_candidates, reasoning)
            contents.update(extra)
            seed_block = "\n\n".join(f"=== {p} ===\n{c}" for p, c in contents.items()) or "(nessun file letto)"
            files_block = seed_block + (f"\n\nCONTESTO DALLE DIPENDENZE (dal grafo):\n{ctx_block}" if ctx_block else "")
            result = await llm.chat_structured(
                _changes_prompt(),
                f"WORK ORDER:\n{wo_description}\n\nCONTENUTO DEI FILE RILEVANTI:\n{files_block}",
                schema=ChangesResult,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                retries=2,
            )

        abstained = result.needs_more_context and not result.changes
        changes = {
            "summary": result.summary[:2000],
            "changes": [c.model_dump() for c in result.changes[:20]],
            "generated_by": settings.service_name,
            "model": settings.llm_model,
            "files_consulted": list(contents.keys()),
            "context_files": list(dep_contents.keys()),  # dipendenze lette dal grafo
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        if abstained:
            # niente modifiche inventate: la palla passa allo Scrum Master (HITL)
            changes["needs_more_context"] = True
            changes["missing"] = result.missing[:1000]
            changes["summary"] = f"[CONTESTO INSUFFICIENTE] {changes['summary']}"[:2000]
            await reasoning.add(
                f"L'agente si è ASTENUTO dal proporre modifiche non fondate. "
                f"Manca: {result.missing or 'non specificato'}"
            )
        await reasoning.add(f"Generate {len(changes['changes'])} modifiche. Sintesi: {changes['summary'][:200]}")

        # 4c. VERIFICHE ANTI-ALLUCINAZIONE (solo se ci sono modifiche da verificare)
        if changes["changes"]:
            grounding = _verify_grounding(changes["changes"], set(contents.keys()), known)
            changes["grounding"] = grounding
            if grounding["warnings"]:
                await reasoning.add("⚠ Grounding: " + "; ".join(grounding["warnings"]))
            else:
                await reasoning.add("Grounding deterministico: tutte le modifiche riferiscono file letti/noti")
            verdict = await _verify_changes_llm(wo_description, files_block, changes)
            if verdict is not None:
                changes["verifier"] = verdict.model_dump()
                if verdict.grounded:
                    await reasoning.add(f"Verificatore LLM: modifiche fondate (confidenza {verdict.confidence:.2f})")
                else:
                    await reasoning.add(
                        "⚠ Verificatore LLM: possibili affermazioni NON supportate → "
                        + "; ".join(verdict.unsupported_claims[:5])
                    )

        # 4d. PIANO DEI TEST: quali file di test l'intervento deve produrre.
        # È un segnale allo Scrum Master (Human-in-the-Loop), NON un blocco: se il
        # modello non ha previsto test lo si vede subito nel terminale e nel pannello
        # di revisione, e l'Agente Tester eseguirà comunque la suite di regressione.
        if settings.test_planning:
            test_files = [c["file"] for c in changes["changes"] if c.get("kind") == "test"]
            changes["test_plan"] = {
                "strategy": (result.test_strategy or "")[:2000],
                "files": test_files,
                "convention_hint": settings.test_convention_hint,
            }
            if changes["changes"] and not test_files:
                await reasoning.add(
                    "⚠ Il piano non prevede file di test: la verifica automatica sarà "
                    "limitata alla sola suite di regressione"
                )
            elif test_files:
                await reasoning.add(f"Test previsti dal piano: {', '.join(test_files)}")

        # 5. handoff: invio del JSON agli agenti cloud esterni.
        # Se l'agente si è astenuto non c'è codice da inviare: si va direttamente
        # alla revisione umana, che deciderà come sbloccare il contesto.
        if not abstained:
            dispatch_status = await _dispatch_to_cloud(wo, changes, reasoning)
            changes["dispatch_status"] = dispatch_status
        await reasoning.add("In attesa di validazione dello Scrum Master (Human-in-the-Loop)")
        await _set_status(wo_id, "code_ready", changes=changes, log=reasoning.text())
    except llm.LLMError as exc:
        await reasoning.add(f"ERRORE: l'agente LLM non ha prodotto un risultato valido — {exc}")
        await _set_status(wo_id, "failed", log=reasoning.text())
        return {"status": "failed", "wo_id": wo_id, "detail": str(exc)}

    try:
        await publish(
            "code.generated",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "wo_id": wo_id,
                "ticket_id": wo["ticket_id"],
                "summary": changes["summary"],
                "changes_count": len(changes["changes"]),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento code.generated non pubblicato")

    return {"status": "code_ready", "wo_id": wo_id, "changes_count": len(changes["changes"]), "summary": changes["summary"]}