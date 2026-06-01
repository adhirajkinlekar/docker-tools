# PostgreSQL + pgAdmin Docker Setup

PostgreSQL is the world's most advanced open-source relational database. This setup pairs it with **pgAdmin 4**, a powerful web-based GUI for managing your database visually.

## 📦 Services Included

| Service     | Description                                    | Port  |
|-------------|------------------------------------------------|-------|
| `postgres`  | PostgreSQL 16 database server                  | 5432  |
| `pgadmin`   | pgAdmin 4 web UI for database management       | 5050  |

---

## 📂 Project Structure

```
postgres/
├── docker-compose.yml
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **PostgreSQL** → `localhost:5432`
- **pgAdmin UI** → [http://localhost:5050](http://localhost:5050)
  - Email: `admin@admin.com`
  - Password: `secret`

---

## 🔗 Connect pgAdmin to PostgreSQL

1. Open [http://localhost:5050](http://localhost:5050) and log in.
2. Right-click **Servers** → **Register** → **Server**.
3. Fill in:
   - **Name**: any label (e.g., `Local`)
   - **Host**: `postgres`
   - **Port**: `5432`
   - **Username**: `admin`
   - **Password**: `secret`
4. Click **Save**.

---

## ⚙️ Environment Variables

Override defaults with environment variables or a `.env` file:

| Variable            | Default           | Description               |
|---------------------|-------------------|---------------------------|
| `POSTGRES_USER`     | `admin`           | PostgreSQL superuser      |
| `POSTGRES_PASSWORD` | `secret`          | PostgreSQL password       |
| `POSTGRES_DB`       | `appdb`           | Default database name     |
| `PGADMIN_EMAIL`     | `admin@admin.com` | pgAdmin login email       |
| `PGADMIN_PASSWORD`  | `secret`          | pgAdmin login password    |
| `PGADMIN_PORT`      | `5050`            | pgAdmin exposed port      |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down          # Stop containers
docker-compose down -v       # Stop and remove volumes (data loss!)
```

---

## 🔗 Additional Resources

- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [pgAdmin 4 Documentation](https://www.pgadmin.org/docs/)
