-- Schema iniziale della piattaforma SDLC (eseguito automaticamente al primo avvio del container MySQL)

CREATE DATABASE IF NOT EXISTS sdlc_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE sdlc_platform;

-- ---------------------------------------------------------------------------
-- Utenti e RBAC
-- ---------------------------------------------------------------------------
CREATE TABLE roles (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(50) NOT NULL UNIQUE,   -- product_manager | scrum_master | developer
    description VARCHAR(255)
);

CREATE TABLE users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    username        VARCHAR(100) NOT NULL UNIQUE,
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    domain          VARCHAR(100),              -- dominio assegnato allo sviluppatore (es. billing, hr)
    github_username VARCHAR(100),              -- handle GitHub (per reviewer/assignee delle Pull Request)
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_roles (
    user_id INT NOT NULL,
    role_id INT NOT NULL,
    PRIMARY KEY (user_id, role_id),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Ticket (importati o creati internamente)
-- ---------------------------------------------------------------------------
CREATE TABLE tickets (
    id            VARCHAR(36) PRIMARY KEY,                 -- UUID interno
    external_id   VARCHAR(100),                            -- ID sul sistema esterno (Reverse Sync)
    source        ENUM ('jira', 'github', 'bugzilla', 'cve', 'internal') NOT NULL,
    type          ENUM ('bug', 'cve', 'change_request') NOT NULL,
    title         VARCHAR(500) NOT NULL,
    description   TEXT,
    severity      VARCHAR(50),
    domain        VARCHAR(100),                            -- dominio software (es. billing, hr)
    repo_url      VARCHAR(500),                            -- progetto di appartenenza (repository Git)
    status        ENUM ('raw', 'deduplicated', 'prioritized', 'analyzed',
                        'approved', 'rejected', 'postponed', 'in_progress',
                        'completed') NOT NULL DEFAULT 'raw',
    priority      INT,
    duplicate_of  VARCHAR(36),                             -- ticket master in caso di duplicato
    created_by    INT,                                     -- NULL se importato
    imported_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (duplicate_of) REFERENCES tickets (id),
    FOREIGN KEY (created_by) REFERENCES users (id)
);

-- ---------------------------------------------------------------------------
-- Analisi di impatto (Agente di Planning)
-- ---------------------------------------------------------------------------
CREATE TABLE impact_analyses (
    id                 VARCHAR(36) PRIMARY KEY,
    ticket_id          VARCHAR(36) NOT NULL,
    impact_percentage  DECIMAL(5, 2) NOT NULL,
    decision           ENUM ('auto_approved', 'needs_authorization', 'blocked') NOT NULL,
    affected_files     JSON,
    risk_notes         TEXT,
    is_what_if         BOOLEAN NOT NULL DEFAULT FALSE,     -- TRUE per la What-If Analysis del PM
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Work Order
-- ---------------------------------------------------------------------------
CREATE TABLE work_orders (
    id             VARCHAR(36) PRIMARY KEY,
    ticket_id      VARCHAR(36) NOT NULL,
    status         ENUM ('generated', 'running', 'code_ready', 'validated',
                         'merged', 'failed') NOT NULL DEFAULT 'generated',
    payload        JSON NOT NULL,                          -- il JSON di input per l'Agente Sviluppatore
    changes        JSON,                                   -- il JSON delle modifiche prodotto
    reasoning_log  LONGTEXT,                               -- log per il terminale virtuale
    approved_by    INT,                                    -- Scrum Master che ha autorizzato
    validated_by   INT,                                    -- Scrum Master che ha validato il codice
    assignee_id    INT,                                    -- sviluppatore assegnato dallo Scrum Master (reviewer della PR)
    -- Gestione Git (assegnazione manuale del repo + ciclo delle Pull Request)
    repo_url       VARCHAR(500),                           -- repository Git assegnato al WO (scelto dallo Scrum Master)
    base_branch    VARCHAR(255),                           -- branch base da cui deriva il lavoro (es. main/develop)
    work_branch    VARCHAR(255),                           -- branch di lavoro del WO (es. wo/<id>)
    code_pr_url    VARCHAR(500),                           -- URL della Pull Request del codice
    -- 'conflict' = non fondibile (conflitti/base cambiata); 'error' = errore CI o operazione Git
    code_pr_state  ENUM ('none', 'draft', 'open', 'merged', 'closed', 'conflict', 'error') NOT NULL DEFAULT 'none',
    docs_pr_url    VARCHAR(500),                           -- URL della Pull Request della documentazione
    docs_pr_state  ENUM ('none', 'draft', 'open', 'merged', 'closed', 'conflict', 'error') NOT NULL DEFAULT 'none',
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (ticket_id) REFERENCES tickets (id),
    FOREIGN KEY (approved_by) REFERENCES users (id),
    FOREIGN KEY (validated_by) REFERENCES users (id),
    FOREIGN KEY (assignee_id) REFERENCES users (id)
);

-- Ticket raggruppati in un Work Order (un WO può unire più ticket).
-- L'Agente di Pianificazione decide il raggruppamento; la membership è
-- modificabile dallo Scrum Master (aggiungi/rimuovi ticket).
CREATE TABLE work_order_tickets (
    wo_id      VARCHAR(36) NOT NULL,
    ticket_id  VARCHAR(36) NOT NULL,
    PRIMARY KEY (wo_id, ticket_id),
    FOREIGN KEY (wo_id) REFERENCES work_orders (id) ON DELETE CASCADE,
    FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Esecuzioni della suite di test su un Work Order (Agente Tester)
-- ---------------------------------------------------------------------------
-- Storico delle esecuzioni: ogni run testa un branch preciso a un commit preciso.
-- I conteggi sono separati fra test introdotti dal Work Order e suite preesistente,
-- perché sono due domande diverse: «il nuovo codice funziona?» e «ho rotto qualcosa?».
CREATE TABLE test_runs (
    id              VARCHAR(36) PRIMARY KEY,
    wo_id           VARCHAR(36) NOT NULL,
    status          ENUM ('running', 'passed', 'failed', 'no_tests', 'error')
                        NOT NULL DEFAULT 'running',
    -- 'manual' = avvio da parte di Sviluppatore o Scrum Master (gate HITL)
    trigger_kind    ENUM ('manual', 'event') NOT NULL DEFAULT 'manual',
    triggered_by    INT,                                    -- utente che ha avviato
    repo_url        VARCHAR(500),
    branch          VARCHAR(255),                           -- branch effettivamente testato
    base_branch     VARCHAR(255),                           -- riferimento per il diff
    commit_sha      VARCHAR(40),
    -- Valorizzato quando si è ripiegato sul branch base: es. 'work_branch_missing'
    fallback_reason VARCHAR(60),
    -- Fase in cui l'esecuzione si è interrotta: workspace|install|db_unavailable|
    -- db_schema|vitest|timeout|report
    error_stage     VARCHAR(40),
    error_detail    TEXT,                                   -- ultime righe di output
    -- Esito del controllo dei tipi, eseguito prima della suite: 'skipped' quando
    -- il repository non dichiara lo script corrispondente.
    typecheck_status ENUM ('skipped', 'passed', 'failed') NOT NULL DEFAULT 'skipped',
    typecheck_output TEXT,                                  -- errori di compilazione
    -- Esito della produzione degli artefatti eseguibili, che è cosa distinta dal
    -- controllo dei tipi: un progetto può superare i tipi e non impacchettarsi.
    build_status    ENUM ('skipped', 'passed', 'failed') NOT NULL DEFAULT 'skipped',
    build_output    TEXT,
    total_tests     INT NOT NULL DEFAULT 0,
    passed_tests    INT NOT NULL DEFAULT 0,
    failed_tests    INT NOT NULL DEFAULT 0,
    skipped_tests   INT NOT NULL DEFAULT 0,
    new_total       INT NOT NULL DEFAULT 0,                 -- test introdotti dal WO
    new_failed      INT NOT NULL DEFAULT 0,
    regression_total  INT NOT NULL DEFAULT 0,               -- suite preesistente
    regression_failed INT NOT NULL DEFAULT 0,
    duration_ms     INT,
    log             LONGTEXT,                               -- terminale dell'esecuzione
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TIMESTAMP NULL,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (wo_id) REFERENCES work_orders (id) ON DELETE CASCADE,
    FOREIGN KEY (triggered_by) REFERENCES users (id),
    INDEX idx_test_runs_wo (wo_id, started_at)
);

-- Esito del SINGOLO test: è ciò che permette di mostrare non solo «quanti»
-- test non sono passati, ma QUALI, con il relativo messaggio d'errore.
CREATE TABLE test_case_results (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id       VARCHAR(36) NOT NULL,
    file         VARCHAR(500) NOT NULL,                     -- percorso relativo al repo
    suite        VARCHAR(500),                              -- describe annidati, uniti da " › "
    name         VARCHAR(500) NOT NULL,
    status       ENUM ('passed', 'failed', 'skipped') NOT NULL,
    -- 'new' = test introdotto da questo Work Order; 'regression' = suite preesistente
    origin       ENUM ('new', 'regression') NOT NULL DEFAULT 'regression',
    duration_ms  INT,
    message      TEXT,                                      -- messaggio d'errore, se fallito
    FOREIGN KEY (run_id) REFERENCES test_runs (id) ON DELETE CASCADE,
    INDEX idx_tcr_run_status (run_id, status),
    INDEX idx_tcr_run_origin (run_id, origin)
);

-- ---------------------------------------------------------------------------
-- Documenti (metadati; il contenuto vettorizzato vive in Qdrant)
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    id           VARCHAR(36) PRIMARY KEY,
    title        VARCHAR(500) NOT NULL,
    file_name    VARCHAR(255),
    content      LONGTEXT,
    target_rag   ENUM ('main', 'future') NOT NULL DEFAULT 'main',
    status       ENUM ('uploaded', 'validating', 'accepted', 'forced',
                       'rejected', 'in_review', 'archived') NOT NULL DEFAULT 'uploaded',
    uploaded_by  INT,
    wo_id        VARCHAR(36),                              -- WO che ha generato il documento
    validation_report JSON,                                -- ultimo report dell'Agente di Coerenza
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (uploaded_by) REFERENCES users (id),
    FOREIGN KEY (wo_id) REFERENCES work_orders (id)
);

-- ---------------------------------------------------------------------------
-- Report degli Scarti (alimenta l'Agente di Coerenza)
-- ---------------------------------------------------------------------------
CREATE TABLE discard_reports (
    id          VARCHAR(36) PRIMARY KEY,
    ticket_id   VARCHAR(36) NOT NULL,
    reason      ENUM ('rejected', 'postponed') NOT NULL,
    details     TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Credenziali Git per-repository (token dedicato, cifrato a riposo con Fernet)
-- ---------------------------------------------------------------------------
CREATE TABLE repo_credentials (
    repo_slug       VARCHAR(255) PRIMARY KEY,          -- owner/repo minuscolo
    repo_url        VARCHAR(500) NOT NULL,             -- URL originale (per la UI)
    token_encrypted VARBINARY(1024) NOT NULL,          -- token GitHub cifrato (mai in chiaro)
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Audit log (tracciabilità delle decisioni Human-in-the-Loop)
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT,
    action      VARCHAR(100) NOT NULL,   -- es. wo_approved, code_validated, document_forced
    entity_type VARCHAR(50),
    entity_id   VARCHAR(36),
    details     JSON,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

-- ---------------------------------------------------------------------------
-- Dati iniziali
-- ---------------------------------------------------------------------------
INSERT INTO roles (name, description)
VALUES ('product_manager', 'Inserisce Change Request, esegue What-If Analysis'),
       ('scrum_master', 'Carica documentazione, autorizza i Work Order, valida il codice'),
       ('developer', 'Monitora il reasoning degli agenti per il proprio dominio');
