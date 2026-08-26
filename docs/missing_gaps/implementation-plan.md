# Review Spine — Implementation Plan

Companion to [`review-answers.md`](./review-answers.md). That document says *what* is broken; this one says *exactly how it gets built*, in dependency order, with real file paths, line numbers, schemas, and the UI/UX contract every new pixel must obey.

**Scope of this plan:** the MVP section of the audit — the status spine, guest approval sync, the notification gap, version carry-forward, the review inbox, the mobile player, and revision mode. Everything else stays out (see §12).

---

## 0. Ground truth this plan is built on

Verified by reading the code, not assumed:

| Fact | Evidence |
|---|---|
| Alembic HEAD is `b2c3d4e5f6a7` | `alembic/versions/b2c3d4e5f6a7_activity_feed_cascade_on_project_delete.py`; no other file names it as `down_revision` |
| `Video.status` is written in exactly 2 places | `app/api/routes/videos.py:505`, `app/api/routes/video_detail.py:307` — near-duplicate handlers, one typed (`VideoStatusUpdate`), one `data: dict` |
| Nothing on the frontend reads or writes status | `updateVideoStatus` (`lib/api/videos.ts:111`) and `STATUS_CONFIG` (`player-header/status-config.tsx:6`) both have zero importers |
| Guest approval never touches the video | `review_links.py:1874-1910` sets `ReviewSession.approved_at` only |
| Guest comments notify mentions only | `review_links.py:1557-1695` — the entire notification block sits inside `if handles:` |
| `ReviewLink` has no `project_id` | `models.py:642-693`; `review_links.py:2216-2245` reads `link.project_id` → `AttributeError` → 500 |
| Mobile player renders no panels | `player-workspace-body.tsx:277-336` closes without mounting comments or tasks |
| The notification emit block is copy-pasted 3× | `comments.py:396-469`, `review_links.py:1642-1683`, `review_links.py:670-705` |
| There is no test harness | `tests/conftest.py` is 0 bytes; no `TestClient` anywhere in `tests/` |
| Repo is not under version control | `git status` → `fatal: not a git repository` |

Two design constraints that fall out of this:

1. **`Notification.type` is a free `String`** (`models.py:1069`) with no enum or constraint — new notification types need **no migration**, only a push-title mapping (`app/jobs/push_notifications.py:15-42`) and a frontend `META` entry (`components/Navigation/notification-presentation.tsx`).
2. **Tests run on in-memory SQLite with a `JSONB` → `JSON` compile shim** (`tests/test_video_versions.py:24-27`) and an explicit table subset. `ReviewLink` uses `ARRAY(String)`, which SQLite cannot render — any test touching review links must either exclude that table or run against Postgres. This shapes §11.

---

## 1. Architecture decisions

Decisions made up front so the phases below don't re-litigate them.

