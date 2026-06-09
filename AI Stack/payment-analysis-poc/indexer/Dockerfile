FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the fastembed model at build time so startup isn't delayed
# by a ~130 MB download.
RUN python -c "\
from fastembed import TextEmbedding; \
list(TextEmbedding('BAAI/bge-small-en-v1.5').embed(['warmup']))"

COPY . .

EXPOSE 8001

CMD ["uvicorn", "indexer:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "info"]
