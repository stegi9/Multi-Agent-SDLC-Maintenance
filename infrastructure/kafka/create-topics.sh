#!/bin/bash
# Crea i topic Kafka della piattaforma. Eseguito dal container kafka-init in docker-compose.
set -e

BOOTSTRAP="${KAFKA_BOOTSTRAP_SERVERS:-kafka:9092}"

TOPICS=(
  documents.uploaded
  documents.validated
  tickets.raw
  tickets.prioritized
  impact.analyzed
  workorders.approved
  workorders.generated
  code.generated
  code.validated
  docs.generated
  tests.executed
  rag.normalize
  events.dlq
)

echo "Attendo che Kafka sia pronto su $BOOTSTRAP..."
until /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server "$BOOTSTRAP" > /dev/null 2>&1; do
  sleep 2
done

for topic in "${TOPICS[@]}"; do
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists --topic "$topic" --partitions 3 --replication-factor 1
  echo "Topic creato/verificato: $topic"
done

echo "Tutti i topic sono pronti."
