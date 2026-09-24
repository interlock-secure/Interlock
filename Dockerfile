# Single stage. A multi-stage build would shave perhaps 150MB off an image
# that is already small, at the cost of a Dockerfile nobody can debug at
# eleven at night before an interview.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    INTERLOCK_DATABASE=/data/interlock.db

WORKDIR /app

# Dependencies before source, so a code change does not re-resolve them.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /data

# Non-root. The process needs to read its own code and write one SQLite file,
# and nothing else on the filesystem.
RUN useradd --create-home --uid 10001 interlock && chown -R interlock /data
USER interlock

EXPOSE 8000

# The chain check is the health check. A process that is up but whose audit
# chain no longer verifies is not healthy in any sense that matters here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "interlock.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
