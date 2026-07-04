# Expense IDP web app — runs the FastAPI layer + pipeline in one container.
# Build:   az acr build --registry <acr> --image expense-idp:v1 .
# Runtime auth is Managed Identity (AZURE_CLIENT_ID env) — no keys baked in.

FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml README.md ./
COPY expense_extractor ./expense_extractor
COPY agents ./agents
COPY tools ./tools
COPY workflow ./workflow
COPY webapp ./webapp
RUN pip install --no-cache-dir ".[agents,web,observability]"

# Local fallback store lives here when Cosmos isn't configured (ephemeral per replica —
# fine for demo; set COSMOS_ENDPOINT for durable records).
ENV LOCAL_STORE_PATH=/tmp/records.json

EXPOSE 8000
CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
