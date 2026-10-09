"""Esecuzione di comandi esterni con streaming, timeout e ambiente ripulito.

Due responsabilità che non vanno separate perché sono lo stesso rischio:

* **streaming**: l'installazione delle dipendenze dura minuti, e senza un flusso
  di output riga per riga l'utente non ha modo di distinguere un'attesa da un
  blocco;
* **isolamento**: qui dentro si esegue codice scritto da un modello linguistico,
  quindi l'ambiente del processo figlio viene ricostruito da zero invece di
  essere ereditato.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque

from app.core.config import settings

logger = logging.getLogger(__name__)

# Righe di output conservate per popolare il dettaglio d'errore di un run fallito.
_TAIL_LINES = 200


class CommandError(RuntimeError):
    """Comando terminato male, o non terminato affatto entro il tempo massimo.

    Conserva l'output del comando: serve a distinguere un guasto reale da una
    condizione tollerabile, senza dover riesaminare il testo del messaggio.
    """

    def __init__(self, message: str, output: str = "", code: int | None = None) -> None:
        super().__init__(message)
        self.output = output
        self.code = code


def child_env(database_url: str = "") -> dict[str, str]:
    """Ambiente MINIMO per i processi figli.

    L'anchor di configurazione di docker compose inietta nel container i segreti
    della piattaforma (chiave JWT, chiave interna fra servizi, chiave del
    fornitore del modello, credenziali del database). Il codice dei test è
    scritto da un agente e non deve poter leggere nulla di tutto questo, quindi
    l'ambiente si costruisce da zero invece di partire da os.environ.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": settings.test_workspace_path,
        "CI": "1",
        "NODE_ENV": "test",
        "PNPM_STORE_DIR": f"{settings.test_workspace_path}/pnpm-store",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "COREPACK_ENABLE_STRICT": "0",
        # Alcuni strumenti a riga di comando richiedono un TERM valido.
        "TERM": "dumb",
    }
    if database_url:
        env["DATABASE_URL"] = database_url
    # Valori applicativi richiesti dalla suite del repository bersaglio: segreti
    # fittizi di test, dichiarati in configurazione, non ereditati dal container.
    env.update(settings.test_extra_env)
    return env


async def run_command(
    cmd: list[str],
    *,
    cwd: str,
    timeout: int,
    env: dict[str, str] | None = None,
    log=None,
    check: bool = True,
    quiet: bool = False,
) -> tuple[int, str]:
    """Esegue un comando riversando l'output nel log del run.

    Ritorna (codice di uscita, output). Alla scadenza del tempo massimo uccide
    l'intero gruppo di processi: il runner di test lascia processi figli che
    altrimenti resterebbero orfani a tenere aperte le connessioni al database.
    """
    printable = " ".join(cmd)
    if log is not None and not quiet:
        await log.add(f"$ {printable}")

    kwargs: dict = {}
    if hasattr(os, "setsid"):
        # Gruppo di processi separato, così il kill raggiunge anche i figli.
        kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env if env is not None else child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
    except (OSError, ValueError) as exc:
        raise CommandError(f"Impossibile avviare «{printable}»: {exc}") from exc

    tail: deque[str] = deque(maxlen=_TAIL_LINES)

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            tail.append(line)
            if log is not None and not quiet:
                await log.add(line)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        if log is not None:
            await log.add(f"Tempo massimo superato ({timeout}s): processo interrotto")
        raise CommandError(
            f"Il comando «{printable}» ha superato il tempo massimo di {timeout} secondi"
        ) from None

    output = "\n".join(tail)
    code = proc.returncode if proc.returncode is not None else -1
    if check and code != 0:
        raise CommandError(
            f"«{printable}» terminato con codice {code}\n{output[-2000:]}",
            output=output,
            code=code,
        )
    return code, output


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Termina il processo e, dove possibile, il suo intero gruppo."""
    try:
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        logger.warning("Il processo %s era già terminato", proc.pid)
