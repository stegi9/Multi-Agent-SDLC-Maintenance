# Build context: la root del repository (per includere la libreria shared/)
FROM python:3.12-slim

WORKDIR /app

COPY services/core/rag-db-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY services/core/rag-db-manager/app ./app

ENV PYTHONPATH=/app
ENV SERVICE_NAME=rag-db-manager
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
