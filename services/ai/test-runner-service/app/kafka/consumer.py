"""Consumer Kafka dell'Agente Tester.

Il servizio ascolta `code.generated` ma, per impostazione predefinita, si limita
a registrarlo. Il motivo è architetturale e vale la pena esplicitarlo: quell
evento viene emesso subito dopo l'invio dell'handoff all'agente cloud, cioè
quando il codice non è ancora stato scritto né pubblicato e il branch di lavoro
non esiste. Un avvio automatico su quell'evento fallirebbe in modo sistematico.
L'esecuzione è quindi un'azione umana (gate Human-in-the-Loop), coerente con gli
altri punti di controllo della pipeline.

Attivando `auto_run_on_code_generated` il consumer attende la comparsa del branch
sul remoto prima di avviare, invece di partire subito.
"""
import asyncio
import logging
import uuid

from app.core.config import settings
from app.kafka.producer import publish
from app.services import persistence, runner, workspace
from shared.common.kafka import ConsumerRunner

logger = logging.getLogger(__name__)

# Attesa della pubblicazione del codice da parte dell'agente cloud: dieci
# tentativi a mezzo minuto di distanza, cioè una finestra di cinque minuti.
_BRANCH_WAIT_ATTEMPTS = 10
_BRANCH_WAIT_DELAY = 30


async def handle_message(topic: str, payload: dict) -> None:
    wo_id = payload.get("wo_id")
    if topic == "code.generated" and wo_id:
        if not settings.auto_run_on_code_generated:
            logger.info(
                "Work Order %s pronto: in attesa di avvio manuale dei test "
                "(Sviluppatore o Scrum Master)",
                wo_id,
            )
            return
        asyncio.create_task(_wait_and_run(wo_id))
    else:
        logger.info("Ricevuto messaggio su %s: %s", topic, payload)


async def _wait_and_run(wo_id: str) -> None:
    """Attende che il branch di lavoro compaia su origin, poi avvia i test."""
    wo = await persistence.load_workorder(wo_id)
    if wo is None or not wo.get("repo_url"):
        return
    branch = wo.get("work_branch") or f"wo/{wo_id[:8]}"

    for attempt in range(1, _BRANCH_WAIT_ATTEMPTS + 1):
        if await workspace.branch_exists_on_remote(wo["repo_url"], branch):
            break
        logger.info(
            "Branch %s non ancora pubblicato (%s/%s)", branch, attempt, _BRANCH_WAIT_ATTEMPTS
        )
        await asyncio.sleep(_BRANCH_WAIT_DELAY)
    else:
        logger.warning(
            "Branch %s non pubblicato entro la finestra di attesa: test non avviati", branch
        )
        return

    if await persistence.has_running_run(wo_id):
        return
    run_id = str(uuid.uuid4())
    await persistence.create_run(
        run_id,
        wo_id,
        repo_url=wo["repo_url"],
        base_branch=wo.get("base_branch") or "main",
        branch=branch,
        triggered_by=None,
        trigger_kind="event",
    )
    await runner.execute_test_run(run_id, wo_id)


_runner = ConsumerRunner(
    topics=settings.kafka_consume_topics,
    bootstrap_servers=settings.kafka_bootstrap_servers,
    group_id=settings.kafka_consumer_group,
    handler=handle_message,
    dlq_publish=publish,
)


async def start_consumer() -> None:
    await _runner.start()


async def stop_consumer() -> None:
    await _runner.stop()
