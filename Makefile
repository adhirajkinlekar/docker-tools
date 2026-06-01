# Root Docker helper for all sample projects
# Usage: make <target>

.PHONY: help \
        kib kib-down \
        kafka kafka-down \
        mongo mongo-down \
        minio minio-down \
        nginx nginx-down \
        typescript typescript-down \
        swarm swarm-down \
        postgres postgres-down \
        redis redis-down \
        prometheus-grafana prometheus-grafana-down \
        rabbitmq rabbitmq-down \
        keycloak keycloak-down \
        plg plg-down \
        jaeger jaeger-down \
        sonarqube sonarqube-down \
        portainer portainer-down \
        fastapi fastapi-down

help:
	@echo ""
	@echo "Available targets:"
	@echo ""
	@echo "  Databases        : postgres  redis  mongo  minio"
	@echo "  Messaging        : kafka  rabbitmq"
	@echo "  Observability    : kib  prometheus-grafana  plg  jaeger"
	@echo "  Auth & Security  : keycloak  sonarqube"
	@echo "  Web & Apps       : nginx  typescript  fastapi"
	@echo "  Infrastructure   : swarm  portainer"
	@echo ""
	@echo "Append '-down' to any target to stop it (e.g. make postgres-down)"
	@echo ""

# ── Existing stacks ────────────────────────────────────────────────────────────

kib:
	docker-compose -f KIB/docker-compose.yml up -d --build

kib-down:
	docker-compose -f KIB/docker-compose.yml down

kafka:
	docker-compose -f kafka/docker-compose.yml up -d --build

kafka-down:
	docker-compose -f kafka/docker-compose.yml down

mongo:
	docker-compose -f mongo/docker-compose.yml up -d

mongo-down:
	docker-compose -f mongo/docker-compose.yml down

minio:
	docker-compose -f Minio/docker-compose.yml up -d

minio-down:
	docker-compose -f Minio/docker-compose.yml down

nginx:
	docker-compose -f nginx/docker-compose.yml up -d --build

nginx-down:
	docker-compose -f nginx/docker-compose.yml down

typescript:
	docker-compose -f typescript/docker-compose.yml up -d --build

typescript-down:
	docker-compose -f typescript/docker-compose.yml down

swarm:
	@echo "Deploying Swarm stack mystack. Ensure Docker Swarm is initialized and the manager node is active."
	docker stack deploy -c swarm/docker-compose.yml mystack

swarm-down:
	docker stack rm mystack

# ── New stacks ─────────────────────────────────────────────────────────────────

postgres:
	docker-compose -f postgres/docker-compose.yml up -d

postgres-down:
	docker-compose -f postgres/docker-compose.yml down

redis:
	docker-compose -f redis/docker-compose.yml up -d

redis-down:
	docker-compose -f redis/docker-compose.yml down

prometheus-grafana:
	docker-compose -f prometheus-grafana/docker-compose.yml up -d

prometheus-grafana-down:
	docker-compose -f prometheus-grafana/docker-compose.yml down

rabbitmq:
	docker-compose -f rabbitmq/docker-compose.yml up -d --build

rabbitmq-down:
	docker-compose -f rabbitmq/docker-compose.yml down

keycloak:
	docker-compose -f keycloak/docker-compose.yml up -d

keycloak-down:
	docker-compose -f keycloak/docker-compose.yml down

plg:
	docker-compose -f plg/docker-compose.yml up -d

plg-down:
	docker-compose -f plg/docker-compose.yml down

jaeger:
	docker-compose -f jaeger/docker-compose.yml up -d

jaeger-down:
	docker-compose -f jaeger/docker-compose.yml down

sonarqube:
	docker-compose -f sonarqube/docker-compose.yml up -d

sonarqube-down:
	docker-compose -f sonarqube/docker-compose.yml down

portainer:
	docker-compose -f portainer/docker-compose.yml up -d

portainer-down:
	docker-compose -f portainer/docker-compose.yml down

fastapi:
	docker-compose -f fastapi/docker-compose.yml up -d --build

fastapi-down:
	docker-compose -f fastapi/docker-compose.yml down
