# Editube Backend

FastAPI backend for the Editube project.

## Prerequisites

- Python 3.10+ (tested here with Python 3.14)
- pip
- **PostgreSQL** (via `DATABASE_URL`)
- **ffmpeg** on `PATH` (required for **transcription** and **aspect export** workers; the API process does not shell out to ffmpeg)
- **Redis** (optional but recommended: transcription, YouTube publish, aspect exports, chapter synthesis, and mention emails all enqueue **RQ** jobs when `REDIS_URL` is set)

## Run locally

From this directory (`editube/`):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # pip = p-i-p, not "ip"
# Optional ML/AI stack (transcription + whisperx + google-genai):
# .venv/bin/python -m pip install -r requirements-ml.txt
.venv/bin/python -m pip install email-validator
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

API root: `http://127.0.0.1:8000/`.

### Use the project virtualenv for Alembic and pip

Run **`pip`** and **`alembic`** through the venv above (`.venv/bin/python -m pip …`, `.venv/bin/python -m alembic …`), or activate the venv first (`source .venv/bin/activate`) and then use `pip` / `python -m alembic`.

**Do not** rely on a globally installed **`alembic`** (for example from Homebrew: `/opt/homebrew/bin/alembic`). That uses a different Python and will not see this project’s packages, which commonly causes:

`ModuleNotFoundError: No module named 'psycopg2'`

**Install command (copy exactly):** `pip install -r requirements.txt` — the program name is **`pip`** (three letters: p, i, p). A common typo is **`ip`**, which is not a command (`command not found: ip`).

## Environment variables

Configure `.env` (see your local template). Commonly used:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `CLOUDINARY_CLOUD_NAME` | Video/image storage |
| `CLOUDINARY_API_KEY` | |
| `CLOUDINARY_API_SECRET` | |
| `REDIS_URL` | e.g. `redis://127.0.0.1:6379/0`. Used for **RQ** workers: transcription, **YouTube publish**, **aspect exports**, **LLM auto-chapters**, mention emails. If unset, those features stay queued/pending until Redis and a worker are available. |

### Email (SMTP)

Transactional email (invitations, subscription welcome/cancel) uses SMTP when configured. If unset, emails are skipped and a warning is logged.

