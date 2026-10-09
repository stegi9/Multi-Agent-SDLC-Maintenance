"""Esecuzioni della suite di test e esito per singolo caso (Agente Tester).

Aggiunge `test_runs` e `test_case_results`: lo storico delle esecuzioni della
suite su un Work Order, con l'esito del singolo test e la distinzione fra test
introdotti dal Work Order e suite di regressione preesistente.
Idempotente: usa CREATE TABLE IF NOT EXISTS (le installazioni fresche ricevono
le tabelle da 01-schema.sql).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    connection = op.get_bind()
    row = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": table},
    ).scalar()
    return bool(row)


TEST_RUNS = """
CREATE TABLE IF NOT EXISTS test_runs (
    id              VARCHAR(36) PRIMARY KEY,
    wo_id           VARCHAR(36) NOT NULL,
    status          ENUM ('running', 'passed', 'failed', 'no_tests', 'error')
                        NOT NULL DEFAULT 'running',
    trigger_kind    ENUM ('manual', 'event') NOT NULL DEFAULT 'manual',
    triggered_by    INT,
    repo_url        VARCHAR(500),
    branch          VARCHAR(255),
    base_branch     VARCHAR(255),
    commit_sha      VARCHAR(40),
    fallback_reason VARCHAR(60),
    error_stage     VARCHAR(40),
    error_detail    TEXT,
    total_tests     INT NOT NULL DEFAULT 0,
    passed_tests    INT NOT NULL DEFAULT 0,
    failed_tests    INT NOT NULL DEFAULT 0,
    skipped_tests   INT NOT NULL DEFAULT 0,
    new_total       INT NOT NULL DEFAULT 0,
    new_failed      INT NOT NULL DEFAULT 0,
    regression_total  INT NOT NULL DEFAULT 0,
    regression_failed INT NOT NULL DEFAULT 0,
    duration_ms     INT,
    log             LONGTEXT,
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TIMESTAMP NULL,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (wo_id) REFERENCES work_orders (id) ON DELETE CASCADE,
    FOREIGN KEY (triggered_by) REFERENCES users (id),
    INDEX idx_test_runs_wo (wo_id, started_at)
)
"""

TEST_CASE_RESULTS = """
CREATE TABLE IF NOT EXISTS test_case_results (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id       VARCHAR(36) NOT NULL,
    file         VARCHAR(500) NOT NULL,
    suite        VARCHAR(500),
    name         VARCHAR(500) NOT NULL,
    status       ENUM ('passed', 'failed', 'skipped') NOT NULL,
    origin       ENUM ('new', 'regression') NOT NULL DEFAULT 'regression',
    duration_ms  INT,
    message      TEXT,
    FOREIGN KEY (run_id) REFERENCES test_runs (id) ON DELETE CASCADE,
    INDEX idx_tcr_run_status (run_id, status),
    INDEX idx_tcr_run_origin (run_id, origin)
)
"""


def upgrade() -> None:
    op.execute(TEST_RUNS)
    op.execute(TEST_CASE_RESULTS)


def downgrade() -> None:
    # Ordine inverso: prima la tabella figlia, che ha la chiave esterna su test_runs.
    if _table_exists("test_case_results"):
        op.execute("DROP TABLE test_case_results")
    if _table_exists("test_runs"):
        op.execute("DROP TABLE test_runs")
