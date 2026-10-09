"""Orchestratore dell'Agente Tester: dal branch di lavoro all'esito per test.

Sequenza di un'esecuzione:

    1. preparazione dell'area di lavoro (checkout del branch di lavoro)
    2. installazione delle dipendenze del repository bersaglio
    3. controllo dei tipi e produzione degli artefatti eseguibili
    4. creazione del database di test e del suo schema
    5. esecuzione della suite con un report leggibile a macchina
    6. lettura del report, classificazione nuovo/regressione, persistenza

Ogni fase è associata a un `error_stage`, così un guasto è sempre attribuibile a
un punto preciso invece di presentarsi come un generico fallimento.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.kafka.producer import publish
from app.services import persistence, testdb, vitest_report, workspace
from app.services.process import CommandError, child_env, run_command
from app.services.persistence import TestLog

logger = logging.getLogger(__name__)

# Una sola esecuzione alla volta: la working copy è condivisa fra i run, e due
# checkout simultanei si toglierebbero il codice da sotto i piedi. La guardia
# applicativa (409 se esiste già un run in corso) copre il caso normale, questo
# lock copre le corse fra richieste ravvicinate.
_run_lock = asyncio.Lock()


class StageError(RuntimeError):
    """Errore attribuito a una fase precisa dell'esecuzione."""

    def __init__(self, stage: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.detail = detail or message


async def execute_test_run(
    run_id: str,
    wo_id: str,
    *,
    allow_base_branch: bool = False,
) -> dict:
    """Esegue la suite di test per un Work Order e ne persiste l'esito."""
    log = TestLog(run_id)
    started = time.monotonic()
    db_name = ""
    info: workspace.WorkspaceInfo | None = None
    counts: dict[str, int] = {}
    status = "error"
    error_stage = ""
    error_detail = ""
    typecheck_status = "skipped"
    typecheck_output = ""
    build_status = "skipped"
    build_output = ""

    async with _run_lock:
        try:
            wo = await persistence.load_workorder(wo_id)
            if wo is None:
                raise StageError("workspace", "Work Order non trovato")

            repo_url = wo.get("repo_url") or ""
            base_branch = wo.get("base_branch") or "main"
            work_branch = wo.get("work_branch") or f"wo/{wo_id[:8]}"
            planned = _planned_test_files(wo.get("changes"))

            await log.add(f"» Avvio esecuzione dei test per il Work Order {wo_id[:8]}")
            if planned:
                await log.add(f"Test previsti dal piano dell'Agente Sviluppatore: {', '.join(planned)}")

            # 1. area di lavoro
            await log.add(f"» Preparazione dell'area di lavoro (branch {work_branch})")
            try:
                info = await workspace.prepare(
                    repo_url, base_branch, work_branch, log,
                    allow_base_branch=allow_base_branch,
                )
            except CommandError as exc:
                raise StageError("workspace", str(exc)) from exc

            repo_path = info.path
            env = child_env()

            # 2. dipendenze
            await log.add(
                "» Installazione delle dipendenze: al primo avvio può richiedere alcuni minuti"
            )
            await _install(repo_path, env, log)

            # 2b. controllo dei tipi: accerta che il codice consegnato compili.
            # Non interrompe l'esecuzione, perché sapere quali test passano resta
            # informativo anche su un codice che non compila del tutto.
            if settings.typecheck_enabled:
                await log.add("» Controllo dei tipi del codice consegnato")
                typecheck_status, typecheck_output = await _typecheck(repo_path, env, log)
                if typecheck_status == "failed":
                    await log.add(
                        "⚠ Il codice consegnato non supera il controllo dei tipi: "
                        "la suite viene eseguita ugualmente, ma l'esito va letto alla luce di questo"
                    )
                elif typecheck_status == "passed":
                    await log.add("Il codice consegnato compila correttamente")

            # 2c. produzione degli artefatti: superare il controllo dei tipi non
            # garantisce che il progetto si impacchetti, perché la compilazione
            # risolve anche configurazioni e risorse che i tipi non toccano.
            if settings.build_enabled:
                await log.add("» Produzione degli artefatti eseguibili")
                build_status, build_output = await _build(repo_path, env, log)
                if build_status == "failed":
                    await log.add(
                        "⚠ Gli artefatti non sono stati prodotti: il codice consegnato "
                        "non è impacchettabile nello stato attuale"
                    )
                elif build_status == "passed":
                    await log.add("Artefatti prodotti correttamente")

            # 3. database di test
            await log.add("» Predisposizione del database di test")
            try:
                await testdb.wait_ready(log)
            except RuntimeError as exc:
                raise StageError("db_unavailable", str(exc)) from exc
            db_name = await testdb.create_run_database(run_id)
            database_url = testdb.database_url(db_name)
            env = child_env(database_url)
            await _push_schema(repo_path, env, log)

            # 4. suite
            await log.add("» Esecuzione della suite di test")
            report_path = workspace.reports_dir() / f"{run_id}.json"
            await _run_suite(repo_path, env, log, report_path)

            # 5. report
            await log.add("» Analisi del report")
            raw = _read_report(report_path)
            cases, summary = vitest_report.parse_report(raw, repo_path)
            vitest_report.classify(cases, info.changed_files, planned)
            counts = {
                "total": summary.total,
                "passed": summary.passed,
                "failed": summary.failed,
                "skipped": summary.skipped,
                **vitest_report.split_counts(cases),
            }

            if summary.total == 0:
                status = "no_tests"
                await log.add(
                    f"Nessun test eseguito: il branch non contiene test nel pacchetto "
                    f"{settings.test_package_path}"
                )
            else:
                status = "failed" if summary.failed else "passed"
                await log.add(
                    f"» Esito: {summary.total} test, {summary.passed} passati, "
                    f"{summary.failed} falliti, {summary.skipped} saltati "
                    f"(nuovi {counts['new_total']}, regressione {counts['regression_total']})"
                )

            await persistence.save_cases(run_id, cases)

        except StageError as exc:
            status = "error"
            error_stage = exc.stage
            error_detail = exc.detail
            await log.add(f"ERRORE nella fase «{exc.stage}»: {exc}")
        except Exception as exc:  # noqa: BLE001 - nessun guasto deve lasciare il run appeso
            status = "error"
            error_stage = error_stage or "report"
            error_detail = str(exc)
            logger.exception("Errore imprevisto nel run %s", run_id)
            await log.add(f"ERRORE imprevisto: {exc}")
        finally:
            if db_name:
                await log.add("» Pulizia del database di test")
                await testdb.drop_run_database(db_name)

            duration_ms = int((time.monotonic() - started) * 1000)
            await persistence.finish_run(
                run_id,
                status=status,
                log=log,
                branch=info.branch if info else "",
                commit_sha=info.commit_sha if info else "",
                fallback_reason=info.fallback_reason if info else "",
                error_stage=error_stage,
                error_detail=error_detail,
                typecheck_status=typecheck_status,
                typecheck_output=typecheck_output,
                build_status=build_status,
                build_output=build_output,
                counts=counts,
                duration_ms=duration_ms,
            )
            await _publish_result(run_id, wo_id, status, info, counts, duration_ms)

    return {"status": status, "run_id": run_id, "wo_id": wo_id, **counts}


def _planned_test_files(changes: object) -> set[str]:
    """File di test previsti dal piano dell'Agente Sviluppatore.

    Sopravvive al merge del Work Order, quando il diff Git non distingue più i
    test nuovi da quelli preesistenti.
    """
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except (ValueError, TypeError):
            return set()
    if not isinstance(changes, dict):
        return set()
    plan = changes.get("test_plan")
    if isinstance(plan, dict) and plan.get("files"):
        return {str(f) for f in plan["files"] if f}
    # Ripiego: i file marcati come test nell'elenco delle modifiche.
    return {
        str(c.get("file"))
        for c in changes.get("changes") or []
        if isinstance(c, dict) and c.get("kind") == "test" and c.get("file")
    }


# Il repository bersaglio applica una politica di sicurezza che impedisce
# l'esecuzione degli script di post-installazione delle dipendenze. Il gestore di
# pacchetti installa comunque tutto, ma esce con un codice d'errore per
# segnalarlo. Non è un guasto: è la politica del repository che funziona, e il
# servizio la rispetta senza forzarla.
_IGNORED_BUILDS_MARKER = "ERR_PNPM_IGNORED_BUILDS"


async def _install(repo_path: str, env: dict[str, str], log: TestLog) -> None:
    """Installa le dipendenze del repository bersaglio.

    Due condizioni non fatali sono gestite esplicitamente:

    * lockfile disallineato, tipico quando l'agente cloud aggiunge un import
      senza rigenerarlo: si ritenta una sola volta senza il vincolo;
    * script di post-installazione bloccati dalla politica di sicurezza del
      repository: l'installazione è comunque avvenuta, e lo si verifica invece
      di darlo per scontato.
    """
    pm = settings.package_manager
    attempts = [
        (["install", "--frozen-lockfile"], None),
        (
            ["install"],
            "Lockfile non allineato al codice generato dall'agente: "
            "reinstallo senza vincolo di lockfile",
        ),
    ]

    last_error: CommandError | None = None
    for args, retry_message in attempts:
        if retry_message:
            await log.add(retry_message)
        try:
            await run_command(
                [pm, *args],
                cwd=repo_path,
                env=env,
                timeout=settings.install_timeout_seconds,
                log=log,
            )
            return
        except CommandError as exc:
            last_error = exc
            if _IGNORED_BUILDS_MARKER in (exc.output or ""):
                await log.add(
                    "Il repository vieta gli script di post-installazione delle "
                    "dipendenze: verifico che le dipendenze siano comunque utilizzabili"
                )
                if await _test_runner_available(repo_path, env, log):
                    await log.add("Dipendenze installate e runner di test disponibile")
                    return
                raise StageError(
                    "install",
                    "Le dipendenze non sono utilizzabili: il runner di test non è eseguibile",
                    exc.output,
                ) from exc
            logger.info("Tentativo di installazione fallito: %s", exc)

    raise StageError(
        "install",
        "Installazione delle dipendenze non riuscita",
        (last_error.output if last_error else ""),
    )


async def _test_runner_available(repo_path: str, env: dict[str, str], log: TestLog) -> bool:
    """Verifica che il runner di test sia realmente invocabile dopo l'installazione."""
    try:
        code, _ = await run_command(
            [
                settings.package_manager,
                "-C",
                settings.test_package_path,
                "exec",
                settings.test_runner_bin,
                "--version",
            ],
            cwd=repo_path,
            env=env,
            timeout=120,
            log=log,
            check=False,
            quiet=True,
        )
        return code == 0
    except CommandError:
        return False


async def _typecheck(repo_path: str, env: dict[str, str], log: TestLog) -> tuple[str, str]:
    """Verifica che il codice consegnato compili, restituendo esito e diagnostica.

    Risponde a una domanda che la suite non pone. Un difetto di tipo in un file
    che nessun test attraversa supererebbe l'esecuzione senza lasciare traccia,
    per riemergere alla prima compilazione fuori dalla piattaforma.

    Non solleva mai: un controllo dei tipi che non si può eseguire, perché il
    repository non dichiara lo script corrispondente, non è un guasto e non deve
    impedire l'esecuzione dei test.
    """
    try:
        code, output = await run_command(
            [
                settings.package_manager,
                "run",
                settings.typecheck_script_name,
            ],
            cwd=repo_path,
            env=env,
            timeout=settings.typecheck_timeout_seconds,
            log=log,
            check=False,
        )
    except CommandError as exc:
        await log.add("Controllo dei tipi non eseguibile: si prosegue con la suite")
        logger.info("Typecheck non eseguito: %s", exc)
        return "skipped", ""

    if code == 0:
        return "passed", ""
    # Lo script assente fa uscire il gestore di pacchetti con un codice dedicato:
    # è un repository che non dichiara il controllo, non un codice che non compila.
    if "Missing script" in output or "command not found" in output:
        await log.add(
            f"Il repository non dichiara lo script «{settings.typecheck_script_name}»: "
            "controllo dei tipi saltato"
        )
        return "skipped", ""
    return "failed", output[-8000:]


async def _build(repo_path: str, env: dict[str, str], log: TestLog) -> tuple[str, str]:
    """Produce gli artefatti eseguibili del repository bersaglio.

    Si invocano gli script dei singoli pacchetti e non quello della radice,
    poiché quest'ultimo, nella convenzione del repository, ripeterebbe il
    controllo dei tipi già eseguito nella fase precedente.

    Come per il controllo dei tipi, un fallimento non interrompe l'esecuzione:
    viene registrato e la suite prosegue.
    """
    try:
        code, output = await run_command(
            [
                settings.package_manager,
                "-r",
                "--if-present",
                "run",
                settings.build_script_name,
            ],
            cwd=repo_path,
            env=env,
            timeout=settings.build_timeout_seconds,
            log=log,
            check=False,
        )
    except CommandError as exc:
        await log.add("Produzione degli artefatti non eseguibile: si prosegue con la suite")
        logger.info("Build non eseguita: %s", exc)
        return "skipped", ""

    if code == 0:
        return "passed", ""
    return "failed", output[-8000:]


async def _push_schema(repo_path: str, env: dict[str, str], log: TestLog) -> None:
    """Crea lo schema del database di test nel pacchetto che lo definisce."""
    try:
        await run_command(
            [
                settings.package_manager,
                "-C",
                settings.test_db_package_path,
                "run",
                settings.db_push_script_name,
            ],
            cwd=repo_path,
            env=env,
            timeout=settings.db_schema_timeout_seconds,
            log=log,
        )
    except CommandError as exc:
        raise StageError(
            "db_schema", "Creazione dello schema del database di test non riuscita", str(exc)
        ) from exc


async def _run_suite(
    repo_path: str, env: dict[str, str], log: TestLog, report_path: Path
) -> None:
    """Esegue la suite chiedendo un report leggibile a macchina.

    Si invoca il runner con `exec` e non con lo script dichiarato dal
    repository: passando per lo script, il separatore di argomenti del gestore
    di pacchetti arriva letteralmente al runner, che interpreta le opzioni
    seguenti come filtri sui nomi dei file e ignora la richiesta di report.
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if report_path.exists():
        report_path.unlink()

    cmd = [
        settings.package_manager,
        "-C",
        settings.test_package_path,
        "exec",
        settings.test_runner_bin,
        "run",
        "--reporter=json",
        f"--outputFile={report_path}",
    ]
    try:
        # Un codice di uscita diverso da zero è NORMALE quando ci sono test
        # falliti: la verità sta nel report, non nel codice di uscita.
        await run_command(
            cmd,
            cwd=repo_path,
            env=env,
            timeout=settings.test_timeout_seconds,
            log=log,
            check=False,
        )
    except CommandError as exc:
        stage = "timeout" if "tempo massimo" in str(exc) else "vitest"
        raise StageError(stage, "Esecuzione della suite non riuscita", str(exc)) from exc

    if not report_path.exists():
        raise StageError(
            "vitest",
            "La suite non ha prodotto alcun report: verifica il terminale per il dettaglio",
        )


def _read_report(report_path: Path) -> dict:
    try:
        return json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise StageError("report", f"Report della suite illeggibile: {exc}") from exc


async def _publish_result(
    run_id: str,
    wo_id: str,
    status: str,
    info: workspace.WorkspaceInfo | None,
    counts: dict[str, int],
    duration_ms: int,
) -> None:
    """Pubblica l'esito sulla coreografia. Un guasto Kafka non invalida il run."""
    try:
        await publish(
            "tests.executed",
            {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "produced_by": settings.service_name,
                "wo_id": wo_id,
                "run_id": run_id,
                "status": status,
                "branch": info.branch if info else "",
                "total": counts.get("total", 0),
                "passed": counts.get("passed", 0),
                "failed": counts.get("failed", 0),
                "skipped": counts.get("skipped", 0),
                "new_total": counts.get("new_total", 0),
                "new_failed": counts.get("new_failed", 0),
                "regression_total": counts.get("regression_total", 0),
                "regression_failed": counts.get("regression_failed", 0),
                "duration_ms": duration_ms,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kafka non disponibile: evento tests.executed non pubblicato")
