# ---------- build stage: install Python deps into an isolated prefix ----------
FROM python:3.12-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------- runtime stage: slim image with only the app + installed packages ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/app.db \
    PORT=8000
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app ./app
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1
# PORT is injected by hosts such as Render/Railway; defaults to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
