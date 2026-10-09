-- Migrazione: orchestrazione Git più completa e robusta.
--   * users.github_username        -> reviewer/assignee delle Pull Request
--   * work_orders.assignee_id      -> sviluppatore assegnato dallo Scrum Master
--   * code_pr_state / docs_pr_state -> nuovi stati 'conflict' ed 'error'
--
-- Da eseguire UNA VOLTA sui database già esistenti (le nuove installazioni
-- ricevono le colonne direttamente da init/01-schema.sql).
--
--   docker compose exec -T mysql mysql -uroot -p<password> sdlc_platform \
--     < infrastructure/mysql/migrations/2026-07-20-git-orchestration.sql
USE sdlc_platform;

ALTER TABLE users
    ADD COLUMN github_username VARCHAR(100) AFTER domain;

ALTER TABLE work_orders
    ADD COLUMN assignee_id INT AFTER validated_by,
    ADD CONSTRAINT fk_work_orders_assignee FOREIGN KEY (assignee_id) REFERENCES users (id);

ALTER TABLE work_orders
    MODIFY COLUMN code_pr_state
        ENUM ('none', 'draft', 'open', 'merged', 'closed', 'conflict', 'error') NOT NULL DEFAULT 'none',
    MODIFY COLUMN docs_pr_state
        ENUM ('none', 'draft', 'open', 'merged', 'closed', 'conflict', 'error') NOT NULL DEFAULT 'none';

-- Token Git per-repository (cifrati a riposo)
CREATE TABLE IF NOT EXISTS repo_credentials (
    repo_slug       VARCHAR(255) PRIMARY KEY,
    repo_url        VARCHAR(500) NOT NULL,
    token_encrypted VARBINARY(1024) NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
