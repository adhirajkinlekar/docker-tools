# 🔐 Keycloak Docker Setup

Keycloak is an open-source **Identity and Access Management (IAM)** solution that provides OAuth2, OpenID Connect (OIDC), and SAML 2.0 out of the box. Use it to add authentication and authorization to your apps without writing it yourself.

## 📦 Services Included

| Service        | Description                                        | Port  |
|----------------|----------------------------------------------------|-------|
| `keycloak`     | Keycloak 24 identity provider (dev mode)           | 8080  |
| `keycloak-db`  | PostgreSQL 16 — Keycloak's persistence backend     | 5432  |

---

## 📂 Project Structure

```
keycloak/
├── docker-compose.yml
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **Keycloak Admin Console** → [http://localhost:8080](http://localhost:8080)
  - Username: `admin` / Password: `admin`

> ⚠️ This uses `start-dev` mode — suitable for local development only. For production use `start` with HTTPS and proper secrets.

---

## 🛠️ Getting Started with a Realm

1. Log in to the Admin Console at [http://localhost:8080](http://localhost:8080).
2. Click the realm dropdown (top-left) → **Create Realm**.
3. Give it a name (e.g., `myrealm`) → **Create**.
4. Under **Clients** → **Create Client** to register your application.
5. Under **Users** → **Add User** to create test users.

---

## 🔑 OIDC Token Flow (Quick Test)

Get a token using the Resource Owner Password Grant (for testing only):

```bash
curl -X POST http://localhost:8080/realms/myrealm/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=myclient" \
  -d "username=testuser" \
  -d "password=testpass"
```

---

## ⚙️ Environment Variables

| Variable                | Default      | Description                     |
|-------------------------|--------------|---------------------------------|
| `KEYCLOAK_ADMIN`        | `admin`      | Admin console username          |
| `KEYCLOAK_ADMIN_PASSWORD` | `admin`    | Admin console password          |
| `KEYCLOAK_DB`           | `keycloak`   | PostgreSQL database name        |
| `KEYCLOAK_DB_USER`      | `keycloak`   | PostgreSQL username             |
| `KEYCLOAK_DB_PASSWORD`  | `secret`     | PostgreSQL password             |
| `KEYCLOAK_PORT`         | `8080`       | Keycloak exposed port           |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers
docker-compose down -v    # Stop and remove volumes (data loss!)
```

---

## 🔗 Additional Resources

- [Keycloak Documentation](https://www.keycloak.org/documentation)
- [Keycloak OpenID Connect](https://www.keycloak.org/docs/latest/securing_apps/)
- [Keycloak Admin REST API](https://www.keycloak.org/docs-api/24.0.0/rest-api/index.html)
