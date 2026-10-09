"""Report dell'Agente di Coerenza persistito sul documento.

Aggiunge `documents.validation_report` (JSON): l'ultimo report di coerenza
prodotto dall'agente, a supporto della decisione Human-in-the-Loop.
Idempotente: salta l'ALTER se la colonna esiste già (installazioni fresche
la ricevono da 01-schema.sql).

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _column_exists(column: str) -> bool:
    connection = op.get_bind()
    row = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'documents' AND column_name = :c"
        ),
        {"c": column},
    ).scalar()
    return bool(row)


def upgrade() -> None:
    if not _column_exists("validation_report"):
        op.execute("ALTER TABLE documents ADD COLUMN validation_report JSON NULL AFTER wo_id")


def downgrade() -> None:
    if _column_exists("validation_report"):
        op.execute("ALTER TABLE documents DROP COLUMN validation_report")
