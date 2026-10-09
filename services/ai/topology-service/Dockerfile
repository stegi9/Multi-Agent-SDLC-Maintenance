# Build context: la root del repository (per includere la libreria shared/)
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY services/ai/topology-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# graphify: grafo di conoscenza del codice (estrazione AST, on-premise)
RUN pip install --no-cache-dir graphifyy

COPY shared/ ./shared/
COPY services/ai/topology-service/app ./app

ENV PYTHONPATH=/app
ENV SERVICE_NAME=topology-service
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
