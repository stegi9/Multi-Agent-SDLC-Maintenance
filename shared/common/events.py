"""Schemi Pydantic degli eventi scambiati su Kafka tra i microservizi."""
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TicketSource(str, Enum):
    JIRA = "jira"
    GITHUB = "github"
    BUGZILLA = "bugzilla"
    CVE = "cve"
    INTERNAL = "internal"  # creato nativamente in piattaforma


class TicketType(str, Enum):
    BUG = "bug"
    CVE = "cve"
    CHANGE_REQUEST = "change_request"


class ImpactDecision(str, Enum):
    """Policy di approvazione basata sulla percentuale di impatto."""

    AUTO_APPROVED = "auto_approved"        # impatto < 5%
    NEEDS_AUTHORIZATION = "needs_authorization"  # impatto 5-30%
    BLOCKED = "blocked"                    # impatto > 30%, rinviato a major release


class BaseEvent(BaseModel):
    """Campi comuni a tutti gli eventi della piattaforma."""

    event_id: str
    timestamp: datetime = Field(default_factory=_now)
    produced_by: str  # nome del microservizio emittente


class TicketRawEvent(BaseEvent):
    """Pubblicato su `tickets.raw` dall'Ingestion & Sync Service."""

    ticket_id: str
    external_id: str | None = None
    source: TicketSource
    type: TicketType
    title: str
    description: str = ""
    severity: str | None = None


class TicketPrioritizedEvent(BaseEvent):
    """Pubblicato su `tickets.prioritized` dall'Agente Valutatore."""

    ticket_id: str
    priority: int  # 1 = massima
    duplicates: list[str] = []        # ticket assorbiti per deduplicazione
    resolves_also: list[str] = []     # ticket chiusi a cascata
    rationale: str = ""


class ImpactAnalyzedEvent(BaseEvent):
    """Pubblicato su `impact.analyzed` dall'Agente di Planning."""

    ticket_id: str
    impact_percentage: float
    decision: ImpactDecision
    affected_files: list[str] = []
    risk_notes: str = ""


class WorkOrderEvent(BaseEvent):
    """Pubblicato su `workorders.generated` dal WO Generator."""

    wo_id: str
    ticket_id: str
    instructions: str
    affected_files: list[str] = []
    context: dict[str, Any] = {}


class CodeGeneratedEvent(BaseEvent):
    """Pubblicato su `code.generated` dall'Agente Sviluppatore.

    `changes` è il JSON con l'elenco delle modifiche architetturali e di
    codice, da sottoporre alla validazione Human-in-the-Loop.
    """

    wo_id: str
    ticket_id: str
    changes: list[dict[str, Any]] = []
    reasoning_log: str = ""


class DocumentEvent(BaseEvent):
    """Eventi documentali (`documents.uploaded`, `documents.validated`, `docs.generated`)."""

    document_id: str
    title: str
    target_rag: str = "main"  # main | future
    content_ref: str = ""     # riferimento allo storage del contenuto
    related_wo_id: str | None = None


class TestsExecutedEvent(BaseEvent):
    """Pubblicato su `tests.executed` dall'Agente Tester.

    Riporta l'esito della suite eseguita sul branch di lavoro del Work Order,
    distinguendo i test introdotti dal Work Order dalla suite di regressione:
    è l'evidenza che lo Scrum Master usa prima del gate di validazione.
    """

    wo_id: str
    run_id: str
    ticket_id: str = ""
    status: str            # passed | failed | no_tests | error
    branch: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    new_total: int = 0
    new_failed: int = 0
    regression_total: int = 0
    regression_failed: int = 0
    duration_ms: int = 0
    failed_names: list[str] = []   # primi 10 test falliti, per la notifica


class GitMergedEvent(BaseEvent):
    """Pubblicato su `git.merged` quando una Pull Request (codice o docs) è fusa."""

    wo_id: str
    kind: str          # "code" | "docs"
    pr_url: str
    branch: str = ""


class GitMergeFailedEvent(BaseEvent):
    """Pubblicato su `git.merge_failed` quando il merge non è possibile.

    `reason` distingue il tipo di blocco così lo Scrum Master può intervenire:
    conflitti da risolvere, CI rossa o errore dell'operazione Git.
    """

    wo_id: str
    kind: str          # "code" | "docs"
    reason: str        # "conflict" | "ci_failed" | "error"
    detail: str = ""
    pr_url: str = ""
