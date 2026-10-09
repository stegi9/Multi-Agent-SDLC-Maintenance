"""Esito del controllo dei tipi nell'esecuzione dei test (Agente Tester).

Aggiunge `test_runs.typecheck_status` e `test_runs.typecheck_output`: la
compilazione del repository bersaglio viene verificata prima della suite, e il
suo esito è registrato separatamente perché risponde a una domanda diversa da
quella dei test, ovvero se il codice consegnato compili affatto.
Idempotente: salta l'ALTER se le colonne esistono già (le installazioni fresche
le ricevono da 01-schema.sql).

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
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
    if not _column_exists("typecheck_status"):
        op.execute(
            "ALTER TABLE test_runs ADD COLUMN typecheck_status "
            "ENUM('skipped','passed','failed') NOT NULL DEFAULT 'skipped' AFTER error_detail"
        )
    if not _column_exists("typecheck_output"):
        op.execute(
            "ALTER TABLE test_runs ADD COLUMN typecheck_output TEXT AFTER typecheck_status"
        )


def downgrade() -> None:
    if _column_exists("typecheck_output"):
        op.execute("ALTER TABLE test_runs DROP COLUMN typecheck_output")
    if _column_exists("typecheck_status"):
        op.execute("ALTER TABLE test_runs DROP COLUMN typecheck_status")
