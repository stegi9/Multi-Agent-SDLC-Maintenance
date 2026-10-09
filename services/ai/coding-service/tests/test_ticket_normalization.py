"""Query canonica del ticket (invarianza alla riformulazione)."""
import pytest

from app.services.executor import _stem, normalize_ticket


def _tokens(text: str, **kw) -> set[str]:
    return set(normalize_ticket(text, **kw).split())


# ------------------------------------------------------------------ radice

@pytest.mark.parametrize(
    ("prima", "seconda"),
    [
        ("bolla", "bolle"),
        ("politica", "politiche"),
        ("credito", "crediti"),
        ("mensile", "mensili"),
        ("amministrazione", "amministratori"),
    ],
)
def test_forme_flesse_convergono_sulla_stessa_radice(prima, seconda):
    assert _stem(prima) == _stem(seconda), f"{prima} e {seconda} devono avere la stessa radice"


def test_la_radice_non_scende_sotto_la_lunghezza_minima():
    assert _stem("area") == "area", "troncare qui produrrebbe una radice inutilizzabile"


# ------------------------------------------------------------------ canonizzazione

def test_ordine_delle_frasi_indifferente():
    a = normalize_ticket("Il volontario conferma la bolla. Il server risponde 500.")
    b = normalize_ticket("Il server risponde 500. Il volontario conferma la bolla.")
    assert a == b


def test_identificatori_tecnici_conservati_alla_lettera():
    tokens = _tokens("Errore in bolle.ts sulla rotta /api/bolle con TypeError")
    assert "bolle.ts" in tokens
    assert "/api/bolle" in tokens
    assert "typeerror" in tokens


def test_verbi_equivalenti_producono_la_stessa_azione():
    assert "fix" in _tokens("Correggere il calcolo")
    assert "fix" in _tokens("Sistemare il calcolo")
    assert "fix" in _tokens("Risolvere il calcolo")


def test_riempitivi_rimossi():
    tokens = _tokens("Quando l'utente conferma, il sistema restituisce un errore")
    assert "quando" not in tokens
    assert "restituisce" not in tokens
    assert "errore" not in tokens


def test_canale_lessicale_usa_le_radici_e_il_semantico_le_parole_intere():
    testo = "Conferma della bolla di consegna"
    assert "boll" in _tokens(testo, stem=True)
    assert "bolla" in _tokens(testo, stem=False)


def test_testo_vuoto_non_esplode():
    assert normalize_ticket("") == ""
    assert normalize_ticket("   ") == ""

