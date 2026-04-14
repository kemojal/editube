# Deploying Editube API (Dokploy on Hetzner)

Use this folder as the **Git repository root** for the backend. Dokploy builds from the **Dockerfile** at the repo root.

## Build

- **Dockerfile path:** `./Dockerfile`
- **Build context:** repository root (same directory as this file).

Test locally:

```bash
docker build -t editube-api .
docker run --rm -p 8000:8000 --env-file .env editube-api
```

Health check: `GET http://localhost:8000/health` → `{"status":"ok"}`.

## Environment variables

See [`.env.example`](.env.example). In Dokploy, set at least:

- `DATABASE_URL`, `JWT_SECRET_KEY`, Cloudinary trio, `BASE_URL`, `CORS_ORIGINS`, `FRONTEND_BASE_URL`
- Add your production frontend origin to **`CORS_ORIGINS`** (comma-separated, no spaces). It must match the browser origin of your Next app (same scheme + host + port).

## Database migrations

**Option A — Dokploy pre-deploy command** (recommended for zero-downtime patterns):

```bash
alembic upgrade head
```

Use the **same image** as the running app, or a one-off container with the same `DATABASE_URL`.

**Option B — Run on container start:** set `RUN_MIGRATIONS_ON_START=1` in Dokploy env (see [`docker-entrypoint.sh`](docker-entrypoint.sh)). Simpler; adds latency on every restart.

## Transcription worker (second application)

Create a **second** Dokploy application from the **same** Git repo and image:

- **Image:** identical to the API image (same Dockerfile build).
- **Working directory:** `/app` (default from Dockerfile).
- **Command / args** (override default `CMD`), example:

```text
sh -c "rq worker -u \"$REDIS_URL\" default"
```

Set **`REDIS_URL`** on both API and worker. The API enqueues jobs; the worker must have **`ffmpeg`** (included in this image).

## Ports

- API listens on **`PORT`** (default `8000`). In Dokploy map public HTTPS to this port as configured by the platform.

## Production checklist

1. Set a strong **`JWT_SECRET_KEY`** (changing it invalidates existing JWTs).
2. Set **`CORS_ORIGINS`** to your real frontend URL(s).
3. Set **`NEXT_PUBLIC_API_URL`** on the frontend build to this API’s public URL (see frontend `DEPLOY.md`).
4. Run **`alembic upgrade head`** at least once before or right after first deploy.
