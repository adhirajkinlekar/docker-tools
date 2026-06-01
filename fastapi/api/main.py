from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import uuid

app = FastAPI(
    title="FastAPI Docker Sample",
    description="A sample FastAPI application demonstrating multi-stage Docker builds.",
    version="1.0.0",
)

# ---------- Models ----------

class Item(BaseModel):
    name: str
    description: Optional[str] = None
    price: float

class ItemResponse(Item):
    id: str

# ---------- In-memory store ----------

items: dict[str, ItemResponse] = {}

# ---------- Routes ----------

@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "message": "FastAPI is running 🚀"}

@app.get("/health", tags=["Health"])
def health():
    return {"status": "healthy"}

@app.get("/items", response_model=list[ItemResponse], tags=["Items"])
def list_items():
    return list(items.values())

@app.post("/items", response_model=ItemResponse, status_code=201, tags=["Items"])
def create_item(item: Item):
    item_id = str(uuid.uuid4())
    record = ItemResponse(id=item_id, **item.model_dump())
    items[item_id] = record
    return record

@app.get("/items/{item_id}", response_model=ItemResponse, tags=["Items"])
def get_item(item_id: str):
    item = items.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.delete("/items/{item_id}", status_code=204, tags=["Items"])
def delete_item(item_id: str):
    if item_id not in items:
        raise HTTPException(status_code=404, detail="Item not found")
    del items[item_id]
