-- Migrazione: gestione Git dei Work Order (assegnazione repo + ciclo Pull Request).
-- Da eseguire UNA VOLTA sui database già esistenti (le nuove installazioni
-- ricevono le colonne direttamente da init/01-schema.sql).
--
--   docker compose exec -T mysql mysql -uroot -p<password> sdlc_platform \
--     < infrastructure/mysql/migrations/2026-07-09-workorder-git.sql
USE sdlc_platform;

ALTER TABLE work_orders
    ADD COLUMN repo_url      VARCHAR(500)                                            AFTER validated_by,
    ADD COLUMN base_branch   VARCHAR(255)                                            AFTER repo_url,
    ADD COLUMN work_branch   VARCHAR(255)                                            AFTER base_branch,
    ADD COLUMN code_pr_url   VARCHAR(500)                                            AFTER work_branch,
    ADD COLUMN code_pr_state ENUM ('none','draft','open','merged','closed') NOT NULL DEFAULT 'none' AFTER code_pr_url,
    ADD COLUMN docs_pr_url   VARCHAR(500)                                            AFTER code_pr_state,
    ADD COLUMN docs_pr_state ENUM ('none','draft','open','merged','closed') NOT NULL DEFAULT 'none' AFTER docs_pr_url;
