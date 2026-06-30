# Forge Runtime — multi-stage Docker build
# Stage 1: Install dependencies
FROM python:3.11-slim AS deps

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir uvicorn[standard]

# Stage 2: Application
FROM python:3.11-slim AS app

WORKDIR /app
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY . .

# Install git for VCS operations
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
