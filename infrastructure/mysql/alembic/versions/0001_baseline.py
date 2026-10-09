"""Baseline: schema esistente creato dagli script di init.

Lo schema iniziale è creato da `infrastructure/mysql/init/*.sql` (eseguiti da
MySQL al primo avvio del container) più le migrazioni SQL manuali storiche in
`infrastructure/mysql/migrations/` fino al 2026-07-09 compreso.

Questa revisione è volutamente vuota: sui database esistenti va "timbrata"
senza eseguire nulla (`alembic stamp 0001`); da qui in poi ogni modifica di
schema è una nuova revisione Alembic.

Revision ID: 0001
Revises:
"""
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
