"""Endpoint dell'Agente di Planning (analisi di impatto e What-If).

Per ogni ticket simula l'intervento col modello LLM locale, stima la
percentuale di codice impattato e applica le policy aziendali:
  - impatto < 5%  -> approvazione rapida (pubblica workorders.approved)
  - 5% .. 30%     -> richiede autorizzazione esplicita dello Scrum Master
  - impatto > 30% -> bloccato: posticipato a major release + Report degli Scarti
"""
import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import settings
from shared.common.auth import internal_headers, require_roles
from app.core.db import SessionLocal
from app.kafka.producer import publish
from shared.common import llm, ranking

logger = logging.getLogger(__name__)
router = APIRouter()

TOPOLOGY_URL = f"{settings.topology_base_url}/topology"
GRAPH_URL = f"{settings.topology_base_url}/graph"
SEARCH_URL = f"{settings.topology_base_url}/topology/search"

# Manifesti delle dipendenze: vanno sempre offerti al modello, perche' sono il
# punto in cui si risolve l'aggiornamento di una libreria.
MANIFEST_NAMES = ("package.json", "requirements.txt", "pyproject.toml", "pom.xml", "go.mod")

SYSTEM_PROMPT = """Sei l'Agente di Planning di una piattaforma SDLC aziendale.
Ricevi un ticket (bug o change request) e, se disponibile, la topologia del codice
(indice strutturale del repository). Devi SIMULARE l'intervento e stimare:
1. impact_percentage: percentuale di codice impattato (0-100, numero realistico e prudente)
2. affected_files: i file della topologia che andrebbero modificati (se la topologia manca, lista vuota)
3. risk_notes: rischi tecnici e funzionali dell'intervento, in italiano, 2-3 frasi

Linee guida: una correzione localizzata vale 1-4%; una modifica che tocca più moduli 5-30%;
una ristrutturazione architetturale supera il 30%.
Rispondi SOLO con JSON: {"impact_percentage": <numero>, "affected_files": ["..."], "risk_notes": "..."}"""


class AnalyzeRequest(BaseModel):
    ticket_id: str


class WhatIfRequest(BaseModel):
    description: str = Field(min_length=5, max_length=2000)
    use_graph: bool = True  # usa il grafo del codice per espandere l'impatto a valle


class RejectRequest(BaseModel):
    ticket_id: str
    note: str = ""


BAND_TO_DECISION = {"low": "auto_approved", "medium": "needs_authorization", "high": "blocked"}


def _decision(impact: float) -> str:
    """Decisione dalla percentuale d'impatto (policy storica sulla stima del modello).

    Resta come ripiego quando il grafo non e' disponibile, ma non e' piu' la via
    principale: le soglie 5%/30% sono tarate su una distribuzione teorica che il
    modello non produce, e tendono a far degenerare la governance in un gate
    unico (quasi tutte le analisi nella fascia intermedia).
    """
    if impact < 5:
        return "auto_approved"
    if impact <= 30:
        return "needs_authorization"
    return "blocked"


def _downstream_closure(seeds: list[str], fmap: dict, hops: int) -> set[str]:
    """Chiusura a valle sul grafo: chi verrebbe raggiunto dalla modifica entro `hops` salti."""
    seen = {s for s in seeds if s in fmap}
    frontier = set(seen)
    for _ in range(hops):
        nxt: set[str] = set()
        for f in frontier:
            nxt.update(fmap.get(f, {}).get("depended_by", []))
        nxt -= seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return seen