**D1 — One status vocabulary, four values, kept.** `in_progress | in_review | approved | needs_changes`. No `draft` (that's `in_progress`), no `final` (that's a delivery package), no `published` (that's `VideoPublication`). Agencies get internal-vs-client review through workflow stages, not through more status values.

**D2 — Status lives behind a service, never written inline again.** New `app/services/video_status.py` owns the vocabulary, the legal transitions, the timestamps, the activity log, and the notification fan-out. Both HTTP handlers become thin wrappers. This is the single fix that stops the two endpoints from drifting.

**D3 — Approval is an append-only record, not a boolean.** New `video_approvals` table. `Video.status` is the *derived, current* answer; `VideoApproval` rows are the *history* that answers "who approved which version, when, and was it superseded." Guest approval and team approval write the same table — that is what makes the four disconnected mechanisms one mechanism.

**D4 — Carry-forward copies, it does not move.** Open change requests are *copied* onto the new version with `carried_from_comment_id` pointing back. The V1 row stays on V1 as history. This preserves the existing `include_prior` read-only view unchanged and makes carry-forward reversible (delete the copies).

**D5 — Approval blockers become video-scoped, client-visible-only.** Today `unresolved_change_requests_for_link` (`comment_workflow.py:36`) filters on `Comment.review_link_id == link.id`, so a change request raised by the team never blocks approval. That's wrong — those are exactly the ones that should block. New scope: all top-level change requests on the video with `visibility == 'public'` and a non-terminal status. Internal (`team`/`author_only`) change requests deliberately do **not** block a client, because the client can't see them and would be stuck with no way to act.

**D6 — Notifications get one emitter.** New `app/services/notifications.py` with an async `emit_notifications(...)`. The three duplicated blocks collapse into calls. Grouping is implemented in the emitter, so every future notification type inherits it for free.

**D7 — The frontend gets a shared vocabulary module.** `lib/review/status.ts` holds the status union, labels, tones, icons, and the legal-transition map — mirroring the backend service. `status-config.tsx` is deleted; its raw-palette colors (`blue-700`, `amber-800`, `emerald-800`, `red-700`) violate the design system (§2) and cannot be revived as-is.

**D8 — Ship behind no feature flags.** The player already has `playerFeatureFlags` for `compare`/`room`/`ai`. The status spine is not an experiment — it's the missing core. Flagging it would mean shipping a half-connected loop again.

---

## 2. UI/UX contract

Every new surface follows the onboarding flow's language, extracted from `app/(auth)/onboarding/**`. These are not suggestions; a review that finds a violation is a blocking review.

### 2.1 Hard rules (inherited verbatim)

1. **Token colors only.** `brand`, `success`, `warning`, `destructive`, `muted`, `card`, `border`, `foreground`, `muted-foreground`. Never `violet-600`, `emerald-500`, `amber-700`, `bg-white`, `bg-black/50`. *(This is why `status-config.tsx` gets rewritten rather than imported.)*
2. **Never build a Tailwind class at runtime.** Literal strings in a `Record` map — JIT will not emit a class produced by `.replace()` or template concatenation. See `step-workflow.tsx:13-22`.
3. **Never `font-bold`.** `font-medium` for headings and titles; `font-semibold` only on 10–11px uppercase eyebrows and badges.
4. **Never `transition-all`.** Enumerate: `transition-[border-color,box-shadow,transform]`, `transition-colors`, `transition-transform`.
5. **Never `shadow-md`+ on a card.** Depth is border + `hover:-translate-y-0.5`. The only shadows in the system are `shadow-sm` (segmented thumb), `shadow-2xl` (modal), and the selection halo `shadow-[0_0_0_3px_hsl(var(--brand)/0.14)]`.
6. **Radius:** buttons `rounded-full`; cards/tiles/modals `rounded-2xl`; banners/rows/inputs `rounded-xl`; badges/chips `rounded-full`.
7. **Async buttons relabel** to a present participle with a real ellipsis (`Approving…`), they do not merely dim.
8. **Every animation guards `prefers-reduced-motion`** and lands on its end state in cleanup.
9. **Skeletons mirror the real component's measured height** (see `onboarding-skeleton.tsx:6-18`), so nothing shifts on load.
10. **Type ladder:** body `text-[13px]`, helper `text-[12px]`, micro `text-[11px]`, card title `text-[15px] font-medium`, page title `text-h3`. Avoid `text-sm`/`text-base`.

### 2.2 The status tone map (single source of truth)

Lives in `lib/review/status.ts`. Literal strings, no runtime construction:

```ts
export const VIDEO_STATUSES = ["in_progress", "in_review", "approved", "needs_changes"] as const;
export type VideoStatus = (typeof VIDEO_STATUSES)[number];

export const STATUS_META: Record<VideoStatus, {
  label: string; tone: BadgeTone; icon: LucideIcon; description: string;
}> = {
  in_progress:   { label: "In progress",    tone: "neutral", icon: Circle,      description: "Being edited. Not sent for review yet." },
  in_review:     { label: "In review",      tone: "brand",   icon: Clock,       description: "Waiting on reviewers." },
  approved:      { label: "Approved",       tone: "success", icon: Check,       description: "Signed off. Ready to deliver." },
  needs_changes: { label: "Changes requested", tone: "warning", icon: AlertCircle, description: "Reviewer asked for edits." },
};
```

Tones resolve through the shared badge (§2.3). `needs_changes` is **warning**, not destructive — asking for changes is the normal, healthy path through a review, and painting it red teaches editors to dread it. `destructive` is reserved for failures and irreversible actions.

### 2.3 Shared primitives to build first

**`components/ui/status-badge.tsx`** — the tone map from the design spec, `ring-1` not `border` (matching the existing precedent at `app/(sites)/projects/[id]/business/_components/status-badge.tsx:1-21`):

```tsx
const TONE = {
  neutral: "bg-muted text-foreground ring-border",
  brand:   "bg-brand/10 text-brand ring-brand/25",
  success: "bg-success/10 text-success ring-success/25",
  warning: "bg-warning/10 text-warning ring-warning/25",
  danger:  "bg-destructive/10 text-destructive ring-destructive/25",
} as const;

// base: "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-semibold ring-1"
// size="xs" for the video-card overlay: "px-1.5 py-px text-[10px]"
```

**Compact pill button** — the onboarding CTA is `h-11 rounded-full px-6`, but the player header is `h-12`/`h-14`. Rather than break the language, define a scaled variant used by every in-chrome action:

```
compact primary : "h-8 gap-1.5 rounded-full bg-brand px-3.5 text-[12px] font-medium text-brand-foreground
                   transition-[background-color,transform] duration-200 hover:bg-brand/90 active:scale-[0.98]
                   disabled:opacity-60 disabled:active:scale-100"
compact ghost   : "h-8 gap-1.5 rounded-full px-3 text-[12px] font-medium text-muted-foreground
                   transition-colors duration-200 hover:bg-foreground/[0.045] hover:text-foreground"
compact outline : "h-8 gap-1.5 rounded-full border border-border px-3 text-[12px] font-medium text-foreground
                   transition-colors duration-200 hover:bg-muted"
```
Full-size `h-11` pills stay for page-level and dialog CTAs (review inbox, dialogs, guest review page). Codified as `REVIEW_ACTION_CLASS` in `lib/review/status.ts` so it can't drift.

**Empty state** — repo convention, reused for the inbox and revision mode:
```tsx
<div className="flex flex-col items-center rounded-2xl border border-dashed border-border/70 px-6 py-16 text-center">
  <p className="text-[15px] font-medium text-foreground">Nothing waiting on you.</p>
  <p className="mt-1 max-w-sm text-[13px] leading-relaxed text-muted-foreground">
    Every cut sent for review has been signed off. New submissions land here.
  </p>
</div>
```

**Progress meter** — scaled, not width-animated (`onboarding-sidebar.tsx:107-112`):
```tsx
<div className="h-1 w-full overflow-hidden rounded-full bg-muted">
  <div className="h-full origin-left rounded-full bg-brand transition-transform duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]"
       style={{ width: "100%", transform: `scaleX(${done / total})` }} />
</div>
```

### 2.4 Voice

Second person, lowercase-sentence, concrete, contractions, no exclamation marks, real ellipsis on in-flight states. Errors state what failed in we-voice plus what to do.

| Surface | Copy |
|---|---|
| Approve CTA | `Approve this cut` → `Approving…` → `Approved` |
| Reject CTA | `Request changes` → `Sending…` |
| Send CTA | `Send for review` → `Sending…` |
| Approve consequence line | `Approves v3 and unlocks download for your client.` |
| Blocked approval | `2 change requests still open. Resolve them, or approve with notes.` |
| Carry-forward confirm | `4 open change requests will move to v3.` |
| All-clear | `All feedback addressed. Upload v3?` |
| Inbox empty | `Nothing waiting on you.` |
| Version-notes helper | `What changed since v2? Reviewers see this first.` |
| Outdated version (guest) | `v4 is available — you're viewing v2.` |
| Error | `We couldn't save that decision. Try again.` |

---

## 3. Phase 0 — Foundations

Nothing else can be built safely until these exist.

### 0.1 Version control

The repo has no git. Every phase below edits files that currently have no undo.

```bash
cd /Users/kemojallow/Documents/Development/python_dev/fast_api/editubemain
git init
# .gitignore: node_modules/, .next/, __pycache__/, *.pyc, .env*, uploads/, venv/, .DS_Store,
#             editube_mac/build/, edu_mobile/node_modules/, .vercel/
git add -A && git commit -m "chore: baseline before review spine work"
git checkout -b feat/review-spine
```

### 0.2 Test harness — `editube/tests/conftest.py`

Currently 0 bytes. Establish two fixtures so phases 1–7 can assert behavior rather than hope:

- `sqlite_session(tables)` — the pattern already proven in `tests/test_video_versions.py:24-58`: the `@compiles(JSONB, "sqlite")` shim, `create_engine("sqlite://")`, `Base.metadata.create_all(engine, tables=[...])` with an explicit subset. Hoisted into a fixture so each new test file stops re-declaring it.
- `api_client` — `fastapi.testclient.TestClient` with `get_db` and `get_current_user` dependency-overridden. This is new to the codebase; it is what makes endpoint-level assertions (status transitions, 403s, approval blockers) possible at all.

Also add a `pytest.ini` with `testpaths = tests` and `filterwarnings`, since none exists.

**Caveat to encode in the fixture docs:** `ReviewLink` uses `ARRAY(String)` (`models.py:667-668`), which SQLite cannot render. Tests touching review links must either exclude that table from `create_all` or be marked `@pytest.mark.postgres`.

### 0.3 Backend status service — `app/services/video_status.py` (new)

```python
VIDEO_STATUS_IN_PROGRESS   = "in_progress"
VIDEO_STATUS_IN_REVIEW     = "in_review"
VIDEO_STATUS_APPROVED      = "approved"
VIDEO_STATUS_NEEDS_CHANGES = "needs_changes"
VIDEO_STATUSES = (…)

# Legal transitions. Deliberately permissive within the review loop and
# strict about nonsense (you cannot go straight from in_progress to approved
# without the cut ever having been sent for review).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "in_progress":   frozenset({"in_review"}),
    "in_review":     frozenset({"approved", "needs_changes", "in_progress"}),
    "needs_changes": frozenset({"in_review", "in_progress"}),
    "approved":      frozenset({"in_review", "needs_changes"}),  # reopening a review
}

def assert_transition(current: str, target: str) -> None: ...
def apply_video_status(db, video, target, *, actor_user_id, note=None, skip_transition_check=False) -> Video: ...
```

`apply_video_status` writes `status`, `status_changed_at`, `status_changed_by`, calls `log_activity(action="video_status_changed", meta={...})`, and returns the video. It does **not** commit — callers own the transaction, matching `register_video_version`'s convention (`video_versions.py`, flush-not-commit).

`skip_transition_check` exists for exactly one caller: the new-version reset in §6.4, which forces `in_review` regardless of where the previous version sat.

### 0.4 Notification emitter — `app/services/notifications.py` (new)

Collapses the three duplicated ~30-line blocks (`comments.py:396-469`, `review_links.py:1642-1683`, `review_links.py:670-705`).

```python
@dataclass(frozen=True)
class NotificationSpec:
    user_id: int
    type: str
    project_id: int | None = None
    video_id: int | None = None
    comment_id: int | None = None
    message: str | None = None
    group_key: str | None = None   # coalescing key; see §5.4

async def emit_notifications(db, specs: Iterable[NotificationSpec]) -> list[Notification]:
    """Create rows, commit, then fan out to push + WebSocket.
    Deduplicates by (user_id, type, group_key) inside the coalescing window."""
```

Emails stay at the call sites — they are type-specific (different templates, different preference gates) and folding them in would make the emitter a switch statement.

Refactor the three existing sites to call it in the same commit, so there is exactly one emit path from day one.

### 0.5 Frontend primitives

| File | Action |
|---|---|
| `lib/review/status.ts` | **New.** `VideoStatus` union, `STATUS_META`, `ALLOWED_TRANSITIONS` (mirrors backend), `REVIEW_ACTION_CLASS` compact-pill strings, `canApprove()` / `nextActionFor(status, canModerate)` helpers |
| `components/ui/status-badge.tsx` | **New.** `StatusBadge` with the §2.3 tone map, `size?: "xs" \| "sm"` |
| `app/(videos)/player/[id]/_components/player-header/status-config.tsx` | **Delete.** Raw-palette; superseded by `lib/review/status.ts` |
| `lib/query/keys.ts` | Add `tasks: { all, byProject(projectId), byVideo(projectId, videoId) }` and `reviews: { inbox() }`. Then replace the two literal `["project-tasks", projectId, "all"]` usages (`tasks-panel.tsx:301`, `use-player-workspace.ts:347`) and the two invalidations in `comments-panel.tsx:255-286` |
| `lib/api/comments.ts` | **Delete.** 18-line dead stub whose `fetchVideoComments` shadows the real one in `videos.ts:173`. Verify zero importers first |
| `lib/api/videos.ts` | Type `VideoDetail.status` as `VideoStatus` instead of `string` |

---

## 4. Phase 1 — The status spine

The core fix. After this phase a video's state is visible, changeable, and recorded.

### 4.1 Migration `c3d4e5f6a7b8_review_status_spine.py`

`down_revision = "b2c3d4e5f6a7"`.

```python
def upgrade():
    op.add_column("videos", sa.Column("status_changed_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("videos", sa.Column("status_changed_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("videos", sa.Column("review_due_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("videos", sa.Column("version_notes", sa.Text(), nullable=True))

    op.create_table(
        "video_approvals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("video_id", sa.Integer, sa.ForeignKey("videos.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # approved | changes_requested
        sa.Column("decision", sa.String, nullable=False),
        sa.Column("actor_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("review_session_id", sa.Integer,
                  sa.ForeignKey("review_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("review_link_id", sa.Integer,
                  sa.ForeignKey("review_links.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        # Set when a newer version supersedes this decision.
        sa.Column("superseded_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("superseded_by_video_id", sa.Integer,
                  sa.ForeignKey("videos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_video_approvals_video_decision", "video_approvals", ["video_id", "decision"])

    op.add_column("comments", sa.Column("carried_from_comment_id", sa.Integer,
                  sa.ForeignKey("comments.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_comments_carried_from", "comments", ["carried_from_comment_id"])
```

Backfill: none required. `status` already has `server_default="in_progress"`; `status_changed_at` staying NULL on legacy rows is honest — we genuinely don't know when they changed.

### 4.2 Models — `app/db/models.py`

Add the four columns to `Video` (after line 500, beside `status`), the `carried_from_comment_id` column plus a self-referential `carried_from` relationship to `Comment`, and a new `VideoApproval` class with `video`, `actor`, `review_session` relationships. Add `approvals = relationship("VideoApproval", ...)` to `Video`.

### 4.3 Service — `app/services/video_status.py` (completing 0.3)

```python
def record_decision(db, video, decision, *, actor_user_id=None, review_session_id=None,
                    review_link_id=None, note=None) -> VideoApproval:
    """Append a decision and move Video.status to match.
    approved          -> status approved
    changes_requested -> status needs_changes
    Used identically by the team route and the guest route — this is the
    convergence point for the four previously-disconnected mechanisms."""

def supersede_open_approvals(db, chain_videos, *, superseded_by_video_id) -> int:
    """Called when a new version lands: stamp superseded_at on every
    non-superseded approval in the chain."""

def approval_summary(db, video) -> dict:
    """{'decision', 'actor_name', 'created_at', 'superseded'} or None —
    what the header badge and the version switcher render."""
```

### 4.4 Routes

**Consolidate the duplicates.** Both `app/api/routes/videos.py:475-521` and `app/api/routes/video_detail.py:276-323` shrink to permission check + `apply_video_status(...)` + commit + payload. The untyped `data: dict` in `video_detail.py` becomes `VideoStatusUpdate`. A 400 with the transition reason replaces the hardcoded enum tuple in both.

**New: `POST /videos/{video_id}/review-decision`** in `video_detail.py`:
```python
class ReviewDecisionRequest(BaseModel):
    decision: Literal["approved", "changes_requested"]
    note: str | None = None
    override_blockers: bool = False   # "approve with notes"
```
Guarded by `assert_write_project_content`. Returns the full `VideoWithProjectResponse` so the frontend can `setQueryData` without a refetch. On `approved` with open client-visible change requests and `override_blockers=False` → 409 with the blocker list. Emits `video_approved` / `changes_requested` notifications (§5.2).

**New: `POST /projects/{pid}/videos/{vid}/send-for-review`** — sets `in_review`, accepts `reviewer_user_ids: list[int]` and `due_at`, writes `review_due_at`, emits `review_requested` to each reviewer. Separate from the generic status endpoint because "send" has side effects (notifications, deadline) that a raw status write must not silently trigger.

### 4.5 Payload exposure

| File | Change |
|---|---|
| `app/api/video_payload.py:118-165` (`video_detail_dict`) | Add `status_changed_at`, `review_due_at`, `version_notes`, `approval: approval_summary(...)` |
| `app/api/video_payload.py:55-116` (`video_versions_payload`) | Add `"status": v.status or "in_progress"` per row — powers per-version dots in the switcher |
| `app/api/models/videos.py` | Add the fields to `VideoDetailResponse` and `status` to `VideoVersionSummary` |
| `app/api/routes/folders.py` (contents payload) | Add `status` to the video rows so `VideoItem` can render a badge without a second fetch |
| `lib/api/videos.ts`, `lib/api/folders.ts` | Mirror all of the above into `VideoDetail`, `VideoVersionSummary`, `VideoItem` |

### 4.6 Frontend

**`player-header/review-actions.tsx` (new)** — the decision cluster, mounted in `player-header/index.tsx` immediately after the `<div className="flex-1" />` spacer at line 243 (the comment at line 245 already reads `{/* ---- Right: status + primary actions ---- */}` — the slot was designed for this and never filled).

Renders by status and permission:

| Status | `can_moderate` | Renders |
|---|---|---|
| `in_progress` | yes | `<StatusBadge>` + compact-primary **Send for review** |
| `in_review` | yes | `<StatusBadge>` + compact-ghost **Request changes** + compact-primary **Approve** |
| `needs_changes` | yes | `<StatusBadge tone="warning">` + compact-primary **Send for review** (relabelled `Send v{n} for review`) |
| `approved` | yes | `<StatusBadge tone="success">` + overflow → *Reopen review* |
| any | no | `<StatusBadge>` only |

Below `lg` the labels collapse to icons with `aria-label` (matching how compare controls already hide at `lg:flex`), the badge stays.

Mutations follow the house pattern (`comments-panel.tsx:971-979` — `setQueryData` then `invalidateQueries`):
```tsx
const decisionMutation = useMutation({
  mutationFn: (body: ReviewDecisionBody) => postReviewDecision(video.id, body),
  onSuccess: (updated) => {
    queryClient.setQueryData(queryKeys.videos.detail(video.id), updated);
    queryClient.invalidateQueries({ queryKey: queryKeys.videos.detail(video.id) });
    queryClient.invalidateQueries({ queryKey: queryKeys.reviews.inbox() });
  },
});
```

**Request-changes dialog** (`request-changes-dialog.tsx`) — `Dialog` from `components/ui/dialog` with the onboarding overrides (`className="rounded-2xl border-border bg-card p-6"`, `overlayClassName="bg-foreground/40 backdrop-blur-sm"`). Contains the §2.3 form-field skeleton for an optional note, a live count of open change requests, and the `h-11 rounded-full` primary. Copy: *"The editor will see this note plus every open change request."*

**Approve dialog** — only shown when blockers exist. Lists them, offers **Resolve them first** (closes, focuses the comment filter) and **Approve with notes** (sends `override_blockers: true`). No blockers → approve fires directly with no dialog. Zero-friction is the point.

**Badges elsewhere:**
- `video-card.tsx` — grid branch: `<StatusBadge size="xs">` next to the existing version pill at line 329-333 (`right-1.5 top-1.5`); list branch: beside the version chip at line 271-275. Skip the badge entirely for `in_progress` — every video starts there and a badge on everything is a badge on nothing.
- `version-switcher.tsx:78-149` — a `h-1.5 w-1.5 rounded-full` status dot per row, so "which version was approved" is answerable at a glance.

---

## 5. Phase 2 — Guest side and notifications

### 5.1 Guest approval writes through (the D3 convergence)

`review_links.py:1874-1910` becomes: blockers check → `record_decision(db, video, "approved", review_session_id=..., review_link_id=...)` → `session.approved_at = now` (kept for analytics compatibility) → commit → notify.

**New: `POST /review/{token}/request-changes`** — the reject action that has never existed anywhere in the product. Body `{ session_id, note }`. Records `changes_requested`, sets the video to `needs_changes`, notifies the owner and uploader. Optionally creates a `change_request` comment at t=0 carrying the note, so the editor's checklist picks it up.

**Handler must become `async`.** `approve` is currently sync (`def approve`), which is why it can only `enqueue_push_notification_job` and never broadcasts over WebSocket. Converting it to `async def` lets it use `emit_notifications` and reach the live player.

### 5.2 The guest-comment notification gap — the highest-value fix in the plan

In `review_links.py:1557-1695`, lift the notification block out of `if handles:`. New structure:

```python
recipients: dict[int, str] = {}          # user_id -> notification type
for uid in mention_recipient_ids:
    recipients[uid] = "mention"
for uid in {project.creator_id, video.uploader_id} - set(recipients):
    recipients[uid] = "client_comment"

await emit_notifications(db, [
    NotificationSpec(user_id=uid, type=t, project_id=project.id, video_id=video.id,
                     comment_id=comment.id,
                     message=_guest_comment_message(session, comment),
                     group_key=f"client_comment:{video.id}:{session.id}")
    for uid, t in recipients.items()
])
```

The `group_key` is what makes this safe to ship: a client leaving 20 comments produces one coalesced notification, not 20.

### 5.3 New notification types

`review_requested`, `video_approved`, `changes_requested`, `new_version`, `client_comment`. Because `Notification.type` is an unconstrained `String`, this needs no migration — but it does need:

- `app/jobs/push_notifications.py:15-42` — title/body entries for each; without them everything falls through to the generic *"New notification"*.
- `components/Navigation/notification-presentation.tsx` — `META` icons + labels, `notificationMessage` fallbacks, and a `notificationTarget` fix so approval/version notifications deep-link to the player *without* forcing `?tab=comments`.

### 5.4 Grouping

Migration `d4e5f6a7b8c9_notification_grouping.py`: add `group_key` (String, indexed), `actor_user_id` (FK users), `read_at` (TIMESTAMP), and `count` (Integer, server_default `1`) to `notifications`.

`emit_notifications` coalescing rule: if an unread notification exists for the same `(user_id, type, group_key)` within `NOTIFICATION_GROUP_WINDOW_MINUTES` (default 15), increment `count`, refresh `created_at` and `message`, and re-push — instead of inserting a row. Frontend renders `count > 1` as *"Sarah left 7 comments on Summer Campaign v2."*

### 5.5 Blocker scope change (D5)

`comment_workflow.py:36` — replace `unresolved_change_requests_for_link(db, review_link_id, video_id)` with:

```python
def unresolved_change_requests_for_video(db, video_id, *, client_visible_only=True) -> int:
    q = db.query(Comment.id).filter(
        Comment.video_id == video_id,
        Comment.kind == COMMENT_KIND_CHANGE_REQUEST,
        Comment.parent_id.is_(None),
        Comment.status.notin_(list(TERMINAL_STATUSES)),
    )
    if client_visible_only:
        q = q.filter(Comment.visibility == COMMENT_VISIBILITY_PUBLIC)
    return q.count()
```
Keep the old function as a thin deprecated wrapper for one release so nothing breaks mid-deploy.

### 5.6 Fix the `/versions` 500

`review_links.py:2216-2245` — resolve the project through the video, exactly as `video_payload.py:63-69` already does correctly:

```python
video = db.query(Video).filter(Video.id == link.video_id).first()
project_id = video.project_id if video else None
project_type = (db.query(Project.project_type)
                  .filter(Project.id == project_id).scalar()) if project_id else None
is_review = project_type == "review"
```
Peer links then join through `Video.project_id` rather than the nonexistent `ReviewLink.project_id`. Add the regression test that would have caught this (a guest hitting `/versions` on a link whose video has siblings).

### 5.7 Guest UI

In `review-client.tsx`, replace the raw-palette approve button at lines 1945-1953 (`bg-emerald-500 … text-white`) with the token-based compact pill, and add **Request changes** beside it. The blockers chip at 1937-1944 currently renders while the Approve button ignores it — wire `disabled` to the blockers and add the *approve with notes* path.

Add an **outdated-version banner** in the existing banner slot at lines 2000-2029 (where the `info.scope` strip lives), using `/versions` once §5.6 lands:
```
v4 is available — you're viewing v2.   [Go to the latest cut]
```
Commenting on an old version stays allowed; **approving** it does not.

---

## 6. Phase 3 — Versions that carry their work forward

### 6.1 Version notes at upload

`upload-video-modal.tsx` gains an optional `versionOf?: { version: number; openChangeRequests: number }` prop. When present:
- Title becomes `Upload v{n+1}`.
- A **What changed** textarea appears beside the existing description field (lines 239-250), using the §2.3 form-field skeleton. Helper: *"What changed since v2? Reviewers see this first."*
- A carry-forward line renders: *"4 open change requests will move to v3."*

`onSubmit` widens from `(file, name, description)` to `(file, name, description, meta?: { versionNotes?: string })`. Both call sites update: `player-page-inner.tsx:76-93` and `use-project-detail-page.ts:154-171`.

Backend: `version_notes` as a new `Form(None)` field on `POST /projects/{pid}/videos` (`videos.py:149-247`), threaded through `_finalize_project_video` (`videos.py:64-146`).

### 6.2 Carry-forward — `app/services/comment_carry_forward.py` (new)

```python
def carry_forward_open_change_requests(db, source_video, target_video) -> list[Comment]:
    """Copy every top-level, non-terminal change request from source to target.
    Preserves text, timecode/end_timecode, drawing_data, visibility, assignee,
    due_at, and author identity (including guest fields). Resets status to
    'open', clears revision/client_mutation_id, sets carried_from_comment_id.
    Replies are NOT copied — the thread stays on the version where it happened;
    the new copy links back to it."""
```

Called from the `version_of` branch of the upload route (`videos.py:170-181`), after `_finalize_project_video` and inside the same transaction. Transcript-anchored comments carry `anchor_text` but drop the word indices — the existing anchor-remap machinery (`comments.py:224`) will re-resolve or flag drift against the new transcript.

Response gains `carried_comment_count` so the frontend can toast *"4 change requests moved to v3."*

### 6.3 "What's new in v{n}" card

New `comments-panel/version-notes-card.tsx`, rendered at the top of the comment feed when `video.version_notes` is set or carried comments exist. `rounded-2xl border bg-card p-4`, eyebrow `WHAT'S NEW IN V3`, notes body, then a carried-forward count that filters the feed on click. Collapsible, collapsed state persisted per video in `localStorage` (matching the `player_include_prior_comments` convention).

### 6.4 New version resets review state

In the `version_of` branch: `supersede_open_approvals(db, chain, superseded_by_video_id=new.id)` and `apply_video_status(db, new_video, "in_review", skip_transition_check=True)`. Emit `new_version` to everyone who commented on or was assigned work on the previous version. This is the mechanic that makes "client approved v2 while v3 exists" impossible to misread.

---

## 7. Phase 4 — Review inbox

### 7.1 Backend — `app/api/routes/review_inbox.py` (new), prefix `/reviews`

`GET /reviews/inbox` returns three computed sections:

| Section | Query |
|---|---|
| `needs_you` | videos in projects you can access with `status == 'in_review'` where you are creator/uploader/assignee-of-open-comments, **plus** videos with `status == 'needs_changes'` where you are the uploader, **plus** open comments assigned to you |
| `waiting_on_others` | videos you sent (`status == 'in_review'`, you are uploader) — annotated with review-link session data: opened / not opened / last viewed |
| `recently_closed` | last 10 `VideoApproval` rows across your projects, not superseded |

Each row: `video_id, name, thumbnail_url, version, status, project_id, project_name, client_name, review_due_at, open_comment_count, unresolved_change_requests, last_activity_at, reason`. `reason` is a machine-readable enum the UI turns into the row's one-line explanation.

`GET /reviews/inbox/summary` → `{ needs_you_count }` for the sidebar badge — cheap, cached, polled alongside the notification summary.

### 7.2 Frontend

`app/(sites)/dashboard/reviews/page.tsx` — the `redirect("/dashboard")` is deleted and replaced by the real page. The orphaned `_components/reviews-page.tsx` is **not** revived: it lists *projects*, and the inbox is about *videos needing action*. It gets deleted in the same commit.

Layout follows the onboarding content column (`max-w-4xl`, top-aligned, `mb-8` rhythm), inside the existing `(sites)` shell with `useSiteHeaderSlot` for the title — matching what `reviews-page.tsx` already did at lines 33-46.

```
NEEDS YOUR ATTENTION            ← eyebrow: text-[11px] font-semibold uppercase tracking-[0.14em]
┌───────────────────────────────────────────────────────────┐
│ [thumb] Summer Campaign  v3   [In review]                 │  ← rounded-2xl border bg-card
│         Review requested · due today · 12 comments        │     hover:-translate-y-0.5
│                                    [Review this cut →]    │
└───────────────────────────────────────────────────────────┘

WAITING ON OTHERS
│ [thumb] Promo v4  [In review]  Sent Tue · not opened yet  [Nudge] │

RECENTLY CLOSED
│ [thumb] Q3 Teaser  [Approved]  by Dana · 2 days ago  [Sign-off PDF] │
```

Row = the selectable-tile skeleton from the design spec, minus the checkbox. Due-today renders `text-warning`; overdue renders `text-destructive` — the only two places urgency is colored, so it means something.

Skeleton rows are height-measured against the real row (hard rule 9). Empty state uses the §2.3 block.

**Sidebar**: add `{ title: "Reviews", url: "/dashboard/reviews", icon: ClipboardCheck }` to `data.navMain` (`app/components/app-sidebar.tsx:83-96`) with a count badge on the row, reusing the brand-pill idiom from lines 388-394. `isMainNavActive` already handles the path.

---

## 8. Phase 5 — Mobile player

The narrowest fix with the largest experiential payoff.

`player-workspace-body.tsx:277-336` — the `!isDesktop` branch currently renders only the segmented pill. Mount the panel body above it:

```tsx
{!isDesktop && (
  <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-background">
    <div className="min-h-0 flex-1 overflow-hidden pb-16">
      {mobileTab === "comments" ? renderCommentsPanel() : (
        <TasksPanel projectId={video.project_id} videoId={video.id} />
      )}
    </div>
    {/* existing fixed segmented pill, unchanged */}
  </div>
)}
```

`pb-16` clears the `fixed bottom-2` pill. Supporting work:

- `TasksPanel` currently hides its close button with `lg:hidden` — on mobile there is no close, so pass `onClose={undefined}`.
- The comments composer must sit above the mobile keyboard: `sticky bottom-0` inside the scroll container with `env(safe-area-inset-bottom)` padding.
- Verify the comment row action buttons are touch-visible — a comment at `comments-panel.tsx:2157` says they were already made always-visible for touch, so this should be a no-op to confirm rather than build.

---

## 9. Phase 6 — Revision mode

Turn `tasks-panel.tsx` from a read-only projection into the editor's working surface. No new screen; the panel gains agency.

1. **Progress header** — the §2.3 meter plus `✓ 8 resolved · ◉ 2 in progress · 2 open` in `text-[11px] tabular-nums`.
2. **Inline status toggle** — a checkbox-style control per row calling `updateComment(..., { status })` directly, using the existing mutation pattern (`comments-panel.tsx:277-287`). Today every mutation requires navigating back to the comment.
3. **Keyboard-first** — `N`/`P` move between items and seek the player, `X` resolves, `Space` plays. Registered through the existing hotkey system (`lib/player/hotkeys.tsx`) so the shortcuts dialog documents them automatically.
4. **Carried-forward affordance** — rows whose comment has `carried_from_comment_id` get a `from v2` chip.
5. **All-clear CTA** — when open count hits zero: *"All feedback addressed. Upload v3?"* wired to the version-upload modal with notes prefilled from the resolved change requests.
6. **Sort by timecode by default** — an editor works the timeline in order, not in comment-creation order.

---

## 10. Phase 7 — Ship the dormant features

Already built server-side, invisible to users. Each is small and independently shippable.

| Feature | Work |
|---|---|
| **Comment attachments** | `CommentAttachment` + `POST .../comments/{id}/attachments` exist with `waveform` and `transcript` columns. Add a file/voice control to both composers and an attachment row renderer. Voice notes first — they're the highest-value input for non-technical clients |
| **Next-unresolved navigation** | `handleNextUnresolved` and `quickCounts` are written and never rendered (`comments-panel.tsx:2713-2765`). Render the button, bind `U` |
| **Comment-density heat** | `ReviewAnalytics.heatmap` / `rewatch_hotspots` are fetched and reduced to a `{n} hotspots` chip. Render as a strip under the scrubber |
| **Mentions reach the team** | Frontend builds candidates from uploader + thread participants (`comments-panel.tsx:2901`); the backend's `list_users_for_mentions` already returns the whole team. Expose it via an endpoint and use it |
| **Link settings controls** | `nda_required`, `geofence_mode`, `recording_detection_mode`, `watermark_mode` are hardcoded in `share-review-dialog.tsx:188-196, 244-253`. Add real controls behind a "Advanced protection" disclosure |

---

## 11. Testing

| Layer | Coverage |
|---|---|
| `tests/test_video_status_service.py` | Every legal transition; every illegal one raises; timestamps and actor recorded; activity logged |
| `tests/test_review_decision_route.py` | Approve/reject via `api_client`; 403 for clients; 409 when blocked; `override_blockers` path; response carries updated status |
| `tests/test_comment_carry_forward.py` | Only open top-level change requests copy; replies don't; `carried_from_comment_id` set; resolved ones stay behind; guest authorship preserved |
| `tests/test_approval_supersede.py` | New version supersedes prior approvals and forces `in_review` |
| `tests/test_notifications_emitter.py` | Coalescing inside the window; separate rows outside it; push + WS fan-out called once per row |
| `tests/test_guest_comment_notifications.py` | **The regression test for the headline bug** — a guest comment with no mention notifies creator and uploader |
| `tests/test_review_versions_endpoint.py` | `/review/{token}/versions` returns 200 (currently 500) |
| `tests/test_approval_blockers.py` | Video-scoped counting; internal change requests don't block; client-visible ones do |
| Frontend (vitest) | `lib/review/status.ts` transition helpers; `StatusBadge` tone mapping; review-actions rendering matrix by status × permission |
| Manual | The full loop on desktop and at 390px: upload → send → comment as guest → request changes → revise → upload v2 → approve. Both themes |

Command surface: `cd editube && python -m pytest tests -q` · `cd editube-frontend && npm run test && npm run lint && npm run build`.

---

## 12. Explicitly out of scope

Deferred to the audit's V1.x and later tiers, listed so nobody assumes they're implied: resumable/chunked upload (large, independent, deserves its own plan), AI feedback consolidation, comment/transcript search, frame-accurate timecodes, Redis pub/sub for WebSockets (required before multi-worker deploy, but a deployment concern rather than a workflow one), project-level share links, wipe/overlay compare, waveform rendering, HLS packaging, and the NLE panel work (`integrations/premiere-pro`, `davinci-resolve`, `fcpx`, `after-effects` already exist on disk and need their own audit before extension).

---

## 13. Sequencing and risk

**Dependency order is strict for 0 → 1 → 2.** Phase 0 creates the service and emitter every later phase calls. Phase 1 creates the schema Phase 2 writes to. After Phase 2, phases 3–7 are independently shippable in any order.

| Risk | Mitigation |
|---|---|
| No version control | Phase 0.1 — git init before the first edit |
| Two status endpoints drifting again | Phase 1.4 collapses both onto one service; a test asserts both routes produce identical results |
| Changing blocker scope (D5) breaks existing approvals | Keep the old function as a deprecated wrapper; the new scope is strictly *broader* for client-visible change requests, so nothing that could approve before becomes unable to — except where it genuinely should have been blocked |
| Carry-forward duplicating comments on re-upload | `carried_from_comment_id` is checked before copying; carrying twice is a no-op |
| Notification grouping hiding urgent items | Only `client_comment` and reply types group. Decisions (`video_approved`, `changes_requested`, `review_requested`) never coalesce — one event, one notification |
| `ARRAY(String)` breaking SQLite tests | Documented in the conftest fixture; review-link tests exclude the table or mark `postgres` |
| Review projects' "every video is a version" special case | Untouched by this plan, but Phase 3's carry-forward runs through `resolve_version_chain`, so verify against a `project_type == "review"` project before shipping |
