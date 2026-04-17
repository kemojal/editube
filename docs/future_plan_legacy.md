# Editube — Future Plan

Beat Frame.io, Wipster, Vimeo Review, Dropbox Replay. Target: freelance editors, boutique agencies, YouTubers. Winning lever: speed, AI, zero-friction client review, creator-native workflows.

---

## 1. Zero-Friction Client Review (Frame.io's weakest point)

Clients hate signups. Kill the barrier.

- **No-login review links** — tokenised URL, optional password, optional expiry, optional watermark with viewer IP/name. Client lands → comments immediately as "Guest (Name)".
- **Magic-link review** — email address only, one-tap verification, name auto-filled thereafter.
- **Client avatar capture on first comment** — no profile setup friction.
- **Review link analytics** — did the client open it? How long watched? Which sections rewatched? Did they reach the end? (Frame.io charges enterprise for this.)
- **Per-version review rooms** — each version gets its own comment thread, side-by-side compare of v1/v2.
- **Client-side comment drafts** — saved locally, survives tab close.
- **"Done reviewing" button** — explicit sign-off with timestamp + legal trail.
- **Download gating** — require approval before download unlocks.
- **Client comment grouping by scene/chapter** — auto-cluster temporally close comments.

## 2. Review & Approval Workflow

- **Approval stages** — configurable pipeline (Editor → Producer → Client → Legal), each stage auto-notifies next.
- **Sign-off with signature** — typed name or drawn signature, legally binding PDF export.
- **Change requests vs comments** — separate types; change requests block approval until resolved.
- **Threaded replies on comments** (already partially done — extend).
- **@mentions** with notification + email digest.
- **Comment status** — open, in-progress, resolved, wontfix, reopened.
- **"Changes since your last visit" diff view** — highlights new comments/replies since client last viewed.
- **Bulk comment operations** — resolve all, export all, reassign all.
- **Comment export** — CSV, PDF, XML (Premiere markers), EDL, FCPXML.
- **Auto-import comments as markers in Premiere/DaVinci/FCP** via plugin.

## 3. AI Features (the real moat)

Frame.io's AI is weak. Own this.

- **AI video summarizer** — auto-generate 30s highlight for clients before deep review.
- **AI chapter detection** — auto scene/chapter markers from shot changes + audio.
- **Transcription + speaker diarization** (partial exists — extend).
- **Transcript-based editing comments** — click a word in transcript, comment attached to exact timecode.
- **AI silence/filler remover** preview — "remove 14 ums, 23 silences > 0.5s — apply?"
- **AI auto-captions** with editable styles + burn-in export.
- **Multi-language subtitle translation** via Whisper + GPT.
- **AI thumbnail generator** — extract best frames, score for faces/motion/contrast.
- **AI title/description generator** for YouTubers — reads transcript, generates SEO-optimised metadata.
- **AI B-roll suggestor** — "this 8s section on 'mountain hike' has no visuals, here are 5 stock clips matching."
- **AI client briefing digest** — "Client said: loves intro, wants tighter middle, needs brand logo bigger at 0:45."
- **AI review assistant** — answers client questions about the video ("what's the total runtime of interview segments?").
- **AI rough-cut generator** — upload raw footage + brief, get a rough cut in the editor's style.
- **Voice-to-comment** — hold mic, speak review, auto-transcribed + pinned to timecode.

## 4. Creator-Native Features (YouTuber hook) — **MVP shipped; gaps documented**

