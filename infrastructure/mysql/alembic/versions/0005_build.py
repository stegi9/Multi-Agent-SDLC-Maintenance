"""Esito della produzione degli artefatti nell'esecuzione dei test.

Aggiunge `test_runs.build_status` e `test_runs.build_output`. La compilazione
degli artefatti eseguibili è registrata separatamente dal controllo dei tipi
perché risponde a una domanda diversa: un progetto può superare il controllo dei
tipi e non riuscire comunque a impacchettarsi.
Idempotente: salta l'ALTER se le colonne esistono già.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _column_exists(column: str) -> bool:
    connection = op.get_bind()
    row = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'test_runs' "
            "AND column_name = :c"
        ),
        {"c": column},
    ).scalar()
    return bool(row)


def upgrade() -> None:
    if not _column_exists("build_status"):
        op.execute(
            "ALTER TABLE test_runs ADD COLUMN build_status "
            "ENUM('skipped','passed','failed') NOT NULL DEFAULT 'skipped' AFTER typecheck_output"
        )
    if not _column_exists("build_output"):
        op.execute("ALTER TABLE test_runs ADD COLUMN build_output TEXT AFTER build_status")


def downgrade() -> None:
    if _column_exists("build_output"):
        op.execute("ALTER TABLE test_runs DROP COLUMN build_output")
    if _column_exists("build_status"):
        op.execute("ALTER TABLE test_runs DROP COLUMN build_status")
