"""Osservabilità condivisa: espone le metriche Prometheus su /metrics.

Ogni microservizio chiama `setup_metrics(app)` nel proprio main.py. Le metriche
standard (conteggio richieste, latenza, richieste in corso) vengono etichettate per
metodo/rotta/stato; Prometheus distingue i servizi tramite l'etichetta `instance`.
"""
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator


def setup_metrics(app: FastAPI) -> None:
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/health"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
