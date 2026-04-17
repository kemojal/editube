# Review comments and NLE markers

Editube exports video comments for offline editing tools. Use these formats as the integration surface for CEP panels, DaVinci scripts, or manual imports.

## Authenticated export

`GET /projects/{project_id}/videos/{video_id}/comments/export?format=` with Bearer auth.

Supported `format` values: `csv`, `pdf`, `edl`, `fcpxml` (alias `fcp`), `premiere` (aliases `xml`, `xmeml`).

## Public export (review link)

When the link has `allow_export: true`, guests may call:

`GET /review/{token}/comments/export?session_id={id}&format=csv`

The session must belong to the link. No bearer token is required.

## Timecode basis

Exports use integer **seconds** from the start of the master file (`Comment.timecode`). EDL and FCPXML use simple `HH:MM:SS:00` style at **30 fps non-drop** for convenience; adjust in your NLE if your sequence uses a different frame rate.

## Premiere and FCPXML

The `premiere` output is a minimal **XMEML** sequence with `clipitem` rows per top-level comment. The `fcpxml` output uses a `gap` with nested `marker` elements. Validate in your NLE version; Adobe and Apple evolve schemas over time.

## Plugin roadmap

A host-side extension should:

1. Call the authenticated export endpoint (store API base URL + token securely).
2. Save the response to a temp path and run the host’s marker import, or parse XML and call the host SDK directly.
3. Map comment `text` to marker names and `timecode` to marker start.

DaVinci Resolve Studio supports Python scripting; Premiere uses ExtendScript CEP. Keep marker text under host limits (often 255–512 characters).

## Mention digest scheduling

Unread @mention batch emails use `app.jobs.mention_digest.run_digest_for_all_users`, which fans out `send_mention_digest_job` per user with `email_mention_digest` set to `daily` or `weekly` on `user_settings`.

**Recommended (production):** run on a host cron (or Kubernetes CronJob) on your desired cadence, with Redis and an RQ worker already running:

```bash
# Example: daily at 08:00 — enqueue one job that processes all digest-enabled users
0 8 * * * cd /path/to/editube && python -c "from app.jobs.queue import enqueue_mention_digest_all_job; enqueue_mention_digest_all_job()"
```

`enqueue_mention_digest_all_job()` returns `False` if `REDIS_URL` is unset; the worker must consume the `default` RQ queue.

**Optional (dev / small deploy):** if `MENTION_DIGEST_INTERVAL_HOURS` is set to a positive number (for example `24`), the API process starts a background loop that calls `enqueue_mention_digest_all_job()` after each sleep interval. Leave unset or `0` to disable this in-process scheduler when you use cron instead.

Daily vs weekly preference is stored per user; the job currently sends one digest when invoked — run cron **daily** for `daily` users and **weekly** for `weekly` users, or run daily for everyone (weekly users get at most one email per invocation when they have unread mentions).
