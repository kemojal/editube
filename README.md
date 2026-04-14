# Editube Backend

FastAPI backend for the Editube project.

## Prerequisites

- Python 3.10+ (tested here with Python 3.14)
- pip
- **PostgreSQL** (via `DATABASE_URL`)
- **ffmpeg** on `PATH` (required for video **transcription** worker only; API process does not shell out to ffmpeg)
- **Redis** (optional but recommended if you want uploads to enqueue transcription jobs)

## Run locally

From this directory (`editube/`):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # pip = p-i-p, not "ip"
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
| `REDIS_URL` | e.g. `redis://127.0.0.1:6379/0`. Used to enqueue **transcription** jobs after video upload. If unset, uploads still work but transcription stays **`pending`** (no job runs until Redis + worker are available). |

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
2. A separate **RQ worker** runs `app.jobs.transcription.transcribe_video(video_id)`: downloads the stored video URL, runs **ffmpeg** to 16 kHz mono WAV, runs **faster-whisper**, writes **`segments`** (JSON list of `start` / `end` / `text` in seconds) and sets **`completed`** or **`failed`** (+ `error_message`).
3. The player loads **`GET /videos/{video_id}`** (or project-scoped video detail); the JSON includes nested **`transcription`** (`status`, `segments`, `error_message`). The frontend polls while `status` is `pending`, `queued`, or `processing`.
4. To **start or retry** transcription (e.g. legacy videos, failed jobs, or stuck `pending`), call **`POST /videos/{video_id}/transcription`** or **`POST /projects/{project_id}/videos/{video_id}/transcription`** (same access rules as the video). Returns the same payload shape as **`GET /videos/{video_id}`**. If a job is already **`queued`** or **`processing`**, the API responds **409**.

### Dependencies

Declared in `requirements.txt`: `httpx`, `redis`, `rq`, `faster-whisper` (plus transitive deps such as `ctranslate2`).

### Transcription-related env vars

| Variable | Description |
|----------|-------------|
| `REDIS_URL` | Broker for RQ (see table above). |
| `WHISPER_MODEL_SIZE` | faster-whisper model id (default `base`). Larger models are slower and heavier. |
| `TRANSCRIPTION_DEVICE` | `cpu` or `cuda` (default `cpu`). |
| `TRANSCRIPTION_COMPUTE_TYPE` | e.g. `int8`, `float16` (default `int8` on CPU; use `float16` on GPU as appropriate). |

### Run the worker (second terminal)

Use the **same** virtualenv and working directory as the API (`editube/`), with **ffmpeg** available on `PATH`:

```bash
cd editube
source .venv/bin/activate   # or: .venv/bin/activate on Windows Git Bash
export REDIS_URL=redis://127.0.0.1:6379/0
rq worker -u "$REDIS_URL" default
```

The job function is registered as **`app.jobs.transcription.transcribe_video`**. RQ **`job_timeout`** is set to **3600** seconds in code for long files.

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

## Notes

- `email-validator` is required by the current Pydantic model definitions and should be installed in the virtual environment.
- Some runtime warnings may appear for `orm_mode` because parts of the codebase still use Pydantic v1-style config keys.
