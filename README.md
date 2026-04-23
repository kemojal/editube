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
