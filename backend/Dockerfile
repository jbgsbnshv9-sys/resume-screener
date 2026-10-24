FROM python:3.12-slim

# LibreOffice (headless writer) converts DOCX to PDF for intake; DejaVu gives it fonts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu-core postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/screener ./screener
RUN pip install --no-cache-dir -e ".[dev]"
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/tests ./tests
COPY configs /app/configs
COPY fixtures /app/fixtures

ENV SCREENER_CONFIGS_DIR=/app/configs \
    SCREENER_BLOB_DIR=/data/blobs \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn screener.api.app:app --host 0.0.0.0 --port 8000"]
