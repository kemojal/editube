# Editube — Future Plan

Beat Frame.io, Wipster, Vimeo Review, Dropbox Replay. Target: freelance editors, boutique agencies, YouTubers. Winning lever: speed, AI, zero-friction client review, creator-native workflows.

> **Roadmap status (audited against codebase).** Each section below is tagged **Shipped / Partial / Missing** per item with file-level evidence. High-level snapshot:
>
> - **Core moats already in production:** §1 zero-friction review, §2 approval workflow, §3 most AI (summarize, chapters, captions, translation, thumbnails, B-roll, briefing, chat), §4 creator studio vertical slice, §5 freelancer business layer, §6 workspaces + templates + branding, §8 delivery/handoff, §10 forensic + SSO + audit + NDA + geofence, §11 mobile MVP.
> - **Biggest remaining gaps:** §7 NLE integrations (Premiere/DaVinci/AE panels, proxies, watch folder, ingest), §9 watch-party + PIP + loop-region comments, §3 rough-cut executor + voice-to-comment backend + title/description generator, §10 screen-record detection + SOC 2, §12 per-project pricing + PAYG storage, §6 team capacity view, §8 R2/S3 cold-storage adapter.

---

## 1. Zero-Friction Client Review (Frame.io's weakest point) — **mostly shipped**

Clients hate signups. Kill the barrier.

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **No-login review links** | Token + optional password/expiry/watermark. `routes/review_links.py`, `models/review_links.py`. |
| **Shipped** | **Magic-link review** | `ReviewMagicToken` model + send/verify endpoints. |
| **Shipped** | **Client avatar capture on first comment** | `guest_avatar_url` on `ReviewSession`. |
| **Shipped** | **Review link analytics** | `ReviewEvent` (play/pause/seek/progress), `max_position`, `reached_end`, heatmap in `ReviewAnalyticsResponse`. |
| **Partial** | **Per-version review rooms** | `version_group_id` + multi-version list endpoint exist; **side-by-side v1/v2 compare UI not built**. |
| **Shipped** | **Client-side comment drafts** | `ReviewCommentDraft` model + public draft endpoints. |
| **Shipped** | **"Done reviewing" sign-off** | `ReviewSignoff` with typed/drawn signature, PDF export. |
| **Shipped** | **Download gating** | `approval_required_for_download` + download-blocker check. |
| **Shipped** | **Client comment grouping by scene/chapter** | `build_review_scene_groups` auto-clusters by chapter/gap. |

## 2. Review & Approval Workflow — **mostly shipped**

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Approval stages** | `ReviewWorkflowTemplate` / `Stage` / `Run`; ordered progression. |
| **Shipped** | **Sign-off with signature** | Typed/drawn + `pdf_url` via `review_signoff_pdf.py`. |
| **Shipped** | **Change requests vs comments** | `kind: comment \| change_request`, blocks approval. |
| **Shipped** | **Threaded replies** | `parent_id` on Comment. |
| **Shipped** | **@mentions w/ notification + email digest** | `services/mentions.py`, `mention_digest` + `mention_email` jobs. |
| **Shipped** | **Comment status** | open/in-progress/resolved/wontfix/reopened on `CommentUpdate`. |
| **Shipped** | **"Changes since your last visit"** | `PublicReviewCommentDeltaResponse`. |
| **Shipped** | **Bulk comment operations** | `CommentBulkAction`. |
| **Shipped** | **Comment export** | CSV/PDF/EDL/FCPXML/Premiere XML via `services/comment_export.py`. |
| **Missing** | **NLE plugin auto-import** | Export formats exist but no Premiere/DaVinci/FCP panel / two-way sync. |

## 3. AI Features (the real moat) — **mostly shipped**

Frame.io's AI is weak. Own this. Routes in `routes/ai.py`, jobs in `jobs/ai_jobs.py` + `chapter_synthesis.py` + `transcription.py`.

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **AI video summarizer** | `/ai/summarize` returns highlight segments. |
| **Shipped** | **AI chapter detection** | `/ai/chapters` + `chapter_synthesis_job`. |
| **Shipped** | **Transcription + speaker diarization** | `transcription.py` with speaker tracking (diarization minimal pre-WhisperX). |
| **Shipped** | **Transcript-based editing comments** | `transcript_segment_index`, `word_start_index/end_index`, `anchor_text` on public comment create. |
| **Shipped** | **AI silence/filler remover preview** | `/ai/detect-fillers`, `/ai/remove-fillers`. |
| **Shipped** | **AI auto-captions** | `/ai/captions/generate`, editable style/segments, `burned_video_url`. |
| **Shipped** | **Multi-language subtitle translation** | `/ai/translate` w/ target_language. |
| **Shipped** | **AI thumbnail generator** | `/ai/thumbnails` with per-frame score/reason. |
| **Missing** | **AI title/description generator** | No route / job found. |
| **Shipped** | **AI B-roll suggestor** | `/ai/broll-suggestions`. |
| **Shipped** | **AI client briefing digest** | `/ai/briefing-digest` + builder job. |
| **Shipped** | **AI review assistant (Q&A)** | `/ai/chat` contextualized on transcript + comments. |
| **Partial** | **AI rough-cut generator** | `/ai/rough-cut` endpoint queues request but **no executor job** — stub only. |
| **Partial** | **Voice-to-comment** | Web `SpeechRecognition` hook exists in review client; **no backend STT route** for audio uploads. Mobile `VoiceNoteRecorder` attaches audio blob, not transcript. |

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

