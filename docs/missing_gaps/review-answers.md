# Editube Review Workflow Audit — Answers

Audit of the video review → feedback → revision → approval workflow, grounded in the actual codebase (backend `editube/`, frontend `editube-frontend/`), answering `review.md` deliverable by deliverable.

**One-line verdict:** Editube has an unusually deep *feature perimeter* (review links with watermark/NDA/geofence/analytics, a genuinely strong comment primitive, real-time rooms, AI review) wrapped around a *broken spine*. The core loop — send for review → get feedback → revise → approve → know it's approved — does not hold together end-to-end. Status is a dead field, approval doesn't survive a new version, clients' feedback can arrive silently, there is no review inbox, and the mobile player renders no comments at all. Fix the spine before adding anything.

---

## 1. Existing Workflow Assessment — what's already strong

**The comment primitive is genuinely best-in-class on paper.** `Comment` supports point *and* range timecodes, threads, five workflow statuses (`open/in_progress/resolved/wontfix/reopened`), a `comment` vs `change_request` kind, tri-state visibility (`public/team/author_only`), assignee + due date, FabricJS drawings, transcript-word anchoring with drift detection, optimistic locking (`revision`), idempotency (`client_mutation_id`), an offline op-queue (`/comments/sync`), and export to CSV/PDF/EDL/FCPXML/Premiere markers. Very few competitors have transcript anchoring or NLE marker round-tripping (`/integrations/nle/{vid}/markers` + `/markers/diff`).

**The guest review link is agency-grade.** Password, expiry, revocation with reason, email gate + magic-link identity, NDA click-through, geofencing, forensic watermarking, screen-recording detection with consented session recording, download gating (including "paid invoice unlocks download"), per-session analytics (watch time, heatmap, rewatch hotspots, completion), legal sign-off with typed/drawn signature and generated PDF, and workspace branding with custom domains. This is beyond Frame.io in several places.

**Real-time collaboration exists.** Team video rooms with presence, live labelled cursors with "jump to timestamp," typing signals, follow-host playback sync, host lock and moderation, chat with sequence-replay reconnect. The guest review page gets the same room model plus a delta feed ("N new since your last visit").

**The annotation toolkit is real.** Eight tools with hotkeys, 8 colors + custom, stroke weights, per-shape opacity and on-screen duration, undo — plus annotations as first-class timeline markers.

**The freelancer/agency business layer is a differentiator nobody in this category has.** Scoped revision rounds with billable overage (`ProjectRevision`, `scope_revisions_included`, `change_request_fee_cents`), invoices, milestones, contracts with e-sign, estimates, time entries, delivery packages tied to an `approved_version_id`, delivery links with receipts.

**AI foundation is deep.** Whisper transcription with diarization, AI review scorecard with timestamped severity-ranked notes that convert directly into comments/change requests, chapters, captions, summaries.

The honest framing: the *materials* for the best review product in the market are all here. The *workflow* connecting them is not.

---

## 2. Missing Features

### Critical (the product does not work as a review tool without these)

1. **A working status/approval spine.** `Video.status` (`in_progress/in_review/approved/needs_changes`) exists in the DB and API but is **dead in the UI**: `player-header/status-config.tsx` is imported by nothing, `updateVideoStatus` (`lib/api/videos.ts:111`) is called by nothing. There is no "Send for review," no "Request changes," no "Approve" on the internal player, no status badge anywhere. Guest approval (`POST /review/{token}/approve`) sets `ReviewSession.approved_at` and **never touches `Video.status`**. Four approval mechanisms (video status, guest approval, workflow stages, legal sign-off) exist and none of them talk to each other.
2. **Review inbox / "Needs Your Attention."** `/dashboard/reviews` literally redirects to `/dashboard`; the built `reviews-page.tsx` component is orphaned. There is no way to answer "which of my 30 videos needs me right now."
3. **Owner notification for guest comments.** The public comment endpoint (`review_links.py:1557+`) notifies mentions only. **A client can leave 20 comments and the editor is never told.** This is the single worst bug in the workflow — client feedback silently rotting is the exact failure mode this product exists to eliminate.
4. **Comment carry-forward across versions.** When V2 is uploaded, V1's unresolved comments become read-only history (`?include_prior=true`). Nothing carries an open change request forward; an editor's punch list evaporates on every version bump.
5. **Mobile player.** Below 1024px, `player-workspace-body.tsx:266–372` renders the video and a Comments/Tasks tab pill that **toggles nothing** — the panels only render in the `isDesktop` branch. Scenario 5 (creator reviews on phone) fails completely on the internal player. (The public `/review/[token]` page is more complete — clients are okay; the *creator/editor* is not.)
6. **Request-changes action.** No reject affordance exists anywhere — not for guests, not for team. The only "no" a reviewer can express is composing a `change_request` comment.
7. **Resumable upload.** Single XHR, 5 GB hard cap with a 413 that references a multipart flow that doesn't exist, modal blocked while uploading, no retry. Video people upload multi-GB files on hotel Wi-Fi; this will burn them weekly.
8. **Bug: `GET /review/{token}/versions` 500s on every call** — `review_links.py:2216–2245` reads `link.project` / `link.project_id`, neither of which exists on `ReviewLink`. The guest version switcher is dead on arrival.

