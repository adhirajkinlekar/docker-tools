# 🔎 SonarQube Docker Setup

SonarQube is the leading open-source platform for **continuous code quality and security analysis**. It detects bugs, code smells, security vulnerabilities, and code duplications across 30+ programming languages.

This setup runs **SonarQube 10 Community Edition** backed by **PostgreSQL 16**.

## 📦 Services Included

| Service       | Description                                  | Port  |
|---------------|----------------------------------------------|-------|
| `sonarqube`   | SonarQube 10 Community code analysis server  | 9000  |
| `sonar-db`    | PostgreSQL 16 — SonarQube's database backend | 5432  |

---

## 📂 Project Structure

```
sonarqube/
├── docker-compose.yml
└── README.md
```

---

## ⚠️ System Prerequisite

SonarQube requires `vm.max_map_count` to be at least `524288`. Run this on your host before starting:

```bash
# Linux / macOS (Docker Desktop)
sysctl -w vm.max_map_count=524288

# Make it permanent (Linux)
echo "vm.max_map_count=524288" | sudo tee -a /etc/sysctl.conf
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

SonarQube takes ~60 seconds to start on first launch while it initialises the database.

- **SonarQube UI** → [http://localhost:9000](http://localhost:9000)
  - Username: `admin` / Password: `admin`
  - You will be prompted to change the password on first login.

---

## 🧪 Analysing Your First Project

### Option 1 — Using the UI wizard

1. Log in → **Create Project** → **Locally**.
2. Generate a project token.
3. Run the scanner command shown in the UI against your codebase.

### Option 2 — Using sonar-scanner CLI (Docker)

```bash
docker run --rm \
  --network sonarqube_sonar-net \
  -e SONAR_HOST_URL="http://sonarqube:9000" \
  -e SONAR_TOKEN="<your-token>" \
  -v "$(pwd):/usr/src" \
  sonarsource/sonar-scanner-cli
```

---

## ⚙️ Environment Variables

| Variable             | Default      | Description                    |
|----------------------|--------------|--------------------------------|
| `SONAR_PORT`         | `9000`       | SonarQube exposed port         |
| `SONAR_DB`           | `sonarqube`  | PostgreSQL database name       |
| `SONAR_DB_USER`      | `sonar`      | PostgreSQL username            |
| `SONAR_DB_PASSWORD`  | `sonar`      | PostgreSQL password            |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers
docker-compose down -v    # Stop and remove volumes (data loss!)
```

---

## 🔗 Additional Resources

- [SonarQube Documentation](https://docs.sonarsource.com/sonarqube/latest/)
- [SonarScanner CLI](https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner/)
- [Community Edition Features](https://www.sonarsource.com/products/sonarqube/downloads/)
