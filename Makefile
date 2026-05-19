# Root Docker helper for all sample projects
# Usage: make <target>

.PHONY: help kib kafka mongo minio nginx typescript swarm swarm-down

help:
	@echo "Available targets: kib kafka mongo minio nginx typescript swarm swarm-down"

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
