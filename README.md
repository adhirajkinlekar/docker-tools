# Docker Samples Repository

> A curated collection of Dockerfiles, Docker Compose stacks, and sample apps for local development and learning.

This repository contains ready-to-run Docker tooling for databases, storage, messaging, observability, auth, web apps, and orchestration patterns.

## 🚀 Quick Start

1. Install Docker Engine and Docker Compose.
2. Clone the repo:
   ```bash
   git clone https://github.com/your-username/your-repo.git
   cd your-repo
   ```
3. Run the helper menu:
   ```bash
   make help
   ```
4. Start any sample stack:
   ```bash
   make postgres
   make redis
   make rabbitmq
   ```

## 📦 Available Samples

### 🗄️ Databases & Storage

| Directory | Description |
|---|---|
| `postgres/` | PostgreSQL 16 + pgAdmin 4 web UI |
| `mongo/` | MongoDB replica set sample for transaction-capable development |
| `redis/` | Redis 7 + RedisInsight GUI |
| `Minio/` | MinIO object storage sample with console support |

### 📬 Messaging

| Directory | Description |
|---|---|
| `kafka/` | Kafka + Zookeeper with a sample Node.js producer service |
| `rabbitmq/` | RabbitMQ 3.13 with management UI and a Node.js producer |

### 📊 Observability

| Directory | Description |
|---|---|
| `KIB/` | InfluxDB + Grafana stack with a Node.js API and k6 load testing |
| `prometheus-grafana/` | Prometheus + Grafana + Node Exporter metrics stack |
| `plg/` | PLG stack — Promtail + Loki + Grafana for log aggregation |
| `jaeger/` | Jaeger distributed tracing with HotROD demo app |

### 🔐 Auth & Code Quality

| Directory | Description |
|---|---|
| `keycloak/` | Keycloak 24 OAuth2/OIDC identity provider backed by PostgreSQL |
| `sonarqube/` | SonarQube 10 Community code quality analysis backed by PostgreSQL |

### 🌐 Web & Application Stacks

| Directory | Description |
|---|---|
| `nginx/` | React + Vite + NGINX sample app |
| `typescript/` | TypeScript Node.js app with multi-stage Docker build |
| `fastapi/` | Python FastAPI app with multi-stage Docker build |

### 🏗️ Infrastructure & Orchestration

| Directory | Description |
|---|---|
| `portainer/` | Portainer CE Docker management web UI |
| `swarm/` | Docker Swarm sample stack with Traefik reverse proxy |
| `k8s/` | Kubernetes service images for local build and deployment manifests |

## 📁 Project Structure

```
/ (repo root)
├── Makefile
├── README.md
├── .dockerignore
├── KIB/
├── Minio/
├── fastapi/
├── jaeger/
├── kafka/
├── keycloak/
├── k8s/
├── mongo/
├── nginx/
├── plg/
├── portainer/
├── postgres/
├── prometheus-grafana/
├── rabbitmq/
├── redis/
├── sonarqube/
├── swarm/
└── typescript/
```

## 🔧 Recommended Commands

```bash
# Databases
make postgres           make postgres-down
make redis              make redis-down
make mongo              make mongo-down
make minio              make minio-down

# Messaging
make kafka              make kafka-down
make rabbitmq           make rabbitmq-down

# Observability
make kib                make kib-down
make prometheus-grafana make prometheus-grafana-down
make plg                make plg-down
make jaeger             make jaeger-down

# Auth & Code Quality
make keycloak           make keycloak-down
make sonarqube          make sonarqube-down

# Web & Apps
make nginx              make nginx-down
make typescript         make typescript-down
make fastapi            make fastapi-down

# Infrastructure
make portainer          make portainer-down
make swarm              make swarm-down
```

> Note: `swarm` uses `docker stack deploy` and requires a Swarm manager node.