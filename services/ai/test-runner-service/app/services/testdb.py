"""Ciclo di vita del PostgreSQL usato dalla suite del repository bersaglio.

I test del repository bersaglio condividono un database reale e sono
dichiaratamente non parallelizzabili. Per questo ogni esecuzione riceve un
**database dedicato**, creato e distrutto dal servizio: è l'unico modo per avere
isolamento vero fra due run vicini, e il costo è di qualche centinaio di
millisecondi. Un unico database ripulito a ogni giro sarebbe più semplice, ma
due esecuzioni sovrapposte si distruggerebbero i dati a vicenda producendo
fallimenti fantasma, cioè il risultato peggiore possibile per uno strumento la
cui unica ragione d'essere è dire la verità sui test.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.core.config import settings

logger = logging.getLogger(__name__)

# Prefisso dei database effimeri: consente di riconoscere e ripulire gli orfani.
_PREFIX = "t_"


def _dsn(database: str) -> str:
    return (
        f"postgresql://{settings.test_pg_user}:{settings.test_pg_password}"
        f"@{settings.test_pg_host}:{settings.test_pg_port}/{database}"
    )


def database_url(database: str) -> str:
    """URL di connessione da passare alla suite del repository bersaglio."""
    return _dsn(database)


async def _admin_connection() -> asyncpg.Connection:
    return await asyncpg.connect(_dsn(settings.test_pg_admin_db))


async def wait_ready(log=None, attempts: int = 30, delay: float = 1.0) -> None:
    """Attende che il PostgreSQL di test accetti connessioni."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            conn = await _admin_connection()
            await conn.close()
            return
        except (OSError, asyncpg.PostgresError) as exc:
            last = exc
            if log is not None and attempt % 5 == 0:
                await log.add(f"Attendo il database di test… ({attempt}/{attempts})")
            await asyncio.sleep(delay)
    raise RuntimeError(
        f"Il PostgreSQL di test non risponde su {settings.test_pg_host}:{settings.test_pg_port}: {last}"
    )


def run_database_name(run_id: str) -> str:
    """Nome del database dedicato a un run: identificatore valido e sotto i 63 caratteri."""
    return _PREFIX + run_id.replace("-", "")[:24]


async def create_run_database(run_id: str) -> str:
    """Crea il database dell'esecuzione e ne ritorna il nome."""
    name = run_database_name(run_id)
    conn = await _admin_connection()
    try:
        # L'identificatore è generato da noi (prefisso + esadecimale), non
        # proviene dall'utente; le virgolette lo rendono comunque esplicito.
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return name


async def drop_run_database(name: str) -> None:
    """Elimina il database dell'esecuzione. Non deve mai far fallire il run."""
    if not name:
        return
    try:
        conn = await _admin_connection()
        try:
            # WITH (FORCE) chiude le connessioni lasciate aperte da un runner di
            # test interrotto per tempo massimo superato.
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001 - la pulizia non è mai critica
        logger.warning("Impossibile eliminare il database di test %s", name)


async def cleanup_orphan_databases() -> int:
    """Elimina i database effimeri rimasti da esecuzioni interrotte.

    Senza questa pulizia all'avvio, dopo qualche riavvio brusco del container il
    PostgreSQL di test si riempirebbe di database orfani.
    """
    removed = 0
    try:
        conn = await _admin_connection()
        try:
            # L'underscore è un carattere jolly in LIKE: senza ESCAPE il pattern
            # "t_%" intercetterebbe anche template0, template1 e qualunque altro
            # database che inizi per «t». Si escludono comunque i template e il
            # database di servizio, per non dipendere dal solo pattern.
            rows = await conn.fetch(
                "SELECT datname FROM pg_database "
                "WHERE datname LIKE $1 ESCAPE '!' AND NOT datistemplate AND datname <> $2",
                f"t!{_PREFIX[1:]}%",
                settings.test_pg_admin_db,
            )
            for row in rows:
                name = row["datname"]
                if not name.startswith(_PREFIX):
                    continue
                try:
                    await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                    removed += 1
                except Exception:  # noqa: BLE001
                    logger.warning("Database orfano non eliminabile: %s", name)
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001 - all'avvio il database può non esserci ancora
        logger.info("Pulizia dei database orfani non eseguita: PostgreSQL non raggiungibile")
    return removed
