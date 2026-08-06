# Editube API. Same image for RQ worker: override CMD in Dokploy (see DEPLOY.md).
# Build: docker build -t editube-api .

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

COPY requirements.txt requirements-ml.txt ./

# Background removal is a first-class editor feature, so the default image must
# actually contain its runtime. Deployments that route segmentation to a remote
# GPU service can opt out with `--build-arg INSTALL_ML=0`.
ARG INSTALL_ML=1
RUN pip install --no-cache-dir --upgrade setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_ML" = "1" ]; then pip install --no-cache-dir --no-build-isolation -r requirements-ml.txt; fi

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
