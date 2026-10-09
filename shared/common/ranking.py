"""Ordinamento dei file candidati rispetto a un ticket.

Perche' e' un modulo condiviso
------------------------------
Due agenti devono rispondere alla stessa domanda, "quali file di questo
repository riguardano questo ticket": l'Agente Sviluppatore per decidere cosa
leggere, l'Agente di Planning per stimare l'impatto. Finche' i due hanno usato
criteri diversi, i loro risultati non erano confrontabili ne' coerenti.

Ordinando i candidati per CENTRALITA' nel grafo si mostrerebbero al modello
solo i file piu' connessi del repository: il modello non potrebbe scegliere i
file giusti perche' non li vedrebbe, e sceglierebbe gli hub perche' sarebbero
gli unici disponibili, facendo esplodere la stima d'impatto.

Qui la scelta e' una sola, per entrambi: si ordina per PERTINENZA al ticket,
combinando tre segnali, e la centralita' resta come contributo minore invece che
come criterio dominante.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Vocabolari di supporto
# ---------------------------------------------------------------------------

# Parole troppo generiche per discriminare un percorso di file.
STOPWORDS = {
    "cambiare", "modificare", "aggiungere", "rimuovere", "creare", "gestire", "aggiornare",
    "correggere", "sistemare", "questo", "questa", "della", "dello", "delle", "degli", "nel",
    "nella", "con", "per", "una", "uno", "gli", "del", "dei", "che", "non", "più", "sono",
    "essere", "viene", "change", "modify", "add", "remove", "create", "update", "fix", "should",
    "from", "this", "that", "with", "into", "when", "colore", "colori", "color",
}

# Ponte italiano -> inglese per i termini ricorrenti: i ticket sono in italiano,
# i percorsi del codice in inglese.
SYNONYMS = {
    "bottone": "button", "pulsante": "button", "pagina": "page", "accesso": "login",
    "utente": "user", "utenti": "user", "modulo": "form", "elenco": "list", "tabella": "table",
    "impostazioni": "settings", "ricerca": "search", "notifica": "notification",
}

# Funzionali e generici dell'italiano tecnico: non discriminano alcun file, ma
# diluiscono il confronto fra due riformulazioni dello stesso ticket, perche'
# ciascuna ne usa di propri. Toglierli lascia emergere i termini di dominio.
FILLER = {
    "alla", "alle", "allo", "agli", "dalla", "dalle", "dallo", "dagli", "dall", "sulla",
    "sulle", "sullo", "sugli", "nelle", "nello", "negli", "come", "dove", "quando", "quand",
    "perche", "poiche", "mentre", "anche", "ancora", "sempre", "solo", "soltanto", "invece",
    "oppure", "quindi", "dunque", "allora", "cosi", "molto", "poco", "tutto", "tutti", "tutte",
    "ogni", "alcuni", "alcune", "altro", "altra", "altri", "altre", "stesso", "stessa",
    "avere", "fare", "dare", "stare", "andare", "venire", "potere", "dovere", "volere",
    "sapere", "vedere", "deve", "devono", "possono", "vengono", "risulta", "risultano",
    "succede", "accade", "capita", "compare", "appare", "mostra", "restituisce", "risponde",
    "funziona", "consente", "permette", "richiede", "serve", "occorre", "vogliamo",
    "richiesta", "problema", "errore", "error", "caso", "casi", "volta", "volte", "parte",
    "punto", "modo", "sistema", "applicazione", "attuale", "attualmente", "presente",
    "seguente", "relativo", "relativa", "possibile", "necessario", "corretto", "corretta",
    "dentro", "sopra", "sotto", "prima", "dopo", "durante", "tramite", "mediante", "verso",
    "senza", "contro", "oltre", "entro", "presso", "cannot", "undefined", "properties",
    "reading", "read", "stack", "trace",
}

# Suffissi flessivi dell'italiano, dal piu' lungo al piu' corto. Non e' un
# lemmatizzatore: e' troncamento controllato, serve a rendere confrontabili le
# stringhe. NON si include "che"/"chi", altrimenti `politiche` darebbe `politi`
# mentre `politica` darebbe `politic`.
SUFFIXES = (
    "azioni", "azione", "amenti", "amento", "atori", "atore", "atrice",
    "zioni", "zione", "abili", "abile", "ibili", "ibile", "mente",
    "anti", "ante", "enti", "ente", "ini", "ine", "oni", "one", "ori", "ore",
    "he", "hi", "i", "e", "o", "a",
)
MIN_STEM = 4

# Token gia' invarianti, da conservare alla lettera.
TECHNICAL_RE = re.compile(
    r"[\w./-]+\.[a-z]{2,4}\b"          # nomi di file e percorsi con estensione
    r"|/[\w/-]{3,}"                     # rotte e percorsi assoluti
    r"|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b"   # camelCase
    r"|\b[A-Z][a-zA-Z]*Error\b"         # TypeError, RangeError, ...
    r"|\bCVE-[\w-]+\b"
    r"|\b[A-Z]{3,}\b"                   # sigle: URL, API, SQL
)

# Verbi d'azione ricondotti a forma canonica.
ACTIONS = {
    "corregg": "fix", "correz": "fix", "sistem": "fix", "risolv": "fix", "ripar": "fix",
    "aggiung": "add", "introdurr": "add", "introduc": "add", "creare": "add", "nuov": "add",
    "rimuov": "remove", "eliminar": "remove", "cancellar": "remove",
    "modific": "change", "cambiar": "change", "aggiornar": "change", "configurar": "change",
}

# Pesi del ranking ibrido. Il semantico domina perche' e' l'unico che supera il
# divario linguistico fra ticket italiano e codice inglese; il lessicale premia i
# match esatti che l'embedding a volte sottopesa; la centralita' resta un
# contributo di rottura della parita', non un criterio.
W_SEMANTIC = 0.6
W_LEXICAL = 0.3
W_DEGREE = 0.1
DEGREE_CAP = 20


# ---------------------------------------------------------------------------
# Funzioni
# ---------------------------------------------------------------------------

def stem(word: str) -> str:
    """Radice approssimata: toglie il suffisso flessivo piu' lungo che lascia
    almeno `MIN_STEM` caratteri."""
    for suffix in SUFFIXES:
        if len(word) - len(suffix) >= MIN_STEM and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def ticket_keywords(text: str) -> list[str]:
    """Parole chiave dal testo del ticket (>=4 lettere, senza stopword), con sinonimi EN."""
    kws: set[str] = set()
    for w in re.findall(r"[a-zA-Zàèéìòù]{4,}", text.lower()):
        if w in STOPWORDS:
            continue
        kws.add(w)
        if w in SYNONYMS:
            kws.add(SYNONYMS[w])
    return list(kws)


def normalize_ticket(text: str, *, stem_words: bool = True) -> str:
    """Query canonica: stessa stringa per formulazioni diverse dello stesso problema.

    Conserva cio' che due parafrasi hanno in comune e scarta cio' in cui
    differiscono: identificatori tecnici alla lettera, termini di dominio privati
    dei riempitivi e ordinati alfabeticamente, verbo d'azione canonico.
    L'ordinamento alfabetico e' cio' che rende il risultato indipendente da come
    la frase e' costruita.

    `stem_words` distingue i due canali del ranking: il lessicale lavora meglio
    sulle radici (il confronto e' per sottostringa, `boll` aggancia sia
    `bolle.ts` sia `bollaDelivery.ts`), il semantico sulle parole intere.
    """
    if not text:
        return ""
    technical = sorted({m.lower() for m in TECHNICAL_RE.findall(text)})

    terms: set[str] = set()
    actions: set[str] = set()
    for word in re.findall(r"[a-zA-Zàèéìòùç]{4,}", text.lower()):
        # I verbi d'azione vanno riconosciuti PRIMA del filtro: molti compaiono
        # fra le stopword perche' come termini di ricerca non discriminano, ma
        # come intento si'.
        action = next((c for p, c in ACTIONS.items() if word.startswith(p)), None)
        if action:
            actions.add(action)
            continue
        if word in STOPWORDS or word in FILLER:
            continue
        if word in SYNONYMS:
            terms.add(SYNONYMS[word])
            continue
        candidate = stem(word) if stem_words else word
        if candidate not in FILLER and len(candidate) >= 3:
            terms.add(candidate)

    return " ".join([*technical, *sorted(terms), *sorted(actions)])


def lexical_relevance(path: str, keywords: list[str]) -> int:
    """Pertinenza di un percorso rispetto alle parole chiave del ticket.

    Il match sul nome del file pesa il doppio di quello sul percorso: `bolle.ts`
    e' piu' indicativo di `src/routes/qualcosa.ts` dentro una cartella `bolle`.
    """
    lowered = path.lower()
    base = lowered.rsplit("/", 1)[-1]
    score = 0
    for k in keywords:
        if k in base:
            score += 2
        elif k in lowered:
            score += 1
    return score


def hybrid_score(
    path: str,
    keywords: list[str],
    semantic: dict[str, float],
    degree: dict[str, int],
) -> float:
    """Punteggio ibrido di pertinenza, fra 0 e 1.

    Fonde i tre segnali che una selezione del contesto ha a disposizione:
    similarita' semantica fra ticket e contenuto del file, corrispondenza
    lessicale sul percorso, centralita' nel grafo delle dipendenze.
    """
    lex = min(lexical_relevance(path, keywords), 4) / 4.0
    sem = semantic.get(path, 0.0)
    deg = min(degree.get(path, 0), DEGREE_CAP) / DEGREE_CAP
    return W_SEMANTIC * sem + W_LEXICAL * lex + W_DEGREE * deg


def rank_paths(
    paths: list[str],
    ticket_text: str,
    semantic: dict[str, float],
    degree: dict[str, int],
    *,
    normalize: bool = True,
) -> list[tuple[str, float]]:
    """Percorsi ordinati per pertinenza decrescente, con il punteggio.

    E' il punto d'ingresso condiviso: entrambi gli agenti ordinano i candidati
    con questa funzione, cosi' la lista che il modello riceve e' la stessa.
    """
    query = normalize_ticket(ticket_text, stem_words=True) if normalize else ticket_text
    keywords = ticket_keywords(query)
    scored = [(p, hybrid_score(p, keywords, semantic, degree)) for p in paths]
    scored.sort(key=lambda x: -x[1])
    return scored
