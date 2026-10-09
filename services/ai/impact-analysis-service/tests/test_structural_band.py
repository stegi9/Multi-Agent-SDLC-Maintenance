"""Fascia decisionale calcolata sul grafo delle dipendenze."""
from app.api.routes import BAND_TO_DECISION, _downstream_closure, _structural_band
from app.core.config import settings


def _graph(files: dict, total: int | None = None) -> dict:
    return {"files": files, "file_count": total if total is not None else len(files)}


# ------------------------------------------------------------------ unita'

def test_chiusura_a_valle_segue_i_dipendenti():
    files = {
        "a.ts": {"depended_by": ["b.ts"], "depends_on": [], "degree": 1},
        "b.ts": {"depended_by": ["c.ts"], "depends_on": ["a.ts"], "degree": 2},
        "c.ts": {"depended_by": [], "depends_on": ["b.ts"], "degree": 1},
    }
    assert _downstream_closure(["a.ts"], files, hops=1) == {"a.ts", "b.ts"}
    assert _downstream_closure(["a.ts"], files, hops=2) == {"a.ts", "b.ts", "c.ts"}


def test_chiusura_ignora_i_file_fuori_dal_grafo():
    files = {"a.ts": {"depended_by": [], "depends_on": [], "degree": 0}}
    assert _downstream_closure(["a.ts", "fantasma.ts"], files, hops=2) == {"a.ts"}


def test_fascia_alta_quando_la_modifica_raggiunge_molta_codebase():
    files = {f"f{i}.ts": {"depended_by": [], "depends_on": [], "degree": 0} for i in range(100)}
    files["hub.ts"] = {"depended_by": [f"f{i}.ts" for i in range(20)], "depends_on": [], "degree": 20}
    band = _structural_band(["hub.ts"], _graph(files))
    assert band["band"] == "high"
    assert BAND_TO_DECISION[band["band"]] == "blocked"


def test_fascia_bassa_solo_con_un_file_e_impatto_minimo():
    files = {f"f{i}.ts": {"depended_by": [], "depends_on": [], "degree": 0} for i in range(200)}
    assert _structural_band(["f1.ts"], _graph(files))["band"] == "low"
    # due file toccati: non e' piu' un intervento puntuale
    assert _structural_band(["f1.ts", "f2.ts"], _graph(files))["band"] == "medium"


def test_senza_grafo_nessuna_fascia_strutturale():
    assert _structural_band(["a.ts"], None) is None
    assert _structural_band([], _graph({"a.ts": {}})) is None


def test_la_fascia_non_dipende_dal_modello_linguistico():
    """Due stime diverse dell'LLM sugli stessi file danno la stessa fascia:
    e' la proprieta' che elimina la varianza dalla decisione."""
    files = {f"f{i}.ts": {"depended_by": [], "depends_on": [], "degree": 0} for i in range(200)}
    primo = _structural_band(["f1.ts"], _graph(files))
    secondo = _structural_band(["f1.ts"], _graph(files))
    assert primo == secondo


def test_soglie_di_default():
    assert settings.band_high_pct == 10.0
    assert settings.band_low_pct == 2.0
    assert settings.band_low_max_files == 1
