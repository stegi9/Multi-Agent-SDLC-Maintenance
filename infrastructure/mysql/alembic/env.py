"""Ambiente Alembic: costruisce l'URL MySQL dalle variabili d'ambiente."""
import os

from alembic import context
from sqlalchemy import create_engine


def _database_url() -> str:
    host = os.getenv("MYSQL_HOST", "localhost")
    port = os.getenv("MYSQL_PORT", "3307")  # porta pubblicata da docker-compose sull'host
    user = os.getenv("MYSQL_USER", "sdlc")
    password = os.getenv("MYSQL_PASSWORD", "sdlc")
    database = os.getenv("MYSQL_DATABASE", "sdlc_platform")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
