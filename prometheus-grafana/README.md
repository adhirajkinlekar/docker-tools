# 📊 Prometheus + Grafana Metrics Stack

A production-ready metrics monitoring stack using **Prometheus** for scraping and storing metrics, **Grafana** for visualization, and **Node Exporter** for host-level system metrics.

## 📦 Services Included

| Service         | Description                                     | Port  |
|-----------------|-------------------------------------------------|-------|
| `prometheus`    | Metrics collection and storage engine           | 9090  |
| `grafana`       | Dashboard and visualization UI                  | 3000  |
| `node-exporter` | Host CPU, memory, disk metrics exporter         | 9100  |

---

## 📂 Project Structure

```
prometheus-grafana/
├── docker-compose.yml
├── prometheus.yml                          # Prometheus scrape config
├── grafana/
│   └── provisioning/
│       └── datasources/
│           └── prometheus.yml              # Auto-provisioned data source
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **Prometheus UI** → [http://localhost:9090](http://localhost:9090)
- **Grafana UI** → [http://localhost:3000](http://localhost:3000)
  - Login: `admin` / `admin`
- **Node Exporter metrics** → [http://localhost:9100/metrics](http://localhost:9100/metrics)

Prometheus is pre-configured as a Grafana data source via provisioning — no manual setup required.

---

## 📊 Import a Dashboard

1. Open Grafana → **+** → **Import**.
2. Enter a dashboard ID from [grafana.com/dashboards](https://grafana.com/grafana/dashboards/):
   - `1860` — Node Exporter Full (system metrics)
   - `3662` — Prometheus 2.0 Overview
3. Select **Prometheus** as the data source.
4. Click **Import**.

---

## ⚙️ Adding Your Own Scrape Targets

Edit `prometheus.yml` and add your service under `scrape_configs`:

```yaml
- job_name: 'my-service'
  static_configs:
    - targets: ['my-service:8080']
```

Then reload Prometheus:

```bash
curl -X POST http://localhost:9090/-/reload
```

---

## ⚙️ Environment Variables

| Variable          | Default  | Description              |
|-------------------|----------|--------------------------|
| `GF_ADMIN_USER`   | `admin`  | Grafana admin username   |
| `GF_ADMIN_PASSWORD` | `admin`| Grafana admin password   |
| `GRAFANA_PORT`    | `3000`   | Grafana exposed port     |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers
docker-compose down -v    # Stop and remove volumes
```

---

## 🔗 Additional Resources

- [Prometheus Documentation](https://prometheus.io/docs/)
- [Grafana Documentation](https://grafana.com/docs/)
- [Node Exporter](https://github.com/prometheus/node_exporter)
