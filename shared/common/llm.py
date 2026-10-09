"""Client LLM condiviso per gli agenti AI.

Supporta due provider dietro la stessa interfaccia, così lo stesso agente può
essere eseguito con un modello locale on-premise o con un modello di frontiera
senza modifiche al codice chiamante:

- **ollama**   : modello locale on-premise (sovranita' del dato)
- **anthropic**: modello di frontiera via API

Il provider e' dedotto dal nome del modello (`claude-*` -> anthropic) oppure
imposto da LLM_PROVIDER. Ogni invocazione viene registrata dalla telemetria
condivisa (token, latenza, esito).
"""
import json
import logging
import os
import re
import time

import httpx

from . import telemetry

logger = logging.getLogger(__name__)

# I modelli piccoli in locale possono essere lenti: timeout generoso
LLM_TIMEOUT = 300.0
# mantiene il modello caricato tra una chiamata e l'altra (evita i reload a freddo)
LLM_KEEP_ALIVE = "30m"

# Tetto di output per il provider Anthropic (non-streaming: sotto il timeout HTTP)
ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "16000"))
# Profondita' di ragionamento: low | medium | high | xhigh | max
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium")

_anthropic_client = None


class LLMError(RuntimeError):
    """Il modello non è raggiungibile o ha risposto in modo inutilizzabile."""


def provider_for(model: str) -> str:
    """Deduce il provider dal nome del modello (override: LLM_PROVIDER)."""
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced:
        return forced
    return "anthropic" if model.lower().startswith("claude") else "ollama"


def _service_name() -> str:
    return os.getenv("SERVICE_NAME", os.getenv("HOSTNAME", "unknown"))


