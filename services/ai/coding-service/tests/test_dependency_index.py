"""Analisi di raggiungibilita' delle dipendenze (percorso dedicato ai CVE).

I test costruiscono un finto repository su disco, cosi' verificano il modulo
end-to-end (scansione dei manifesti, scansione degli import, filtro del codice
applicativo) senza dipendere dal clone reale.
"""
import json

import pytest

from app.services import dependency_index as di


@pytest.fixture()
def repo(tmp_path):
    """Repository minimo che riproduce i casi tipici di un avviso CVE.

    - `express-session`: dipendenza di runtime importata dal codice applicativo
    - `nodemailer`:      dipendenza di runtime importata da un servizio interno
    - `supertest`:       devDependency importata SOLO dai test
    - `esbuild`:         devDependency mai importata
    - `lodash`:          dipendenza di runtime importata solo da un file di test
    """
    api = tmp_path / "api-server"
    (api / "src" / "lib").mkdir(parents=True)
    (api / "tests").mkdir(parents=True)

    (api / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "express-session": "^1.19.0",
                    "nodemailer": "^6.9.0",
                    "cors": "^2.8.5",
                    "lodash": "^4.17.21",
                },
                "devDependencies": {"supertest": "^7.0.0", "esbuild": "^0.21.0"},
            }
        ),
        encoding="utf-8",
    )
    (api / "src" / "app.ts").write_text(
        'import session from "express-session";\nimport cors from "cors";\n', encoding="utf-8"
    )
    (api / "src" / "lib" / "emailService.ts").write_text(
        'import nodemailer from "nodemailer";\n', encoding="utf-8"
    )
    (api / "tests" / "auth.test.ts").write_text(
        'import request from "supertest";\nimport _ from "lodash";\n', encoding="utf-8"
    )
    # Rumore che non deve mai finire negli indici.
    (api / "node_modules").mkdir()
    (api / "node_modules" / "package.json").write_text('{"dependencies": {"fantasma": "1"}}', encoding="utf-8")
    return tmp_path


@pytest.fixture()
def index(repo):
    return di.DependencyIndex(repo)


def test_manifesti_indicizzati_saltando_node_modules(index):
    assert "express-session" in index.packages
    assert "fantasma" not in index.packages, "node_modules non deve essere indicizzato"


def test_pacchetto_riconosciuto_solo_se_dichiarato(index):
    assert index.detect_packages("vulnerabilita' in express-session") == ["express-session"]
    assert index.detect_packages("problema nella libreria inesistente-xyz") == []


def test_il_nome_lungo_vince_sul_nome_breve(tmp_path):
    """`cors` e' contenuto in `connect-pg-simple`? No, ma il principio vale per
    qualunque coppia annidata: si ordina per lunghezza decrescente."""
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"pg": "^8", "connect-pg-simple": "^9"}}), encoding="utf-8"
    )
    idx = di.DependencyIndex(tmp_path)
    assert idx.detect_packages("SQL injection in connect-pg-simple")[0] == "connect-pg-simple"


def test_dipendenza_di_runtime_usata_nel_codice_e_raggiungibile(index):
    finding = index.analyse("express-session")
    assert finding.reachable is True
    assert finding.runtime_declared is True
    assert any(p.endswith("src/app.ts") for p in finding.call_sites)
    assert any(p.endswith("package.json") for p in finding.manifests)


def test_devdependency_usata_solo_nei_test_non_e_raggiungibile(index):
    finding = index.analyse("supertest")
    assert finding.reachable is False
    assert finding.total_site_count == 1, "l'import nei test esiste"
    assert finding.application_site_count == 0, "ma non e' codice applicativo"
    assert finding.files == finding.manifests, "l'intervento si esaurisce nel manifesto"


def test_devdependency_mai_importata_non_e_raggiungibile(index):
    finding = index.analyse("esbuild")
    assert finding.reachable is False
    assert finding.total_site_count == 0


def test_dipendenza_di_runtime_usata_solo_nei_test_non_e_raggiungibile(index):
    """Caso limite: dichiarata in `dependencies` ma di fatto usata solo nei test."""
    finding = index.analyse("lodash")
    assert finding.runtime_declared is True
    assert finding.reachable is False


def test_pacchetto_non_dichiarato_restituisce_none(index):
    assert index.analyse("pacchetto-inesistente") is None


def test_trigger_richiede_marcatore_di_vulnerabilita(index):
    """Citare una libreria non basta: senza marcatore il percorso non si attiva,
    altrimenti una change request verrebbe dirottata per errore."""
    assert di.analyse_ticket("Aggiungere un pulsante alla pagina", index) is None
    assert di.analyse_ticket("Refactor del modulo cors per leggibilita'", index) is None
    assert di.analyse_ticket("CVE-2024-1 in cors: ReDoS sull'origin", index) is not None


def test_trigger_richiede_pacchetto_noto(index):
    assert di.analyse_ticket("CVE-2024-9 critica, CVSS 9.8, aggiornare subito", index) is None


def test_punti_di_chiamata_limitati_e_ordinati_per_grado(index):
    finding = di.analyse_ticket(
        "CVE-2024-1: prototype pollution in express-session",
        index,
        degree={"api-server/src/app.ts": 42},
        max_call_sites=1,
    )
    assert len(finding.call_sites) == 1


def test_selezione_e_invariante_alla_riformulazione(index):
    """La proprieta' che il percorso deterministico garantisce e che il ranking
    semantico non garantisce: due formulazioni diverse dello stesso avviso
    producono esattamente la stessa selezione."""
    a = di.analyse_ticket("CVE-2024-1: prototype pollution in express-session", index)
    b = di.analyse_ticket(
        "Allarme sicurezza CVSS 9.1 su express-session, dipendenza diretta del backend", index
    )
    assert a.files == b.files


def test_specificatori_relativi_ignorati(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18"}}), encoding="utf-8")
    (tmp_path / "a.ts").write_text('import x from "./locale";\nimport React from "react";\n', encoding="utf-8")
    idx = di.DependencyIndex(tmp_path)
    finding = idx.analyse("react")
    assert finding.call_sites == ["a.ts"]


def test_scope_e_sottopercorsi_riducono_al_pacchetto(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"@scope/pkg": "^1"}}), encoding="utf-8"
    )
    (tmp_path / "a.ts").write_text('import y from "@scope/pkg/sub/deep";\n', encoding="utf-8")
    idx = di.DependencyIndex(tmp_path)
    assert idx.analyse("@scope/pkg").call_sites == ["a.ts"]