def _structural_band(affected: list[str], graph: dict | None) -> dict | None:
    """Fascia decisionale calcolata sul GRAFO, senza il modello linguistico.

    Perche' non si usa piu' la percentuale generata dal modello: non e' uno
    stimatore affidabile. Con il grafo produce valori sistematicamente gonfiati,
    senza grafo degenera in pochi valori ricorrenti. In entrambi i casi la fascia
    risultante e' quasi sempre la stessa.

    I due segnali strutturali usati qui sono invece osservabili e deterministici:

      pct2     quota di codebase raggiunta a valle entro DUE salti. La chiusura a
               due salti e' quella che separa nettamente gli interventi
               architetturali (>= 9%) da quelli ordinari (<= 6%); a un solo salto
               le due popolazioni si sovrappongono.
      ndirect  numero di file toccati direttamente.

    Le soglie sono configurabili (vedi `app.core.config`) e vanno calibrate sulla
    distribuzione empirica del repository, non su una scala teorica.
    """
    if not graph or not affected:
        return None
    fmap = graph.get("files", {})
    total = graph.get("file_count") or len(fmap)
    if not total:
        return None

    impact_set = _downstream_closure(affected, fmap, hops=2)
    pct2 = round(100 * len(impact_set) / total, 3)
    ndirect = len(affected)

    if pct2 >= settings.band_high_pct:
        band = "high"
    elif pct2 < settings.band_low_pct and ndirect <= settings.band_low_max_files:
        band = "low"
    else:
        band = "medium"
    return {
        "band": band,
        "structural_impact_pct": pct2,
        "structural_impact_set_size": len(impact_set),
        "direct_files": ndirect,
    }


# Linguaggi considerati "codice" ai fini della selezione (esclude doc/config/rumore).
CODE_LANGUAGES = {
    "python", "typescript", "javascript", "java", "go", "rust", "c", "c++", "c#",
    "ruby", "php", "html", "css", "scss", "kotlin", "sql", "shell", "powershell",
}
# File più centrali (ordinati per grado dal grafo). Contesto compatto = più affidabile
# sui modelli locali; un 7B regge liste più lunghe e sceglie con più precisione.
MAX_PROMPT_FILES = 80


async def _semantic_candidates(query: str, limit: int = 40) -> dict[str, float]:
    """Punteggi di similarità semantica dall'indice del codice del Mappatore.

    In caso di indisponibilità ritorna vuoto: il ranking ricade sul solo segnale
    lessicale più la centralità, come faceva prima.
    """
    if not query:
        return {}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                SEARCH_URL,
                params={"q": query[:2000], "limit": limit},
                headers=internal_headers(),
            )
            if response.status_code == 200:
                return {r["path"]: r["score"] for r in response.json().get("results", [])}
    except httpx.HTTPError:
        pass
    return {}


async def _load_topology_summary(graph: dict | None = None, ticket_text: str = "") -> str:
    """Riassunto compatto della topologia per il prompt.

    I candidati sono ordinati per PERTINENZA AL TICKET, con lo stesso ranking
    ibrido dell'Agente Sviluppatore (`shared.common.ranking`).

    Ordinando per centralità nel grafo il modello vedrebbe quasi solo gli hub del
    repository e finirebbe per sceglierli, gonfiando con la loro chiusura a valle
    la stima d'impatto. La centralità resta quindi come
    contributo minore del punteggio, e viene comunque riportata accanto al file
    perché è informazione utile al giudizio.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(TOPOLOGY_URL, headers=internal_headers())
            if response.status_code != 200:
                return ""
            topo = response.json()
    except httpx.HTTPError:
        return ""

    files = topo.get("files", [])
    # Ai file di codice si aggiungono sempre i manifesti: sono il punto in cui si
    # risolve un aggiornamento di dipendenza, e la sola estensione .json li
    # avrebbe esclusi dal prompt.
    pool = [
        f for f in files
        if f.get("language") in CODE_LANGUAGES or f["path"].endswith(MANIFEST_NAMES)
    ] or files

    degree = {}
    if graph:
        degree = {p: info.get("degree", 0) for p, info in graph.get("files", {}).items()}

    if ticket_text:
        query = ranking.normalize_ticket(ticket_text, stem_words=False)
        semantic = await _semantic_candidates(query)
        scored = ranking.rank_paths(
            [f["path"] for f in pool], ticket_text, semantic, degree
        )
        ordine = {p: i for i, (p, _s) in enumerate(scored)}
        pool = sorted(pool, key=lambda f: ordine.get(f["path"], len(ordine)))[:MAX_PROMPT_FILES]
        intestazione = f"i {min(len(pool), MAX_PROMPT_FILES)} più pertinenti al ticket"
    else:
        pool = sorted(pool, key=lambda f: (-degree.get(f["path"], 0), -(f.get("loc") or 0)))[:MAX_PROMPT_FILES]
        intestazione = f"i {len(pool)} più connessi"

    lines = []
    for f in pool:
        deg = degree.get(f["path"])
        conn = f", {deg} dipendenze" if deg else ""
        lines.append(f"{f['path']} ({f.get('language', '?')}, {f.get('loc', '?')} loc{conn})")
    return (
        f"File del repository ({topo.get('file_count', len(files))} totali, "
        f"{intestazione}):\n" + "\n".join(lines)
    )


async def _require_topology(graph: dict | None = None, ticket_text: str = "") -> str:
    """L'analisi di impatto è una stima sul codice reale: senza topologia si rifiuta."""
    topology = await _load_topology_summary(graph, ticket_text)
    if not topology:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Impossibile stimare l'impatto: nessun codice caricato su cui valutare la modifica. "
                "Configura l'URL del repository nella pagina Impostazioni (sezione 'Repository del "
                "software') e lancia la scansione dell'Agente Mappatore (POST /scan del "
                "topology-service), poi riprova."
            ),
        )
    return topology


