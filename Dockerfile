# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# nour-code — local AI coding agent server
#
# The container talks to an Ollama daemon running on the *host*:
#   * Linux  -> add `--add-host=host.docker.internal:host-gateway` to `docker run`
#   * Docker Desktop (macOS/Windows) -> host.docker.internal works out of the box
#
# Build:  docker build -t nour-code:latest .
# Run:    docker run --rm -p 8000:8000 \
#           --add-host=host.docker.internal:host-gateway \
#           -v "$PWD/permissions.yaml:/app/permissions.yaml:ro" \
#           -v "$HOME/projects:/data/projects:ro" \
#           -e NOUR_CODE_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
#           nour-code:latest
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt

FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Ollama lives on the Docker host by default.
    NOUR_CODE_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    NOUR_CODE_HOST=0.0.0.0 \
    NOUR_CODE_PORT=8000 \
    NOUR_CODE_MODEL=qwen3:8b

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY nour_code ./nour_code
COPY permissions.yaml ./permissions.yaml
COPY pyproject.toml README.md ./
COPY SKILL_TEMPLATE.py ./SKILL_TEMPLATE.py

# Runtime directories (logs, session file, backups) live in a writable volume.
RUN useradd --create-home --uid 10001 --shell /bin/bash nour \
 && mkdir -p /app/logs /app/sandbox \
 && chown -R nour:nour /app
VOLUME ["/app/logs"]

USER nour
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "nour_code.main:app", "--host", "0.0.0.0", "--port", "8000"]