Frame.io ignores solo creators. Win them. The **vertical slice is in production code** (tables, REST, Creator studio UI, RQ jobs). Treat bullets below as **shipped / partial / ops** so roadmap matches the repo.

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **YouTube direct upload** | Draft publication → `POST .../publish` queues **`youtube_publish_job`** (Google Data API) when the author has **YouTube connected**, video has **`file_path`**, **Redis + RQ worker** run, and credentials refresh cleanly. |
| **Partial** | **TikTok / Shorts / Reels-style exports** | **Aspect export** jobs (16:9 → 9:16, 1:1, etc.) run via **`aspect_export_job`** (center / smart crop per README). **AI subject tracking** beyond crop is **not** implemented (`creator.py` docstring). |
| **Shipped** | **Multi-platform aspect preview** | List/create/delete aspect exports + studio UI; outputs depend on worker. |
| **Partial** | **Thumbnail A/B hub** | Variants, **winner** flag, **impressions/clicks** on model + API; studio **Thumbnails** tab explains that **YouTube does not expose per-variant A/B stats** off-platform — editors **enter metrics manually** (e.g. from YT Studio). |
| **Partial** | **Chapter marker export to YouTube** | Manual chapters + **YouTube description block** endpoint; **`POST .../chapters/auto`** enqueues **LLM synthesis** when transcript + AI env exist (`chapter_synthesis_job`). |
| **Shipped** | **End-screen + pinned-comment drafts** | Stored on video / publication models; surfaced in Creator studio where wired. |
| **Shipped** | **Brand deal tracking** | Project-scoped CRUD (`/creator/projects/{id}/brand-deals`). |

Backend: `routes/creator.py`, `models/creator.py`, related tables. Frontend: `app/(videos)/player/[id]/studio/page.tsx`, `lib/api/creator.ts`.

## 5. Freelancer Business Layer — **v1 shipped** (configure Stripe, SMTP, workers as needed)

Freelancers juggle invoicing, scope, handoff. Bake it in.

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Scope-locked projects** | `projects.scope_revisions_included`, `change_request_fee_cents`; client-facing counter via review/scope payloads. |
| **Shipped** | **Revision counter** | `project_revisions`; billable when over included count; **Freelancer hub** + review integration. |
| **Shipped** | **Integrated invoicing** | **Stripe Connect** (account, account link, status) + **create / send / mark paid / webhook** when `stripe` + keys are configured; optional **`FREELANCER_ALLOW_PLATFORM_INVOICES`** for dev. |
| **Shipped** | **Deposits + milestones** | `project_milestones`, **50/50 seed** endpoint; **`invoice_id`** FK; **UI**: link milestone ↔ invoice in **Milestones** tab, **`?tab=`** deep links, and **cross-links** between **Invoices** and **Milestones** tabs. |
| **Shipped** | **Deliverables lock** | `projects.deliverables_locked`; enforced in **review / download** paths. |
| **Shipped** | **Client contracts + e-sign** | `contracts` table; public JSON under **`/api/public/freelancer/contracts/{token}`**; **signing UI** at **`/contract/{token}`** (link emailed/API). PDF + Cloudinary when configured. |
| **Shipped** | **Time tracking** | `time_entries` + hub **Time** tab. |
| **Shipped** | **Project estimator** | Rate card + runtime/complexity; hub **Estimator** tab. |
| **Shipped** | **Portfolio showcase** | Public **`/portfolio/[slug]`** when scope marks portfolio public + slug set. |

Backend: `routes/freelancer.py`, `models/freelancer.py`, migrations including `v3w4x5y6z7a8_add_creator_freelancer_features.py`. Frontend: `app/(sites)/projects/[id]/business/` (modular `_components`), `app/portfolio/[slug]/page.tsx`, `lib/api/freelancer.ts`.

## 6. Agency & Team Features

- **Workspaces with role hierarchy** — owner / producer / editor / assistant / client / guest.
- **Project templates** — wedding, podcast, YouTube long-form, ad spot, each with folder structure + review stages.
- **Shared asset library** — logos, LUTs, music, SFX, lower-thirds; drag into projects.
- **Internal vs client comment threads** — team discusses privately, client never sees.
- **Assignment + task tracking** — comment → task → assignee → due date → status.
- **Team capacity view** — who's on what, when's free.
- **White-label mode** — custom domain, logo, colors on client-facing pages (big agency sell).

## 7. Editor Integration (steal Frame.io's Camera-to-Cloud moat)

- **Premiere Pro panel** — open project, comments appear as markers, two-way sync.
- **DaVinci Resolve integration** — same, via Workflow Integrations API.
- **Final Cut Pro X** — via FCPXML round-trip.
- **After Effects** — comment-to-comp-marker sync.
- **Camera-to-cloud ingest** — mobile app records + uploads, auto-proxies, ready for review before editor even touches it.
- **Proxy generation** — 540p H.264 for fast review, original for delivery.
- **Watch folder** — drop export in local folder, auto-uploads new version.

