# 🐳 Portainer — Docker Management UI

Portainer CE is a lightweight, open-source **web-based Docker management interface**. It lets you manage containers, images, volumes, networks, and stacks through a clean GUI — no CLI required.

## 📦 Services Included

| Service      | Description                                      | Port  |
|--------------|--------------------------------------------------|-------|
| `portainer`  | Portainer CE web UI for managing Docker          | 9443  |

---

## 📂 Project Structure

```
portainer/
├── docker-compose.yml
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **Portainer UI (HTTPS)** → [https://localhost:9443](https://localhost:9443)

On first launch, you will be prompted to **create an admin user**. Do this within 5 minutes or the instance will lock itself for security.

---

## 🛠️ What You Can Do

- **Containers** — Start, stop, restart, inspect logs, open a shell
- **Images** — Pull, build, delete images
- **Volumes** — Create and manage named volumes
- **Networks** — Inspect and manage Docker networks
- **Stacks** — Deploy and manage Docker Compose stacks via the UI
- **Environment** — Connect to remote Docker hosts or Kubernetes clusters

---

## 🔒 Security Note

Portainer mounts the Docker socket (`/var/run/docker.sock`) in **read-only** mode for discovery. Full management still works because Portainer uses the Docker API through the socket.

> ⚠️ Exposing the Docker socket gives the container root-equivalent access to your host. Use Portainer only in trusted local environments or behind authentication.

---

## ⚙️ Environment Variables

| Variable          | Default  | Description               |
|-------------------|----------|---------------------------|
| `PORTAINER_PORT`  | `9443`   | HTTPS management UI port  |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers (data preserved)
docker-compose down -v    # Stop and remove volumes (data loss!)
```

---

## 🔗 Additional Resources

- [Portainer Documentation](https://docs.portainer.io/)
- [Portainer CE GitHub](https://github.com/portainer/portainer)
