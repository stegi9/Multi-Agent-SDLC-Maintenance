"""Analisi di raggiungibilita' delle dipendenze (percorso dedicato ai ticket CVE).

Perche' esiste questo modulo
----------------------------
La selezione del contesto guidata dalla similarita' semantica fallisce
sistematicamente sui ticket di vulnerabilita': un avviso formulato a livello di
*dipendenza* non ha alcuna somiglianza testuale con il manifesto che lo risolve.
Il file `package.json` non parla di vulnerabilita', parla di versioni, e il
ranking ibrido finisce per agganciare falsi amici lessicali (il termine
"session" di `express-session` recupera i file di dominio delle sessioni di
cassa).

La distanza non e' semantica ma ontologica, quindi non si colma affinando il
ranking. Serve un meccanismo di natura diversa: risalire dalla dipendenza
dichiarata nel manifesto ai punti del codice applicativo che la importano.

Come funziona
-------------
Tre indici, tutti deterministici e senza alcuna invocazione del modello:

1. `manifest_index()`   pacchetto -> [(manifesto, sezione)]
2. `import_sites()`     pacchetto -> [file che lo importano]
3. `_is_application()`  filtro che distingue il codice applicativo da test e
   configurazioni

Il terzo e' il discriminante che rende corretta la nozione di raggiungibilita':
un pacchetto importato solo dalla suite di test non e' raggiungibile dal codice
di produzione, e infatti il suo aggiornamento tocca il solo manifesto.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Cartelle che non contengono codice del progetto.
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "coverage", "graphify-out", ".next", "out"}

# Estensioni dei sorgenti in cui cercare gli import.
_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")

# Sezioni del manifesto che dichiarano una dipendenza.
_RUNTIME_SECTION = "dependencies"
_DECLARING_SECTIONS = (_RUNTIME_SECTION, "devDependencies", "peerDependencies", "optionalDependencies")

# Specificatore di modulo in `import x from "pkg"`, `require("pkg")`, `import("pkg")`.
_IMPORT_RE = re.compile(r"""(?:from|require\(|import\()\s*['"]([^'"]+)['"]""")

# File che NON sono codice applicativo: se una dipendenza compare solo qui, non
# e' raggiungibile in produzione.
_NON_APPLICATION_RE = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|e2e|cypress|fixtures?|mocks?|stories)/"
    r"|\.(test|spec|stories)\.[jt]sx?$"
    r"|(^|/)[\w.-]*\.config\.[jt]s$"
    r"|(^|/)(vitest|jest|playwright|karma)\.[\w.]*$"
)

# Marcatori che qualificano il ticket come segnalazione di vulnerabilita'.
_VULNERABILITY_RE = re.compile(r"\bcve\b|cvss|vulnerabilit|exploit|advisory|security\s+advisory", re.I)

# Quanti punti di chiamata proporre oltre al manifesto. Tenuto basso di
# proposito: il Work Order deve indicare dove intervenire, non elencare ogni
# occorrenza.
MAX_CALL_SITES = 2


@dataclass
class DependencyFinding:
    """Esito dell'analisi di raggiungibilita' per un ticket di vulnerabilita'."""

    package: str
    manifests: list[str]
    call_sites: list[str]
    reachable: bool
    runtime_declared: bool
    application_site_count: int = 0
    total_site_count: int = 0
    sections: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        """File da presentare all'agente: il manifesto, piu' i punti di chiamata
        solo se la dipendenza e' davvero raggiungibile dal codice applicativo."""
        sites = self.call_sites if self.reachable else []
        return list(dict.fromkeys([*self.manifests, *sites]))

    def explain(self) -> str:
        if self.reachable:
            return (
                f"Dipendenza '{self.package}' dichiarata in {', '.join(self.sections)} e "
                f"raggiungibile dal codice applicativo ({self.application_site_count} punti di "
                f"chiamata su {self.total_site_count} occorrenze totali): oltre al manifesto "
                f"va verificato l'uso nei file che la importano."
            )
        if not self.runtime_declared:
            return (
                f"Dipendenza '{self.package}' dichiarata solo in {', '.join(self.sections)}: "
                f"non entra nel bundle di produzione, l'intervento si esaurisce nel manifesto."
            )
        return (
            f"Dipendenza '{self.package}' dichiarata in {', '.join(self.sections)} ma priva di "
            f"punti di chiamata nel codice applicativo ({self.total_site_count} occorrenze, tutte "
            f"in test o configurazioni): l'intervento si esaurisce nel manifesto."
        )


def _is_application(rel_path: str) -> bool:
    """True se il file appartiene al codice applicativo (non test, non configurazione)."""
    return not _NON_APPLICATION_RE.search(rel_path)


def _package_of(specifier: str) -> str | None:
    """Nome del pacchetto da uno specificatore di import.

    Ignora i percorsi relativi e assoluti (non sono dipendenze esterne) e
    riduce i sottopercorsi al nome del pacchetto, tenendo conto degli scope:
    `@scope/pkg/sub` -> `@scope/pkg`, `pkg/sub` -> `pkg`.
    """
    if not specifier or specifier.startswith((".", "/")):
        return None
    parts = specifier.split("/")
    if specifier.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


def _walk(root: Path):
    """Percorre il repository saltando le cartelle che non contengono progetto."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".venv")]
        for name in filenames:
            yield Path(dirpath) / name


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace(os.sep, "/")


class DependencyIndex:
    """Indici dei manifesti e dei punti di chiamata, con cache invalidata sul mtime.

    La costruzione richiede una passata sul repository, quindi il risultato viene
    conservato e ricalcolato solo quando un manifesto cambia. E' la stessa
    strategia con cui il Mappatore conserva topologia e grafo.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root)
        self._packages: dict[str, list[tuple[str, str]]] = {}
        self._sites: dict[str, list[str]] = {}
        self._signature: tuple | None = None

    # ---------------------------------------------------------------- costruzione

    def _manifest_signature(self) -> tuple:
        """Impronta dei manifesti: se cambia, gli indici vanno ricostruiti."""
        stamps = []
        for path in _walk(self.root):
            if path.name == "package.json":
                try:
                    stamps.append((_relative(path, self.root), path.stat().st_mtime_ns))
                except OSError:
                    continue
        return tuple(sorted(stamps))

    def _build(self) -> None:
        packages: dict[str, list[tuple[str, str]]] = {}
        sites: dict[str, list[str]] = {}

        for path in _walk(self.root):
            rel = _relative(path, self.root)
            if path.name == "package.json":
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(manifest, dict):
                    continue
                for section in _DECLARING_SECTIONS:
                    for name in (manifest.get(section) or {}):
                        packages.setdefault(name, []).append((rel, section))
            elif path.suffix in _SOURCE_SUFFIXES:
                try:
                    source = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for specifier in _IMPORT_RE.findall(source):
                    package = _package_of(specifier)
                    if not package:
                        continue
                    bucket = sites.setdefault(package, [])
                    if rel not in bucket:
                        bucket.append(rel)

        self._packages = packages
        self._sites = sites
        logger.info(
            "Indice delle dipendenze: %d pacchetti dichiarati, %d con punti di chiamata",
            len(packages),
            len(sites),
        )

    def refresh(self) -> None:
        """Ricostruisce gli indici solo se un manifesto e' cambiato."""
        signature = self._manifest_signature()
        if signature and signature == self._signature:
            return
        self._build()
        self._signature = signature

    # ---------------------------------------------------------------- consultazione

    @property
    def packages(self) -> dict[str, list[tuple[str, str]]]:
        self.refresh()
        return self._packages

    def detect_packages(self, text: str) -> list[str]:
        """Nomi di pacchetto citati nel testo, cercati a VOCABOLARIO CHIUSO.

        Non si tenta di riconoscere un nome di libreria in linguaggio naturale: si
        confrontano i soli 100-200 nomi realmente dichiarati nei manifesti del
        repository. E' questo che rende il riconoscimento esatto e insensibile
        alla riformulazione del ticket.

        L'ordinamento per lunghezza decrescente evita che un nome breve contenuto
        in uno lungo prevalga (`cors` dentro `connect-pg-simple` non deve vincere).
        """
        lowered = text.lower()
        found: list[str] = []
        for name in sorted(self.packages, key=len, reverse=True):
            pattern = r"(?<![\w/@-])" + re.escape(name.lower()) + r"(?![\w-])"
            if re.search(pattern, lowered):
                found.append(name)
        return found

    def analyse(self, package: str) -> DependencyFinding | None:
        """Raggiungibilita' di un pacchetto dal codice applicativo."""
        self.refresh()
        declarations = self._packages.get(package)
        if not declarations:
            return None
        manifests = list(dict.fromkeys(m for m, _ in declarations))
        sections = sorted({s for _, s in declarations})
        runtime_declared = any(s == _RUNTIME_SECTION for _, s in declarations)

        all_sites = self._sites.get(package, [])
        application_sites = [p for p in all_sites if _is_application(p)]
        return DependencyFinding(
            package=package,
            manifests=manifests,
            call_sites=application_sites,
            reachable=runtime_declared and bool(application_sites),
            runtime_declared=runtime_declared,
            application_site_count=len(application_sites),
            total_site_count=len(all_sites),
            sections=sections,
        )


def looks_like_vulnerability(text: str) -> bool:
    """True se il testo si presenta come segnalazione di vulnerabilita'."""
    return bool(_VULNERABILITY_RE.search(text))


def analyse_ticket(
    text: str,
    index: DependencyIndex,
    degree: dict[str, int] | None = None,
    max_call_sites: int = MAX_CALL_SITES,
) -> DependencyFinding | None:
    """Percorso dedicato ai CVE: dal testo del ticket ai file su cui intervenire.

    Restituisce None quando il ticket non e' una segnalazione di vulnerabilita'
    oppure non cita alcun pacchetto dichiarato nel repository: in quel caso il
    chiamante prosegue con il ranking ibrido ordinario.

    La doppia condizione (marcatore di vulnerabilita' E pacchetto noto) e' cio'
    che evita i falsi positivi: una change request che nomini di sfuggita una
    libreria non deve dirottare la selezione su questo percorso.
    """
    if not looks_like_vulnerability(text):
        return None
    candidates = index.detect_packages(text)
    if not candidates:
        return None

    # Fra piu' pacchetti citati si preferisce quello effettivamente raggiungibile:
    # e' il piu' probabile soggetto dell'avviso.
    findings = [f for f in (index.analyse(name) for name in candidates) if f is not None]
    if not findings:
        return None
    finding = next((f for f in findings if f.reachable), findings[0])

    ranked = sorted(finding.call_sites, key=lambda p: -(degree or {}).get(p, 0))
    finding.call_sites = ranked[:max_call_sites]
    return finding
