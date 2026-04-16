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

## Stripe billing (catalog sync)

Checkout resolves **Stripe Price IDs from the database** (`stripe_prices`), populated from Stripe webhooks and/or a bootstrap sync. Legacy **`STRIPE_PRICE_*`** env vars are optional and only used when **`STRIPE_PRICE_FALLBACK=1`** (see [`.env.example`](.env.example)).

### Webhook URL and events

- Endpoint: **`POST {BASE_URL}/billing/webhook`** (same as today).
- In the Stripe Dashboard, enable at least:
  - `checkout.session.completed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `product.created`, `product.updated`, `product.deleted`
  - `price.created`, `price.updated`, `price.deleted`

### Price metadata (recommended)

On each subscription Price in Stripe, set metadata (or a `lookup_key` like `editube_pro_monthly`):

- **`editube_plan`**: `pro` or `scale` (aligned with checkout body `plan`).
- **`editube_interval`**: `month` or `year`.

Only one **active** price per `(editube_plan, editube_interval)` should exist for core tiers (archive old prices in Stripe when changing amounts).

### Bootstrap (empty DB before webhooks)

After deploy, run **one** of:

1. **HTTP:** `POST /billing/sync-catalog` with header **`X-Stripe-Catalog-Sync-Secret`** equal to env **`STRIPE_CATALOG_SYNC_SECRET`** (set a long random value in Dokploy).
2. **CLI:** from the `editube/` directory, with `DATABASE_URL` and `STRIPE_SECRET_KEY` set:

   ```bash
   python scripts/sync_stripe_catalog.py
   ```
