# Redis + RedisInsight Docker Setup

Redis is an in-memory data structure store used as a database, cache, and message broker. This setup pairs it with **RedisInsight**, Redis's official browser-based GUI for monitoring and managing your Redis instance.

## 📦 Services Included

| Service        | Description                              | Port  |
|----------------|------------------------------------------|-------|
| `redis`        | Redis 7 server with AOF persistence      | 6379  |
| `redisinsight` | Redis GUI for browsing keys and metrics  | 5540  |

---

## 📂 Project Structure

```
redis/
├── docker-compose.yml
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d
```

- **Redis** → `localhost:6379` (password: `secret`)
- **RedisInsight UI** → [http://localhost:5540](http://localhost:5540)

---

## 🔗 Connect RedisInsight to Redis

1. Open [http://localhost:5540](http://localhost:5540).
2. Click **Add Redis Database**.
3. Fill in:
   - **Host**: `redis`
   - **Port**: `6379`
   - **Password**: `secret`
4. Click **Add Redis Database**.

---

## 🧪 Test via CLI

```bash
# Connect with password
docker exec -it $(docker-compose ps -q redis) redis-cli -a secret

# Basic commands
SET hello world
GET hello
LPUSH mylist a b c
LRANGE mylist 0 -1
```

---

## ⚙️ Environment Variables

| Variable           | Default   | Description             |
|--------------------|-----------|-------------------------|
| `REDIS_PASSWORD`   | `secret`  | Redis auth password     |
| `REDISINSIGHT_PORT`| `5540`    | RedisInsight port       |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers
docker-compose down -v    # Stop and remove volumes (data loss!)
```

---

## 🔗 Additional Resources

- [Redis Documentation](https://redis.io/docs/)
- [RedisInsight Documentation](https://redis.io/docs/connect/insight/)
