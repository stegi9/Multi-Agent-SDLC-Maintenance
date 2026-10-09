# Build context: la root del repository (per includere la libreria shared/)
#
# Immagine ibrida: Python per il microservizio FastAPI, Node per l'esecuzione
# della suite di test del repository bersaglio. È l'unico servizio della
# piattaforma che ha bisogno di entrambi i runtime, perché è l'unico che esegue
# davvero il codice prodotto dagli agenti.
#
# I binari Node si copiano dall'immagine ufficiale invece di installarli da un
# repository esterno: le due immagini condividono la stessa base Debian
# (bookworm), quindi la libreria di sistema è compatibile, e la versione resta
# riproducibile nel tempo.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && corepack enable

# Gestore di pacchetti preinstallato, così il servizio è utilizzabile anche
# prima di aver scaricato alcunché.
#
# Nota sulla versione effettiva: quando il repository bersaglio non dichiara un
# campo "packageManager", corepack risolve da sé la versione più recente una
# volta entrato nella cartella del progetto, e usa quella al posto di questa.
# È il comportamento desiderato: il repository bersaglio dichiara politiche
# (età minima delle release, controlli sulla catena di fornitura) che
# richiedono una versione recente del gestore di pacchetti.
ARG PNPM_VERSION=10.15.0
RUN corepack prepare pnpm@${PNPM_VERSION} --activate

COPY services/ai/test-runner-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY services/ai/test-runner-service/app ./app

ENV PYTHONPATH=/app
ENV SERVICE_NAME=test-runner-service
# Corepack non deve mai chiedere conferme interattive né bloccarsi su una
# versione diversa da quella attesa: il container gira senza terminale.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV COREPACK_ENABLE_STRICT=0
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
