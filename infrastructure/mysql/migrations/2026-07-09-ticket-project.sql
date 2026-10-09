-- Migrazione: supporto multi-progetto sui ticket.
-- Ogni ticket appartiene a un progetto, identificato dal repository Git (repo_url).
-- Da eseguire UNA VOLTA sui database già esistenti.
--
--   docker compose exec -T mysql mysql -usdlc -psdlc sdlc_platform \
--     < infrastructure/mysql/migrations/2026-07-09-ticket-project.sql
USE sdlc_platform;

ALTER TABLE tickets
    ADD COLUMN repo_url VARCHAR(500) AFTER domain;
