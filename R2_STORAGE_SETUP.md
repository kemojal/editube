# Cloudflare R2 storage — setup

Editube stores uploaded media (videos, images, export renders, clips, PDFs) in
Cloudflare R2. Design/rationale: [`docs/r2-storage-migration-plan.md`](../docs/r2-storage-migration-plan.md).

This file is the operator checklist for provisioning R2 and the env it needs.

## 1. Create the bucket

Cloudflare dashboard → R2 → **Create bucket**.

- Name: **`editube-media`** (lowercase + hyphens only — **underscores are invalid**
  and the S3 API rejects them with `InvalidBucketName`). Matches the account
  convention (`cutframe-storage`, `forumo-media`, …).
- Location: Automatic.

## 2. Enable public read access

R2 objects are private by default. Editube stores permanent public URLs in the DB
and serves them straight to `<video src>` / `<img src>`, so the bucket needs
public read.

**Now (no custom domain yet):** bucket → **Settings → Public Development URL → Enable**.
Cloudflare gives you `https://pub-<hash>.r2.dev`. Use that as `R2_PUBLIC_BASE_URL`.

> ⚠️ `*.r2.dev` is rate-limited by Cloudflare and meant for dev/staging, not
> production traffic. Fine to start; move to a custom domain before real load.

**Later (production):** bucket → **Settings → Custom Domains → Connect Domain** →
`cdn.editube.com` (requires the domain in Cloudflare DNS). Then switch
`R2_PUBLIC_BASE_URL=https://cdn.editube.com`. No code change — just the env value.

## 3. API token (credentials)

R2 → **Manage R2 API Tokens** → Create token with **Object Read & Write** on the
bucket. Gives an Access Key ID + Secret Access Key.

## 4. Environment variables

Add to `editube/.env`:

```bash
STORAGE_BACKEND=r2                 # r2 | cloudinary | local — default cloudinary until R2 is ready
R2_ACCOUNT_ID=<cloudflare account id>
R2_ACCESS_KEY_ID=<from step 3>
R2_SECRET_ACCESS_KEY=<from step 3>
R2_BUCKET=editube-media
R2_PUBLIC_BASE_URL=https://pub-<hash>.r2.dev     # step 2 dev URL now; cdn.editube.com later
```

Notes:
- **Do not set `R2_ENDPOINT_URL`** — the backend derives the S3 endpoint from the
  account id: `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`. (Override only if
  Cloudflare ever changes the endpoint format.)
- `R2_PUBLIC_BASE_URL` is the **public read** URL (step 2), **not** the S3 API
  endpoint. They are different hosts — the S3 endpoint is auth-only and cannot
  serve public media.
- Keep `STORAGE_BACKEND=cloudinary` (or unset) until R2 is verified — existing
  Cloudinary assets keep working regardless (dual-read). Flip to `r2` only after
  the smoke test below passes.

### Browser upload CORS

Project creation uploads videos directly to R2, avoiding API temporary-disk
limits. In the bucket's **Settings → CORS Policy**, allow the frontend origins:

```json
[
  {
    "AllowedOrigins": [
      "http://localhost:3000",
      "http://localhost:3001",
      "http://localhost:3002",
      "http://localhost:3003",
      "http://127.0.0.1:3000",
      "http://127.0.0.1:3001",
      "http://127.0.0.1:3002",
      "http://127.0.0.1:3003",
      "https://editube-kemojals-projects.vercel.app"
    ],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["Content-Type"],
    "MaxAgeSeconds": 3600
  }
]
```

Add every deployed frontend origin explicitly; do not use `*` in production.

## 5. Verify (smoke test)

`boto3` is required (`pip install boto3`, added to `requirements.txt`). Round-trip
test — put an object, read it back over the S3 API and over the public URL, delete it:

```bash
cd editube
.venv/bin/python scripts/r2_smoke_test.py
```

Expect: `head_bucket: OK`, `get_object roundtrip: True`, `public GET: 200 | match: True`.
A failing `public GET` means step 2 (public access) isn't enabled or
`R2_PUBLIC_BASE_URL` points at the wrong host.

## 6. r2.dev + Cloudflare bot protection (gotcha)

Cloudflare's bot protection on `*.r2.dev` **403s requests with a non-browser
User-Agent** (e.g. `Python-urllib/x`, and possibly default ffmpeg/yt-dlp UAs).
Browsers and video players send normal UAs and are unaffected — so `<video>`
playback works fine.

This matters for **server-side** fetches of R2 objects (thumbnail extraction,
proxy/transcode reading a public R2 URL): send a browser `User-Agent`. The
transcription path already uses `FFMPEG_USER_AGENT` for this reason. A custom
domain (`cdn.editube.com`) is not subject to the same r2.dev bot rules — another
reason to move to it for production.

## 7. Current status (2026-07-21)

- ✅ Account id, access key, secret, S3 endpoint — verified (auth + write).
- ✅ Bucket `editube-media` — created.
- ✅ Public access / `R2_PUBLIC_BASE_URL=https://pub-53f4145a20b747748d610646302d6486.r2.dev`
  — verified: public GET `200`, `Accept-Ranges: bytes` (range/streaming works).
- ✅ `scripts/r2_smoke_test.py` — passes end to end.
- ✅ Backend `app/storage/` abstraction + Cloudinary/local backends — built, 26 tests pass.
- ✅ All upload call sites (16 files) routed through the abstraction.
- ✅ Server-side ffmpeg thumbnails (`app/services/thumbnail.py`) — verified end to end
  against real R2 (including the browser-UA fetch fix).
- ⬜ **Activation** — `STORAGE_BACKEND` is not set, so the app still uses Cloudinary.
  Set `STORAGE_BACKEND=r2` in `.env` (+ restart API & worker) to go live on R2.
  Roll back anytime by setting it back to `cloudinary`.
- ⬜ Custom domain `cdn.editube.com` — deferred (domain not owned yet).
