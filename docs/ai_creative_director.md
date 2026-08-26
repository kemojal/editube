# AI Creative Director — Requirements & Implementation Plan

**Status:** Draft for review. No code written yet.
**Owner:** TBD
**Last updated:** 2026-08-11

---

## 1. What we are building

> After a user uploads a video and the auto-edit finishes cutting it, the system hands the
> transcript and the cut timeline to **Claude Opus 5**, which acts as a creative director:
> it reads the piece, decides what it should feel like, and writes a detailed edit —
> B-roll, animations, transitions, titles, effects, music. The system then **generates**
> the B-roll media it asked for and **applies** every instruction to the correct track at
> the correct position in the editor, automatically.

The user opens the editor and finds a directed cut, not a rough cut.

### 1.1 User story (the happy path)

1. User creates a project in the wizard, uploads a video, toggles **Auto edit** on.
2. *(new)* User toggles **AI Creative Director** on, picks a style preset and a budget tier.
3. Upload → transcode → transcribe → auto-cut (all existing).
4. **(new)** Director stage runs headless: Claude reads transcript + timeline → `EditPlan`.
5. **(new)** Asset stage fans out: image/video generations for every B-roll directive.
6. **(new)** Compile stage writes the plan into the `rough_cut_draft` blob.
7. User opens `/dashboard/rough-cut` → the editor *performs* the director's edit on screen
   (extending the existing auto-edit HUD choreography), then settles into a fully-populated
   timeline the user can edit like any other.
8. Export renders everything the director placed.

### 1.2 Scope

**In scope**
- Director planning stage (Claude Opus 5, headless, RQ job).
- B-roll asset generation (images + video) driven by the plan.
- Plan → draft compiler (places clips, transitions, animations, titles, effects).
- Render support for the new primitives (generated media, images, transitions).
- Wizard opt-in, progress surfacing, on-screen performance, review/undo.
- Budget, credit, cancellation and idempotency controls.

