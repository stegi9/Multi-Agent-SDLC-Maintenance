"""Topic Kafka della piattaforma.

Unica fonte di verità per i nomi dei topic: tutti i microservizi importano
da qui per evitare stringhe duplicate e refusi.
"""

# Ingestion documentale
DOCUMENTS_UPLOADED = "documents.uploaded"      # documento caricato dallo Scrum Master
DOCUMENTS_VALIDATED = "documents.validated"    # documento approvato dall'Agente di Coerenza

# Ciclo di vita dei ticket
TICKETS_RAW = "tickets.raw"                    # ticket importati con aging zero
TICKETS_PRIORITIZED = "tickets.prioritized"    # output dell'Agente Valutatore
IMPACT_ANALYZED = "impact.analyzed"            # output dell'Agente di Planning

# Work Order
WORKORDERS_APPROVED = "workorders.approved"    # approvazione dello Scrum Master
WORKORDERS_GENERATED = "workorders.generated"  # WO pronti per l'Agente Sviluppatore

# Codice e documentazione
CODE_GENERATED = "code.generated"              # JSON delle modifiche prodotto dall'Agente Sviluppatore
CODE_VALIDATED = "code.validated"              # codice validato dallo Scrum Master e unito al repo
DOCS_GENERATED = "docs.generated"              # documentazione prodotta dall'Agente Documentatore

# Verifica automatica
TESTS_EXECUTED = "tests.executed"              # esito della suite eseguita dall'Agente Tester

# Normalizzazione del RAG
RAG_NORMALIZE = "rag.normalize"                # richiesta di spostare feature da RAG Futuro a RAG Principale

# Resilienza
EVENTS_DLQ = "events.dlq"                      # dead-letter queue dei messaggi non processabili

ALL_TOPICS = [
    DOCUMENTS_UPLOADED,
    DOCUMENTS_VALIDATED,
    TICKETS_RAW,
    TICKETS_PRIORITIZED,
    IMPACT_ANALYZED,
    WORKORDERS_APPROVED,
    WORKORDERS_GENERATED,
    CODE_GENERATED,
    CODE_VALIDATED,
    DOCS_GENERATED,
    TESTS_EXECUTED,
    RAG_NORMALIZE,
    EVENTS_DLQ,
]
