# 📋 PLG Stack — Promtail + Loki + Grafana

The **PLG stack** is a lightweight, cloud-native log aggregation system. It collects logs from Docker containers and system files via **Promtail**, stores them in **Loki**, and visualizes them in **Grafana**.

> Think of it as the "ELK stack but cheaper" — Loki indexes only metadata (labels), not full log text, making it far more resource-efficient.

## 📦 Services Included

| Service     | Description                                          | Port  |
|-------------|------------------------------------------------------|-------|
| `loki`      | Log storage and query engine                         | 3100  |
| `promtail`  | Log collector/shipper (reads Docker + system logs)   | 9080  |
| `grafana`   | Log exploration and dashboard UI                     | 3000  |

---

## 📂 Project Structure

```
plg/
├── docker-compose.yml
├── loki-config.yml                         # Loki server config
├── promtail-config.yml                     # Promtail scrape config
├── grafana/
│   └── provisioning/
│       └── datasources/
│           └── loki.yml                    # Auto-provisioned Loki datasource
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **Grafana UI** → [http://localhost:3000](http://localhost:3000)
  - Login: `admin` / `admin`
- **Loki API** → [http://localhost:3100](http://localhost:3100)

Loki is auto-provisioned as a Grafana data source — no manual setup needed.

---

## 🔍 Exploring Logs in Grafana

1. Open [http://localhost:3000](http://localhost:3000) → **Explore** (compass icon).
2. Select **Loki** as the data source.
3. Use LogQL to query logs:

```logql
# All logs from a specific container
{container="your-container-name"}

# Filter by log level
{container="api"} |= "ERROR"

# Count error rate over time
count_over_time({container="api"} |= "ERROR" [5m])
```

---

## 🧠 How It Works

1. **Promtail** tails Docker container logs via the Docker socket and system `/var/log` files.
2. Logs are shipped to **Loki** with labels (container name, service, stream).
3. **Grafana** queries Loki using **LogQL** and renders log panels and dashboards.

---

## ⚙️ Environment Variables

| Variable             | Default  | Description              |
|----------------------|----------|--------------------------|
| `GF_ADMIN_USER`      | `admin`  | Grafana admin username   |
| `GF_ADMIN_PASSWORD`  | `admin`  | Grafana admin password   |
| `GRAFANA_PORT`       | `3000`   | Grafana exposed port     |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers
docker-compose down -v    # Stop and remove volumes
```

---

## 🔗 Additional Resources

- [Loki Documentation](https://grafana.com/docs/loki/latest/)
- [Promtail Documentation](https://grafana.com/docs/loki/latest/send-data/promtail/)
- [LogQL Query Language](https://grafana.com/docs/loki/latest/query/)
