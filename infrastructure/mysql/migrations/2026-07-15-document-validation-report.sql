-- Report dell'Agente di Coerenza persistito sul documento, a supporto della
-- decisione Human-in-the-Loop dello Scrum Master.
-- Per i database esistenti (le nuove installazioni lo hanno già da 01-schema.sql).
ALTER TABLE documents
    ADD COLUMN validation_report JSON NULL AFTER wo_id;
