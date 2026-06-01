const amqplib = require("amqplib");
const express = require("express");

const app = express();
app.use(express.json());

const QUEUE = "orders";
const RABBITMQ_URL = process.env.RABBITMQ_URL || "amqp://admin:secret@rabbitmq:5672/";

let channel;

async function connect() {
  let retries = 10;
  while (retries > 0) {
    try {
      const conn = await amqplib.connect(RABBITMQ_URL);
      channel = await conn.createChannel();
      await channel.assertQueue(QUEUE, { durable: true });
      console.log(`[RabbitMQ] Connected. Queue "${QUEUE}" is ready.`);
      return;
    } catch (err) {
      console.error(`[RabbitMQ] Connection failed. Retrying... (${retries} left)`);
      retries--;
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
  console.error("[RabbitMQ] Could not connect. Exiting.");
  process.exit(1);
}

// POST /order — publish a message to the queue
app.post("/order", async (req, res) => {
  const order = req.body;
  if (!order || !order.id) {
    return res.status(400).json({ error: "Order must have an 'id' field." });
  }
  const message = JSON.stringify(order);
  channel.sendToQueue(QUEUE, Buffer.from(message), { persistent: true });
  console.log(`[Producer] Sent order: ${message}`);
  res.json({ status: "queued", order });
});

// GET /health
app.get("/health", (_req, res) => res.json({ status: "ok" }));

connect().then(() => {
  app.listen(3000, () => console.log("[Producer] HTTP server listening on port 3000"));
});