| Variable | Description |
|----------|-------------|
| `SMTP_HOST` | SMTP server hostname (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | Port (default `587`) |
| `SMTP_USER` | Login username |
| `SMTP_PASSWORD` | App password or SMTP secret |
| `EMAIL_FROM` | From address (defaults to `SMTP_USER` if omitted) |
| `SMTP_USE_TLS` | `true` / `false` (default `true`; uses STARTTLS) |
| `FRONTEND_BASE_URL` | Used in email links (e.g. `https://app.example.com`) |

## Encrypted API request logging

The backend keeps a best-effort operational record for every HTTP API request, whether it succeeds or fails. WebSocket traffic is excluded. The log is intended for incident investigation and aggregate API reliability analytics; it is not a replacement for the existing product-analytics event pipeline.

Each request metadata record includes:

- request/correlation ID, timestamp, deployment environment, release, HTTP method, normalized route template, and endpoint name;
- status code, duration, request/response sizes, client IP and user-agent keyed hashes, and authenticated user/workspace identifiers when available;
- sanitized request headers, with authorization credentials, cookies, API keys, signatures, and other secrets removed;
- payload-capture state, truncation state, error classification, and enough metadata to distinguish intentionally excluded bodies from dropped or failed captures.

Eligible JSON request and response bodies are recursively redacted, size-bounded, and Fernet-encrypted before entering the background write queue. The default limits are 64 KiB for request bodies and 128 KiB for response bodies. Authentication, token, MFA, OAuth callback, Stripe webhook, health, documentation, `OPTIONS`, multipart, binary, download, and streaming payloads are metadata-only. Unknown routes also default to metadata-only. Oversized or malformed JSON is never partially stored because a truncated fragment cannot be safely redacted.

Retention defaults are 30 days for request metadata, 14 days for failed-request payloads, and 3 days for successful-request payloads. The application invokes a restricted database maintenance function every 24 hours to build daily reliability rollups and remove expired data. Access audits and daily rollups default to 400 days.

Writes are asynchronous, bounded, batched, and fail-open: an unavailable log database must not take down the customer API. At 80% queue occupancy, encrypted payloads are shed first so metadata can continue to flow; a completely full queue then drops records. Monitor the privileged log health endpoint and application warnings; “every request” is achievable while the logging path is healthy, not a durability guarantee during infrastructure failure.

### Security and access

Logs live in a dedicated PostgreSQL `log` schema. The public API writes with a least-privilege `editube_log_writer` role. A separate internal-admin process reads with `editube_log_reader`. Never configure either value with the Neon database-owner credentials, and never give the public API `LOG_READ_DATABASE_URL`.

Decrypted payload access is restricted to explicitly granted internal administrators. A role string alone is insufficient: the account must have an active database grant and verified MFA. Workspace owners and producers are rejected even if a grant row is inserted accidentally. Every search/decrypt/reproduction operation requires a session-bound MFA step-up issued within the previous five minutes, a stated incident/debugging reason, and an append-only access-audit record. If the audit insert fails, access fails closed. Log payloads and cryptographic keys are never sent to product analytics, Sentry, or application logs.

The system produces a sanitized reproduction manifest; it does not send outbound replay requests. Credentials and cookies are replaced. `GET`/`HEAD` manifests are marked safe-method candidates, while `POST`, `PUT`, `PATCH`, and `DELETE` must be reproduced in a non-production environment or through an endpoint-specific side-effect-controlled adapter.

### Configuration

| Variable | Description |
|----------|-------------|
| `LOG_WRITE_DATABASE_URL` | PostgreSQL URL for the insert-only `editube_log_writer` role. Use the Neon pooled hostname for runtime traffic. |
| `LOG_READ_DATABASE_URL` | PostgreSQL URL for the restricted `editube_log_reader` role. Supply only to the internal-admin deployment. |
| `LOG_PAYLOAD_ENCRYPTION_KEY` | Dedicated Fernet key used to encrypt captured payloads. Generate it independently; it does not come from Neon. |
| `LOG_PAYLOAD_ENCRYPTION_KEY_ID` | Non-secret identifier for the active payload key, for example `2026-08-v1`; change it when rotating keys. |
| `LOG_HMAC_KEY` | Independent random 32-byte URL-safe base64 key for stable keyed hashes. Do not reuse the Fernet, JWT, or database password. |
| `LOG_PAYLOAD_DECRYPTION_KEYS` | Optional JSON map of retired key IDs to Fernet keys. Keep old keys only until their payloads have expired. |

Runtime database URLs have these shapes:

```dotenv
LOG_WRITE_DATABASE_URL=postgresql://editube_log_writer:<writer-password>@<pooler-host>/neondb?sslmode=require&channel_binding=require
LOG_READ_DATABASE_URL=postgresql://editube_log_reader:<reader-password>@<pooler-host>/neondb?sslmode=require&channel_binding=require
LOG_PAYLOAD_ENCRYPTION_KEY=<generated-fernet-key>
LOG_PAYLOAD_ENCRYPTION_KEY_ID=2026-08-v1
LOG_HMAC_KEY=<generated-32-byte-base64-key>
```

Generate the two application keys locally, then place them directly in the deployment secret manager:

```bash
./.venv/bin/python - <<'PY'
import base64
import secrets
from cryptography.fernet import Fernet

print("LOG_WRITER_ROLE_PASSWORD=" + secrets.token_urlsafe(32))
print("LOG_READER_ROLE_PASSWORD=" + secrets.token_urlsafe(32))
print("LOG_PAYLOAD_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("LOG_HMAC_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
PY
```

Do not commit generated keys or production database URLs. Treat any credential pasted into chat, an issue, or source control as compromised and rotate it before deployment.

### Database and role setup

Use a direct, non-pooler owner URL only for migrations and bootstrap. Runtime connections should use the pooled hostname. Run these after generating two independent high-entropy role passwords:

```bash
export LOG_MIGRATION_DATABASE_URL='postgresql://<owner>:<rotated-owner-password>@<direct-host>/neondb?sslmode=require&channel_binding=require'
DATABASE_URL="$LOG_MIGRATION_DATABASE_URL" .venv/bin/python -m alembic upgrade head

export LOG_WRITER_ROLE_PASSWORD='<generated-writer-password>'
export LOG_READER_ROLE_PASSWORD='<different-generated-reader-password>'
.venv/bin/python scripts/setup_request_log_database.py --create-roles --check
```

The migration creates `log.api_requests`, `log.api_payloads`, `log.access_events`, `log.admin_access_grants`, `log.api_request_daily_rollups`, and the owner-controlled `log.retention_policy`, plus the restricted zero-argument retention/rollup function. The writer cannot shorten retention or directly delete rows. The setup script creates SQL roles without inherited Neon-owner privileges and verifies the expected positive and negative grants.

Grant an existing internal administrator access only after the account has an internal role and verified MFA:

```bash
export DATABASE_URL='postgresql://<primary-role>:<password>@<primary-host>/editube?sslmode=require'
export LOG_MIGRATION_DATABASE_URL='postgresql://<log-owner>:<password>@<log-host>/neondb?sslmode=require'

.venv/bin/python -m scripts.manage_request_log_admin grant \
  --email incident-admin@example.com \
  --granted-by security-owner@example.com \
  --reason 'Approved for production incident response' \
  --expires-days 90
```

`DATABASE_URL` is used only to validate the account's role and verified MFA in
the primary database. Grants and audit rows are written through
`LOG_MIGRATION_DATABASE_URL` to the separate log database. Use the same
script’s `revoke` and `list` commands for lifecycle management.

### Deploy the two processes

Public API (`uvicorn app.main:app`):

- set `LOG_WRITE_DATABASE_URL`, the active encryption key/key ID, and `LOG_HMAC_KEY`;
- do not set `LOG_READ_DATABASE_URL`;
- keep the request-log writer role insert-only.

Internal admin API (`uvicorn app.internal_admin:app`):

- set `DATABASE_URL` for normal user/session/MFA verification;
- set `LOG_READ_DATABASE_URL`, the active/retired decryption keys, and `LOG_HMAC_KEY`;
- do not set `LOG_WRITE_DATABASE_URL` and explicitly set `LOG_REQUESTS_ENABLED=0`;
- expose it only on a private network or identity-aware proxy and set `LOG_ADMIN_CORS_ORIGINS` to the exact internal UI origin.

The public API refuses to start if a read URL is present, and the internal service refuses to start if a write URL is present. This makes accidental role co-location a deployment failure instead of a silent loss of isolation.

The internal service intentionally disables OpenAPI and documentation routes. Its workflow is:

1. `POST /internal/request-logs/mfa-step-up` with the current TOTP code.
2. Send the returned token as `X-Log-Step-Up-Token` and a meaningful `X-Log-Access-Reason` on every privileged request.

3. Search `GET /internal/request-logs`, decrypt `GET /internal/request-logs/{id}/payload`, build a safe manifest with `GET /internal/request-logs/{id}/reproduction`, or inspect privileged activity through `GET /internal/request-logs/access-events`.
4. Check database visibility and the latest captured request through `GET /internal/request-logs/health` using the same privileged headers.

### Internal monitoring console

The separate `../editube-admin` TanStack Start application provides the human-facing log console. It uses TanStack Query for live overview/request/audit data and Better Auth for an HttpOnly dashboard session, while preserving this backend as the authority for roles, explicit grants, MFA step-up, payload decryption, and access auditing. Editube bearer and step-up tokens are encrypted in the console's dedicated `admin_auth` database schema and are never stored in browser storage.

The live `GET /internal/request-logs/analytics/timeline` endpoint supports bounded minute (up to 24 hours), hour (up to 31 days), and day (up to 31 days) aggregation from retained metadata. It returns request/failure totals, average and p95 latency, status-class buckets, transfer sizes, and top routes. The request search endpoint also accepts an exact `environment` filter. Longer historical reporting should continue to use the retained daily rollups.

See `../editube-admin/README.md` for database bootstrap, secret generation, deployment, first-administrator access, and validation commands.

Monitor `GET /health/request-logging` on the public API for queue depth, payload shedding, dropped records, failed writes, and writer-thread health. It returns HTTP 503 when capture is degraded without exposing database errors or secrets.

## Video transcription (RQ + faster-whisper)

### What happens

1. On **POST** `/projects/{project_id}/videos/` (upload), the API creates a **`video_transcriptions`** row (`status` starts as `pending`, then `queued` if the job is enqueued).
2. A separate **RQ worker** runs `app.jobs.transcription.transcribe_video(video_id)`: **ffmpeg** reads a media URL. For **YouTube**, the worker resolves a fresh **audio** stream via `yt-dlp` using **`videos.ingest_page_url`** (canonical watch URL) or the repurpose job’s `source_url` — **not** `videos.file_path` alone (that URL is often **video-only** DASH, which yields empty transcripts under `ffmpeg -vn`). Then **faster-whisper** writes **`segments`** (`start` / `end` / `text` in seconds) and sets **`completed`** or **`failed`** (+ `error_message`). *(This stack is **editube** Postgres + Alembic; the **reelcut** repo uses a separate Go schema and is not the same database.)*
3. The player loads **`GET /videos/{video_id}`** (or project-scoped video detail); the JSON includes nested **`transcription`** (`status`, `segments`, `error_message`). The frontend polls while `status` is `pending`, `queued`, or `processing`.
4. To **start or retry** transcription (e.g. legacy videos, failed jobs, or stuck `pending`), call **`POST /videos/{video_id}/transcription`** or **`POST /projects/{project_id}/videos/{video_id}/transcription`** (same access rules as the video). Returns the same payload shape as **`GET /videos/{video_id}`**. If a job is already **`queued`** or **`processing`**, the API responds **409** unless you pass **`?force=true`**, which resets a stuck row and enqueues again (after a worker crash, RQ timeout, or expired YouTube signed URL).

### Dependencies

Core API/worker deps are in `requirements.txt` (`httpx`, `redis`, `rq`, etc.).  
Transcription/ML extras are in `requirements-ml.txt` (`faster-whisper`, `whisperx`, `google-genai`) because they can conflict or require platform-specific wheels on newer Python versions.

### Transcription-related env vars

| Variable | Description |
|----------|-------------|
| `REDIS_URL` | Broker for RQ (see table above). |
| `WHISPER_MODEL_SIZE` | faster-whisper model id (default `base`). Use **`tiny`** for much faster local dev on CPU. |
| `WHISPER_BEAM_SIZE` | Decoder beam size (default **`1`**; max `5`). Higher is slower with modest quality gains. |
| `TRANSCRIPTION_DEVICE` | `cpu` or `cuda` (default `cpu`). |
| `TRANSCRIPTION_COMPUTE_TYPE` | e.g. `int8`, `float16` (default `int8` on CPU; use `float16` on GPU as appropriate). |
| `TRANSCRIPTION_JOB_TIMEOUT_SEC` | RQ **`job_timeout`** for `transcribe_video` (default **`14400`** = 4h). Long videos on CPU may exceed 1h. |
| `FFMPEG_PROCESS_TIMEOUT_SEC` | **`subprocess`** timeout for the ffmpeg extract step (default **`10800`** = 3h). |
| `FFMPEG_USER_AGENT` | Optional. Set if **ffmpeg** cannot open a remote URL (some CDNs require a browser-like User-Agent). |

### Run the worker (second terminal)

Use the **same** virtualenv and working directory as the API (`editube/`), with **ffmpeg** available on `PATH`:

```bash
cd editube
source .venv/bin/activate   # or: .venv/bin/activate on Windows Git Bash
export REDIS_URL=redis://127.0.0.1:6379/0
rq worker -u "$REDIS_URL" default
```

The job function is registered as **`app.jobs.transcription.transcribe_video`**. RQ **`job_timeout`** is set to **3600** seconds in code for long files.

### One-command local run (API + worker)

If your clips/transcriptions get stuck in `queued` because the worker is not running, start both processes together:

```bash
cd editube
chmod +x scripts/dev_with_worker.sh
./scripts/dev_with_worker.sh
```

This runs:
- `rq worker -u "$REDIS_URL" default`
- `uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload` (when port 8000 is free)

On macOS, the script also sets `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` to avoid fork-related worker crashes.

That variable only disables the Objective-C runtime's fork-safety *check*; it does
not make CoreFoundation, SystemConfiguration or Metal fork-safe. Work that touches
those (background removal, and anything else loading torch or making HTTP calls
through the system proxy config) still has to run outside the forked child — which
is what `SEGMENTATION_ISOLATE` does. See
[Why removal runs in its own interpreter](#why-removal-runs-in-its-own-interpreter).

### Queue health check (API + CLI)

Live queue diagnostics endpoint:

```bash
GET /health/queue
```

Response includes:
- `redis_reachable`
- `worker_connected`
- `worker_count`
- `queue_backlog_count`

If this URL returns **404**, the API process on port 8000 was started **before** the route existed. Restart uvicorn (or run it with `--reload` as in `./scripts/dev_with_worker.sh` when nothing else is bound to 8000).

CLI helper:

```bash
cd editube
.venv/bin/python scripts/check_queue_health.py
```

### Database

The **`video_transcriptions`** table is created by Alembic (revision `g8h9i0j1k2l3`). After pulling changes, from `editube/` run migrations **with the venv’s Python** (see [Use the project virtualenv for Alembic and pip](#use-the-project-virtualenv-for-alembic-and-pip)):

```bash
.venv/bin/python -m alembic upgrade head
```

Existing videos created before this feature will have **`transcription: null`** in the API until you add rows and enqueue jobs (optional backfill / retry endpoint can be added later).

### Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Transcript stuck **`pending`** | `REDIS_URL` not set or Redis not running; enqueue skipped. |
| Stuck **`queued`** | No RQ worker running, or worker cannot import `app` (run worker from `editube/` with venv activated). |
| **`failed`** with ffmpeg message | `ffmpeg` missing on the **worker** host, or bad/corrupt download. |
| **`failed`** with CUDA / model errors | Wrong `TRANSCRIPTION_DEVICE` / `TRANSCRIPTION_COMPUTE_TYPE` for your machine. |

## Background removal, chroma key and click-to-select

Three separate paths, with deliberately different requirements:

| Feature | Needs | Runs in |
|---------|-------|---------|
| **Chroma key** | nothing beyond `ffmpeg` | RQ worker (pure filter, no model) |
| **Auto removal** | `rembg` (ONNX, in `requirements-ml.txt`) | spawned child of the worker |
| **Custom removal / click-to-select** | `torch` + `sam2` | spawned child; preview in the API process |

Chroma key needs no model at all, so it works on any host with no provider
configured. Only auto/custom removal load a model.

### Installing SAM 2 (click-to-select and custom removal)

```bash
.venv/bin/python -m pip install -r requirements-ml.txt   # includes torch + sam2
```

`torch` is by far the heaviest dependency here. To keep it out of the API image,
set `SEGMENTATION_PROVIDER=http` and point `ROUGH_CUT_ML_PROVIDER_URL` at a GPU
service instead. Capabilities are negotiated, so the editor hides the subject
picker when the configured provider cannot honour point prompts.

Checkpoints download from HuggingFace on first use (`sam2.1-hiera-tiny` for
*Faster*, `sam2.1-hiera-large` for *Better*) and are cached thereafter. The first
segmentation of a session therefore takes several seconds; subsequent ones are
~0.3s on Apple silicon.

### Env vars

| Var | Default | Meaning |
|-----|---------|---------|
| `SEGMENTATION_PROVIDER` | `auto` | `auto` \| `local` \| `http`. |
| `SEGMENTATION_DEVICE` | auto-detected | Override device. Detection order is `cuda` → `mps` → `cpu`. |
| `SEGMENTATION_LOCAL_MAX_SECONDS` | `120` | Refuses longer **clips** (not sources) rather than appearing to hang. |
| `SEGMENTATION_ISOLATE` | `1` | Run removal in a separate interpreter. **Leave on** — see below. |
| `SEGMENTATION_ISOLATE_TIMEOUT_SEC` | `3600` | Hard ceiling on the isolated child. |

### Why removal runs in its own interpreter

The RQ worker forks a child per job, and on macOS a forked child cannot safely
use what this work needs. It fails by *signal*, not exception, so the worker dies
mid-job and logs nothing — the visible symptom is a **"Python quit unexpectedly"**
dialog. Three distinct crash sites were observed:

- `_scproxy.get_proxies` → `SCDynamicStoreCopyProxiesWithOptions`. SystemConfiguration
  is not fork-safe, and *any* HTTP client reaches it — including HuggingFace
  checking for a checkpoint that is already cached.
- `at::mps::MPSAllocator::allocate` → `IOGPUDeviceGetAllocatedSize`. The inherited
  GPU handle is invalid in the child.
- Metal's shader compiler (`SIGABRT`).

`SEGMENTATION_ISOLATE=1` runs the work via
`python -m app.services.segmentation.child`, a fresh interpreter that inherits no
CoreFoundation state, no Metal context and no GPU handles. Set it to `0` only to
get a readable traceback while debugging, and expect crashes if you do so under a
forking worker.

The interactive click-to-select preview is *not* isolated: it runs in the API
process, which does not fork, because an interpreter start per click would defeat
the point. It does mean a preview occupies a request thread while it runs.

### Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| "Python quit unexpectedly", worker dies mid-job | `SEGMENTATION_ISOLATE=0` under a forking worker. Set it back to `1`. |
| `remove bg requires ROUGH_CUT_ML_PROVIDER_URL` | `SEGMENTATION_PROVIDER=http` with no URL set. |
| "needs SAM 2, which is not installed" | `pip install -r requirements-ml.txt`, then restart **both** API and worker. |
| "This clip is Ns, over the 120s limit" | Trim the clip, raise `SEGMENTATION_LOCAL_MAX_SECONDS`, or use a GPU provider. |
| Cutout looks opaque when inspected | Expected artefact: ffmpeg's *native* vp9 decoder drops WebM alpha. Decode with `-c:v libvpx-vp9`. The file carries `alpha_mode=1`. |
| Subject picker missing in the editor | Provider reports no `point_prompt` capability — SAM 2 not installed, or an HTTP provider without it. |

## Creator studio (YouTube, aspect exports, chapters)

### YouTube OAuth and publish

1. **Google Cloud Console:** enable **YouTube Data API v3** for the same project as your OAuth client. Add authorized redirect URI for the backend callback, e.g. `http://127.0.0.1:8000/users/google/youtube/callback`, or set `GOOGLE_YOUTUBE_REDIRECT_URI` to that exact URL.
2. **Env:** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (same as Google login), `TOKEN_ENCRYPTION_KEY` (Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`), optional `FRONTEND_YOUTUBE_OAUTH_RETURN_URL` (where Google redirects the browser after connect; default uses `FRONTEND_BASE_URL`).
3. **Connect:** authenticated `POST /users/google/youtube/authorize-url` returns `authorization_url`; the frontend opens it. Callback exchanges the code and stores an encrypted refresh token in **`user_youtube_connections`** (Alembic revision `z2b3c4d5e6f7`).
4. **Publish:** `POST /creator/publications/{id}/publish` for `platform=youtube` enqueues **`app.jobs.youtube_publish.youtube_publish_job`**. The draft author (`created_by`) must have connected YouTube. Requires **ffmpeg** on the worker host to download the master file (same pattern as transcription).

Optional **`YOUTUBE_PUBLISH_DRY_RUN=1`**: marks the publication published without calling YouTube (for smoke tests).

**Scheduled uploads** use `status.publishAt` (YouTube requires a **verified** channel and privacy `private` until publish time). **Chapters** in the description are appended from `video_chapters` when uploading.

### Aspect exports (ffmpeg + Cloudinary)

`POST /creator/videos/{id}/aspect-exports` enqueues **`app.jobs.aspect_export.aspect_export_job`**: download source, center-crop to target aspect, upload to Cloudinary, set `output_path` to the secure URL. **`subject_tracking`** is reserved for a future ML path; v1 uses smart center crop only.

### LLM auto-chapters

`POST /creator/videos/{id}/chapters/auto` enqueues **`app.jobs.chapter_synthesis.chapter_synthesis_job`** (needs `GEMINI_API_KEY` / AI client config like other AI jobs). Writes `VideoChapter` rows with `source=llm`.

### Single RQ worker

The same worker process picks up all jobs:

```bash
cd editube
source .venv/bin/activate
export REDIS_URL=redis://127.0.0.1:6379/0
rq worker -u "$REDIS_URL" default
```

Registered job callables include string paths such as **`app.jobs.youtube_publish.youtube_publish_job`**, **`app.jobs.aspect_export.aspect_export_job`**, and **`app.jobs.chapter_synthesis.chapter_synthesis_job`** (see `app/jobs/queue.py`).

## Google Drive import (create-project wizard source)

Lets a user connect Google Drive and pick a video in the **New project** modal. Design + rationale: `docs/google-drive-import-plan.md`.

**Scope choice matters.** We request only **`drive.file`** (non-sensitive), which grants access solely to files the user picks through the **Google Picker**. The broader `drive.readonly` needed for an in-app Drive file browser is a **restricted** scope requiring an annual third-party **CASA** security assessment, so it is deliberately avoided.

1. **Google Cloud Console:** enable **Google Picker API** + **Google Drive API**. Add `https://www.googleapis.com/auth/drive.file` to the OAuth consent screen. Create a browser **API key** restricted to your frontend origins. Note your **project number** (IAM & Admin > Settings) — that is the Picker `appId`. Add the backend callback to authorized redirect URIs, e.g. `http://127.0.0.1:8000/users/google/drive/callback`.
2. **Env:** `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (shared with Google login), `TOKEN_ENCRYPTION_KEY`, `GOOGLE_DRIVE_REDIRECT_URI`, `GOOGLE_PICKER_API_KEY`, `GOOGLE_PICKER_APP_ID`. Optional: `DRIVE_IMPORT_MAX_FILE_SIZE_MB` (default 10240), `DRIVE_IMPORT_JOB_TIMEOUT_SEC` (default 3600). The frontend needs **no** new env — the Picker key and appId are served by `GET /users/google/drive/picker-token`.
3. **Connect:** `POST /users/google/drive/authorize-url` → the frontend opens it in a **popup**. The callback returns a small HTML page that `postMessage`s the result to `window.opener` and closes, so the wizard's in-progress draft survives (a top-level redirect would destroy it). Tokens land in **`user_google_drive_connections`** (Alembic revision `d5e6f7a8b9c0`), refresh token Fernet-encrypted. Uniqueness is `(user_id, google_sub)`, so a user can connect several Google accounts.
4. **Pick + validate:** `POST /users/google/drive/resolve` runs every gate *before* any bytes move — not a video/audio file, Google-native Docs, dangling shortcut, trashed, `canDownload: false`, size ceiling, and the workspace storage cap. Drive's `videoMediaMetadata.durationMillis` and `thumbnailLink` come back immediately, so the wizard's trim step is usable while the transfer runs.
5. **Import:** `POST /users/google/drive/imports` creates a **`drive_imports`** row and enqueues **`app.jobs.drive_import.drive_import_job`**, which streams the file (8 MB chunks) to a temp file, `ffprobe`s duration when Drive omits it, uploads via the configured storage backend, and writes `file_path`. Progress: download maps to 0–90%, storage upload to 90–100%. Poll `GET /users/google/drive/imports/{id}`; `POST .../cancel` stops the worker between chunks when the user removes the file or discards the wizard.
6. **Attach:** the resulting `file_path` goes through the existing `POST /projects/{id}/videos/from-upload` — identical to a local upload, so transcription/thumbnail/proxy enqueue unchanged.

Failure modes worth knowing: a revoked/expired refresh token sets the connection `status='revoked'` and the import `error_code='reauth_required'`, and the UI offers **Reconnect** instead of Retry. `error_code='queue_unavailable'` means `REDIS_URL` is unset or no RQ worker is running.

## Docker and Dokploy (Hetzner)

Production container image: **[`Dockerfile`](Dockerfile)** at this repo root (use this folder as the Git repo root when you split from a monorepo).

- **Health check:** `GET /health`
- **Port:** set `PORT` in the environment (default `8000`).
- **Optional migrations on start:** `RUN_MIGRATIONS_ON_START=1` (see [`docker-entrypoint.sh`](docker-entrypoint.sh)).
- **Secrets template:** [`.env.example`](.env.example) — copy locally; in Dokploy set variables in the UI.

Step-by-step deploy (worker second app, migrations, CORS): **[`DEPLOY.md`](DEPLOY.md)**.

## Notes

- `email-validator` is required by the current Pydantic model definitions and should be installed in the virtual environment.
- Some runtime warnings may appear for `orm_mode` because parts of the codebase still use Pydantic v1-style config keys.
