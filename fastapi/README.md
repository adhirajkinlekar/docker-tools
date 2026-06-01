# 🐍 Python + FastAPI Docker Setup

A **Python FastAPI** application containerized with a **multi-stage Dockerfile** supporting both development (hot-reload) and production (multi-worker) modes — mirroring the pattern used in the `typescript/` sample.

## 📦 Services Included

| Service | Description                                    | Port  |
|---------|------------------------------------------------|-------|
| `api`   | FastAPI app with Uvicorn ASGI server           | 8000  |

---

## 📂 Project Structure

```
fastapi/
├── api/
│   ├── Dockerfile          # Multi-stage: base → development → production
│   ├── main.py             # FastAPI application
│   ├── requirements.txt    # Python dependencies
│   └── .dockerignore
├── docker-compose.yml
└── README.md
```

---

## 🛠️ Dockerfile Stages

| Stage         | Purpose                                        |
|---------------|------------------------------------------------|
| `base`        | Installs Python dependencies                   |
| `development` | Hot-reload via `--reload`, mounts source files |
| `production`  | Multi-worker production server                 |

---

## 🚀 How to Run

### Development (hot-reload)

```bash
docker-compose up --build
```

- **API** → [http://localhost:8000](http://localhost:8000)
- **Swagger UI** → [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc** → [http://localhost:8000/redoc](http://localhost:8000/redoc)

Source files are volume-mounted so any changes to `main.py` reload the server instantly.

---

## 🧪 Sample API Calls

```bash
# Create an item
curl -X POST http://localhost:8000/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Widget", "description": "A great widget", "price": 9.99}'

# List all items
curl http://localhost:8000/items

# Get a specific item (replace <id> with the returned id)
curl http://localhost:8000/items/<id>

# Delete an item
curl -X DELETE http://localhost:8000/items/<id>
```

---

## ⚙️ Environment Variables

| Variable | Default | Description          |
|----------|---------|----------------------|
| `PORT`   | `8000`  | Exposed port         |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down
```

---

## 🔗 Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Uvicorn Documentation](https://www.uvicorn.org/)
- [Pydantic v2](https://docs.pydantic.dev/)