async def _load_code_graph() -> dict | None:
    """Carica il grafo delle dipendenze dal Mappatore (graphify), se disponibile."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GRAPH_URL, headers=internal_headers())
            if response.status_code == 200:
                return response.json()
    except httpx.HTTPError:
        pass
    return None


async def _publish_event(topic: str, payload: dict) -> None:
    try:
        await publish(topic, payload)
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento %s non pubblicato", topic)


RAG_FUTURE_INDEX_URL = f"{settings.rag_base_url}/collections/future/documents"


async def _report_to_future_rag(ticket_id: str, ticket_title: str, reason: str, details: str) -> None:
    """Genera il Report degli Scarti come documento e lo indicizza nel RAG Futuro
    (documento vettoriale in Qdrant), oltre al record in `discard_reports`."""
    document_id = str(uuid.uuid4())
    title = f"Report Scarti — {ticket_title} ({reason})"[:500]
    content = (
        f"# {title}\n\n"
        f"**Ticket:** {ticket_title}\n\n"
        f"**Esito:** {reason}\n\n"
        f"**Motivazione / dettagli:**\n{details}\n"
    )
    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO documents (id, title, file_name, content, target_rag, status) "
                "VALUES (:id, :t, :f, :c, 'future', 'accepted')"
            ),
            {"id": document_id, "t": title, "f": f"discard-{ticket_id[:8]}.md", "c": content},
        )
        await session.commit()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(RAG_FUTURE_INDEX_URL, json={"document_id": document_id}, headers=internal_headers())
        logger.info("Report scarti %s indicizzato nel RAG Futuro", document_id)
    except httpx.HTTPError as exc:
        logger.warning("Report scarti %s creato ma non indicizzato (%s)", document_id, exc)


def _expand_impact(affected: list[str], graph: dict | None) -> dict:
    """Espande i file modificati usando il grafo del codice, in ENTRAMBE le direzioni:
      - a valle (`depended_by`): chi verrebbe impattato dalla modifica → definisce l'impact_set;
      - a monte (`depends_on`): da cosa dipendono i file toccati → contesto tecnico.
    Calcola anche la quota strutturale di codebase toccata (impact_set / file totali)."""
    if not graph:
        return {"graph_used": False, "downstream_files": [], "upstream_files": [],
                "impact_set": affected, "graph_impact_pct": None}
    fmap = graph.get("files", {})
    total = graph.get("file_count") or len(fmap) or 0
    downstream: set[str] = set()
    upstream: set[str] = set()
    for f in affected:
        info = fmap.get(f, {})
        downstream.update(info.get("depended_by", []))
        upstream.update(info.get("depends_on", []))
    downstream.difference_update(affected)
    upstream.difference_update(affected)
    impact_set = set(affected) | downstream
    graph_impact_pct = round(100 * len(impact_set) / total, 2) if total else None
    return {
        "graph_used": True,
        "downstream_files": sorted(downstream),
        "upstream_files": sorted(upstream),
        "impact_set": sorted(impact_set),
        "graph_impact_pct": graph_impact_pct,
    }


async def _run_llm_analysis(subject: dict, topology: str, graph: dict | None = None) -> dict:
    user_prompt = json.dumps({"ticket": subject}, ensure_ascii=False)
    user_prompt += f"\n\nTOPOLOGIA DEL CODICE:\n{topology}"
    if graph and graph.get("god_nodes"):
        hot = ", ".join(f"{g['file']} (grado {g['degree']})" for g in graph["god_nodes"][:8])
        user_prompt += (
            "\n\nAREE AD ALTA CONNESSIONE nel grafo delle dipendenze "
            "(modificarle propaga impatto su molti file):\n" + hot
        )
    result = await llm.chat_json(
        SYSTEM_PROMPT, user_prompt, base_url=settings.llm_base_url, model=settings.llm_model
    )
    if not isinstance(result, dict):
        raise llm.LLMError("Risposta del modello non valida")
    llm_impact = max(0.0, min(100.0, float(result.get("impact_percentage") or 0)))
    affected = [str(f) for f in (result.get("affected_files") or [])][:50]
    notes = str(result.get("risk_notes") or "")[:2000]

    # Scarta i file "inventati" che non esistono nel grafo (se il grafo c'è).
    if graph:
        known = set(graph.get("files", {}))
        filtered = [f for f in affected if f in known]
        affected = filtered or affected

    # Guardia sul non-risultato: risposta degenerata (0% + nessun file + nessun rischio)
    # = il modello ha fallito. Meglio un errore chiaro che un falso "impatto 0 / auto-approvato".
    if not affected and not notes and llm_impact == 0:
        raise llm.LLMError(
            "Il modello non ha prodotto una stima valida (nessun file impattato, nessun rischio, 0%). "
            "Riprova o configura un modello più capace (es. qwen2.5:7b)."
        )

    expansion = _expand_impact(affected, graph)
    gpct = expansion.get("graph_impact_pct")
    # Stima ancorata al grafo: media fra il giudizio del modello e la quota
    # strutturale raggiunta dall'impact_set. Resta esposta per continuità e
    # confronto, ma NON governa più la decisione quando la fascia strutturale
    # è disponibile.
    impact = round((llm_impact + gpct) / 2, 2) if (gpct is not None and affected) else round(llm_impact, 2)

    structural = _structural_band(affected, graph) if settings.structural_impact else None
    return {
        "impact_percentage": impact,
        "llm_impact_percentage": round(llm_impact, 2),
        "affected_files": affected,
        "risk_notes": notes,
        # Fascia deterministica dal grafo: None se il grafo manca o se
        # l'interruttore è disattivato, e in quel caso si ricade sulle soglie.
        "structural": structural,
        **expansion,
    }


def _decide(analysis: dict) -> str:
    """Fascia decisionale definitiva: strutturale se disponibile, altrimenti a soglie.

    Concentrare qui la scelta evita che i tre punti in cui si decide (analisi su
    ticket, rilettura di un'analisi salvata, What-If) divergano fra loro.
    """
    structural = analysis.get("structural")
    if structural:
        return BAND_TO_DECISION[structural["band"]]
    return _decision(analysis["impact_percentage"])


def _policy_label(analysis: dict) -> str:
    """Etichetta della regola che ha deciso: rende tracciabile il criterio applicato."""
    structural = analysis.get("structural")
    if structural:
        return (
            f"policy:struttura<{settings.band_low_pct}% "
            f"({structural['structural_impact_pct']}%, {structural['direct_files']} file)"
        )
    return "policy:impact<5%"


def _motivation(analysis: dict) -> str:
    """Motivazione leggibile della decisione, per il Report degli Scarti."""
    structural = analysis.get("structural")
    if structural:
        return (
            f"Impatto strutturale {structural['structural_impact_pct']}% della codebase "
            f"({structural['structural_impact_set_size']} file raggiunti a valle, soglia "
            f"{settings.band_high_pct}%)"
        )
    return f"Impatto stimato {analysis['impact_percentage']}% (> 30%)"


@router.post("/analyze", summary="Esegue l'analisi di impatto su un ticket prioritizzato")
async def analyze_ticket(body: AnalyzeRequest) -> dict:
    async with SessionLocal() as session:
        ticket = (
            await session.execute(
                text("SELECT id, type, title, description, severity, status FROM tickets WHERE id = :id"),
                {"id": body.ticket_id},
            )
        ).mappings().first()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket non trovato")

    graph = await _load_code_graph()
    # Il testo del ticket serve a ordinare i candidati per pertinenza: senza di
    # esso la lista tornerebbe a essere quella dei file più connessi.
    testo_ticket = f"{ticket['title']}\n\n{ticket['description'] or ''}"
    topology = await _require_topology(graph, testo_ticket)
    try:
        analysis = await _run_llm_analysis(
            {
                "type": ticket["type"],
                "title": ticket["title"],
                "description": (ticket["description"] or "")[:1500],
                "severity": ticket["severity"],
            },
            topology,
            graph,
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agente LLM non disponibile: {exc}")

    decision = _decide(analysis)
    analysis_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO impact_analyses (id, ticket_id, impact_percentage, decision, affected_files, risk_notes) "
                "VALUES (:id, :tid, :impact, :decision, :files, :notes)"
            ),
            {
                "id": analysis_id,
                "tid": ticket["id"],
                "impact": analysis["impact_percentage"],
                "decision": decision,
                "files": json.dumps(analysis["affected_files"]),
                "notes": analysis["risk_notes"],
            },
        )
        if decision == "auto_approved":
            await session.execute(
                text("UPDATE tickets SET status = 'approved' WHERE id = :id"), {"id": ticket["id"]}
            )
        elif decision == "needs_authorization":
            await session.execute(
                text("UPDATE tickets SET status = 'analyzed' WHERE id = :id"), {"id": ticket["id"]}
            )
        else:  # blocked
            postpone_details = (
                f"{_motivation(analysis)}: posticipato a major release. {analysis['risk_notes']}"
            )
            await session.execute(
                text("UPDATE tickets SET status = 'postponed' WHERE id = :id"), {"id": ticket["id"]}
            )
            await session.execute(
                text(
                    "INSERT INTO discard_reports (id, ticket_id, reason, details) "
                    "VALUES (:id, :tid, 'postponed', :details)"
                ),
                {"id": str(uuid.uuid4()), "tid": ticket["id"], "details": postpone_details},
            )
        await session.commit()

    if decision == "blocked":
        # Report degli Scarti come documento indicizzato nel RAG Futuro
        await _report_to_future_rag(ticket["id"], ticket["title"], "postponed", postpone_details)

    base_event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "produced_by": settings.service_name,
        "ticket_id": ticket["id"],
        "impact_percentage": analysis["impact_percentage"],
        "decision": decision,
        "affected_files": analysis["affected_files"],
        "risk_notes": analysis["risk_notes"],
    }
    await _publish_event("impact.analyzed", base_event)
    if decision == "auto_approved":
        # procedura rapida: il WO Generator ascolta workorders.approved
        await _publish_event(
            "workorders.approved",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "ticket_id": ticket["id"],
                "approved_by": _policy_label(analysis),
                "instructions": f"Approvazione rapida. File attesi: {', '.join(analysis['affected_files'][:10])}",
            },
        )

    return {
        "analysis_id": analysis_id,
        "ticket_id": ticket["id"],
        "model": settings.llm_model,
        "topology_used": bool(topology),
        **analysis,
        "decision": decision,
    }


@router.post("/what-if", dependencies=[Depends(require_roles("product_manager", "scrum_master", "developer"))], summary="What-If Analysis su una bozza di modifica del Product Manager")
async def what_if(body: WhatIfRequest) -> dict:
    """Analisi di impatto immediata senza creare ticket (non inquina i sistemi di tracciamento)."""
    graph = await _load_code_graph() if body.use_graph else None
    topology = await _require_topology(graph, body.description)
    try:
        analysis = await _run_llm_analysis(
            {"type": "change_request", "title": "What-If Analysis", "description": body.description},
            topology,
            graph,
        )
    except llm.LLMError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agente LLM non disponibile: {exc}")
    return {
        "what_if": True,
        "model": settings.llm_model,
        "topology_used": bool(topology),
        **analysis,
        "decision": _decide(analysis),
    }


@router.post("/reject", dependencies=[Depends(require_roles("scrum_master"))], summary="Lo Scrum Master rifiuta un ticket: genera il Report degli Scarti")
async def reject_ticket(body: RejectRequest) -> dict:
    """Decisione opposta all'autorizzazione: il ticket viene scartato e si registra
    un Report degli Scarti che alimenta la memoria dell'Agente di Coerenza."""
    async with SessionLocal() as session:
        ticket = (
            await session.execute(
                text("SELECT id, title, status FROM tickets WHERE id = :id"),
                {"id": body.ticket_id},
            )
        ).mappings().first()
        if ticket is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket non trovato")
        if ticket["status"] in ("completed", "rejected"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Il ticket è già in stato '{ticket['status']}': non può essere rifiutato",
            )

        # recupera l'eventuale analisi di impatto per arricchire il report
        analysis = (
            await session.execute(
                text(
                    "SELECT impact_percentage, risk_notes FROM impact_analyses "
                    "WHERE ticket_id = :tid ORDER BY created_at DESC LIMIT 1"
                ),
                {"tid": body.ticket_id},
            )
        ).mappings().first()
        impact_part = (
            f"Impatto stimato {float(analysis['impact_percentage'])}%. {analysis['risk_notes']} "
            if analysis
            else ""
        )
        details = (
            f"Rifiutato dallo Scrum Master. {impact_part}"
            + (f"Motivazione: {body.note}" if body.note else "Nessuna motivazione fornita.")
        )

        ticket_title = ticket["title"]
        await session.execute(
            text("UPDATE tickets SET status = 'rejected' WHERE id = :id"), {"id": body.ticket_id}
        )
        await session.execute(
            text(
                "INSERT INTO discard_reports (id, ticket_id, reason, details) "
                "VALUES (:id, :tid, 'rejected', :details)"
            ),
            {"id": str(uuid.uuid4()), "tid": body.ticket_id, "details": details},
        )
        await session.commit()

    # Report degli Scarti come documento indicizzato nel RAG Futuro
    await _report_to_future_rag(body.ticket_id, ticket_title, "rejected", details)

    return {
        "ticket_id": body.ticket_id,
        "status": "rejected",
        "detail": "Ticket rifiutato: Report degli Scarti generato e indicizzato nel RAG Futuro",
    }


@router.get("/analyses/{ticket_id}", summary="Restituisce l'analisi di impatto di un ticket")
async def get_analysis(ticket_id: str) -> dict:
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, ticket_id, impact_percentage, decision, affected_files, risk_notes, created_at "
                    "FROM impact_analyses WHERE ticket_id = :tid ORDER BY created_at DESC LIMIT 1"
                ),
                {"tid": ticket_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nessuna analisi per questo ticket")
    data = dict(row)
    if isinstance(data.get("affected_files"), str):
        data["affected_files"] = json.loads(data["affected_files"])
    data["impact_percentage"] = float(data["impact_percentage"])
    return data