## 6. Agency & Team Features — **mostly shipped**

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Workspaces w/ role hierarchy** | `WorkspaceMember.role` (owner/producer/editor/assistant/client/guest); RBAC in `workspace_permissions.py`. |
| **Shipped** | **Project templates** | `ProjectTemplate` w/ JSONB def + `project_template_apply.py`. |
| **Shipped** | **Shared asset library** | `WorkspaceAsset` + `ProjectWorkspaceAssetLink`. |
| **Partial** | **Internal vs client comment threads** | Private-comment flag + visibility service exist; **no hard separation between internal channel and client thread UI**. |
| **Shipped** | **Assignment + task tracking** | `assignee_user_id` on Comment + `routes/project_tasks.py`. |
| **Missing** | **Team capacity view** | No workload/capacity endpoint or UI. |
| **Shipped** | **White-label mode** | `WorkspaceBranding` (custom_domain, logo_url, primary_color) + `workspace_branding_resolve.py` + DNS verify. |

## 7. Editor Integration (steal Frame.io's Camera-to-Cloud moat) — **largely missing**

| Status | Item | Notes |
|--------|------|--------|
| **Missing** | **Premiere Pro panel** | No `.jsx`/UXP panel. Export XML only (one-way). |
| **Missing** | **DaVinci Resolve integration** | None. |
| **Partial** | **Final Cut Pro X (FCPXML)** | `export_comments_fcpxml` one-way export; no round-trip ingest. |
| **Missing** | **After Effects comment-to-marker** | None. |
| **Partial** | **Camera-to-cloud ingest** | Mobile app (`edu_mobile/`) has upload hooks but no dedicated ingest→auto-proxy pipeline. |
| **Missing** | **Proxy generation (540p H.264)** | No ffmpeg proxy job found. |
| **Missing** | **Watch folder** | No local folder monitor. |

## 8. Delivery & Handoff — **shipped**

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Delivery packages** | `DeliveryPackage` + `delivery_package_job` (zip + manifest). |
| **Shipped** | **Multi-format export** | `multi_format_export.py` job (4K/1080p/720p presets). |
| **Shipped** | **Branded delivery page** | Public `delivery.py` endpoint merges `WorkspaceBranding`. |
| **Shipped** | **Expiring renewable download links** | `DeliveryLink.expires_at` + renew endpoint. |
| **Shipped** | **Delivery receipts** | `DeliveryReceipt` tracks downloader + file + timestamp. |
| **Partial** | **Archive + cold storage** | `archive_cold_storage_job` + `ProjectRetentionPolicy` (default 90d). Backend today is `local_fs` — **needs R2/S3 adapter** for production cheap tier. |

## 9. Collaboration Quality-of-Life — **partial**

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Live cursors + presence** | `RoomPresence` type + WebSocket `presence.update` via `websocket_manager.py`. |
| **Missing** | **Watch party mode** | Presence exists; no synced playhead / shared chat. |
| **Partial** | **Recorded review sessions** | `PublicReviewRecordingCreate` + session recording model; no screen-record enforcement. |
| **Missing** | **Picture-in-picture client review** | None. |
| **Shipped** | **Hotkey cheatsheet** | J/K/L, `,`/`.`, C, M, ? in review-client. |
| **Shipped** | **Keyboard-driven comment entry** | C hotkey focuses composer. |
| **Partial** | **Comment on transcript lines** | Transcript anchoring fields exist on comment model; **click-to-comment gesture on transcript UI not wired**. |
| **Missing** | **Loop region comments** | No range-based comment creation. |

## 10. Security & Trust (enterprise moat) — **mostly shipped**

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Forensic watermark (DRM-lite)** | `forensic_watermark.py` SHA256 fingerprint (link/session/email/IP/country/hour) + `ReviewForensicAsset` + `review_forensic` job. |
| **Missing** | **Screen-record detection** | `recording_detection_mode` field exists but no detection logic. |
| **Shipped** | **2FA + SSO** | `mfa_totp.py`; `oidc_sso.py` (Google/Okta/Azure AD via OIDC discovery). |
| **Shipped** | **Audit log** | `SecurityAuditLog` + `/security/audit` w/ CSV export. |
| **Shipped** | **NDA gate** | `NDADocument`, `NDAAcceptance`, `nda_required` on links. |
| **Shipped** | **Geofencing** | `geoip.py` country allow/block in review gate. |
| **Shipped** | **Expiring shares w/ auto-revoke** | Link expiry + `review_links_maintenance` job. |
| **Missing** | **SOC 2 path** | No compliance/attestation framework. |

## 11. Mobile — **shipped (MVP)**

Expo app at `edu_mobile/`.

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Native iOS/Android review app** | Expo Router tabs + review screen. |
| **Shipped** | **Offline review** | `lib/offline/outbox` + sync engine for pending mutations. |
| **Shipped** | **Push notifications** | `expo-notifications` + `DevicePushToken` model + push job. |
| **Shipped** | **Record-to-upload VO note** | `VoiceNoteRecorder` + `attachVoiceNote` API. |

## 12. Pricing Levers vs Frame.io — **partial**

Frame.io: ~$15-45/user/mo, charges per seat, clients count.

| Status | Item | Notes |
|--------|------|--------|
| **Shipped** | **Free tier w/ unlimited guest reviewers** | Guests counted via `ReviewSession`, not billed. |
| **Missing** | **Per-project pricing option** | No per-project checkout flow. |
| **Shipped** | **Freelancer / Agency tiers** | `routes/billing.py` Stripe checkout + portal; plan metadata on `Subscription`. Pricing numbers not yet finalised. |
| **Missing** | **Pay-as-you-go storage** | Storage metering + overage billing not implemented. |

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
