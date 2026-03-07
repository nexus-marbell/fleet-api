# Stage 1: Build
FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

# Stage 2: Runtime
FROM python:3.12-slim

RUN groupadd --gid 1000 fleet && \
    useradd --uid 1000 --gid fleet --shell /bin/bash fleet

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY alembic/ alembic/
COPY alembic.ini .

USER fleet

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

CMD ["uvicorn", "fleet_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
