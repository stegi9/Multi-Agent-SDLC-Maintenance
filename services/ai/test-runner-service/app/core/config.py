"""Configurazione del microservizio Agente Tester (Test Runner)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "test-runner-service"

    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "test-runner-service"
    kafka_consume_topics: list[str] = ["code.generated"]

    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "sdlc"
    mysql_password: str = "sdlc"
    mysql_database: str = "sdlc_platform"

    # Area di lavoro DEDICATA (volume test-workspace). Non si usa /workspace, dove
    # Agente Mappatore e Agente Sviluppatore tengono il clone del branch principale:
    # un checkout del branch di lavoro là dentro falserebbe la selezione del contesto.
    test_workspace_path: str = "/testspace"

    # PostgreSQL effimero per la suite del repository bersaglio
    test_pg_host: str = "test-postgres"
    test_pg_port: int = 5432
    test_pg_user: str = "magazzino"
    test_pg_password: str = "magazzino"
    test_pg_admin_db: str = "postgres"  # database di servizio per CREATE/DROP DATABASE

    # Layout del repository bersaglio (monorepo pnpm). Sono impostazioni, non
    # costanti: un repository diverso si adatta senza toccare il codice.
    test_package_path: str = "artifacts/api-server"  # pacchetto con la suite
    test_db_package_path: str = "lib/db"             # pacchetto con drizzle-kit
    test_runner_bin: str = "vitest"                  # eseguibile del runner di test
    db_push_script_name: str = "push-force"
    # Controllo dei tipi eseguito prima della suite. Risponde a una domanda che i
    # test non pongono: se il codice consegnato compili affatto. Un difetto di
    # tipo in un file che nessun test attraversa resterebbe altrimenti invisibile.
    typecheck_enabled: bool = True
    typecheck_script_name: str = "typecheck"
    # Produzione degli artefatti eseguibili. Si invocano gli script dei singoli
    # pacchetti anziché quello della radice, perché quest'ultimo rifarebbe il
    # controllo dei tipi già eseguito nella fase precedente.
    build_enabled: bool = True
    build_script_name: str = "build"
    package_manager: str = "pnpm"

    # Variabili d'ambiente richieste dalla suite del repository bersaglio.
    # Sono segreti FITTIZI di un ambiente di test, non credenziali della
    # piattaforma: l'ambiente dei processi figli viene ricostruito da zero e
    # questi sono gli unici valori applicativi che vi entrano.
    test_extra_env: dict[str, str] = {
        "SESSION_SECRET": "test-runner-session-secret-non-usare-in-produzione",
        "NODE_ENV": "test",
        # Richieste dalla configurazione di build del repository bersaglio, che
        # si arresta senza di esse prima ancora di iniziare.
        "PORT": "8080",
        "BASE_PATH": "/",
    }

    # Tempi massimi in secondi: oltre, il processo viene ucciso e il run va in 'error'
    clone_timeout_seconds: int = 300
    install_timeout_seconds: int = 900
    typecheck_timeout_seconds: int = 600
    build_timeout_seconds: int = 600
    db_schema_timeout_seconds: int = 300
    test_timeout_seconds: int = 900

    # Avvio automatico alla ricezione di code.generated. Disattivo per default:
    # quando quell'evento arriva l'agente cloud ha appena RICEVUTO l'handoff e il
    # branch di lavoro non esiste ancora, quindi l'esecuzione fallirebbe sempre.
    auto_run_on_code_generated: bool = False

    log_level: str = "info"


settings = Settings()
