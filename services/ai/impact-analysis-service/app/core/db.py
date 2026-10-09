"""Connessione asincrona a MySQL (SQLAlchemy + aiomysql)."""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    f"mysql+aiomysql://{settings.mysql_user}:{settings.mysql_password}"
    f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}",
    pool_recycle=3600,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
