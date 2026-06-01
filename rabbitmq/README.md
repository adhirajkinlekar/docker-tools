# 🐇 RabbitMQ Docker Setup

RabbitMQ is a reliable, open-source AMQP message broker. Unlike Kafka (which is built for high-throughput log streaming), RabbitMQ is designed for **task queues, request/reply patterns, and routable messages** — making it ideal for microservices that need flexible routing and acknowledgment semantics.

## 📦 Services Included

| Service      | Description                                         | Port           |
|--------------|-----------------------------------------------------|----------------|
| `rabbitmq`   | RabbitMQ 3.13 broker with management plugin         | 5672 / 15672   |
| `producer`   | Node.js Express service that publishes to a queue   | 3005           |

---

## 📂 Project Structure

```
rabbitmq/
├── producer/
│   ├── Dockerfile
│   ├── index.js
│   └── package.json
├── docker-compose.yml
└── README.md
```

---

## 🚀 How to Run

```bash
docker-compose up -d --build
```

- **RabbitMQ Management UI** → [http://localhost:15672](http://localhost:15672)
  - Username: `admin` / Password: `secret`
- **Producer API** → [http://localhost:3005](http://localhost:3005)

---

## 🧪 Test the Producer

Publish an order message to the `orders` queue:

```bash
curl -X POST http://localhost:3005/order \
  -H "Content-Type: application/json" \
  -d '{"id": "order-1", "product": "Widget", "qty": 3}'
```

You can then see the message in the **RabbitMQ UI** under **Queues** → `orders`.

---

## 🧠 How It Works

1. The `producer` service connects to RabbitMQ on startup (with retry logic).
2. A POST to `/order` publishes a JSON message to the durable `orders` queue.
3. A downstream consumer (not included) would `channel.consume()` from the same queue and process the message.

---

## ⚙️ Environment Variables

| Variable            | Default   | Description                    |
|---------------------|-----------|--------------------------------|
| `RABBITMQ_USER`     | `admin`   | RabbitMQ admin username        |
| `RABBITMQ_PASSWORD` | `secret`  | RabbitMQ admin password        |
| `RABBITMQ_VHOST`    | `/`       | Virtual host                   |
| `RABBITMQ_UI_PORT`  | `15672`   | Management UI port             |

---

## 🛑 Stop & Clean Up

```bash
docker-compose down       # Stop containers
docker-compose down -v    # Stop and remove volumes
```

---

## 🔗 Additional Resources

- [RabbitMQ Documentation](https://www.rabbitmq.com/documentation.html)
- [amqplib (Node.js client)](https://amqp-node.github.io/amqplib/)
- [RabbitMQ vs Kafka](https://www.rabbitmq.com/docs/quorum-queues)