### High-value

- **"What changed in this version"** — a version-notes field at upload (manual first, AI-drafted later) + changed-region markers on the timeline.
- **Notification grouping/digest** — 10 comments = 10 rows/10 pushes today; no `read_at`, no actor on the notification, no coalescing ("Sarah left 7 comments on Summer Campaign V2").
- **Notifications for the events that matter**: new version uploaded, status change, change request resolved, assignment, due date approaching. None are emitted today (only 7 types exist: mention, comment, approval, review_workflow, project/workspace invites).
- **Approval invalidation on new version** + per-user, per-version approval records (today approval is per guest *session*).
- **Enforced approval authority** — today any project member can flip status and advance workflow stages; stage `notify_user_ids` is a mailing list, not an authorization list.
- **Reviewer selection + deadline at send time** — upload modal collects name and description only; `Project`/`Video` have no due-date column at all.
- **Comment attachments in the UI** — the backend fully supports voice notes/images/files with waveforms and transcripts (`CommentAttachment`); the frontend has no upload control. Free feature, sitting unshipped.
- **Comment search + global search** — the only search in the product is an ILIKE on folder/video names.
- **Frame-accurate comments** — `Comment.timecode` is Integer *seconds* (annotations get a Float). A review tool that can't say "frame 847" undersells its own drawing tools.
- **Multi-select comment triage** — only "Resolve all (top-level)" exists; `handleNextUnresolved` is built and never rendered (`comments-panel.tsx:2751`).
- **Mentions that reach the team** — the frontend builds @-candidates from uploader + prior thread participants only, while the backend has `list_users_for_mentions` returning the whole project team. Also: handles are fuzzy-derived from names/emails (no `username` column) and can collide.
- **Comment-density heat on the scrubber** — the data exists (`ReviewAnalytics.heatmap`, hotspots) and is only shown as a "{n} hotspots" chip.
- **Project-level share links** — links are per-video only; "review these 10 Reels" requires 10 links.
- **Redis pub/sub for WS** — managers are in-process dicts; real-time silently breaks the day the API scales past one worker.

### Nice-to-have

