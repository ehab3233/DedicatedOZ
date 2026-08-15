# One image, three roles: API, Celery worker, Celery beat. The role is chosen
# by the command, so what runs in production is byte-identical across all three.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOZ_INSTALLER_TEMPLATE_DIR=/opt/doz/installer/templates

# ipmitool is needed by workers for the SOL console bridge. It is small enough
# that keeping one image is worth more than shaving it off the API.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ipmitool \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repository root, so the installer templates can be
# copied into the image alongside the backend.
WORKDIR /opt/doz/backend

COPY backend/pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY installer /opt/doz/installer

RUN useradd --system --uid 10001 doz && chown -R doz /opt/doz
USER doz

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
