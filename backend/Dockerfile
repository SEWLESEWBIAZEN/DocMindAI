# syntax=docker/dockerfile:1
#
# Targets:
#   dev      - all dependencies incl. dev tools; source bind-mounted by Compose.
#   runtime  - minimal production image: no compilers, no uv, non-root user.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# Tesseract OCR (scanned PDFs, ADR 0011) with English language data.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*
RUN pip install "uv==0.11.32"
WORKDIR /app

# --------------------------------------------------------------------------- #
FROM base AS dev
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
ENV PATH="/opt/venv/bin:${PATH}"
EXPOSE 8000
CMD ["uvicorn", "documind.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "src"]

# --------------------------------------------------------------------------- #
FROM base AS builder
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
# Tesseract OCR (scanned PDFs, ADR 0011) with English language data.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN groupadd --system --gid 10001 documind \
 && useradd --system --uid 10001 --gid documind --no-create-home --shell /usr/sbin/nologin documind
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
USER documind
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=4).status != 200)"]
# Trusted proxy IPs come from FORWARDED_ALLOW_IPS (read by uvicorn), set at deploy time.
CMD ["uvicorn", "documind.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--timeout-graceful-shutdown", "20"]