def extract_json(text: str) -> dict | list:
    """Estrae il primo blocco JSON valido da una risposta del modello."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # tenta con un blocco ```json ... ``` o con il primo {...} / [...]
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Risposta del modello non interpretabile come JSON: {text[:200]!r}")


# ---------------------------------------------------------------------------
# Provider: Ollama (locale)
# ---------------------------------------------------------------------------


async def _chat_ollama(
    system: str,
    user: str,
    *,
    base_url: str,
    model: str,
    json_mode: bool,
    fmt: dict | None,
    temperature: float,
    timeout: float | None,
    options: dict | None,
) -> tuple[str, int, int]:
    opts = {"temperature": temperature}
    if options:
        opts.update(options)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "keep_alive": LLM_KEEP_ALIVE,
        "options": opts,
    }
    if fmt is not None:
        payload["format"] = fmt
    elif json_mode:
        payload["format"] = "json"
    eff_timeout = timeout or LLM_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=eff_timeout) as client:
            response = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException as exc:
        raise LLMError(
            f"Timeout del modello {model} dopo {eff_timeout:.0f}s: la generazione è troppo lenta "
            f"(prompt grande o modello sotto carico). Riprova o riduci il contesto."
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM non raggiungibile su {base_url} (modello {model}): {exc}") from exc
    content = (data.get("message") or {}).get("content", "")
    if not content:
        raise LLMError("Il modello ha restituito una risposta vuota")
    return content, int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)


# ---------------------------------------------------------------------------
# Provider: Anthropic (Claude)
# ---------------------------------------------------------------------------


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "Provider anthropic richiesto ma il pacchetto 'anthropic' non è installato"
            ) from exc
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY non impostata: impossibile usare il provider anthropic")
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


def _anthropic_text(response) -> str:
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "".join(parts)


async def _chat_anthropic(
    system: str,
    user: str,
    *,
    model: str,
    json_mode: bool,
    timeout: float | None,
) -> tuple[str, int, int]:
    client = _get_anthropic()
    if timeout:
        client = client.with_options(timeout=timeout)
    instruction = user
    if json_mode:
        instruction += "\n\nRispondi ESCLUSIVAMENTE con JSON valido, senza testo intorno."
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=system,
            output_config={"effort": ANTHROPIC_EFFORT},
            messages=[{"role": "user", "content": instruction}],
        )
    except Exception as exc:  # noqa: BLE001 - l'SDK espone molte eccezioni tipate
        raise LLMError(f"Chiamata Anthropic fallita (modello {model}): {exc}") from exc
    if response.stop_reason == "refusal":
        raise LLMError(f"Richiesta rifiutata dai classificatori di sicurezza (modello {model})")
    text = _anthropic_text(response)
    if not text:
        raise LLMError("Il modello ha restituito una risposta vuota")
    return text, int(response.usage.input_tokens), int(response.usage.output_tokens)


async def _structured_anthropic(
    system: str,
    user: str,
    *,
    schema: type,
    model: str,
    timeout: float | None,
):
    """Structured outputs nativi: il decoding è vincolato allo schema Pydantic."""
    client = _get_anthropic()
    if timeout:
        client = client.with_options(timeout=timeout)
    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Chiamata Anthropic strutturata fallita (modello {model}): {exc}") from exc
    if response.stop_reason == "refusal":
        raise LLMError(f"Richiesta rifiutata dai classificatori di sicurezza (modello {model})")
    parsed = response.parsed_output
    tokens_in = int(response.usage.input_tokens)
    tokens_out = int(response.usage.output_tokens)
    if parsed is None:
        raise LLMError("Structured output non valido: parsed_output vuoto")
    return parsed, tokens_in, tokens_out


# ---------------------------------------------------------------------------
# Interfaccia pubblica
# ---------------------------------------------------------------------------


async def chat(
    system: str,
    user: str,
    *,
    base_url: str,
    model: str,
    json_mode: bool = False,
    fmt: dict | None = None,
    temperature: float = 0.2,
    timeout: float | None = None,
    options: dict | None = None,
    _attempt: int = 1,
) -> str:
    """Invoca il modello configurato e restituisce il testo della risposta.

    `fmt`: JSON Schema per i structured outputs di Ollama. Vincola il decoding
    a produrre JSON conforme -> elimina alla radice le allucinazioni *di formato*.
    Il provider Anthropic usa invece `chat_structured()` con schema Pydantic.
    """
    provider = provider_for(model)
    started = time.perf_counter()
    try:
        if provider == "anthropic":
            text, t_in, t_out = await _chat_anthropic(
                system, user, model=model, json_mode=json_mode or fmt is not None, timeout=timeout
            )
        else:
            text, t_in, t_out = await _chat_ollama(
                system,
                user,
                base_url=base_url,
                model=model,
                json_mode=json_mode,
                fmt=fmt,
                temperature=temperature,
                timeout=timeout,
                options=options,
            )
    except LLMError as exc:
        telemetry.record_llm_call(
            provider=provider,
            model=model,
            service=_service_name(),
            duration_s=time.perf_counter() - started,
            attempt=_attempt,
            structured=fmt is not None,
            ok=False,
            error=str(exc),
            prompt_chars=len(system) + len(user),
        )
        raise
    telemetry.record_llm_call(
        provider=provider,
        model=model,
        service=_service_name(),
        tokens_in=t_in,
        tokens_out=t_out,
        duration_s=time.perf_counter() - started,
        attempt=_attempt,
        structured=fmt is not None,
        ok=True,
        prompt_chars=len(system) + len(user),
        response_chars=len(text),
    )
    return text


async def chat_json(
    system: str,
    user: str,
    *,
    base_url: str,
    model: str,
    timeout: float | None = None,
    options: dict | None = None,
) -> dict | list:
    """Come chat(), ma forza e valida una risposta JSON."""
    content = await chat(
        system, user, base_url=base_url, model=model, json_mode=True, timeout=timeout, options=options
    )
    return extract_json(content)


async def chat_structured(
    system: str,
    user: str,
    *,
    schema: type,
    base_url: str,
    model: str,
    retries: int = 1,
    timeout: float | None = None,
    options: dict | None = None,
):
    """Come chat_json(), ma valida la risposta con un modello Pydantic.

    Guardrail per i modelli piccoli: se la risposta non rispetta lo schema, il
    modello viene rilanciato con gli errori di validazione come feedback (fino
    a `retries` volte). Ritorna un'istanza di `schema`; solleva LLMError se
    anche l'ultimo tentativo è invalido.

    Il flag STRUCTURED_OUTPUT=false disattiva il vincolo di decoding.
    """
    from pydantic import BaseModel, ValidationError

    assert issubclass(schema, BaseModel), "schema deve essere un modello Pydantic"

    provider = provider_for(model)
    use_structured = os.getenv("STRUCTURED_OUTPUT", "true").lower() != "false"

    if provider == "anthropic" and use_structured:
        started = time.perf_counter()
        try:
            parsed, t_in, t_out = await _structured_anthropic(
                system, user, schema=schema, model=model, timeout=timeout
            )
        except LLMError as exc:
            telemetry.record_llm_call(
                provider=provider, model=model, service=_service_name(),
                duration_s=time.perf_counter() - started, structured=True, ok=False,
                error=str(exc), prompt_chars=len(system) + len(user),
            )
            raise
        telemetry.record_llm_call(
            provider=provider, model=model, service=_service_name(), tokens_in=t_in,
            tokens_out=t_out, duration_s=time.perf_counter() - started, structured=True,
            ok=True, prompt_chars=len(system) + len(user),
        )
        return parsed

    # I modelli piccoli "dimenticano" il formato in fondo ai prompt lunghi:
    # il JSON Schema atteso viene ribadito in coda al messaggio utente.
    schema_hint = (
        "\n\nRISPONDI ESCLUSIVAMENTE con un oggetto JSON conforme a questo JSON Schema "
        "(nessun testo intorno, nessun campo contenitore tipo 'response'):\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)[:3000]
    )
    user = user + schema_hint
    prompt = user
    # Structured outputs: vincolo il decoding di Ollama al JSON Schema del modello
    # Pydantic. La validazione + retry sotto resta come backstop (schemi con
    # vincoli semantici — min_length, pattern — che il grammar non copre).
    json_schema = schema.model_json_schema() if use_structured else None
    last_error: str = ""
    for attempt in range(retries + 1):
        content = await chat(
            system, prompt, base_url=base_url, model=model, fmt=json_schema,
            json_mode=json_schema is None, timeout=timeout, options=options,
            _attempt=attempt + 1,
        )
        try:
            data = extract_json(content)
        except LLMError as exc:
            # risposta non parsabile come JSON: nuovo tentativo con feedback
            last_error = str(exc)
            logger.warning(
                "Risposta LLM non è JSON (tentativo %d/%d)", attempt + 1, retries + 1
            )
            prompt = (
                f"{user}\n\nLA TUA RISPOSTA PRECEDENTE NON ERA JSON VALIDO.\n"
                f"RISPOSTA: {content[:1500]}\n"
                f"Rispondi SOLO con il JSON richiesto, senza testo intorno."
            )
            continue
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "Risposta LLM non conforme allo schema %s (tentativo %d/%d)",
                schema.__name__, attempt + 1, retries + 1,
            )
            # feedback al modello: risposta precedente + errori di validazione
            prompt = (
                f"{user}\n\nLA TUA RISPOSTA PRECEDENTE NON ERA VALIDA.\n"
                f"RISPOSTA: {json.dumps(data, ensure_ascii=False)[:2000]}\n"
                f"ERRORI DI VALIDAZIONE:\n{last_error[:1500]}\n"
                f"Correggi e rispondi SOLO con il JSON valido."
            )
    raise LLMError(f"Risposta del modello non conforme allo schema {schema.__name__}: {last_error[:300]}")


async def is_available(base_url: str, model: str) -> dict:
    """Verifica che il provider risponda e che il modello sia utilizzabile."""
    provider = provider_for(model)
    if provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            return {"ok": False, "detail": "ANTHROPIC_API_KEY non impostata"}
        try:
            client = _get_anthropic()
            await client.models.retrieve(model)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"Modello {model} non disponibile: {exc}"}
        return {"ok": True, "detail": f"Modello {model} disponibile (anthropic)"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            models = [m.get("name", "") for m in response.json().get("models", [])]
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Ollama non raggiungibile su {base_url}: {exc}"}
    if not any(name == model or name.split(":")[0] == model for name in models):
        return {"ok": False, "detail": f"Modello {model!r} non trovato. Installati: {models}"}
    return {"ok": True, "detail": f"Modello {model} disponibile"}
