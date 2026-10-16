# Build from the repository root: docker build -f fleet/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLEET_AGENT_PATH=/app/unit/app.py
WORKDIR /app
COPY fleet/pyproject.toml fleet/alembic.ini ./
COPY fleet/fleet ./fleet
RUN pip install . && useradd --system --uid 10001 fleet
# The unit agent the server offers to units as a remote update.
COPY unit/app.py ./unit/app.py
USER fleet
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4).status == 200 else 1)"
CMD ["fleet", "serve", "--host", "0.0.0.0", "--port", "8800", "--migrate"]