## 8. Delivery & Handoff

- **Delivery packages** — one approved version + source files + captions + thumbnails, zipped.
- **Multi-format export** — 4K master, 1080p YT, 720p social, auto.
- **Client-branded delivery page** — "Your video is ready" with play + download + share buttons.
- **Expiring download links** — 30-day access, auto-renewable.
- **Delivery receipt tracking** — client downloaded? which file? when?
- **Archive + cold storage** — old projects auto-move to cheap tier after 90 days.

Implementation note: Section 8 is implemented in the current codebase (delivery packages, multi-format exports, branded delivery page, expiring renewable links, receipts, and retention/cold-storage flow). Current cold-storage backend is `local_fs`; add an R2/S3 provider adapter for production "cheap tier" storage.

## 9. Collaboration Quality-of-Life

- **Live cursors + presence** — see who's viewing, where their playhead is (Figma-style).
- **Watch party mode** — synced playback for remote review calls, shared chat.
- **Recorded review sessions** — screen+audio capture of review with comments baked in.
- **Picture-in-picture client review** — review overlay that stays while client does other work.
- **Hotkey cheatsheet** — J/K/L, `,` `.` frame step, C comment, M mark — editor muscle memory.
- **Keyboard-driven comment entry** — press C anywhere, instant comment, timecode auto-captured.
- **Comment on transcript lines** — click word, comment attached.
- **Loop region comments** — comments on time ranges, not just points (partial — extend UX).

## 10. Security & Trust (enterprise moat)

- **DRM-lite** — forensic watermark per viewer (email, IP, timestamp burned invisibly).
- **Screen-record detection** — flag + notify owner.
- **2FA, SSO (Google, Okta, Azure AD)**.
- **Audit log** — every view, comment, download, permission change, timestamped, exportable.
- **NDA gate** — client must accept NDA before viewing.
- **Geofencing** — restrict by country.
- **Expiring shares** with auto-revoke.
- **SOC 2 path** for agencies that need it.

## 11. Mobile

- **Native iOS/Android review app** — pinch-zoom, draw on video, voice comment.
- **Offline review** — download on phone, comment offline, sync when back.
- **Push notifications** for @mentions + approvals.
- **Record-to-upload** — client phones in a VO note attached to a comment.

## 12. Pricing Levers vs Frame.io

Frame.io: ~$15-45/user/mo, charges per seat, clients count.

- **Free tier with unlimited guest reviewers** (Frame.io charges $$$ here).
- **Per-project pricing option** — pay $X for one project, no subscription.
- **Freelancer tier at $9/mo** — 1 editor, unlimited clients, 100GB, all AI features.
- **Agency tier at $25/editor** — unlimited clients, white-label, 1TB pooled.
- **Pay-as-you-go storage** — $0.02/GB vs Frame.io's inflated tiers.

## 13. Quick Wins (do first, highest ROI)

Ordered by effort-to-impact:

1. **No-signup review links** — moat, low effort.
2. **Comment export to Premiere/DaVinci markers** — one weekend, steals pros immediately.
3. **AI transcript-pinned comments** — extend existing transcription.
4. **Revision counter + scope lock** — trivial feature, massive freelancer pull.
5. **Watch analytics on review links** — 2 days work, closes deals.
6. **Side-by-side version compare** — high-demand, medium effort.
7. **Live cursors/presence** — perceived-magic feature, low-medium effort.
8. **YouTube direct publish** — half day of OAuth, huge YouTuber hook.
9. **Mobile web review** — ensure current player works flawlessly on phone.
10. **Keyboard shortcuts matching Premiere** — J/K/L, frame step, zero-cost moat for pros.

## 14. Positioning

Three taglines to A/B:

- _"Frame.io for freelancers who also want to get paid."_
- _"Video review that your clients actually use."_
- _"Ship edits, not subscriptions."_

Frame.io sells to studios. Wipster sells to marketing teams. Editube should sell to the _operator_ — the solo editor or 5-person shop who wants fewer tabs, faster client signoff, and a paycheck at the end.