**Out of scope (this phase)**
- Licensed stock-footage integration (plan leaves a `source: "stock"` hook, unimplemented).
- Music library / licensed audio beds (plan leaves a `music` directive, compiled to a no-op).
- Voice cloning, dubbing, avatar B-roll.
- Multi-language directing (director runs on the transcript's detected language only).

---

## 2. What already exists (verified inventory)

This feature is mostly *assembly*. Almost every primitive already ships.

| Capability | Where | Notes |
|---|---|---|
| Word-level transcript | `app/jobs/transcription.py`, `VideoTranscription.segments` (JSONB) | segments carry `words: [{word,start,end}]` when Whisper provides them |
| Auto-cut | `app/services/auto_edit.py:386` `run_post_transcription_auto_edit` | writes `keepRanges` + `aiAnalysis` into `AiResult(result_type="rough_cut_draft")` |
| Post-transcription hook point | `app/jobs/transcription.py:432` | already the seam where a director stage attaches |
| Editor draft blob | `rough-cut-draft-state.ts:618` `draftPayload` | single JSONB doc; ~50 keys; `rangeEditVersion: 3` |
| Timeline model | `_lib/rough-cut-types.ts:299-330` | `RoughCutTimelineMediaItem[]` + `RoughCutTimelineTrack[]` |
| Track ordering | `rough-cut-editor-shared.ts:402` `normalizeTimelineTracks` | video + text share one ordered compositing stack; audio pinned below |
| Clip attributes | `rough-cut-types.ts:118` `RoughCutClipAttributes` | video transform, adjust/color, masks, animation, keyframes, speed, audio |
| Clip animations | `_lib/animation/clip-animation.ts:13` | 12 presets × in/out/combo, `duration`, `intensity` — rendered in export (`rough_cut_export.py:732`) |
| Keyframes | `_lib/keyframes/clip-keyframes.ts`, `ClipKeyframeChannel` | 30+ animatable channels, rendered via `_channel_expression` |
| Text overlays / lower thirds / elements / grid | `_lib/text/`, `_lib/elements/`, `_lib/grid/` | rich editor support |
| Placement math | `_lib/timeline-insert.ts` `planAppend` / `planRippleInsert` / `splitRangesAt` | tested, reusable |
| AI media generation | `app/services/ai_media.py`, `app/jobs/ai_media_generation.py`, `GeneratedMedia` model | Gemini image (`gemini-3.1-flash-image-preview`), Veo video (`veo-3.1-generate-preview`), OpenRouter; async with progress + cancel |
| B-roll suggestion (naive) | `app/api/routes/ai.py:278` `/ai/broll-suggestions` | one-shot Gemini call, unused by the editor |
| Export pipeline | `app/jobs/rough_cut_export.py` (2184 lines) | FFmpeg concat + layer compositing + SRT burn-in |
| Layer remapping | `rough_cut_export.py:527` `_remap_timeline_layers_to_export` | intersects source-time layers with `keepRanges` |
| One-shot consent gate | `_lib/auto-edit-gate.ts` | exactly the pattern the director run must reuse |
| On-screen performance | `_lib/auto-edit-choreography.ts` + `_hooks/useAutoEditPerformance.ts` + `rough-cut-auto-edit-hud.tsx` | "watch the AI edit" — commit decoupled from animation, skip always lands identically |

---

## 3. Blocking gaps

These must be closed or the feature ships broken. Each is a real defect found in the current code, not speculation.

### G1 — No Anthropic client exists
`app/services/ai_client.py` is Gemini-only. `anthropic` is not in `requirements.txt`; no
`ANTHROPIC_API_KEY` anywhere. **Must add** a first-class Claude client service.

### G2 — AI-generated B-roll does not render in MP4 export *(critical)*
Three independent bugs stack:

1. `rough-cut-export.ts:405` sends `videoId: item.videoId`. `generatedMediaToPanelItem`
   (`rough-cut-generate-media.tsx:98`) produces a panel item **with no `videoId`**.
2. `rough_cut_export.py:302` therefore defaults `layer_video_id = video_id` — the primary
   video. Generated B-roll silently renders as **duplicated A-roll footage**.
3. `_normalize_timeline_layers` (`rough_cut_export.py:298`) drops any layer where
   `kind != "video"` — so **generated still images never export at all**.
4. `_authorized_layer_sources` (`rough_cut_export.py:363`) only resolves `Video` rows in the
   same project. `GeneratedMedia` rows are not `Video` rows and cannot be resolved.

Fix requires: a layer source discriminator (`sourceKind` + `sourceId`), authorization of
`GeneratedMedia` by `project_id`, and an image-layer branch in the FFmpeg graph
(`-loop 1 -t <dur>` + scale/pad + the existing compositor chain).

### G3 — No clip-to-clip transitions exist anywhere
`_lib/capcut-export.ts:177` literally emits `transitions: []`. There is no transition data
model, no editor UI, no renderer. Clip in/out **animations** exist and are close, but a
director asking for "dissolve into the B-roll" has nothing to compile to. Needs: a
`transitions[]` array on the draft, viewer rendering, and FFmpeg `xfade`/`acrossfade`.

### G4 — Export skips text overlays, lower thirds, elements and grid
`rough_cut_export.py:1746` explicitly builds a `burn_skipped` list containing `lowerThirds`
and `brand`; there is no `drawtext`/overlay path for `textOverlay`, `elementOverlays` or
`gridClips`. Only SRT captions are burned. A director that plans titles produces an editor
preview that does not match the MP4.

### G5 — Coordinate-system mismatch *(highest correctness risk)*
`RoughCutTimelineMediaItem.start/end` are **source seconds of the primary media**, not
post-cut timeline seconds — proven by `_remap_timeline_layers_to_export` intersecting them
with `keptRanges`. Claude will naturally reason about the *edited* video. Compiling
director-time directly into `item.start` would desynchronise every B-roll placement from the
cut. See §6.

### G6 — No music/SFX asset source
`MusicStyle` exists in the draft (`selectedTrack: "none"|"clean"|"warm"|"pulse"`) but there
is no audio asset behind those names and no bed in `_timeline_audio_graph`. Music directives
compile to a no-op this phase.

### G7 — Cost and latency are unbounded
Veo runs minutes per clip (`AI_VIDEO_TIMEOUT_SEC=900`). A 10-minute talking head could
justify 30 B-roll inserts. Without caps, one project can burn hours of worker time and a
large media bill. Needs per-plan budget, concurrency limit, and credit accounting
(a credits system already exists for UGC — `app/services/ugc_credits.py`).

### G8 — No idempotency / consent model for a *generative* run
`auto-edit-gate.ts` solves this for cuts (one-shot consent, cleared server-side, never
clobbers user edits). The director run is far more expensive and more destructive
(it adds tracks and clips), so it needs the same discipline plus explicit cancellation and
a resume-safe state machine.

### G9 — The draft schema is allow-list on load *(under-estimated; found in review)*
Every load path rebuilds objects field-by-field rather than spreading:

- `parseTimelineMediaItems` (`rough-cut-draft-state.ts:897`) constructs a fresh object with
  17 explicit keys.
- `sanitizeClipAttributes` (`rough-cut-editor-shared.ts:652`) does the same with 15 keys —
  and its own docstring warns that *"a field missing from this list cannot be set at all —
  it is written and dropped on the same tick, and the control that wrote it looks broken for
  no visible reason."*
- The top-level draft loader (`rough-cut-draft-state.ts:341`) reads ~40 keys one at a time.

**Consequence:** every new field this feature needs — `sourceKind`, `sourceId`,
`transitions`, any provenance tag — is written, saved, and then **silently dropped on the
next page load**. It works in-session and breaks on refresh, which is the worst possible
failure shape.

Every new field must be added to: the type, the sanitizer/parser, the `draftPayload` save
object, the load path, and the export serializer. Five places, no compiler help. Budget for
it; do not discover it during M4.

---

## 4. Architecture

### 4.1 Pipeline

```
transcription completes  (app/jobs/transcription.py)
        │
        ├─▶ run_post_transcription_auto_edit          [existing]  → keepRanges
        │
        └─▶ enqueue_director_job(video_id)            [NEW]
                    │
        ┌───────────┴────────────────────────────────────────────┐
        │  app/jobs/director.py — director_job(plan_row_id)      │
        ├────────────────────────────────────────────────────────┤
        │ S1  build_director_context()   transcript + timeline    │
        │                                + keyframe stills        │
        │ S2  Claude Opus 5 pass A → beats + creative brief       │
        │     Claude Opus 5 pass B → directives (forced tool)     │
        │     Claude Opus 5 pass C → self-critique / prune        │
        │     ──▶ EditPlan v1 (schema-validated)                  │
        │ S3  asset fan-out → GeneratedMedia rows (bounded)       │
        │ S4  compile_plan_to_draft() → rough_cut_draft patch     │
        │ S5  mark plan `ready`                                   │
        └────────────────────────────────────────────────────────┘
                    │
        editor opens → gate → performs the plan on screen
```

Each stage is independently resumable. The plan row is the state machine.

### 4.2 Where the compiler lives — decision

**Recommendation: compile on the backend, replay on the frontend.**

This mirrors the precedent already in the codebase: `run_post_transcription_auto_edit`
writes `keepRanges` server-side, and the editor replays that cut visually when opened
(`auto-edit-gate.ts` → `draftServerSeeded` → `"run"`).

| | Backend compiler | Frontend compiler |
|---|---|---|
| Works with editor closed | ✅ | ❌ |
| Reuses tested `timeline-insert.ts` math | ❌ (port + parity tests) | ✅ |
| Single source of truth | ✅ | ❌ (headless path would need a second one) |
| On-screen performance | replay from plan | falls out naturally |

The port is small — `planAppend` and `splitRangesAt` are ~60 lines of arithmetic. Mitigate
drift with **golden-fixture parity tests**: the same `EditPlan` + draft fixture must produce
byte-identical `timelineMediaItems` in Python and TypeScript (`docs/fixtures/`).

### 4.3 New modules

**Backend**
```
app/services/claude_client.py        Anthropic SDK wrapper: retries, caching, refusal
                                     handling, token accounting, structured output
app/services/director_context.py     transcript → director-readable context + frames
app/services/director_plan.py        EditPlan pydantic models + validation + repair
app/services/director_prompts.py     system prompts, capability manifest, tool schema
app/services/director_compile.py     EditPlan → rough_cut_draft patch
app/jobs/director.py                 RQ job orchestrating S1–S5
app/api/routes/director.py           REST surface (below)
```

**Frontend**
```
_lib/director/director-types.ts      EditPlan mirror types
_lib/director/director-performance.ts  choreography for the director run
_components/director/director-hud.tsx  progress + plan review + undo
_components/director/director-plan-panel.tsx  read the brief/beats, toggle directives
```

**DB**
```
director_plans            id, video_id, project_id, user_id, status, stage,
                          progress, plan (JSONB), brief (JSONB), model, usage (JSONB),
                          error_message, cancel_requested, applied_at, created_at, updated_at
generated_media          + plan_id (FK, nullable, indexed)
                         + directive_id (String, nullable)     ← provenance for the compiler
```

### 4.4 API surface

```
POST   /videos/{video_id}/ai/director            start a run (idempotent per video)
GET    /videos/{video_id}/ai/director            status + plan + per-asset progress
POST   /videos/{video_id}/ai/director/cancel     cancel; stops asset fan-out
POST   /videos/{video_id}/ai/director/apply      compile a `planned` run into the draft
DELETE /videos/{video_id}/ai/director            revert: remove applied artifacts
PUT    /videos/{video_id}/ai/director-prefs      wizard-captured prefs (mirrors auto-edit-prefs)
```

`GET` is the poll target for the wizard's "sit and wait" screen and the editor HUD.

---

## 5. Data contract — `EditPlan` v1

Version the schema from day one (`version: 1`). The compiler must refuse unknown versions.

```jsonc
{
  "version": 1,
  "planId": "uuid",
  "sourceVideoId": 123,
  "model": "claude-opus-5",
  "brief": {
    "genre": "talking-head educational",
    "audience": "developers evaluating a tool",
    "tone": ["confident", "warm", "unhurried"],
    "pacing": "medium",
    "aspect": "16:9",
    "palette": ["#0F1115", "#E8E3D8", "#C46A3F"],
    "visualMotifs": ["shallow depth of field", "warm practical light", "no on-screen text"],
    "houseStylePrefix": "Cinematic still, 35mm, shallow DOF, warm practical lighting, muted
                         teal-and-amber palette, no text, no logos, no watermarks.",
    "rationale": "..."
  },
  "beats": [
    { "id": "b1", "kind": "hook", "start": 0.0, "end": 11.8,
      "summary": "Poses the problem", "quote": "Most teams lose two days a week to…" }
  ],
  "directives": [
    {
      "id": "d001",
      "type": "broll",
      "start": 4.20, "end": 8.05,            // DIRECTOR TIME (post-cut) — see §6
      "anchor": { "quote": "two days a week", "segmentId": "s12" },
      "track": "V2",
      "intent": "illustrate",
      "asset": {
        "source": "generate-image",           // generate-image | generate-video | project-media | stock
        "prompt": "Overhead shot of a cluttered engineer's desk at dusk…",
        "negativePrompt": "text, watermark, logo, extra fingers",
        "aspectRatio": "16:9",
        "durationSeconds": 4
      },
      "framing": { "scale": 1.04, "x": 0, "y": 0,
                   "kenBurns": { "from": 1.00, "to": 1.10 } },
      "audio": { "enabled": false },
      "animationIn":  { "preset": "fade", "duration": 0.35, "intensity": 80 },
      "animationOut": { "preset": "fade", "duration": 0.35, "intensity": 80 },
      "confidence": 0.82,
      "why": "Speaker quantifies wasted time; a concrete image lands the cost."
    },
    { "id": "d002", "type": "transition", "at": 11.80, "style": "dissolve",
      "duration": 0.40, "track": "V1", "why": "Beat change from hook to context." },
    { "id": "d003", "type": "text", "start": 2.0, "end": 5.4,
      "template": "editorial", "title": "The two-day tax",
      "subtitle": "Where the week actually goes", "position": "lower-left" },
    { "id": "d004", "type": "emphasis", "start": 30.2, "end": 31.0,
      "effect": "punch-in", "amount": 1.12 },
    { "id": "d005", "type": "music", "start": 0, "end": 92,
      "mood": "restrained-uplift", "duckUnderVoice": true }   // no-op this phase
  ],
  "budget": { "images": 12, "videos": 2, "estimatedCredits": 47 },
  "warnings": ["Beat 6 has no visual opportunity; left on A-roll."]
}
```

### 5.1 Capability manifest

Claude must never invent an enum value. The prompt carries a machine-generated manifest
derived from the actual source of truth, so it cannot drift:

| Field | Source of truth |
|---|---|
| `animationIn/Out.preset` | `CLIP_ANIMATION_PRESETS` (`_lib/animation/clip-animation.ts:13`) |
| `text.template` | `TextOverlayStyle["templateId"]` (`rough-cut-types.ts:439`) |
| `transition.style` | new `TRANSITION_STYLES` constant (G3) |
| `track` | `"V1".."V4"`, `"TX1".."TX2"`, `"A1".."A2"` |
| `asset.aspectRatio` | `_lib/ai-media-models.ts` |
| `emphasis.effect` | derived from `ClipKeyframeChannel` |

Generate the manifest at build time from the TS constants into a JSON file the Python
prompt reads. A CI check fails if the manifest is stale.

---

## 6. Coordinate systems — the correctness core

Three clocks are in play. Confusing them is the single most likely way this feature ships
subtly broken.

| Clock | Definition | Used by |
|---|---|---|
| **Source time** | seconds in the original uploaded media | `keepRanges`, `segments`, `timelineMediaItems.start/end`, `sourceStart` |
| **Director time** | seconds in the *cut* video (keepRanges concatenated) | `EditPlan` directives, what Claude reasons about |
| **Export time** | seconds in the rendered MP4 | `_remap_timeline_layers_to_export` output |

Facts, verified in code:
- `timelineMediaItems.start/end` are **source** seconds. `rough_cut_export.py:527` intersects
  them with `kept_ranges` and remaps. Anything past `source_duration` is the "tail" and maps
  1:1 after the kept total.
- Director time → source time is a piecewise-linear map defined entirely by `keepRanges`.

### 6.1 Rules

1. **Claude only ever emits director time**, plus a text anchor.
2. The compiler resolves each directive by **anchor first, time second**:
   - match `anchor.quote` inside the `anchor.segmentId` line, on normalised tokens;
   - if the segment id is wrong, search every segment — a misattributed line is a far
     smaller error than a misquote, and the quote alone identifies the moment;
   - fall back to `directorToSource(start)` only if the quote is nowhere, and mark the
     result inexact so the caller **drops** the shot rather than placing it approximately.

   *Changed during implementation.* The anchor was specified as `wordIds`. Emitting
   per-word ids costs roughly 10k tokens on a ten-minute video and asks the model to copy
   long id strings correctly; `segmentId` + verbatim quote is cheaper, more robust to
   drift, and the quote match still resolves to word-level precision.

   Anchors are what make a plan survive a later re-cut; raw times do not. There is a test
   for precisely that — trim more out of the middle, and the same quote still resolves to
   the same source moment.
3. A B-roll spanning a cut boundary is **split** by the export intersection. That ripple is
   correct, but the compiler must set `playDuration` from the *director-time* span so the
   clip's intended on-screen length is preserved through later moves.
4. Provide `directorToSource(t)` / `sourceToDirector(t)` in **both** Python and TypeScript,
   from the same fixture-tested spec.
5. Every compiled item must round-trip: `sourceToDirector(item.start) ≈ directive.start`
   within 40 ms, asserted in tests.

---

## 7. Claude Opus 5 integration

### 7.1 Client (`app/services/claude_client.py`) — **built**

Configuration is namespaced **`EDITUBE_CLAUDE_*`**, not `CLAUDE_*`. The un-namespaced
names are already taken: Claude Code itself exports `CLAUDE_EFFORT` (among others), so a
worker started from such a shell silently ran at a different effort level than configured.
The request-shape test caught it. `ANTHROPIC_API_KEY` keeps its standard name — that one is
the SDK's own and is correct to read.

Settings: `EDITUBE_CLAUDE_MODEL` (default `claude-opus-5`), `_EFFORT` (`high`),
`_MAX_TOKENS` (16000), `_TIMEOUT_SEC` (900), `_SERVER_FALLBACK` (on).


- SDK: `anthropic` (add to `requirements.txt`), `ANTHROPIC_API_KEY` env.
- Model: **`claude-opus-5`**.
- Thinking: adaptive (default on Opus 5) — do **not** send `budget_tokens` (400).
- Effort: `output_config={"effort": "high"}` for the planning pass; `"medium"` for critique.
- Do **not** send `temperature` / `top_p` / `top_k` — rejected with 400 on Opus 5.
- Stream (`messages.stream(...)` + `get_final_message()`) — planning output can exceed 16K.
- Structured output via **forced tool use**: `tool_choice={"type":"tool","name":"submit_edit_plan"}`
  with `strict: True` and `additionalProperties: false`, so the plan is schema-valid by
  construction and the model retries on mismatch.
- **Refusal handling:** check `response.stop_reason == "refusal"` *before* reading
  `response.content`, and opt into server-side fallback
  (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`).
- **Prompt caching:** put the capability manifest + system prompt behind a
  `cache_control: {"type": "ephemeral"}` breakpoint. Opus 5's minimum cacheable prefix is
  512 tokens, so the manifest alone qualifies. Verify with
  `usage.cache_read_input_tokens`. Keep the transcript *after* the breakpoint.
- Typed error chain: `NotFoundError` → `RateLimitError` → `APIStatusError` →
  `APIConnectionError`.
- Persist `usage` (input / output / cache read / cache creation) on `director_plans.usage`.

### 7.2 Passes

**Pass A — Read & structure.** Input: creative brief prefs, transcript in *director time*
with speaker labels, cut summary (N cuts, X seconds removed), duration, aspect, brand
palette if the workspace has one. Output: `brief` + `beats`. Cheap, high leverage.

**Pass B — Direct.** Input: pass A output + capability manifest + budget ceiling +
(optionally) sampled frames. Output: `directives[]` via forced tool call.

**Pass C — Critique.** *(flag-gated; not on by default until it earns its place.)* Input:
the plan + the manifest + explicit pacing rules (no two B-rolls closer than N seconds, no
B-roll over a face-to-camera punchline, total coverage ≤ X% of runtime). Output: pruned plan
+ `warnings`. Runs at `effort: "medium"`.

Forced tool use already guarantees schema validity, and the pacing rules above are
deterministic — the compiler can enforce them in code for free. Pass C only buys *taste*,
at roughly a third of the planning cost. Ship it behind a flag and let the offline eval
(§13) decide whether it stays on.

Passes run in one conversation so the cached prefix is reused across all of them.

### 7.3 Vision (optional, flag-gated)

Sample one keyframe per beat, ≤20 total, ≤1280px long edge, JPEG, sent as base64 image
blocks before the text block. This lets the director avoid covering a moment where the
speaker gestures at something, and match B-roll colour to the actual footage. Opus 5 is in
the high-resolution tier (2576px long edge, up to ~4784 tokens/image) — **downsample
deliberately** here; the fidelity is not needed and the token cost is real.

Extract with the existing `app/services/review_frames.py` / thumbnail machinery rather than
a new ffmpeg path.

### 7.4 Cost envelope (order of magnitude)

Opus 5: **$5 / MTok input, $25 / MTok output.**

A 10-minute talking head ≈ 1,600 words ≈ ~2.5K transcript tokens. With manifest + system
prompt (~4K, cached after the first pass) and three passes producing ~8K output total:

- Input: ~10K tokens, of which ~8K cache-read → ≈ **$0.02**
- Output: ~8K tokens → ≈ **$0.20**
- Vision (20 frames, downsampled): ~20K tokens → ≈ **$0.10**

**≈ $0.30 per director run.** Negligible next to media generation (Veo minutes) — which is
where the budget controls in §8.3 actually matter. Use the **Batch API** (50% off) for any
non-interactive bulk re-planning; not for the interactive path.

---

## 8. Asset generation

### 8.1 Fan-out

Reuse `GeneratedMedia` + `generate_media_job` unchanged, adding `plan_id` and `directive_id`
so the compiler can join assets back to directives.

```
for directive in plan.directives where type == "broll" and asset.source startswith "generate":
    create GeneratedMedia(plan_id, directive_id, kind, prompt=house_prefix + prompt, ...)
    enqueue generate_media_job(media.id)
```

Bounded by a semaphore (default 4 concurrent), a per-plan ceiling, and the existing
`cancel_requested` flag which the director cancel endpoint sets on every child row.

### 8.2 House style — the biggest quality lever

Twelve independently-prompted images look like twelve stock photos. Prefix **every**
generation prompt with `brief.houseStylePrefix` and append a fixed negative prompt
(`text, watermark, logo, caption, subtitle, signature`). This one change is most of the
difference between "AI slop montage" and "one film". It is also the easiest thing to forget.

For `generate-video`, additionally pass the *previous* directive's generated still as the
`reference` image where the beats are adjacent, so motion clips inherit the look.

### 8.3 Budget & failure

| Control | Default | Rationale |
|---|---|---|
| Max images per plan | 20 | |
| Max videos per plan | 3 | Veo is the cost/latency driver |
| Max B-roll coverage | 35% of runtime | Above this it stops being B-roll |
| Min gap between B-rolls | 6 s | Pacing |
| Concurrency | 4 | Worker pressure |
| Per-plan wall clock | 30 min | Hard abort |

The **budget ceiling is passed into the Claude prompt**, so the director plans within it
rather than being truncated afterwards. Truncating a plan post-hoc produces incoherent
pacing; a director told "you have 12 images and 2 video clips" spends them well.

**Failure policy:** a directive whose asset generation fails is **skipped, not faked**.
Record a `warning`; never place a broken/empty clip. If >40% of assets fail, mark the plan
`degraded` and surface it rather than silently shipping a half-directed cut.

### 8.4 As built — two deferrals

**Reference-chaining is not implemented.** §8.2 called for passing the previous directive's
generated still into an adjacent moving shot as a reference image, so motion inherits the
look. It needs the fan-out to become a dependency graph — a video generation cannot start
until its neighbouring still is `ready` — which is a real scheduler, not a loop. The house
style already carries most of the coherence; this is the increment on top. Recorded rather
than half-built.

**Concurrency is the queue's, not ours.** §8.3 specified a cap of 4. Everything is enqueued
onto the shared `default` queue, so actual parallelism is however many RQ workers are
running — a deployment concern today rather than a code one. A genuine per-plan cap needs
Redis-side coordination across workers; worth building if a large plan is observed starving
other jobs, and not before. The *count* is still bounded, by the budget.

**The wall clock is the job timeout.** `DIRECTOR_TIMEOUT_SEC` (default 1h) bounds the
planning job; each generation carries its own `AI_MEDIA_JOB_TIMEOUT_SEC`. There is no
separate per-plan stopwatch, because nothing holds a worker open across the whole run —
the planning job hands off once the shots are queued.

---

## 9. Compiler (`director_compile.py`)

Reads the plan + current draft, returns a **patch** merged into `rough_cut_draft`.

### 9.1 Per-directive rules

**`broll`**
1. Resolve anchor → source range (§6.2).
2. Ensure the target video track exists above V1; create with correct `order`
   (video/text share one ordered stack — see `normalizeTimelineTracks`).
3. Append `RoughCutTimelineMediaItem` with:
   - `sourceKind: "generated"`, `sourceId: <GeneratedMedia.id>` *(new field, G2)*
   - `start`/`end` in **source** seconds, `sourceStart: 0`
   - `playDuration` = director-time span, `audioEnabled: false`
   - `kind: "image" | "video"` from the asset
4. Write `clipAttributes["media:<id>"]`:
   - `animation: { inPreset, outPreset, duration, intensity }`
   - `video: { scale, x, y }`
   - Ken Burns → `keyframes["video.scale"] = [{t:0,v:from},{t:dur,v:to,easing:"ease-in-out"}]`

**`transition`** → append to the new `transitions[]` array (G3), clamped so it never exceeds
half of either neighbouring clip.

**`text`** → append a `TextOverlayStyle` (`kind: "block"` or `lowerThird`) on the TX track,
times mapped to source seconds.

**`emphasis`** → **deferred out of v1.** `clipAttributeKey` is `${track}:${range.id}`
(`rough-cut-editor-shared.ts:642`) — attributes apply to a *whole* keepRange, not a slice of
one. A 0.8 s punch-in inside a 12 s range therefore requires splitting `keepRanges` first
(`splitRangesAt`), which mints new range ids and orphans every existing
`clipAttributes` key derived from the old one. That is a migration, not a placement. Either
drop `emphasis` from the manifest for v1, or scope it to directives that align with an
existing range boundary. *Recommendation: drop it from the v1 manifest.*

**`music`** → recorded in the plan, compiled to a no-op, surfaced as a warning (G6).

### 9.2 Merge semantics — non-negotiable

- **Never clobber user edits.** If the draft carries `rangeEditVersion` *and* any signal of
  human editing beyond the auto-edit seed, do not apply; leave the plan in `planned` and let
  the user press Apply. The in-editor signal already exists:
  `userEdited: undoStack.length > 0 || localEditRef.current` (`rough-cut-page.tsx:3306`).
- Stamp `directorPlanId` + `directorAppliedAt` on the draft so the run is identifiable.
- **Provenance lives on the server, not on the artifacts.** A per-item `origin` tag would be
  stripped by the allow-list loaders (G9). Instead `director_plans` stores the manifest of
  everything the run created — `{timelineMediaItemIds, clipAttributeKeys, trackIds,
  textOverlayIds, transitionIds}` — and revert is a filter over that list. More robust and
  requires no schema change to the item.
- Applying is idempotent: re-applying the same `planId` is a no-op.

### 9.4 As built — no snapshot needed

The plan called for snapshotting the pre-director draft so a revert could
restore it. Building the compiler showed that to be both unnecessary and wrong.

**Unnecessary:** compiling only ever *adds*. Existing clips, attributes and tracks
are carried through untouched, so applying a plan cannot destroy editing — there
is nothing to restore.

**Wrong:** a restore would take the user's own work with it. They will very
likely have edited between applying and reverting, and rolling the draft back to
a saved copy would silently discard that. Revert is a filter over the ids the run
recorded (`applied_manifest`), so it removes exactly what the director added and
leaves everything else where it is — including the B-roll track itself, which is
only dropped if the user has not put their own clips on it.

The client-side snapshot in §11.4 still stands; that one is the editor's existing
`EditorSnapshot`/`undoStack`, and its job is making the run a single Ctrl-Z.

### 9.3 Determinism requirement

Because the backend compiles and the frontend *replays* (§4.2, §11.3), the replay must land
on the byte-identical draft the compiler produced. This is a **hard requirement**, not a
nice-to-have: if they diverge, the user watches one edit happen and then the autosave writes
a different one. The golden-fixture parity tests in §13 are what enforce it.

---

## 10. Render pipeline changes

Ordered by how badly their absence breaks the promise.

### R1 — Generated media as a first-class layer source *(blocks everything)*
- Frontend: `buildRoughCutTimelineLayers` emits `{sourceKind, sourceId}` instead of a bare
  `videoId`. Keep `videoId` for back-compat with saved drafts.
- Backend: `_normalize_timeline_layers` accepts `sourceKind ∈ {"video","generated"}`;
  `_authorized_layer_sources` resolves `GeneratedMedia` rows by `project_id` (the same scope
  the media panel binds to) and never trusts a client-supplied URL — preserving the existing
  "resolve by id, never by path" security property.

### R2 — Image layers in FFmpeg *(blocks all still B-roll)*
Drop the `kind != "video"` filter. For images: `-loop 1 -t <duration> -i <path>`, then
scale/pad into the canvas and feed the existing `_clip_compositor_filter_complex` chain
(animation, keyframes, adjust, corner radius all apply unchanged).

### R3 — Transitions
- Data: `transitions: [{ id, at, style, duration, trackId, origin? }]` on the draft.
- Editor: render in the viewer (crossfade two decoded frames) + a timeline affordance.
- Export: `xfade` between concat segments; `acrossfade` on the paired audio.
- Start with three styles — `dissolve`, `dip-to-black`, `whip` — not twelve.

### R4 — Burn text overlays / lower thirds
Removes the preview-vs-export mismatch (G4). Render the overlay to a transparent PNG per
overlay (server-side, using the same layout constants as the caption engine) and composite
with `overlay=enable='between(t,...)'`. This reuses the caption renderer's geometry rather
than reimplementing typography in FFmpeg `drawtext`.

R1 + R2 are **required** for the feature to work at all. R3 and R4 can ship in a second
milestone with the director instructed (via the capability manifest) not to emit those
directive types until they land.

---

## 11. UX

### 11.1 Wizard
New card below **Auto edit**: *AI Creative Director* (`components/create-project/card-director.tsx`),
mirroring `card-rough-cut.tsx`. Controls: enable, style preset
(*Documentary / Punchy / Corporate / Minimal*), B-roll budget tier
(*Light 6 / Standard 12 / Rich 20*), motion clips (on/off), and a free-text brief.
Persisted via `/ai/director-prefs`, exactly as auto-edit prefs are.

### 11.2 Progress
The rough-cut editor has **no WebSocket** — it is poll-driven
(`useRoughCutTranscriptionPolling`, `rough-cut-page.tsx:2056`). Reuse that pattern: the
wizard's wait screen and the editor both poll `GET /ai/director`. Stages surface as human
sentences, not percentages: *Reading the cut → Writing the treatment → Sourcing 12 shots
(7 ready) → Assembling*.

Cost preview belongs in the wizard before the run, extending the existing
`components/create-project/wizard-estimate.ts`.

### 11.3 The performance
Extend the existing choreography rather than inventing a second one. `director-performance.ts`
produces steps in the same shape as `AutoEditStep`:

```
thought  "Reading the cut"
thought  "This is a 92-second explainer. Warm, unhurried."
beat     "Hook — the two-day tax"
place    d001  → travel, select track, drop clip, fade in
place    d002  → …
thought  "Checking the pacing"
```

The same two rules hold: **the commit never depends on the animation**, and **skip lands in
exactly the same place**. Total budget ~40 s, skippable, and respects
`prefers-reduced-motion` (applies instantly, no show).

### 11.4 Review & undo
- **Plan panel** — read the brief, the beats, and every directive with its `why`. Toggle any
  directive off before applying, or after.
- **Revert** — the editor already has the machinery: `EditorSnapshot`, `undoStack`, and
  `restoreSnapshot` (`rough-cut-page.tsx:478`, `:980`, `:1053`). Push **one** snapshot
  before the run applies, and the whole director pass becomes a single Ctrl-Z. No bespoke
  revert path needed in the editor; the server-side manifest (§9.2) is for reverting a run
  that was applied headlessly, with the editor closed.
- Do not push 30 snapshots — the performance commits many changes, but it is one edit.

### 11.5 Trust
Show `confidence` and `why` on each placed clip's inspector. A director that explains itself
gets edited; one that doesn't gets reverted wholesale.

### 11.6 As built, and what is left

**Built.** `director-panel.tsx` is the trust surface: the treatment and its rationale, the
house-style sentence verbatim (a user who dislikes the look needs to see the line that
produced it), every shot with the **quote it is anchored to** and the reason for it, coverage
as a percentage of the cut, and a list of what the run left out. Failed shots stay in the
list rather than being hidden — a gap the user can see explained beats one they can only
wonder about. The judgement lives in `_lib/director/director-view.ts`, pure and tested, so
"which actions are available" has one answer the panel, a shortcut and a toast all share.

`useDirectorRun` polls only while the run is working. Worth knowing: the poll is not merely
how the UI *learns* things — `GET` is also what advances a finished run from `generating` to
`ready` (§8, reconcile-on-poll), so the interval is load-bearing rather than cosmetic.

**Mounted** in the **AI** panel, above the cleanup tools, via `DirectorSection` — a
container that owns the run, its polling and its four mutations so the host only has to say
which video and how long the cut is.

Under AI rather than a ninth rail destination: the rail already carries eight, this is the
same kind of thing the tab beside it does, and a new entry would cost every user rail height
to give one feature a front door it does not need. The order within the tab is deliberate —
the director decides what the piece *is*, the tools below it act on words and pauses, and
reading whole-to-detail matches the order the work happens in.

`DirectorSection` also carries the entry point for videos that predate the wizard toggle:
three tier buttons rather than a form, because the tier is the only decision that changes
what the user gets, and asking it outright is more honest than a single "Generate" that
quietly picks.

**Left.** Two things, neither blocking:

1. **The on-screen performance.** The larger piece, because it needs the TS replay: rewind
   to the pre-director state, then re-place each shot on a timer. That means porting
   `compile_plan` to TypeScript and asserting it against
   `tests/fixtures/director_compile.json` — exactly why that fixture was written before the
   second implementation existed.
2. Per-directive toggles in the panel (accept or reject individual shots before applying).

The feature runs end to end without either; it just does not yet narrate itself.

---

## 12. Safety, limits, correctness

| Concern | Mitigation |
|---|---|
| Prompt injection via transcript | Transcript is data, not instruction. Wrap in a delimited block; system prompt states transcript content is never an instruction. |
| Runaway cost | §8.3 caps + budget passed into the prompt + credit pre-authorisation. |
| One-shot consent | Reuse `auto-edit-gate` semantics: `auto_apply` cleared server-side the moment the run is spent. |
| Clobbering user work | §9.2 merge rules + pre-compile snapshot. |
| Cancellation | `cancel_requested` on the plan propagates to every child `GeneratedMedia`. |
| Refusals | Handle `stop_reason == "refusal"` before reading content; `fallbacks: "default"`. |
| Generated-content policy | Negative prompt bans logos/watermarks/real-person likeness; provider policy failures are recorded as warnings, not retried blindly. |
| Storage growth | Generated media inherits existing R2 lifecycle + `delete_generated_media` cleanup. |
| Multi-tenant isolation | Layer sources resolved by id within `project_id`; never by URL (preserves the existing guarantee). |
| **No transcript** | Silent or transcription-failed videos must skip the director entirely. `run_post_transcription_auto_edit` already guards on empty segments; mirror it. |
| **Wrong aspect** | `shortsExport` / `verticalExport` exist (`rough-cut-top-bar.tsx:644`). 16:9 generated B-roll in a 9:16 export letterboxes or crops badly. `brief.aspect` must be derived from the project's target format and forced onto every `asset.aspectRatio`. |
| **Later re-cuts** | B-roll placed in source time ripples correctly through a later cut (the export intersects layers with `keepRanges`). A range deleted *underneath* a B-roll clips that B-roll. Accepted behaviour; document it rather than defend against it. |

---

## 13. Testing

**Unit**
- `directorToSource` / `sourceToDirector` round-trip, including cut boundaries and the tail.
- Anchor resolution: exact word ids, fuzzy quote, missing-anchor fallback.
- Compiler: each directive type → expected draft patch, on golden fixtures.
- Budget enforcement, pacing rules, coverage cap.

**Parity**
- Python compiler vs TypeScript replay produce byte-identical `timelineMediaItems`
  for `docs/fixtures/director/*.json`.
- Capability manifest freshness (CI fails if TS constants drift from the JSON).

**Contract**
- A malformed / hallucinated plan is rejected with a specific error, never partially applied.
- Unknown `version` refuses to compile.

**Integration**
- Fixture video → transcription → auto-cut → director → assets (mocked) → compile → export.
  Assert the MP4 actually contains the B-roll (frame hash at a known timestamp differs from
  the A-roll at that timestamp — this is the test that would have caught **G2**).

**Model-behaviour (offline eval, not CI)**
- 10 fixture transcripts across genres; score plans on: valid enums (must be 100% via forced
  tool use), anchor resolvability, pacing-rule violations, coverage, human rating 1–5.
- Re-run on any prompt change. Track as a regression suite, not a gate.

---

## 14. Milestones

| # | Milestone | Deliverable | Exit criteria |
|---|---|---|---|
| **M0** ✅ | Render foundation *(G2 + G9 + C3)* | R1 + R2 + `sourceKind`/`sourceId` across **eight** allow-list touchpoints + C3 easing fix | **Shipped.** Generated image and video B-roll resolves to its own media, survives a draft reload, and stills render via `-loop 1`. 24 new tests, verified to fail against the reintroduced bugs. |
| **M1** ✅ | Claude client | `claude_client.py`, `ANTHROPIC_API_KEY`, `anthropic` dep | **Shipped.** Forced-tool structured output, refusal-before-content, graceful degrade when the fallback beta is unavailable, cross-pass prompt caching, token accounting. 26 tests. |
| **M2** ✅ | Plan generation | Context builder, prompts, manifest, passes A/B, `director_plans` table, REST + job, **plan debug viewer** | **Shipped** — manifest, schema/validation, coordinate map, prompts, passes A/B, `director_plans` table + migration, RQ job, REST surface (135 tests). The debug viewer moves to M5 with the rest of the UI; `GET /ai/director` returns the whole plan, so a plan is reviewable over the API today. |
| **M3** ✅ | Asset generation | `plan_id`/`directive_id` provenance, fan-out, house style, budget, cancel | **Shipped** — `director_assets.py`, house style on every prompt, skip-never-fake failure policy, `degraded` above 40% failures, reconcile-on-poll (156 director tests). Deferred: reference-chaining stills into moving shots, and a cross-worker concurrency cap — see §8.4. |
| **M4** ✅ | Compiler | `director_compile.py`, merge rules, revert, snapshot, parity fixtures | **Shipped** (202 director tests): director→source conversion, track placement, Ken Burns as keyframes, idempotent re-apply, manifest-based revert, `apply`/`revert` endpoints, and `tests/fixtures/director_compile.json` locking the contract for the editor's replay. No snapshot column — see §9.4. |
| **M5** ◐ | UX | Wizard card, progress, performance, plan panel, undo | **Shipped and reachable**: post-cut trigger with one-shot consent, `director-prefs` endpoints, wizard card, typed API client, `useDirectorRun` polling hook, the plan review panel, and `DirectorSection` mounted in the AI tab (236 backend + 20 view tests). Remaining: the on-screen performance and per-shot toggles — see §11.6. |
| **M6** | Transitions + titles | R3 + R4, manifest expanded | Director may emit `transition` and `text`; both render identically in preview and export. |

M0 first is deliberate: without it, everything downstream produces a beautiful plan that
renders as duplicated A-roll — and that failure is invisible in the editor preview, which is
exactly how it survived undetected until now.

---

## 15. Open decisions

These change the work materially and need a call before M2.

1. **Vision on or off by default?** Costs ~$0.10/run and adds latency; materially improves
   placement quality. *Recommendation: on for Standard/Rich tiers, off for Light.*
2. **Auto-apply or review-first?** The brief says "apply automatically". The safer default
   is auto-apply **only** when the draft is untouched (server-seeded), review-first
   otherwise — same rule `auto-edit-gate` already enforces for cuts.
   *Recommendation: adopt that rule.*
3. **Video B-roll in v1?** Veo is minutes per clip and the dominant cost. *Recommendation:
   images only in M3; enable `generate-video` behind the Rich tier in M4.*
4. **Credits model.** Bill per generated asset (predictable, user-legible) or per director
   run (simpler)? *Recommendation: per asset, reusing `ugc_credits`.*
5. **Stock footage.** Leave `source: "stock"` in the schema unimplemented, or drop it from
   the manifest so the model can't emit it? *Recommendation: keep in schema, exclude from
   manifest — forward-compatible without hallucination risk.*

---

## 16. Review record (2026-08-11)

The first draft was written from a partial read. Every load-bearing claim has now been
checked against the source. What changed:

### Corrections — the draft was wrong

| # | Claim in draft v1 | Reality |
|---|---|---|
| 1 | "notify (WS + notification row)" | The rough-cut editor has **no WebSocket** — it is poll-driven (`useRoughCutTranscriptionPolling`). Corrected to polling throughout. |
| 2 | New fields can be added to timeline items and the draft | **False.** Three separate allow-list loaders reconstruct objects field-by-field and drop everything else. Filed as **G9**; it makes M0 materially larger. |
| 3 | Tag artifacts with `origin: "director:<planId>"` for revert | That tag would be stripped by G9. Replaced with a server-side id manifest on `director_plans`. |
| 4 | `emphasis` → keyframes on the A-roll range | `clipAttributeKey` is `${track}:${range.id}` — attributes are whole-range. A sub-range punch-in needs `splitRangesAt` plus a clip-attribute key migration. **Deferred out of v1.** |
| 5 | Build a bespoke revert path | Unnecessary. `EditorSnapshot` + `undoStack` + `restoreSnapshot` already exist; one snapshot push makes the whole run a single Ctrl-Z. |
| 6 | Pass C (critique) is part of the pipeline | Downgraded to flag-gated. Forced tool use covers validity; the pacing rules are deterministic and belong in code. It only buys taste, for ~⅓ of planning cost. |

### Confirmed — the draft was right

- `_timeline_layer_graph` (`rough_cut_export.py:1150`) **does** apply `_animation_expressions`,
  `_channel_expression` and `build_keyframed_adjust_filter_chain` to layers. Ken Burns via
  `keyframes["video.scale"]` on a B-roll clip will render — once the layer can resolve its
  source (R1/R2). The plan's compile strategy holds.
- `ugc_credits` exposes workspace-scoped `balance` / `reserve` / `refund` / `topup` — directly
  reusable for per-asset billing.
- `review_frames` exposes `pick_timestamps` / `extract_frames` — directly reusable for the
  vision pass. No new ffmpeg path needed.
- No music bed anywhere in `_timeline_audio_graph` (only `amix` over layer audio). **G6 stands.**
- G2's four-part render failure is real and reproduces by inspection.

### Gaps the draft missed entirely

- **G9** (allow-list schema) — the most consequential omission.
- No guard for **videos with no transcript**.
- No handling of **vertical / shorts aspect** (`shortsExport` exists) — 16:9 B-roll in a 9:16
  export is wrong, and the director must be told the target aspect.
- No **determinism requirement** between the backend compiler and the frontend replay. Added
  as §9.3; without it the user watches one edit and autosaves a different one.
- No **cost preview** before the run; `wizard-estimate.ts` is the natural home.
- M2 had no way to be validated — a **plan debug viewer** is now part of the milestone.

### Judgement on the plan as a whole

The architecture holds up: the pipeline shape, the anchor-first coordinate strategy, the
EditPlan contract, the manifest-from-source-of-truth trick, and the backend-compiles /
frontend-replays split all survive scrutiny. The house-style prefix remains the highest-value
detail in the document.

The estimate does not hold up. **M0 was scoped as "fix the export"; it is really "fix the
export *and* thread two new fields through five hand-written serialisation layers."** That is
the correction most likely to matter to the schedule.

One thing to decide before writing code: with `emphasis` deferred and `music` and `stock`
already no-ops, v1 directives reduce to **`broll`, `transition`, `text`**. Of those,
`transition` and `text` need R3/R4 (milestone M6). So the honest v1 is a **B-roll director**,
with the rest arriving in M6. That is still the core of the request — but it should be said
plainly now rather than discovered at M5.

---

## 16b. First real run (2026-08-16) — blocked, but not wasted

Attempted end to end on video 8 (54s product explainer, already auto-cut to 15.9s
across 6 keepRanges), Light tier, stills only.

**Blocked at the model call.** `ANTHROPIC_API_KEY` resolves from the *shell*
environment (108 chars, `sk-ant-api0…`) rather than `.env`, whose value is empty —
and the API returns `401 API key is invalid`. Nothing downstream of planning has
therefore ever executed: no plan, no generated image, no compiled timeline, no
render of a directed cut. The feature remains verified against mocks only.

The failure path did behave: the job recorded the reason on the row and left the
transcript and cut untouched.

**One real bug found before the wall, in `build_context`.** Reaching real ASR
output and real `keepRanges` immediately broke something no fixture had:

- Three of nine segments survived the cut, but only **two** reached the model, and
  both were reported as zero-length spans (`[s1] 0.12–0.12`).
- Cause: the line's bounds were recomputed from each word's *raw source* times
  instead of the director times `director_words` had already clipped. A surviving
  word can start inside a removed gap and only overlap the kept range partway
  through, so `to_director(word.start)` is None even though the word is on
  screen. The None dropped whole segments; the same None at the other end
  collapsed a line to `start`.
- This is exactly the boundary case `director_words` was fixed to handle, undone
  by second-guessing it a few lines later.
- Fixed, plus two regression tests, both verified to fail against the
  reintroduced bug.

Worth recording for what it says about the remaining risk: the bug lived through
29 context tests and 236 director tests, and died within seconds of meeting one
real transcript.


## 17. Craft bar — "designed by a senior motion designer"

**Requirement:** output must read as the work of someone with decades in After Effects and
pro-tier NLE experience — not as template motion. This is a hard acceptance criterion, not a
stretch goal, and it changes the engineering, not just the prompts.

"Make it look professional" is unactionable. Below is the translation into constraints.

### 17.1 What the motion system can already express

Genuinely good raw material, all of it already rendering: per-clip transform + **keyframes on
30+ channels**, temporal **motion blur** via `tmix` (`_motion_blur_filter_parts:879` — real
frame blending, not a spatial fake), **corner radius**, **masks** with tracking, **grid /
split-screen**, and a **full colour pipeline** (curves, HSL, lift-gamma-gain wheels) that runs
identically in preview and export.

### 17.2 Defects that break the bar *(found in review, all verified)*

| # | Defect | Evidence | Why a motion designer notices |
|---|---|---|---|
| ~~**C1**~~ ✅ | ~~**Custom easing does not exist.**~~ **Fixed.** Six curves added: `smooth`, `glide`, `snappy`, `anticipate`, `settle`, `overshoot`. | `clip-keyframes.ts`, `rough_cut_export.py` | Was: pro motion lives on asymmetric ease, overshoot and anticipation, and a quadratic ease is the clearest tell of template motion. See §17.7 for why the implementation is closed-form rather than cubic-bezier. |
| **C2** | **`blur` is silently dropped in export.** `_preset_expressions` returns only `{x,y,scale,rotation,opacity}`; the frontend returns `blur` for `zoom` (7×) and `focus` (14×). | `rough_cut_export.py:754` vs `clip-animation.ts:54,64` | The `focus` preset is a **defocus-in**. In the MP4 it renders as a plain scale+fade — the preset loses the entire thing it is named for, and preview lies. |
| **C3** | **Combo animations are linear in export, eased in preview.** Export ramps `zoom`/`spin`/`slide` on raw `phase`; the frontend applies `easeInOut(phase)`. | `rough_cut_export.py:786-791` vs `clip-animation.ts:74-77` | A linear drift versus an eased one is precisely the difference between "cheap" and "considered". Also a preview/export parity bug in its own right. |
| **C4** | **No clip drop shadow.** `shadow` in the types is colour-grading only. | `rough-cut-types.ts:103` | An inset / PiP B-roll with no shadow does not sit in the frame. Table stakes for pro compositing. |

C2 and C3 are parity bugs independent of this feature and should be fixed regardless.

### 17.3 Engineering additions required

1. ~~**`cubic-bezier(x1,y1,x2,y2)` easing**~~ ✅ **Shipped** as a named vocabulary the
   director emits by name, not by numbers — `smooth`, `glide`, `snappy`, `anticipate`,
   `settle`, `overshoot`. Named curves are what make many clips feel like one hand. See
   §17.7 for why they are closed-form rather than true beziers.
2. **Frame-quantised timing.** Durations snap to the project frame rate. `0.35 s` is a
   number; `8 frames @ 24 fps` is a decision. The director emits frames; the compiler converts.
3. **A house motion grammar**, not free choice. One in/out pair, one duration, one curve per
   project, chosen once in `brief` and applied to every B-roll. Variation is earned per beat,
   never per clip.
4. **Motion blur on by default** for any move above a velocity threshold. It already renders;
   it is simply never switched on.
5. **Composition beyond full-frame.** Inset (scale + offset + `cornerRadius` + shadow),
   masked reveal, split-screen via `gridClips`. Full-frame-cut-in for every B-roll is the
   template look.
6. **Colour continuity.** Emit a per-clip `adjust` grade matching B-roll to A-roll
   (temperature, contrast, a shared curve). Ungraded generated media reads as pasted in —
   more damaging to perceived quality than any animation choice.

### 17.4 Generated-image craft

The house-style prefix (§8.2) must be a **DP brief**, not a vibe: lens and focal length, film
stock or sensor character, lighting direction and quality, colour temperature, depth of
field, grain, and negative space reserved for titles. Same brief on every generation, every
time. This — plus §17.3(6) — is most of what separates a coherent film from twelve stock
photos.

### 17.5 Acceptance

A run is accepted when a motion designer, shown the export with no context, cannot identify
which clips were placed by the system. Track it as a rated criterion in the offline eval
(§13), not as a checkbox.

### 17.6 Scheduling

**C3 — fixed in M0.** The three combo animations now ramp through the same cubic
ease-in-out the viewer uses (`_ease_in_out_cubic`, deliberately distinct from the *quadratic*
`ease-in-out` used by keyframe easings — they are two different curves in the editor and both
have to be reproduced as written).

**C2 — moved to M6.** The first estimate was wrong. `gblur`'s `sigma` is a plain float, not
an expression (`ffmpeg -h filter=gblur`: `sigma <float> ..FV.....T.`), so an animated defocus
cannot be written as a filter expression the way every other animated channel is. The
options are a `sendcmd` schedule driving one `gblur` node, or a staircase of `enable`-gated
instances. `sendcmd` is the right answer, but it adds a filter node to **two** graph builders
(`_clip_compositor_filter_complex` and `_timeline_layer_graph`) and changes
`_preset_expressions`' return contract from "geometry expressions" to "expressions plus a
filter". That is a design change, not a patch. It belongs with C1, which needs the same
care. Until it lands, `focus` renders as scale+fade — a known, recorded gap, not a surprise.

**C1 — shipped early** (§17.7), ahead of M1, so the director's capability manifest can offer
real curves from its first plan rather than emitting quadratic easings that would need
migrating later. **C4 (shadow)** remains in **M6** alongside transitions. The manifest must
not offer C2's `focus` or C4 until they land.

### 17.7 C1 as built — closed-form curves, not cubic-bezier

The plan called for `cubic-bezier(x1,y1,x2,y2)`. Implementing it revealed why that is the
wrong primitive *for this renderer*, and the substitute is better rather than merely cheaper.

**Why not true beziers.** A CSS cubic-bezier is parametric: `x(t)` and `y(t)` are both cubics
in `t`, so easing a progress value means *solving* `x(t) = p` for `t` — Newton-Raphson —
then evaluating `y(t)`. Iterating inside an ffmpeg expression requires the `st()`/`ld()`
register file (verified available). But `_channel_expression` nests every keyframe segment
inside the next in a single `if()` chain up to **200 keyframes deep**, all sharing one
register file. Correctness would then depend on ffmpeg lazily evaluating untaken `if()`
branches — an undocumented property to hang the whole motion system on.

**What shipped instead.** The Penner closed forms, which is the vocabulary motion designers
actually reach for:

| Curve | Formula | Feel |
|---|---|---|
| `smooth` | cubic in-out | the workhorse; steeper mid-move than the quadratic `ease-in-out` |
| `glide` | `1-(1-p)³` | gentle deceleration |
| `snappy` | `1-(1-p)⁵` | leaves fast, arrives hard |
| `anticipate` | `c₃p³ − c₁p²` | dips to **−0.099** before moving |
| `settle` | back-out, `c₁ = 1.70158` | overshoots to **1.099**, settles |
| `overshoot` | back-out, `c₁ = 2.595` | same, further |

No registers, no iteration, expressions small enough to read — and the same asymmetric ease,
anticipation and overshoot the bar asks for. The five original easings are **untouched and
still quadratic**, so every saved project renders exactly as before.

**Parity.** `editube/tests/fixtures/easing_curves.json` is a checked-in contract both
implementations assert against, so the preview and the render cannot drift apart silently.
The Python suite additionally hands each generated expression **to ffmpeg** and reads the
number back out of a 16-bit luma plane — a formula can be correct in Python and still wrong
as an ffmpeg expression (precedence, a missing function, a negative base under `pow`), and
only the real evaluator can say. Agreement is within 2.1×10⁻⁵, which is exactly the 16-bit
quantization step. Both suites were verified to fail against a deliberately introduced drift.