- Emoji reactions (beyond the single ❤️), comment pinning, "edited" marker + history.
- Wipe/overlay compare modes (side-by-side synced compare already works and is enough for launch).
- Waveform on the timeline (Silero-VAD data already computed for auto-edit — reuse it as a poor-man's waveform).
- Quality selector / HLS (proxies already generate 540/720/1080 H.264 — a manual quality switcher is cheap; full HLS can wait).
- Loop-range A/B markers independent of comments; SMPTE/timecode input box; frame-step buttons in the transport bar.
- Batch library actions (multi-select move/share/status).
- Caption track upload (VTT) + working captions toggle — the Captions button is currently inert (`video-area-controls.tsx:251`).
- View-limit and per-recipient share tokens.

### Future

- NLE panel integrations (Premiere/Resolve extensions) built on the existing marker sync API — genuinely differentiating, not MVP.
- AI change-detection between versions (visual diff).
- Client portal home (all their projects/videos in one branded page).
- Reviewer templates / default approval flows per client.
- Native mobile apps (after the responsive web player actually works).

### Avoid / unnecessary

- **Per-section approval and per-comment approval** — the prompt asks; the answer is no. Approval is a video-level decision; comment `resolved` already covers item-level closure.
- **Six comment statuses** — current five are already one too many. Collapse `reopened` into `open` (keep the event in history). Ship Open / In progress / Resolved / Won't change; "Needs clarification" is a *reply*, not a status.
- **Manual feedback categories (Editing/Audio/Color/…)** — reviewers won't tag; editors don't need it at typical comment volumes. If ever, AI-classify silently for filtering. Don't build a taxonomy UI.
- **Kanban/task-management expansion of the tasks panel** — keep it a checklist over comments; don't become Asana.
- **A seventh approval state machine** — see §8; collapse the four existing mechanisms into one, don't add a fifth.
- **More link-security features** — NDA/geofence/forensic watermark/recording detection are already past the market. (Do add form controls for the ones that exist — they're currently hardcoded in `share-review-dialog.tsx:188–196` — but build no more.)

---

## 3. Workflow Problems — where users get hurt today

| Problem | Cause |
|---|---|
| **Editor never learns client left feedback** | Guest comments notify mentions only; no owner/team notification path |
| **Nobody can tell what state a video is in** | Status field dead in UI; no badges on cards, player, or dashboard |
| **"Was this approved?" is unanswerable** | Guest approval lives on `ReviewSession`, invisible on the internal player except buried in share-dialog analytics; doesn't set video status |
| **Client approves V2 while V3 exists** | Approval isn't version-invalidated; guest version list endpoint 500s so guests can't even see there's a newer cut |
| **Editor loses the punch list every version** | No carry-forward; unresolved V1 change requests become read-only history on V2 |
| **Reviewer reviews the wrong version** | No "you're viewing an old version" gate on the guest page; internal "Go to latest" chip exists but nothing prevents commenting on stale cuts |
| **Notification fatigue or notification silence — nothing between** | No grouping, no digest (mentions only), and the important events (new version, status change, resolution) emit nothing at all |
| **Accidental internal-comment exposure risk is well-handled, but…** | Visibility is filtered server-side (`is_client_visible()`) — good — yet the composer defaults and per-link overrides deserve an explicit "INTERNAL" visual treatment stronger than a badge chip; one mis-set default is a fired client |
| **Reviewer on phone is stuck** | Internal player renders no comment panel below 1024px |
| **Upload fails at 90% and starts over** | No resume, no retry, modal locked during upload |
| **Anyone can approve / advance stages** | Authorization checks stop at `can_access_project`; approval authority is a UI fiction |
| **Two status endpoints drift** | `PUT /projects/{pid}/videos/{vid}/status` and `PUT /videos/{vid}/status` are near-duplicate copies with the enum hardcoded twice |
| **Review projects special-case surprises** | For `project_type == "review"`, *every* video in the project is treated as one version chain (`video_payload.py`) — upload two unrelated videos and they become "versions" of each other |
| **Repeat work** | AI review notes, analytics heatmaps, comment attachments, and the reviews page are all built and unshipped/orphaned — users re-solve problems the codebase already solved |

---

## 4. Ideal Creator ↔ Editor Workflow

The loop, using only machinery that already exists plus the Critical fixes:

```text
1. Editor uploads V1
   – resumable upload, background-capable
   – fields: name, version notes ("what to look at"), reviewer(s), due date
   – status auto: in_progress → in_review on "Send for review"

2. Creator is notified (grouped): "Cut ready: Summer Reel V1 — due Fri"
   – one tap → player (works on phone)

3. Creator reviews
   – comments auto-timestamp on pause-and-type
   – draw → playback auto-pauses (already true)
   – finishes with ONE required exit action: Approve  |  Request changes
   – "Request changes" requires ≥1 open change_request comment

4. Editor gets ONE grouped notification: "Sam requested changes — 7 comments (4 change requests)"
   – opens Revision Mode (§12): the change requests as a checklist

5. Editor works the list (statuses flip open → in_progress → resolved)
   – when last CR resolves, surface: "All feedback addressed. Upload V2?"

6. Editor uploads V2 as a new version
   – unresolved comments carry forward automatically, tagged "from V1"
   – version notes prefilled from the resolved-CR list (AI-drafted, editable)
   – status → in_review; prior approvals invalidated

7. Creator reviews V2 seeing "What changed" chips; jumps between changed
   sections; compares side-by-side if wanted (already built)

8. Approve → status: approved, locked to that exact video row
   – Delivery package pins approved_version_id (already built)
   – any later upload → status: in_progress with a warning:
     "This will supersede an approved version."
```

Total new concepts for a solo creator: **one status, one exit action**. Everything else is progressive.

---

## 5. Ideal Agency ↔ Client Workflow

Same spine, two additions: **internal stage before client**, and **client isolation via review links** (which is already the architecture — workspace `client` role members are denied project access and only ever see `/review/[token]`; keep that, it's correct and safe).

```text
Editor uploads V1 ─ status: in_review (internal)
  ↓
Internal stage(s): ReviewWorkflowTemplate stages, but with teeth:
  – each stage has stage approvers (authorization, not just notify list)
  – only a stage approver can advance/return
  – internal comments default to visibility: team
  ↓  all internal stages passed
"Send to client" ─ creates/activates the review link, status: client_review
  – client gets branded email → magic link → zero-login review page
  – client actions: comment, Approve, Request changes (new)
  ↓
Client requests changes ─ status: needs_changes
  – account manager consolidates: marks the client-facing "final
    instruction" per conflict thread (decision-owner pattern, §10 of prompt:
    threading + one designated decider, no voting UI)
  ↓
V2 → internal re-review can be SKIPPED by CD for minor rounds (one click:
"straight to client") — agencies live and die by cycle time
  ↓
Client approves ─ ReviewSession.approved_at AND Video.status: approved, atomically
  – sign-off PDF (already built) attached to the version
  – delivery link with receipts (already built)
```

Agency architecture concerns (portals, white-label, custom domains) are largely **already built** (`WorkspaceBranding`, custom-domain verification, branded review/delivery pages). What's missing is only the connective status tissue above, plus a per-client rollup view (Client A: 3 awaiting review, 1 approved).

---

## 6. Versioning System

Keep the current model — version chains of `Video` rows via `version_group_id` with a single authority service (`video_versions.py`). It's simpler than a separate versions table and already threads through payloads, review links, and delivery. Fix the behavior around it:

1. **Comments stay bound to their version** (current design, correct) **but open items carry forward**: on `register_video_version()`, copy every top-level comment with `status in (open, in_progress)` and `kind = change_request` to the new video row with `carried_from_comment_id` + `source_version` metadata, preserving thread, assignee, due date. Timecode carries as-is with a "timing may have shifted" affordance; transcript-anchored comments re-anchor via the existing anchor-remap machinery. Resolved/wontfix comments stay behind as history (current `include_prior` view).
2. **Per-version approval record**: `VideoApproval(video_id, approver_user_id | review_session_id, decision: approved|changes_requested, note, created_at)`. Approval attaches to exactly one `Video` row — this answers "which version was actually approved" forever.
3. **New version invalidates review state**: uploading Vn+1 sets it `in_review` (or `in_progress`), never inherits `approved`, and stamps the superseded version's status as `approved (superseded)` in the UI.
4. **Version notes** (`Video.version_notes` text) captured at upload; rendered as "What's new in V3" above the comment feed and as timeline chips when timestamped (`00:04 — hook shortened`).
5. **Guest side**: fix the `/review/{token}/versions` 500 (model the relationship through `video_id → project`, as `video_payload.py:63` already does correctly); banner on outdated versions: "V4 is available — you're viewing V2"; block *approval* (not commenting) on superseded versions.
6. **Kill the review-project special case** ("every video is a version") or gate it behind explicit user action — silent chaining of unrelated uploads is a data-corruption footgun.
7. Version numbering: immutable ordinals, never reused, deletion soft (`superseded/withdrawn`), so "V3" in a conversation six months later means one thing.

---

## 7. Comment System

Mostly: **ship what's built, prune the rest.**

- **Anchoring**: point, range, spatial pin, transcript-word — all exist; add frame accuracy (Float or `timecode_ms`) to match `Annotation`.
- **Threads**: one visible reply level (current UI) is correct; the backend's arbitrary nesting stays as capability, not UI.
- **Statuses**: Open / In progress / Resolved / Won't change. `reopened` becomes an event on the history, not a state. Status changes by team + comment author; "needs clarification" is a reply + the existing `?`-style filter, not a status.
- **Kinds**: `comment` vs `change_request` is the right split — CRs are the revision contract (they gate approval via `client_approve_blockers()`, carry forward, and populate Revision Mode); comments are conversation.
- **Internal vs client**: keep tri-state visibility with server-side filtering (already correct). UI hardening: internal comments get an unmistakable amber "INTERNAL" treatment; the composer shows *who will see this* ("Visible to client") not just an icon; per-workspace default visibility for agency workspaces = `team`, so exposure requires an explicit act.
- **Mentions**: add a real `username` column (stop fuzzy-matching display names); frontend pulls candidates from `list_users_for_mentions` (whole team), not thread participants.
- **Attachments**: wire the existing `CommentAttachment` backend into both composers — voice notes especially (waveform + transcript fields already exist; a client saying it beats a client typing it).
- **Migration between versions**: per §6 — open CRs carry forward automatically; anything else is opt-in ("bring this comment to V3").
- **Triage**: render the already-built next-unresolved navigation; add shift-click multi-select feeding the existing `/comments/bulk` endpoint.
- Guest comments: notify the project owner/assigned editor (grouped), not just mentions.

---

## 8. Approval System

**Collapse four mechanisms into one source of truth.** `Video.status` is the spine; everything else writes to it.

States (keep the existing enum + one addition):

```text
in_progress → in_review → (approved | needs_changes)
                              ↑ needs_changes → in_review (on new version / re-request)
optional for agencies: in_review splits into internal_review → client_review
```

Rules:

1. Status changes are **explicit user actions with named buttons**: Send for review / Approve / Request changes. No silent transitions except new-version-resets.
2. **Guest approval and video status are one transaction**: `/review/{token}/approve` sets `ReviewSession.approved_at` AND `Video.status = approved` AND writes a `VideoApproval` row. Same for the new guest "Request changes."
3. **Approval authority is enforced**: workflow stage approvers and link-level "this person's approval counts" — checked in the endpoint, not just implied by `notify_user_ids`. Default (no config) = anyone with project access, which preserves today's solo-creator simplicity.
4. **Approve blockers stay** (`client_approve_blockers()` — open change requests block approval) with an override: "Approve with notes" converts remaining open CRs to next-version carry-forwards. Real clients approve with two nits outstanding constantly; don't force a fake extra round.
5. **Multiple approvers**: only via workflow stages (sequential). No quorum/voting UI — a "final decision maker" per stage is the model that matches how agencies actually operate.
6. **Superseding**: new version → `in_review`, prior approval labeled superseded; delivery packages keep pinning `approved_version_id` (already built) so the shipped file provably matches an approved version.
7. Merge the duplicated status endpoints into one with a shared enum constant and transition validation.

`Draft`/`Final`/`Published` states: skip. `in_progress` is draft; delivery packages are "final"; `VideoPublication` is "published." Don't triple-book states across systems.

---

## 9. Notification System

Principle: **notify on state changes and direct address; digest everything else; group always.**

| Immediately (push + in-app) | Grouped (rolling ~15-min window per actor+video) | Daily digest (opt-in, extend the existing mention-digest job) | Never |
|---|---|---|---|
| Review requested from *you* | Comments: "Sarah left 7 comments on V2" | Activity on projects you follow | Your own actions echoed back |
| Approved / changes requested | Replies in threads you're in | Unresolved-comment aging | Someone watched the video |
| @mention | Resolutions: "Alex resolved 5 of your comments" | Upcoming due dates | Every individual comment when >1 in window |
| New version on a video you're reviewing | | | Workflow-stage advances you're not an approver for |
| Comment assigned to you | | | |
| Due today/overdue on your items | | | |
| Client (guest) commented — to owner/editor **(currently missing entirely)** | | | |

Mechanics: add `group_key` (`{type}:{video_id}:{actor}`), `actor_user_id`, and `read_at` to `Notification`; coalesce on insert; one push per group update. Per-project mute. Notification prefs UI (backend `UserSettings` fields exist; no UI does). Emails follow the same grouping — never one email per comment.

---

## 10. AI Opportunities (ranked by workflow value)

1. **Feedback → revision checklist.** Summarize N comments into a deduplicated, timestamp-ordered action list, each item linked to source comments; one click converts to change requests. Directly attacks the 47-comments problem; the AI-review→comment conversion pipeline (`onCreateDraft → postComment`) already proves the pattern.
2. **AI-drafted version notes.** At upload of Vn+1, draft "Changes in V4" from the CRs resolved since Vn (pure metadata, no video analysis needed — cheap and near-perfect). Editor edits, ships. Feeds the "What changed" experience.
3. **Conflict detection.** Flag comments pulling opposite directions ("shorter hook" vs "longer hook"), surface as a "Needs a decision" cluster for the decision owner. High agency value, tractable NLP.
4. **Voice-note transcription + summarization** — `CommentAttachment.transcript` field already exists; makes voice feedback searchable and skimmable.
5. **"Was it addressed?" check.** For each carried-forward CR, compare the region around its timecode across versions and suggest "likely resolved." Ship as suggestion-only (auto-resolving other people's feedback destroys trust).
6. **Comment/transcript semantic search** ("logo animation feedback") — rides on embeddings of comments + existing transcripts; also the answer to §25 Search.
7. **AI review pre-pass before client send** (already built — `video_review.py` scorecard) — reposition it in the flow as "run before sending to client," catching fillers/pacing/caption errors while they're free to fix.
8. *(Later)* Visual version diff / changed-segment detection — expensive, do after #2 proves the manual+metadata path.

Everything ranked here reduces reading, deciding, or re-watching. Skip: AI-written replies, auto-categorization taxonomies, AI approval recommendations.

---

## 11. Review Screen — ideal information hierarchy

The current player layout is fundamentally right (video | tasks | comments, header, path bar). Changes are about *status legibility*, not rearrangement:

```text
┌ Header ──────────────────────────────────────────────────────┐
│ Title · v3  [Status badge]  [Version switcher▾] [Go to latest]│
│ Due Fri · [viewers·health chips]        [Request changes][Approve]│
├──────────────────────────────────────────────┬───────────────┤
│                                              │ Comments panel │
│                Video                         │  ── What's new │
│   (annotation canvas, pins, captions)        │     in V3 card │
│                                              │  ── filter/sort│
│ ┌ Timeline ─────────────────────────────┐    │  ── feed       │
│ │ markers · range bands · chapter ticks │    │  ── prior vers.│
│ │ comment-density heat · changed-region │    │  (collapsed)   │
│ └───────────────────────────────────────┘    │  ── composer   │
│  transport: play · frame ± · time · speed    │   (visibility  │
│             loop · volume · CC · ⛶          │    always shown)│
├──────────────────────────────────────────────┴───────────────┤
│ Path bar: Home › Project › Video · v3   [Live room] [Share]  │
└──────────────────────────────────────────────────────────────┘
```

Why: **status and the two decision buttons live in the header** because "what state is this in and what's my move" is the first question every open; the version switcher sits beside status because the two are one concept ("V3, in review"). **"What's new" leads the comment panel** because it decides whether to watch everything or jump. Tasks panel stays a toggle (editor-mode concern, §12), collapsed by default for reviewers. The client `/review/[token]` page keeps the same skeleton minus tasks/internal controls — one mental model, filtered by role. Below 1024px: video on top, bottom-sheet comment feed with the composer docked above the keyboard (fix the dead tab pill).

---

## 12. Editor Revision Screen

Don't build a new screen — **promote the tasks panel into Revision Mode** on the player:

- Entered via a "Revise" CTA on any video with open change requests.
- Left: video. Right: the checklist — open CRs (carried-forward first), ordered by timecode, each row: checkbox, time, text, thumbnail-on-hover, assignee, source version chip.
- Row click → seek + show drawing overlay; checkbox → `resolved` (with optional "note what you did," which feeds AI version notes).
- Keyboard-first: `N`/`P` next/prev item, `X` resolve, `space` play — an editor should clear 20 items without touching the mouse.
- Progress header: `✓ 8 resolved · ◉ 2 in progress · 2 open`; at zero open → **"All feedback addressed — upload V2?"** with prefilled version notes.
- The panel's current read-only limitation is the thing to fix: status/assignee editing inline, not via round-trip to the comment popover.
- NLE companion: the marker export/sync API already exists (`/integrations/nle`) — surface "Send checklist to Premiere as markers" here. This is the wedge for the editor-side moat.

---

## 13. Review Inbox

Resurrect `/dashboard/reviews` (the redirect currently pointing away is the product's biggest self-inflicted wound) as **Needs Your Attention**:

```text
NEEDS YOUR ATTENTION
▸ Summer Campaign V3      review requested · due today · 12 comments addressed
▸ Product Launch V2       4 unresolved comments assigned to you
▸ Founder Reel #19        client requested changes · 2h ago

WAITING ON OTHERS
▸ Client A / Promo V4     sent to client Tue · not yet opened   [nudge]

RECENTLY RESOLVED
▸ Q3 Teaser               approved by client · sign-off PDF
```

Sections are computed, not configured: (1) videos where you're a requested reviewer/approver and status is `in_review`; (2) open comments assigned to you; (3) status flips toward you (`needs_changes` where you uploaded). "Waiting on others" uses data that already exists (review-link sessions → "not yet opened," approval blockers). Group by client for agency workspaces. This page is the daily entry point — the dashboard grid answers "what exists," the inbox answers "what's my next action," and no amount of notification tuning substitutes for it.

---

## 14. Agency Architecture

The existing shape is nearly right; resist adding layers.

```text
Workspace (= agency)                 ← branding, domains, SSO, roles, library
 ├─ members: owner/producer/editor/assistant   (internal)
 ├─ clients: never log in — they get review links (current model: keep)
 └─ Project (= client engagement or campaign)
     ├─ collaborators (per-project internal access)
     ├─ folders (campaign structure — already a tree)
     └─ Video ─ version chain ─ review links ─ approvals ─ delivery
```

Recommendations: (1) Add an optional `client` entity (name, contacts, branding override) that projects reference, so "Client A: 3 awaiting review" rollups exist — today `client_name/client_email` strings on Project can't aggregate. (2) Do **not** add a Campaign layer — folders already model it. (3) Per-project roles stay minimal: the only distinction that matters is internal vs client-facing, and that's already carried by visibility + review links. (4) Workspace role list (`owner/producer/editor/assistant/client/guest`) is the right size — but back it with DB enums and move approval authority onto workflow stages rather than inventing an `approver` role. (5) Solo creators see none of this: one workspace auto-created, no members screen until they invite someone — progressive disclosure is already how the codebase leans; keep it.

---

## 15. Edge-Case Matrix

| Edge case | Recommended behavior |
|---|---|
| Editor uploads wrong version | "Withdraw version" (soft): hides from reviewers, keeps comments, notifies anyone mid-review; number not reused |
| Client approves V2 while V3 exists | Guest page banners "newer version exists" before approval; if approved anyway, record binds to V2 and internal player shows "V2 approved — V3 unreviewed" (never silently promote) |
| Reviewer comments on outdated version | Allow, tag with version (already inherent — comments bind to video row); carry forward if still open |
| Video replaced mid-review | Never replace in place — versions are append-only; live room broadcasts "V4 just landed" toast |
| Two people reply simultaneously | Already handled: `client_mutation_id` idempotency + `revision` optimistic lock (409) + delta polling; surface the 409 as "comment changed, review your edit" |
| Client changes mind after approval | "Reopen review" → status `needs_changes`, approval record kept with `superseded_by_reopen`; if `deliverables_locked`, existing billable-revision machinery (`ProjectRevision.billable`) prices the round — this is a differentiator, lean in |
| Editor deletes a version accidentally | Soft-delete + restore window; block hard delete of any version with an approval or active link |
| Review link shared externally | Already strong (email gate, magic link, watermark, geofence, revoke-with-reason, session analytics show unknown viewers); add "new viewer joined" digest line |
| 100+ comments | Grouping + virtualized list + density heatmap + filters (mostly exists); AI checklist (§10.1) is the real answer |
| Comment on a frame that changed drastically | Carried-forward CRs flagged "timing may have shifted"; transcript-anchored ones re-anchor or warn (drift detection exists) |
| One approves, another requests changes | Changes-requested wins; video goes `needs_changes`; both records kept; stage model prevents most of this by sequencing approvers |
| Final delivered file differs from approved version | Already solved: `DeliveryPackage.approved_version_id` + receipts; add checksum surface in the sign-off PDF |
| Upload dies at 90% / browser closes | Resumable protocol (tus or S3 multipart), background upload, auto-retry; until then at minimum retry-same-XHR |
| Guest loses connection mid-comment | Already solved: `ReviewCommentDraft` autosave + offline sync queue — ship UI affordance "draft saved" |
| Approval attempted with open change requests | Already blocked (`client_approve_blockers`) — add the "approve with notes → carry forward" escape hatch |
| Same person is client on one project, editor on another | Roles are per-workspace-membership; keep client isolation via links and this never collides |

---

## 16. Micro-UX Improvements (30+)

Player & commenting
1. Pause-and-type auto-attaches timestamp (composer arms on first keystroke while playing → pauses, stamps).
2. Drawing auto-pauses playback (verify it's universal, including the guest page).
3. Clicking a comment seeks *and* flashes its drawing overlay for 2s.
4. Marker proximity: as playhead passes a comment, its row glows in the panel (subtle, no popup).
5. Render the already-built next-unresolved button; hotkey `U`.
6. `Enter` posts, `Shift+Enter` newline — and show which, inline, the first three times.
7. Composer shows "Visible to: Client" / amber "INTERNAL" — words, not just an icon.
8. Auto-draft persistence in the team composer (guest side already has it).
9. Frame-step buttons in the transport bar (hotkeys exist; buttons don't).
10. Comment-density heat strip under the scrubber from existing analytics data.
11. Range comments: show duration while dragging ("2.4s").
12. `Shift+click` timeline = start range (faster than switching to Range mode).
13. Voice-note record button in the composer (backend fully ready).
14. Paste image into composer → attachment (backend ready).
15. When a reply resolves a thread, offer "Resolve thread?" inline.
16. Copy timestamp link (`?t=87.4&commentId=…`) from any comment's overflow.

Versions & review flow
17. "Go to latest" chip → also on the guest page (currently 500s — fix + ship).
18. On version switch, keep playhead time ("you were at 0:47 — stay here / start over").
19. Upload-new-version modal prefills name (`Summer Reel v4`) and shows the open-CR count it will carry forward.
20. "All feedback addressed — send V2 for review?" surfaced when last CR resolves.
21. Version switcher rows show per-version status dot (approved / had changes).
22. After "Send for review," show exactly who was notified.
23. Approve button shows consequence: "Approves V3 · unlocks download."
24. Nudge button on unopened review links ("sent 3 days ago, not opened").

Library & inbox
25. Status badge + unresolved-count on `video-card.tsx` (currently comment-count only).
26. "Waiting on you" dot on the sidebar Reviews item with count.
27. Project tile: client name chip for agency workspaces.
28. Sort library by "needs attention first."

Notifications
29. Notification rows deep-link to the exact comment *and version* (partially exists — add version).
30. Grouped notification expands inline to show the 7 comments without leaving the page.
31. "Mark all read for this project" (per-project, not global-only).

Upload
32. Background upload: toast with progress, navigation allowed, modal not locked.
33. Drag file anywhere on the player → "Upload as new version of this video?"
34. Post-upload processing states visible ("transcoding · proxy · thumbnail") from existing job statuses.

Guest page
35. "Draft saved" indicator (backend autosave exists silently).
36. First-visit 3-step coach: click timeline to comment · draw · approve when done.
37. Show reviewer their own progress: "You've watched 80% · 3 comments left unresolved below."

---

## 17. Biggest Product Opportunities (differentiators)

1. **The revision contract: change requests → checklist → billable rounds.** No competitor connects feedback to *scope and money*. Editube already has `scope_revisions_included`, billable `ProjectRevision`, invoices, contracts, and delivery gated on payment. Making "Request changes" formally consume a revision round — visible to both sides ("Round 2 of 3 included") — turns the review tool into the freelancer/agency operating system. Frame.io cannot follow without becoming a billing company.
2. **Transcript-anchored review.** Comments pinned to *words*, with drift re-anchoring, is built and nearly unique. Marketed and extended (search "logo feedback," speaker-filtered review), it's a visible "this tool is smarter" moment in every demo.
3. **NLE round-trip as the editor moat.** Marker export/import/diff API exists; a thin Premiere/Resolve panel that pulls the change-request checklist as timeline markers and pushes "resolved" back kills the copy-paste-from-WhatsApp workflow permanently. Editors choose tools; editors bring clients.
4. **Evidence-grade approval.** Sign-off PDFs with signatures, forensic watermarks, session analytics, delivery receipts, audit log — already built. Packaged as "provable approval" (this exact file, this version, this person, this timestamp), it wins agencies and brands with legal exposure; nobody else bundles it.
5. **The review inbox as the product's front door.** Every competitor is a folder of videos; "here is exactly what needs you, across all clients" is the emotional promise ("I finally know where every video stands") and it's a computed view over data that already exists.
6. **AI feedback consolidation** (§10.1–3): 47 comments → 9 deduplicated actions → prefilled version notes → "what changed" for reviewers. Closes the loop competitors leave manual on both ends.
7. **Live review rooms.** Presence, cursors, follow-host, chat already work — "watch it together with the client, right in the review page" replaces the screen-share-over-Zoom review meeting.
8. **Review analytics as creative signal.** Rewatch hotspots and drop-off shown *on the timeline* tell editors what confused reviewers before they even comment — unique data exhaust nobody surfaces.

---

## 18. MVP vs Later

### MVP — the excellent core (mostly wiring, little new invention)
- Status spine: Send for review / Approve / Request changes on player + guest page; status badges everywhere; guest approval ↔ video status in one transaction; approval invalidation on new version; merge duplicate status endpoints.
- Owner/editor notification for guest comments + grouped comment notifications.
- Review inbox v1 (three computed sections).
- Comment carry-forward for open change requests + version notes at upload.
- Fix: `/review/{token}/versions` 500; mobile player panels below 1024px; render next-unresolved; mentions from full team roster.
- Resumable/background upload.
- Revision Mode v1 (interactive checklist over existing tasks data).
- Ship dormant built features: comment attachments UI (voice notes first), heatmap on scrubber, form controls for existing link settings.

### V1.x — once the loop is validated
- AI feedback checklist + AI-drafted version notes + conflict detection.
- Notification digests beyond mentions, prefs UI, per-project mute.
- Comment/transcript search; frame-accurate timecodes; multi-select triage.
- Deadlines on videos/projects + due notifications; "waiting on others" nudges.
- Project-level share links; per-version status dots; approve-with-notes.
- Redis pub/sub for WebSockets (before any multi-worker deploy — arguably MVP if scaling sooner).

### Pro / Agency
- Enforced stage approvers + skip-stage fast path; client entity + per-client rollups; client notification preferences; reviewer templates/default flows.
- Revision-round billing surfaced in the review UI ("Round 2 of 3").
- Watch-folder/NLE panel integrations (Premiere first) on the existing marker API.
- Advanced link controls UI (per-recipient tokens, view limits).

### Future
- AI visual version diff / "was it addressed" detection.
- HLS adaptive streaming (proxy pipeline already exists as the stepping stone).
- Client portal home; native mobile apps; wipe/overlay compare; collaborative live annotation.

---

### Closing note

The prompt asks "what would make me never review videos over WhatsApp again?" The answer isn't a missing feature — most features exist, several beyond the competition. It's that **the app doesn't yet close its own loop**: feedback can arrive unheard, status is invisible, approval doesn't bind to a version, and the punch list dies at every version bump. Close the loop with the MVP list above and the existing perimeter (links, analytics, sign-off, billing, AI, real-time) stops being disconnected machinery and becomes an unmatchable moat.
