-- Impostazioni di piattaforma (credenziali integrazioni, repository, ecc.)
-- Gestite dalla pagina "Impostazioni" della dashboard; i valori del .env
-- fanno da fallback quando una chiave non è presente qui.
USE sdlc_platform;

CREATE TABLE IF NOT EXISTS platform_settings (
    setting_key   VARCHAR(100) PRIMARY KEY,
    setting_value TEXT NOT NULL,
    is_secret     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
