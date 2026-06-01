# 🔭 Jaeger Distributed Tracing

Jaeger is an open-source, end-to-end **distributed tracing** system originally built by Uber. It helps you track requests as they flow through microservices, identify bottlenecks, and debug latency issues.

This setup uses the `all-in-one` image which bundles the agent, collector, query service, and UI into a single container — perfect for local development. It also includes the official **HotROD** demo app to generate real traces out of the box.

## 📦 Services Included

| Service   | Description                                              | Port  |
|-----------|----------------------------------------------------------|-------|
| `jaeger`  | Jaeger all-in-one (agent + collector + query + UI)       | 16686 |
| `hotrod`  | HotROD ride-sharing demo app with built-in tracing       | 8080  |

---

## 📂 Project Structure

```
jaeger/
├── docker-compose.yml
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **Jaeger UI** → [http://localhost:16686](http://localhost:16686)
- **HotROD Demo App** → [http://localhost:8080](http://localhost:8080)

---

## 🧪 Generate Traces with HotROD

1. Open [http://localhost:8080](http://localhost:8080).
2. Click any of the ride buttons (e.g., **Rachel's car**) to trigger requests.
3. Open [http://localhost:16686](http://localhost:16686) → Select service `frontend` → **Find Traces**.
4. Click any trace to see the full span waterfall across services.

---

## 📡 Instrumenting Your Own Service

Send traces via **OpenTelemetry** (OTLP):

```bash
# OTLP gRPC endpoint
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# OTLP HTTP endpoint
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Or via the legacy **Jaeger HTTP Thrift** endpoint:

```bash
JAEGER_ENDPOINT=http://localhost:14268/api/traces
```

---

## 🔌 Exposed Ports

| Port        | Protocol | Purpose                        |
|-------------|----------|--------------------------------|
| `16686`     | HTTP     | Jaeger UI                      |
| `4317`      | gRPC     | OTLP gRPC receiver             |
| `4318`      | HTTP     | OTLP HTTP receiver             |
| `14268`     | HTTP     | Jaeger Thrift (legacy)         |
| `9411`      | HTTP     | Zipkin-compatible endpoint     |
| `6831`      | UDP      | Jaeger agent (compact thrift)  |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down
```

---

## 🔗 Additional Resources

- [Jaeger Documentation](https://www.jaegertracing.io/docs/)
- [OpenTelemetry](https://opentelemetry.io/docs/)
- [HotROD Demo](https://github.com/jaegertracing/jaeger/tree/main/examples/hotrod)
