# Docker Samples Repository

> A curated collection of Dockerfiles, Docker Compose stacks, and sample apps for local development and learning.

This repository contains ready-to-run Docker tooling for databases, storage, messaging, web apps, and orchestration patterns.

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
4. Start a sample stack:
   ```bash
   make mongo
   ```

## 📦 Available Samples

| Directory | Description |
|---|---|
| `k8s/` | Kubernetes service images for local build and deployment manifests |
| `swarm/` | Docker Swarm sample stack with Traefik reverse proxy |
| `kafka/` | Kafka + Zookeeper with a sample Node.js producer service |
| `mongo/` | MongoDB replica set sample for transaction-capable development |
| `Minio/` | MinIO object storage sample with console support |
| `KIB/` | InfluxDB + Grafana stack with a Node.js API sample |
| `nginx/` | React + Vite + NGINX sample app |
| `typescript/` | TypeScript Node.js app with multi-stage Docker build |

## 📁 Project Structure

```
/ (repo root)
├── Makefile
├── README.md
├── .dockerignore
├── KIB/
├── Minio/
├── kafka/
├── mongo/
├── nginx/
├── swarm/
├── typescript/
└── k8s/
```

## 🔧 Recommended Commands

- `make kib`
- `make kafka`
- `make mongo`
- `make minio`
- `make nginx`
- `make typescript`
- `make swarm`

For cleanup:
- `make kib-down`
- `make kafka-down`
- `make mongo-down`
- `make minio-down`
- `make nginx-down`
- `make typescript-down`
- `make swarm-down`

> Note: `swarm` uses `docker stack deploy` and requires a Swarm manager node.
 
 