"""Telemetria delle invocazioni LLM.

Ogni chiamata al modello, da qualunque agente, viene registrata con il suo
costo computazionale (token), la sua latenza e l'esito, in un file JSONL
su volume condiviso.

Il contesto di run (esperimento, ticket, fase, configurazione) viaggia in
ContextVar, così gli agenti non devono propagarlo esplicitamente lungo la
catena di chiamate.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# File JSONL su volume condiviso: una riga per invocazione LLM.
TELEMETRY_PATH = os.getenv("TELEMETRY_PATH", "/telemetry/llm_calls.jsonl")
TELEMETRY_ENABLED = os.getenv("TELEMETRY_ENABLED", "true").lower() != "false"

_write_lock = threading.Lock()


@dataclass
class RunContext:
    """Identifica l'esperimento in corso per attribuire ogni chiamata LLM."""

    run_id: str = ""
    experiment: str = ""
    ticket_id: str = ""
    phase: str = ""
    config_id: str = ""
    repeat: int = 0
    extra: dict = field(default_factory=dict)


_ctx: ContextVar[RunContext] = ContextVar("llm_run_context", default=RunContext())


def current_context() -> RunContext:
    return _ctx.get()


def set_context(ctx: RunContext) -> None:
    _ctx.set(ctx)


@contextmanager
def run_context(**kwargs):
    """Imposta il contesto di run per il blocco corrente (e i suoi await)."""
    base = asdict(current_context())
    base.update({k: v for k, v in kwargs.items() if v is not None})
    token = _ctx.set(RunContext(**base))
    try:
        yield
    finally:
        _ctx.reset(token)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def record_llm_call(
    *,
    provider: str,
    model: str,
    service: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_s: float = 0.0,
    attempt: int = 1,
    structured: bool = False,
    ok: bool = True,
    error: str = "",
    prompt_chars: int = 0,
    response_chars: int = 0,
) -> None:
    """Registra una singola invocazione del modello su JSONL.

    Non deve mai far fallire l'agente: ogni errore di scrittura viene
    degradato a warning.
    """
    if not TELEMETRY_ENABLED:
        return
    ctx = current_context()
    row = {
        "ts": time.time(),
        "run_id": ctx.run_id,
        "experiment": ctx.experiment,
        "ticket_id": ctx.ticket_id,
        "phase": ctx.phase or service,
        "config_id": ctx.config_id,
        "repeat": ctx.repeat,
        "service": service,
        "provider": provider,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "duration_s": round(duration_s, 4),
        "attempt": attempt,
        "structured": structured,
        "ok": ok,
        "error": error[:300],
        "prompt_chars": prompt_chars,
        "response_chars": response_chars,
    }
    if ctx.extra:
        row["extra"] = ctx.extra
    try:
        os.makedirs(os.path.dirname(TELEMETRY_PATH), exist_ok=True)
        with _write_lock:
            with open(TELEMETRY_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - dipende dal filesystem
        logger.warning("Telemetria non scrivibile su %s: %s", TELEMETRY_PATH, exc)
