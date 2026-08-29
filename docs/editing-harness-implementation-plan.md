# Editube Intelligent Editing Harness

## Product Requirements and Implementation Plan

- **Status:** In implementation — Phase 0 substrate and the Phase 1 engine + Subject-behind-text recipe landed on `feat/editing-harness` (backend `42cace1`, frontend `8c1db3b`, 2026-08-29): revisioned draft store with one write path and optimistic concurrency, TimeMap with cross-language fixtures, capability registry, harness run/operation engine with inverse-manifest revert, staged apply through the effect machinery, run-addressed REST surface, the compound-edits panel with the §16.3 draft-handover protocol, group fields through the loader round trip, `textOverlays` persistence, the four §5.3 Phase-0 export bug fixes, and the OpenRouter free-model planner with a live e2e. A second Workstream E push (backend `00fcc9b`, frontend `1fcd459`) closed most of the remaining §5.3 ledger: styled ASS caption export with karaoke word highlight, element/shape burn-ins from the viewer's own primitive data, real audio crossfades on transitions, sidechain ducking and two-pass final-mix loudness targets, per-clip mask scoping, the G7 fail-fast tracking guard (MIL opt-in only), and a libass probe in the export capability (this dev machine's ffmpeg has no `subtitles` filter at all). A third push (backend `b050ff3`, frontend `16b279c`) added the SAM2 **propagation tracking backend** (CSRT > propagate > MIL-opt-in, identical result shape to the box tracker, pure core unit-tested without torch — Recipe C's runtime prerequisite where the ML stack is installed), **brand badge burn-ins** (the unconditional skip note is gone), and the **`disabled` clip flag honored** in both preview and export. A fourth push (backend `33a8dda`, frontend `68c595f`) closed **per-clip speed** end to end — the 0.25–4× slider that previously did nothing anywhere now retimes plain segments in export (`setpts` + `atempo`, with every output-clock consumer rate-aware: captions/karaoke, layer chunks, transitions, burn-in clamps) and drives preview playback, behind a client-mirrored v1 veto (no combining with masks, rendered effects, or compositor features — those render at 1× with a warning). Declared preview limitations: reverse/tail clocks ignore clip speed; the timeline draws sped clips at source length. A fifth push (backend `c06f3c3`, frontend `75d0f19`) opened **Phase 2**: natural-language intent planning is wired into the product — intent-only run creation plans recipe parameters through Claude (forced strict tool, schema = the recipe's own Pydantic model) with the best free OpenRouter model as fallback and an honest `needs_input` state when neither is configured; provenance (provider/model/prompt version/token usage) lands on the run; the panel gains an "Or describe it" box; a live e2e proves the chain on the free tier. A sixth push (backend `e4324aa`, frontend `8f2a422`) delivered the first **non-additive operation family** — `visual.adjust`/`audio.adjust` target the user's own clips by clipKey + source-range fingerprint (skip-with-reason on a re-cut or deletion; merge-never-replace; `restore_value` inverses proven by apply∘revert-identity property tests, including refusal to clobber post-run user changes) — and the recipe's `dimBackground` option exercising it end to end, with the intent planner picking the new field up automatically from the schema. A seventh push (backend `5328978`, frontend `6135e91`) delivered the **AI-Review adapter**: visuals/audio findings may carry a machine-applicable `fixAction` (hardened to the harness's own operation bounds; non-actionable categories never carry one), the `review_fix` recipe compiles it onto the clip under the finding with full fingerprint/merge/exact-revert semantics, and the review panel's wand button plans a real run and opens it in the editor's plan panel via `?harnessRun=` adoption — proven end to end over the HTTP surface. An eighth push (backend `028259b`) opened **Phase 3** with the tracked-callout recipe: four new primitives (`analysis.track_object` staging SAM2 propagation keyframes, `media.stage_label` rendering an owned GeneratedMedia label card server-side, `timeline.place_label` as a first-class generated image layer — a layer, not a burn-in, because callouts move — and `motion.track_keyframes` binding `video.x/y` to the staged track at commit), capability-gated on tracking+storage, lost-tracking surfaced as a warning, follow-motion verified, and creation-from-nothing made fully reversible. Runs live where the SAM 2 stack is installed; proven here with faked ML over the full HTTP loop. A ninth push (backend `129e00a`, frontend `1c1a15f`) delivered the **Director migration (Phase 4)**: `director_compile` split into pure `resolve_placements` (fixture-identical `compile_plan` retained only as the editor-replay parity contract), a new `timeline.place_media` primitive, and `director_apply` rewritten to route through the harness — every apply mints a `HarnessRun` (`recipe_id="director_broll"`, run-id-addressed entity ids `ehr{run}-shot_{directive}`) that owns the compiled plan, applied/inverse manifests, operation rows, verification report and draft revision, all landing atomically in the store's commit; revert routes through `executor.revert_run` and **keeps the inverse manifest as the audit trail the old path destroyed**, while the DirectorPlan row resets to `ready` for re-apply without re-generation. Anchor resolution still runs against the live draft inside the conflict-retry loop, so re-cuts move the shots. Pre-migration manifests (no `harnessRunId`) keep the legacy id-filter revert. The applied-manifest-unreachable-after-new-run defect is now structurally impossible for new runs (Phase 4 exit criterion). A tenth push (frontend `ed6b0cd`) gave the tracked-callout recipe its **draw-a-box gesture**: the harness panel arms it, the viewer mounts a one-drag overlay above every other stage layer (pausing playback so the boxed frame is the seeded frame), a pure tested helper converts the drag into the backend's centre-offset percent box, and the panel plans the run through the normal review-apply loop — with entry availability gated on the (previously unused) capabilities endpoint so a deployment without a tracker says why. The tracking window defaults to six seconds from the drawn frame unless the user's selection covers that moment: their selection says when, the box says where. An eleventh push (backend `6f637c7`, frontend `626dd8d`) recovered **overlay motion in export** (§5.3 item 9, by a better route than server-side text rendering): the client raster stays the viewer's own pixels — no font drift — but each burn-in now carries the overlay's animation (preset pair, duration, resolved em, pivot box from the real layout, grown to rotated bounds), and the render replays it with the compositor's existing moves (geq alpha in pixel time, rotate on a transparent canvas, per-frame scale, centre-anchored overlay), mirroring `text-canvas-animation.ts` curve for curve with exit-wins-over-enter ordering. Pivot presets degrade to fade when the box is missing; static entries keep the exact old graph; a real-ffmpeg test renders the animated graph. `motionLost` narrows to the honest residue: per-word/letter stagger and loops. A twelfth push (backend `2ac5ab8`, frontend `d52c27b`) extended the channel to **element animations** via a second, generic motion form: rather than porting ten presets × twenty-four easings expression-by-expression, the client samples its own engine (`sampleElementAnimation`, phase-isolated, 25 samples/phase — spring easings, intensity and delay baked in) into piecewise-linear tracks, one phase per anchor (`in` from entrance, `out` ending at exit, `loop` wrapping `mod(t-start,duration)`), and the render replays them through the same crop/geq/rotate/scale/overlay chain; phases merge as the element engine merges (opacity/scale multiply, offsets/rotation add), pivot tracks drop without a box, and a real-ffmpeg test renders the sampled graph. Element `motionLost` narrows to the clip-path pair (`draw`/`wipe` reveal pixels rather than move them). A thirteenth push (backend `4ce1a72`, frontend `63310bc`) closed the clip-path residue with a **`reveal` track**: ffmpeg cannot animate a crop's size per frame, but geq can gate alpha on X (`gte(W*(reveal),X)`), so element `draw`/`wipe` sample into reveal (the engine's own `inset()` parsed back to a fraction) and lower thirds get their real power3.out wipe entrance back — sampled from `lowerThirdReveal`, scoped to the card's actual `lowerThirdLayout` bounds — instead of the 0.42s fade stand-in. Element `motionLost` is now empty for good; text-side residue is per-word/letter stagger and loops only. Remaining: CSS-variant element cards (skipped by design), generation-loss reduction, timeline length semantics for speed, recipe versioning/team parameters (Phase 4 tail), controlled autonomy (Phase 5).
- **Date:** 2026-08-29
- **Scope:** Rough Cut editor, AI Creative Director, AI Review fixes, reusable editing recipes, and future editing agents
- **Primary objective:** Turn natural-language or structured editing intent into safe, deterministic, reviewable, reversible, and export-correct compound edits.

**Grounding.** Every current-state claim in this document has been verified against the source at the date above and carries a `path:line` reference. Backend paths are relative to `editube/`; frontend paths marked `fe:` are relative to `editube-frontend/`, and rough-cut paths abbreviate `app/(sites)/dashboard/rough-cut/` to `rough-cut/`. The companion document `docs/ai_creative_director.md` is the Director's own spec and review record; this plan builds on it and does not repeat it.

---

## 1. Executive decision

Editube should build a model-independent **editing harness** between AI/planning surfaces and the editor's deterministic primitives.

The harness is not a chat box that edits draft JSON. It is a transaction engine with five responsibilities:

1. Resolve the user's intent and targets.
2. Compile the intent into a typed operation graph.
3. Show an exact preview, risk summary, cost, and timeline diff.
4. Apply approved operations atomically or in explicitly disclosed stages.
5. Verify the saved draft and exported media, then provide precise revert.

The first production recipes:

1. **Subject behind text** — duplicate a clip, create a visual-only foreground layer, remove its background, place text between the layers, track the subject, and animate the result.
2. **Polish talking head** — improve edit rhythm, captions, framing, color, face retouching, voice clarity, music ducking, and transitions without making the result look synthetic.
3. **Tracked product callout** — locate a product or selected object, track it, add a label/shape/arrow group, keep it inside safe zones, and animate it coherently.

Those three recipes force the architecture to solve the difficult reusable problems: time mapping, stable identity, grouping, linked duplicates, ML dependencies, keyframes, layering, audio ownership, revision conflicts, export parity, cancellation, verification, and rollback.

**The honest framing after the audit:** roughly half of this harness already exists as the Creative Director (§2), and it works — additive plans, quote-anchored placement, manifest revert, forced-tool structured output, budget validation, reconcile-on-poll. What does *not* exist anywhere is the substrate a general editing agent needs: a revisioned draft, stable clip identity, a linked/grouped compound model, non-additive rollback, job idempotency, and preview/export parity for most of the visible feature surface. The audit found the parity situation materially worse than previously believed (§5.3): captions styling, shapes, per-clip speed, brand overlays, ducking, and final-mix loudness all silently diverge or vanish in export today — some with no warning at all.

This is not a one-sprint feature. A credible production implementation is approximately **18–26 engineer-weeks** for a focused team of three to four engineers; the increase over the previous estimate is the export-parity debt itemised in §5.3 and the identity/revision substrate in §11–§12, both now sized against real code rather than assumption. A demo can be produced faster; a trustworthy editing system cannot.

---

## 2. What the Director already proved — inherit, don't reinvent

The Director (commit `f80dbf6`, spec `docs/ai_creative_director.md`) is the harness's working prototype for the *additive* case. Five of its design decisions survive contact with production code and become harness law:

1. **Pure compiler behind a thin DB applier.** `director_compile.compile_plan` / `revert_plan` are dict-in/dict-out, fixture-tested (`tests/fixtures/director_compile.json`), portable to TypeScript for on-screen replay; `director_apply.apply_plan` / `revert` are the only functions that touch the database (`app/services/director_apply.py:1-15`). The harness executor keeps exactly this seam.
2. **Idempotency by derived ids, not dedupe tables.** `item_id = f"dir{plan_id}-{directive_id}"` makes re-apply a structural no-op (`app/services/director_compile.py:196-310`, `tests/test_director_compile.py:276`). Harness operations reserve ids the same way: derived from `(run_id, operation_key)`, never minted at apply time.
3. **Anchor-first targeting.** Directives anchor to a verbatim transcript quote plus segment id; `resolve_anchor` re-resolves against the draft *as it is at apply time*, and an inexact match **drops the operation with a warning rather than placing approximately** (`app/services/director_context.py`, `director_plan.py:248-386`). Anchors are what make a plan survive a re-cut; raw timestamps do not.
4. **Rules enforced in both the prompt and the code.** The manifest text and the JSON Schema enums are generated from the same Python constants so they cannot drift, and `validate_plan` re-checks every relational constraint the schema cannot express. The rationale at `director_plan.py:9-16` — pacing rules stated only in the prompt get mostly followed; enforced only in code they produce plans that were coherent before trimming and arbitrary after — is the single most transferable design note in the codebase.
5. **Partial failure as a first-class outcome.** Bad directives are dropped individually with user-readable reasons; failed asset generation is skipped, never faked; a run past a 40% failure ratio becomes `degraded` and remains appliable (`app/services/director_assets.py:41,185-198`). The harness generalises this into per-operation status.

And four of its known limits define harness work:

- **Manifest revert requires strict additivity.** `applied_manifest = {timelineMediaItemIds, clipAttributeKeys, trackIds}` and revert is a filter over those ids (`director_compile.py:317-368`). The moment an operation *modifies or deletes* existing state, that design stops working. The harness needs true inverse manifests (§14) — before-values per changed path — and must keep them after revert (the Director nulls the manifest on revert, destroying the audit trail: `director_apply.py:145`).
- **No revision check on apply.** `apply_plan` reads the draft blob, compiles, writes it back with no version/ETag guard — a lost-update race against the editor's 900 ms autosave. The spec's "never clobber" rule (`docs/ai_creative_director.md` §9.2) was never implemented; `rangeEditVersion` is consulted only by auto-edit (`app/services/auto_edit.py:781`).
- **Runs are addressed by `_latest(video_id)`, not by id** (`app/api/routes/director.py:50-56`). Starting a new run makes the previous applied run's manifest unreachable — its clips can never again be reverted as a group. Harness runs are addressed by run id, always.
- **The job is not resumable.** Re-running `director_job` re-plans (a second paid model call) and re-requests assets with no dedupe; the docstring's resume claim is aspirational (`app/jobs/director.py:3-6`). Harness staging must be idempotent per operation (§14–§15).

The editor-side gap is the sharpest product lesson: Director apply invalidates React Query key `["rough-cut-draft", videoId]` (`fe:rough-cut/_hooks/useDirectorRun.ts:70-84`) — **a cache the editor never reads**. The editor loads its draft once in a plain `useEffect` (`fe:rough-cut/_components/rough-cut-draft-state.ts:348-684`), so an applied plan appears only after a full page reload, and the next local edit's autosave overwrites the server's applied draft. The harness's draft-handover protocol (§16.3) exists to fix this class of bug permanently.

---

## 3. Ruthless product principles

### 3.1 Non-negotiable rules

- AI may propose an edit, but only deterministic code may mutate the project.
- The model never emits or applies an arbitrary JSON Patch.
- Every mutation is represented by a versioned, schema-validated operation.
- Every operation declares its target, time basis, dependencies, preconditions, effect, and rollback data.
- A run is bound to an immutable base draft revision. Stale plans must never silently overwrite newer work.
- Background jobs must be capability-gated before the plan is presented.
- Preview and export must interpret the same feature contract. A preview-only effect is not a supported harness feature.
- Audio ownership must be explicit when clips are duplicated.
- Destructive edits require approval unless the user has enabled a narrowly scoped, reversible auto-apply policy.
- Partial success must be visible. The UI must never claim the entire intent succeeded when a dependent operation failed.
- Repeating the same request or retrying a network call must not duplicate edits.
- A precise revert must be available after application, even after a page refresh, addressed by run id — never only by "latest".
- Safety-relevant effects fail **closed**: a mask placed as a redaction that fails to render blocks the export; it does not ship an unmasked frame. (Today the exporter fails open — `rough_cut_export.py:3637` sets `maskingFailed` and ships anyway. The harness must not inherit that default.)

### 3.2 Product quality rules

- Straight cuts remain the default. Transitions are selected because they support story or continuity, not because the feature exists.
- Beautification defaults are subtle and face-aware. Skin texture and identity are preserved.
- Color adjustments protect skin tones and legal output levels.
- Animation uses restrained motion, consistent easing, and a shared intensity vocabulary.
- Overlay placement respects faces, focal objects, captions, title-safe zones, and aspect-ratio crops.
- The UI is a compact inspector, not a large conversational overlay. It discloses detail progressively — the codebase's own articulation, from the mask panel: "a control that can't act right now is absent, not disabled" (`fe:rough-cut/_components/mask/mask-panel.tsx:1292`).
- Every automated choice is explainable in one short sentence backed by machine-readable evidence. The Director panel already sets this bar: each shot shows the quote it is anchored to and the reason it exists (`fe:rough-cut/_components/director/director-panel.tsx`).

---

## 4. Goals and non-goals

### 4.1 Goals

- Execute multi-step edits from natural language, review findings, quick actions, and reusable recipes.
- Make compound edits behave as a coherent group when moved, trimmed, duplicated, disabled, or reverted.
- Support both project-wide and selection-scoped requests.
- Let users inspect and edit the proposed operation list before applying it.
- Support asynchronous analysis and rendering without corrupting the live draft.
- Preserve manual work and detect concurrent edits.
- Provide reliable preview/export parity.
- Produce observable, testable behavior independent of the selected language model.
- Migrate the Creative Director onto the harness instead of maintaining a separate mutation system.

### 4.2 Non-goals for the first release

- Fully autonomous editing of an entire long-form project without review.
- A general node compositor comparable to After Effects or Fusion.
- Free-form model-generated shaders, expressions, FFmpeg filters, or executable code.
- Frame-perfect collaborative multiplayer editing.
- Training a proprietary foundation model.
- Supporting every current editor control as a harness operation on day one.
- Learning silently from user media or edits without explicit data governance and consent.

---

## 5. Current-state assessment (verified)

### 5.1 Verified inventory

Editube already contains most of the required primitives. The missing product is the safe composition layer that coordinates them.

| Area | Existing capability | Evidence | Harness implication |
| --- | --- | --- | --- |
| Transcription | faster-whisper, Silero VAD, word timestamps on by default; 18-model catalog where every non-Whisper entry runs as a Whisper stand-in | `app/jobs/transcription.py`, `app/services/transcription_models.py:24` (`RUNNABLE_ENGINES={"faster-whisper"}`) | Transcript phrase and speech-range resolution is solid; no diarization (speaker is always `"SPEAKER_1"`, `transcription.py:439`) |
| Cut model | `keepRanges` + play-order source↔rendered time maps | `fe:rough-cut/_lib/rough-cut-utils.ts:207,233`; Python `CutMap` in `app/services/director_context.py` | The TimeMap (§11.1) extends these, it does not replace them |
| Timeline | Tracks, timeline media items, per-clip attribute bag, transitions | `fe:rough-cut/_lib/rough-cut-types.ts:404,439,210`; `_lib/transitions/transition-types.ts:21` | Needs formal operation semantics and stable identity (§5.2 G3) |
| Draft storage | One canonical per-project draft (JSONB blob) | `app/db/models.py:889` (`AiResult`, `result_type="rough_cut_draft"`); `app/services/rough_cut_workspace.py` | Needs revisioning, ownership fix, concurrency (§5.2 G1) |
| Undo/redo | In-memory `EditorSnapshot` stack, depth 40, gesture coalescing | `fe:rough-cut-page.tsx:573,1319`; `beginHistoryEntry/endHistoryEntry:1340` | The coalescing mechanism is exactly what a one-undo harness apply should reuse; the stack itself is session-local and insufficient for revert |
| Background removal | Provider registry: rembg auto-matte, SAM 2.1 point-prompt, SAM2 video propagation, chroma-only ffmpeg path; process-isolated child with orphan watchdog | `app/services/segmentation/__init__.py:22`, `local.py`, `isolated.py`, `child.py:31` | Job-backed operation with a real capability probe already exists (`ai.py:957`) |
| Masks | Shape/text/brush/pen masks, 9 keyframe channels, Pillow matte renderer mirrored FE/BE with golden fixtures | `fe:rough-cut/_lib/mask/*`, `app/services/mask_matte.py:475`, `tests/fixtures/mask-*-golden.json` | Solid primitive; export application is broken per-clip (§5.3) |
| Tracking | `cv2.TrackerCSRT_create()` — **dead in every current environment** | `app/jobs/mask_track.py:261`; installed `opencv-python-headless` 4.14 has no CSRT (contrib-only) | Must be repaired or gated before any tracked recipe (§5.2 G7) |
| Color | Full adjust engine (temp/tint/wheels+offset/curves/HSL/LUT/clarity/vignette/grain), keyframed adjust, LUT resolution with workspace auth | `app/services/color_adjust.py:94`, `color_adjust_keyframes.py:151`, `lut.py` | Ready for typed grading ops; two real export bugs (§5.3) |
| Retouch | YuNet + Haar face detection, 16 sliders, temporal landmark smoothing, OpenCV-only | `app/services/retouch/` (1,062 lines), `settings.py:9` | Bounded beautification exists; no availability probe, 180 s local cap (`video.py:15`) |
| Text/lower thirds/captions | Rich canvas-rendered editor primitives (~100-field `TextOverlayStyle`, ~200-field `CaptionStyle`, 40+ caption templates) | `fe:rough-cut/_lib/text/`, `_lib/captions/` | Editor surface is excellent; export path is client-rasterized PNGs for text and bare-SRT for captions (§5.3) |
| Shapes/elements | Element catalog, 12 animation presets, ~30 easings | `fe:rough-cut/_lib/elements/element-types.ts` | **Zero export path** (§5.3) |
| Keyframes | 30+ channels, 11 closed-form easings, mirrored in Python and contract-tested by actually evaluating the ffmpeg expression | `fe:rough-cut/_lib/keyframes/clip-keyframes.ts:147`, `rough_cut_export.py:1475`, `tests/test_easing_parity.py` | The parity-test pattern to copy for every harness feature |
| Transitions | Data model, WebGL preview (gl-transitions), xfade export from an allow-list with runtime-preserving half-duration handles | `fe:rough-cut/_lib/transitions/`, `rough_cut_export.py:33,173` | Ready for typed operations; audio side dips to silence, no `acrossfade` (§5.3) |
| Audio | Demucs + DeepFilterNet enhancement (isolated venv), ffmpeg fallback, stem mix with `loudnorm` — **inside the effect job only** | `app/services/audio_enhancement.py:64,95` | Enhancement is real; the final export mix has no loudness normalization, ducking, or crossfade (§5.3) |
| AI media | Gemini image + Veo video + OpenRouter, async with progress/cancel, provenance link to Director directives | `app/services/ai_media.py`, `app/jobs/ai_media_generation.py`, `models.py:2570` | Gated generation exists; **no cost/credit enforcement anywhere on this path** |
| Creative Director | Full additive plan→preview→apply→revert loop | §2 | Prior art; migrates onto the harness in Phase 4 |
| Claude client | Forced-tool structured output (`strict:true`), streaming, refusal handling, prompt caching, usage persisted | `app/services/claude_client.py` (451 lines) | The harness planner adapter starts from this, not from scratch |
| Consent gates | One-shot auto-apply consent, spent server-side before enqueue | `fe:rough-cut/_lib/auto-edit-gate.ts`, `app/services/director_trigger.py:99-101` | The pattern for recipe auto-apply policies (Phase 5) |
| Propose→approve UI | Cleanup popover (per-row accept/reject, one-undo commit), AutoEditHud receipt, mask-tracking Keep/Discard, generated-media review queue | `fe:rough-cut/_components/rough-cut-transcript-cleanup.tsx`, `rough-cut-auto-edit-hud.tsx`, `mask-panel.tsx:1292`, `rough-cut-generate-media.tsx:130` | The harness plan panel composes these proven idioms rather than inventing a new grammar |

### 5.2 Blocking gaps

Each gap below is a verified defect or absence, with evidence. G1–G4 are the substrate; G5–G10 are correctness; all block advertising the corresponding capability.

**G1 — The draft has no revision contract, no dedicated table, and five uncoordinated writers.**
The entire timeline lives in one JSONB column of the generic `ai_results` table (`app/db/models.py:889-910`): no revision, no checksum, no unique constraint on `(video_id, result_type)` — duplicate rows resolve nondeterministically because every reader uses `.first()` with no `order_by`. Five writers race with no coordination: the 900 ms-debounced editor PUT (whole-document replace via `_upsert_result`, `app/api/routes/ai.py:65-86`), the GET handler itself (the legacy-merge branch **commits a write on GET**, `app/services/rough_cut_workspace.py:193-203`), the transcription worker (`auto_edit.py:775-800`), the effect worker (`rough_cut_effect.py:391-413`), and the Director applier. Writers 3–5 query by `AiResult.video_id` directly, bypassing the workspace resolver, so they can write to a different row than the editor reads. The canonical owner itself is unstable — resolved as the newest non-asset video by `updated_at DESC` (`rough_cut_workspace.py:55-67`), so the workspace can silently move to a different row mid-session. The only clobber guard in the system is the heuristic `"rangeEditVersion" in existing_data` (`auto_edit.py:781`).

**G2 — The allow-list problem is real, but it lives in the frontend, not the API.**
The previous draft of this plan said "persistence uses explicit allow-lists." The truth is stranger and more dangerous. The backend draft model is `ConfigDict(extra="allow")` (`ai.py:591-626`) — unknown fields survive storage fine; in fact the ~20 largest structural keys (`timelineMediaItems`, `timelineTracks`, `transitions`, `elementOverlays`, `gridClips`, `rangeEditVersion`, …) are *undeclared extras* that persist only by that grace. The real allow-lists are: (a) the **client serializer** — `draftPayload` is an explicit object literal (`fe:rough-cut/_components/rough-cut-draft-state.ts:686-800`); (b) the **client loaders** — `parseTimelineMediaItems` and friends rebuild every item key-by-key and are self-documented as the trap ("a field added anywhere else … is silently dropped here on the next load", `:990-995`); and (c) the **export body** (`RoughCutExportBody`, `ai.py:1420`, default `extra="ignore"`). So a new field is stored, arrives on load, **is stripped by the loader, and the next autosave destroys it on disk**. Two aggravations: any PUT that omits keys resets them to defaults (the create-project wizard sends a 7-key body that wipes the timeline fields, `fe:components/create-project/use-create-project-wizard-submit.ts:240-247`), and "tightening" the backend model to a real allow-list would silently break the `rangeEditVersion` clobber guard.

**G3 — No stable clip identity.**
A-roll clip ids are derived from their times: `stableRangeId = ${prefix}-${start.toFixed(3)}-${end.toFixed(3)}-${index}` (`fe:rough-cut/_components/rough-cut-editor-shared.ts:679`), and `clipAttributes` is keyed off them (`clipAttributeKey:690`). Trimming a clip changes its identity, detaching its attributes and transitions unless call sites remember to re-key. No operation contract can target "this clip" durably until ids are minted once and persist through edits.

**G4 — No grouping, linking, or audio-ownership model.**
Exhaustive search confirms no `groupId`/`linkId`/`role` field exists on any draft type. What exists: `TimelineClipTarget.linkedAudio` (a selection-time hint derived ad hoc at ~8 call sites) and one global `trackState.audio.linked` toggle. A foreground duplicate, its mask, its text, and its animations have nothing to hold them together, and nothing prevents a duplicated clip from carrying duplicate audio except the single `audioEnabled` boolean the Director happens to set to `False`.

**G5 — The editor never picks up a server-side apply.**
Described in §2. Apply must hand the new draft to a live editor without a page reload and without the autosave clobbering it. The guards to extend already exist: `hydratedVideoIdRef`, `draftReadyVideoId`, `localEditRef` (`fe:rough-cut/_components/rough-cut-draft-state.ts`).

**G6 — Jobs have no idempotency, no retries, and inconsistent cancellation.**
One queue (`"default"`) across all 31 `Queue()` sites; zero RQ retry policies anywhere; the only deterministic `job_id` pattern in the repo is two analytics sweeps (`app/jobs/queue.py:62,93`) — everything else double-enqueues on a double-click. Cancellation: rough-cut effects have the full path (`job.cancel` + `send_stop_job_command` + cooperative flag + orphan-watchdog child, `ai.py:1155-1194`); mask-track can only cancel *queued* jobs; generated media is flag-only. Dead-job reconciliation exists **only** for effects (`_reconcile_dead_effect`, `ai.py:1066-1152`) — a killed worker leaves transcription, mask-track, export, and generation rows in `processing` forever. Storage is split: effects/exports publish via Cloudinary-or-`UPLOADS_DIR` directly (`rough_cut_effect.py:416`), only AI media uses the R2-capable `app.storage` abstraction.

**G7 — Object tracking is dead code.**
`cv2.TrackerCSRT_create()` (`app/jobs/mask_track.py:261`) is the only tracker reference in the repo, with no guard and no fallback, and the pinned `opencv-python-headless` build does not ship it (contrib module; the pin exists for retouch's `FaceDetectorYN`, `requirements-ml.txt`). The job commits `status="processing"`, then dies on `AttributeError`, surfacing the raw exception text to the user. SAM2 video propagation works but is wired only into background removal, not tracking. Repair options, in order of preference: wire `video_backend.propagate_masks` into mask tracking (semantic propagation, already installed); or adopt `TrackerVit`/`TrackerNano` with ONNX weights; or take `opencv-contrib-python-headless` and re-validate retouch. Until one lands with an integration test, tracking is **absent from capabilities**, and Recipe C is blocked.

**G8 — Time is floating seconds with no frame model, and it already loses data.**
Everything is float seconds formatted `:.6f`. There is no project fps and no quantisation (the timeline nudge hardcodes 30 fps, `fe:rough-cut/_components/rough-cut-timeline-panel.tsx:1463`). The exporter uses `round(x, 3)` dict keys to join client ranges to settings, and because browser `media.duration` and ffprobe disagree by tens of milliseconds, **the last clip of an edit can silently lose its grade, canvas, or cutout** — documented in the exporter itself, with only `audioRanges` given the tolerant matcher (`rough_cut_export.py:1197-1201`).

**G9 — Capabilities are configuration-dependent and mostly unprobed.**
Four real probes exist (segmentation `ai.py:957`, director `director.py:177`, media providers `ai_media.py:120`, model catalog `users.py:248`), plus `GET /health/queue`. Retouch, audio enhancement, mask tracking, and export dependencies have **no probe at all** — they fail at job time. `ML_EFFECTS` advertises four effects (`voice_changer`, `ai_stylize`, `video_enhance`, `mask`) that hard-fail locally (`segmentation/local.py:214`). There is no aggregate capabilities endpoint and no `Settings` object — every flag is a bare `os.environ.get` at its point of use.

**G10 — Long-running ML work cannot be wrapped in a database transaction.**
Veo runs minutes per clip; segmentation is capped at 120 s of clip locally (`SEGMENTATION_LOCAL_MAX_SECONDS`, `segmentation/local.py:35`), retouch at 180 s (`retouch/video.py:15`). The architecture needs staged assets followed by an atomic draft commit (§14) — never incremental application while jobs run.

Two smaller items that belong in Phase 0 because they are cheap and load-bearing:

- **The draft PUT uses a read-level permission check.** `_check_video_access` → `can_access_project` allows `guest` workspace members and `client` project collaborators to PUT the full timeline; `can_write_project_content` exists and is unused on these routes (`app/services/project_access.py:83-98`). Harness routes use the write check from day one, and the draft PUT is upgraded alongside.
- **Naming collision:** "suggestions" is an existing product feature-request board (`suggestions`/`suggestion_votes` tables). Harness proposals are called *operations* and *plans*, never suggestions.

### 5.3 Export parity ledger

The single most important audit finding. The exporter (`app/jobs/rough_cut_export.py`, 3,717 lines — pure FFmpeg subprocess + Pillow, up to **seven successive libx264 re-encode generations** per export, no lossless intermediate) does not read the draft at all: the client re-derives a differently-shaped `RoughCutExportBody` from live React state (`fe:rough-cut/_components/rough-cut-top-bar.tsx:348`, `rough-cut-export.ts`). Feature-by-feature, verified:

| Feature | Export status | Evidence |
| --- | --- | --- |
| Cuts / keep ranges | ✅ works, **but clip order is destroyed** — ranges are sorted by source start, so a reordered timeline renders in source order (frontend warns) | `rough_cut_export.py:501`; `fe:rough-cut-top-bar.tsx:496` |
| Multi-track compositing | ◐ binary above/below-text split only, caps 64 layers / 160 chunks | `:612,1064,3459,3509` |
| Keyframed transform/opacity/crop | ✅ real interpolation as ffmpeg expressions, 10 closed-form easings, parity-tested against ffmpeg itself | `:1431,1475,1521`; `tests/test_easing_parity.py` |
| Keyframed color | ◐ time-sliced at ≤24 fps with a filter-length budget — fast ramps stair-step vs. preview; new adjust keys silently dropped by `_SETTING_KEYS` | `app/services/color_adjust_keyframes.py:151,16` |
| Color adjust + LUT (A-roll) | ✅ full chain, tetrahedral `lut3d`, workspace-authorised | `color_adjust.py:94`, `lut.py:186` |
| **LUT on timeline/B-roll layers** | ❌ **silently absent from the MP4** — LUT resolution touches only `colorRanges` and `videoRanges`; layer settings keep the unresolved reference and emit no `lut3d`. No warning. One-line fix identified. | `rough_cut_export.py:3150-3166` vs `:717-721,2219` |
| Transitions (video) | ✅ xfade from the `_XFADE_TRANSITIONS` allow-list, runtime-preserving handles, ≤5 s | `:33,92,173` |
| Transitions (audio) | ❌ dip-to-silence per-segment `afade`; no `acrossfade` anywhere | `:274` |
| Text overlays | ◐ client-rasterized full-frame PNGs (`kind:"block"` only); **all motion except fade is lost** (drawn at settled transform, reported as `motionLost`); raster is resolution-bound | `fe:rough-cut/_lib/export/overlay-raster.ts:99,133,167`; `rough_cut_export.py:2864,2969` |
| Lower thirds | ◐ client-rasterized; wipe entrance replaced by a fade | `overlay-raster.ts:216` |
| **Captions** | ❌ **the entire `captionStyle` is discarded** — export burns a bare SRT with libass defaults; word highlights and per-word formats have no export representation; caption templates feed nothing. (The *repurpose* pipeline has its own styled ASS engine — two divergent caption renderers in one repo.) | `:3481-3503`; `app/services/clip_captions.py` |
| **Shapes / elements** | ❌ **no export path, and silent** — `elementOverlays` is not in the export body and nothing rasterizes it; no warning is emitted | backend touches the key once, in `rough_cut_workspace.py:34` |
| Grid layouts | ❌ refused with an explicit client-side message | `fe:rough-cut/_components/rough-cut-export.ts:118` |
| Brand overlays | ❌ unconditionally `burn_skipped.append("brand")` | `:3132` |
| **Per-clip speed** | ❌ no field, no filter; the speed effect job's output is orphaned by the exporter's allow-lists | `:561,934,1165` |
| Masks | ◐ matte rendering works, **but one flat mask list is applied to every segment**; masks on B-roll clips are dropped; a matte crash **fails open** and ships unmasked | `:3326,3637`; `fe:rough-cut-export.ts:241-260` |
| Processed effects (remove-bg/retouch/audio) | ✅ composited, re-verified against completed owned effect rows — client URLs never trusted | `:533,828,1114` |
| Blend modes | ◐ 7 mapped; everything else silently falls back to `normal` | `:1407-1418` |
| Crop | ◐ alpha cut with feathering + origin compensation — not a reframe | `:1804,1840` |
| Music (`musicStyle`) | ❌ dead payload; music works only as user-placed audio-lane clips | `amix` only, `:2401` |
| Ducking | ❌ no `sidechaincompress` in the repo | — |
| Final-mix loudness | ❌ `loudnorm` exists only inside the enhancement job; the export mix is never normalized (`amix normalize=0`) | `audio_enhancement.py:64` |
| `disabled` / `freeze` / `reverse` clip flags | ❌ settable (the `D` shortcut toggles `disabled`) and consumed by nothing — a hidden clip still exports | `fe:rough-cut-page.tsx:5884` |
| Verification | ❌ none for pixels: no frame sampling, no perceptual diff, no output checksums; self-reported metadata only (`burnInSkipped`, `motionLost`, `maskingFailed`, warnings) | `:3627-3649` |

The rule this ledger enforces: **no capability enters the harness registry until its row here is ✅ or the capability entry explicitly carries the ◐ limitation.** The ❌ rows are Workstream E's backlog (§24), ordered by blast radius: captions styling, elements, speed, layer-LUTs, mask-per-clip, fail-closed masks, order preservation, audio crossfade/ducking/loudness, brand.

---

## 6. Product surfaces and behavior

### 6.1 Entry points

- **Editor command bar** (new — `cmdk` is installed but `CommandDialog` has zero call sites today): natural-language request using the current selection by default.
- **Selection quick actions:** extend the existing transcript floating toolbar (`fe:rough-cut/_components/rough-cut-transcript-panel.tsx:2051-2330`) and the clip/track context menus with curated actions — Remove background, Subject behind text, Polish shot, Track callout, Match color, Smooth jump cut.
- **Recipe library:** parameterized, named workflows with preview thumbnails and capability requirements.
- **AI Review:** a review finding can offer a structured "Preview fix" action. (Today findings carry only free-text `fix` prose capped at 14 words and the sole CTA is a navigation to `?autoClean=1` — `fe:app/(videos)/player/[id]/_components/ai-review/ai-review-panel.tsx:196`. Phase 2 gives findings a `planTemplate` field that compiles to harness operations.)
- **Creative Director:** project-wide planning produces harness plans instead of proprietary mutations (Phase 4).
- **API/MCP:** authorized clients plan and apply the same versioned contracts. The MCP surface today is three read-only tools (`app/api/routes/mcp.py`); harness tools are added only after the HTTP surface is stable, and with the same write-permission checks.

### 6.2 Standard interaction flow

1. User enters an intent or chooses a recipe.
2. Harness captures the current selection and base draft revision.
3. Resolver identifies exact clips, transcript ranges, subjects, faces, objects, scenes, or markers — returning evidence and confidence, mutating nothing.
4. Capability service removes unavailable actions before planning.
5. Planner produces a typed plan with evidence and confidence (skipped entirely for deterministic recipes).
6. Compiler validates and expands the plan into primitive operations and dependencies.
7. Diff service simulates the result against an isolated copy of the base revision.
8. UI shows affected duration, operation count, warnings, estimated processing time, estimated provider cost, and before/after structure.
9. User approves the full plan or toggles/edits individual independent operations.
10. Harness stages required assets/effects (idempotent per operation).
11. It revalidates the base revision and operation preconditions.
12. It atomically commits the draft mutation and increments the revision.
13. Verification checks draft structure and representative rendered output.
14. UI reports success, partial success, failure, or conflict, hands the new draft to the editor (§16.3), and offers precise revert.

### 6.3 Run states

`draft → planning → needs_input | planned → approved → staging → applying → verifying → ready`

Terminal or exceptional: `partially_applied`, `failed`, `cancelled`, `conflicted`, `reverted`, `superseded`.

State transitions are server-authoritative; every transition records actor, timestamp, reason, and request id. Two lessons from the Director's state machine are codified: never declare a state that nothing writes (`compiling` exists in its comment and route tuples but is never entered — `models.py:2609`), and state advancement may happen on GET (reconcile-on-poll is load-bearing, `fe:rough-cut/_hooks/useDirectorRun.ts:16-29`) but must **also** happen on a server-side sweep so a run whose owner closed the tab still completes.

### 6.4 Approval behavior

Operations are classified:

- **Non-destructive:** markers, analysis, optional overlays, disabled drafts.
- **Reversible:** trim, mute, adjust, retouch, animation, transitions, and generated overlays with a durable inverse.
- **Destructive or costly:** delete source references, overwrite external media, start paid generation, publish, or apply large project-wide changes.

Defaults: planning and simulation require no approval; paid generation requires approval before provider submission; timeline mutations require approval unless a recipe-specific auto-apply policy is enabled (built on the `auto-edit-gate` one-shot-consent semantics — consent spent server-side before enqueue, `director_trigger.py:99-101`); publishing and external writes are outside the harness unless separately authorized.

### 6.5 Editing a plan

- Users can enable or disable independent operations (the cleanup popover's per-row accept/reject `Set` is the proven interaction — `fe:rough-cut/_components/rough-cut-transcript-cleanup.tsx`).
- Disabling a dependency also disables its dependents and explains why.
- Editable parameters use the same constrained schema used by execution.
- Changing a material parameter triggers recompile and re-simulation.
- The model is not called again for deterministic parameter changes.

### 6.6 Cancellation and failure

- Before commit, cancellation leaves the live draft unchanged; unreferenced staged assets are garbage-collected asynchronously, never deleted inline.
- After commit, cancellation becomes a revert request.
- Failed operations block their dependents but are not mislabeled as attempted.
- A partially applied run lists applied, skipped, failed, and rolled-back operations separately.
- Retry uses the same operation key and is idempotent — and replays the operation's recorded parameters verbatim, the way effect retry already replays `processing.settings` (`fe:rough-cut/_components/rough-cut-inspector-panel.tsx:963-977`).
- Every harness job registers with the liveness sweep (§15) so a killed worker produces a failed run with a reason, never an eternal `staging`.

### 6.7 Concurrent editing

- A plan stores `base_revision` and target fingerprints.
- If the draft changes, the harness attempts a dry rebase only for operations whose targets and preconditions remain identical; transcript-anchored operations re-resolve their anchors the way the Director does, and an inexact anchor drops the operation rather than approximating.
- Any ambiguous target, modified time range, deleted clip, changed track, or conflicting overlay produces `conflicted`.
- The user may re-plan against the new revision or inspect the conflict. The system never performs last-write-wins.

---

## 7. Feature catalogue

All features below are harness capabilities, delivered in phases; no capability is advertised until its preview and export paths pass verification (§5.3 rule).

### 7.1 Smart compositing

Subject behind text; subject in front of shapes, generated backgrounds, images, or B-roll; background removal, replacement, blur, dim, tint; foreground-only grade, glow, outline, shadow, or blur; selective face/object/region blur; tracked cutouts and masks; picture-in-picture, split screen, comparison, grid, and reaction layouts; freeze-frame cutout moments; clone/trail effects from time-offset linked layers; chroma/luma key where source quality permits; occlusion-aware labels; automatic safe placement avoiding faces, captions, logos, and crop boundaries.

Required behavior:

- Duplicated visual layers are linked to the base clip's source and trim by default; they reference the same source media, never a copy.
- Only one layer owns audio unless explicitly requested; audio ownership is validated at compile and verification time.
- Masks expose confidence and edge-quality warnings; a redaction-intent mask fails closed (§3.1).
- Background-removal failure never hides or deletes the base layer.
- Every composite has a group id and semantic layer roles.
- Segmentation constraints are honored at plan time: the local provider caps clips at `SEGMENTATION_LOCAL_MAX_SECONDS` (120 s default) and the plan must say so, not fail at stage time.

### 7.2 Intelligent color grading

Auto exposure/contrast/white-balance/saturation and recovery; shot-to-shot matching against a reference shot or scene median; skin-tone-aware corrections; LUT application with intensity (the engine already bakes intensity into the cube via `blend_with_identity`, `app/services/lut.py:105`); scene- or range-aware keyframed adjustments; background/foreground differential grading when masks exist; broadcast/legal range checks; bounded style intents (clean, warm, cool, cinematic, product-neutral, high-energy).

Required behavior: never stack incompatible auto adjustments without normalization; preserve user-authored keyframes unless the plan declares replacement; show numerical changes plus a representative frame comparison (the exact-frame server path already exists — `color_adjust.py:217 apply_adjust_frame` pipes a frame through the identical export chain); default intensity stays conservative. Known preview limits are disclosed, not hidden: the browser previews LUTs as a 1-D diagonal of the cube while export uses true 3-D tetrahedral interpolation, so creative LUTs differ by construction and the exact preview is the server render (`fe:rough-cut/_lib/adjust/lut.ts:81`, `adjust-preview.ts:1-18`).

### 7.3 Beautification and retouching

Face detection with explicit selection when multiple people are visible (`targetFaces: all|primary` already exists — `app/services/retouch/settings.py`); the 16 existing sliders with luma-adaptive auto presets; temporal stabilization (landmark smoothing exists — `beauty.py:432`); per-face and per-range strength; optional foreground/background separation.

Required behavior: preserve pores, identity, age characteristics, and facial geometry by default; never process a face below the confidence/size threshold; cap strength in automatic mode; verification samples frames across the whole range, not the first frame; reduce processing during occlusion, motion blur, or profile angles. The 180 s local cap is a capability limit surfaced at plan time.

### 7.4 Text, shapes, and tracked callouts

Titles, subtitles, captions, lower thirds, labels, quotes, chapter cards, data callouts; rectangles, circles, lines, arrows, underlines, highlights, badges, icons, brand elements; object-, face-, point-, or screen-anchored positioning; leader lines that track without covering their target; brand tokens for typography/color/stroke/radius/spacing/logo; responsive placement across 16:9, 9:16, 1:1.

Required behavior: overlay content is editable before application; generated text passes spelling, length, prohibited-term, and safe-zone checks; tracking data uses normalized coordinates with an explicit source resolution; linked shapes/text animate and move as one group; export uses the same fonts or a declared substitution — and the font reality is stated plainly: server-side text rendering today has exactly **one** font family (mask-text `vera-sans`, `app/services/mask_text.py:57`); everything else renders in the browser. Until server-side text rendering lands (Workstream E), text export rides the client-raster path and its declared limitations (motion loss beyond fade).

### 7.5 Animation and keyframes

Enter/exit/emphasis/loop/camera/path presets; keyframes on every channel the export expression engine supports (`video.*`, `adjust.*` — the only channel prefixes that exist server-side); staggered groups; motion paths and tracked anchors; beat-synced emphasis; shared intensity levels (subtle, standard, expressive).

Required behavior: presets compile to explicit keyframes, never opaque renderer commands (the Director's Ken Burns → `video.scale` keyframes is the model, `director_compile.py:99-132`); motion uses bounded velocity and the 11 shared easings — cubic-bezier is deliberately out (the ffmpeg expression language cannot iterate; documented at `rough_cut_export.py:1475-1490`); enter/exit fit inside the visible range; reduced-motion preview honors `prefers-reduced-motion` without changing the export; existing manual keyframes merge only when mathematically safe, otherwise the plan declares replacement.

### 7.6 Transitions and edit smoothing

The supported xfade catalog plus audio J/L-cuts and crossfades; jump-cut smoothing through reframing, B-roll, punch-in, short dissolve, or motion-matched transition; scene-aware selection.

Required behavior: straight cut is the default recommendation; duration limited by available source handles; adjacent transitions cannot overlap illegally; audio and visual transition decisions are independent — which first requires the exporter to gain `acrossfade` (§5.3); flash/high-frequency transitions carry accessibility warnings and are never chosen automatically for sensitive profiles.

### 7.7 Audio intelligence

Voice enhancement (the demucs+DeepFilterNet chain), noise reduction, de-reverb, EQ, compression, loudness normalization; silence and filler cleanup with protected pauses (the cleanup-proposal engine already exists — `fe:rough-cut/_lib/transcript-cleanup.ts`); music beds, fades, ducking, scene-aware levels; stem-level control where supported; J/L cuts, room-tone fills, click/pop detection; SFX suggestions.

Required behavior: dialogue intelligibility is the primary optimization target; loudness targets are output-profile-specific — YouTube −14 LUFS / social −14 / podcast −16 / EBU R128 −23 — and **measured after the full mix** with a two-pass `loudnorm`, which means Workstream E must first add mix-level loudness to the exporter (today it exists only inside the enhancement job); a duplicated clip never duplicates audio by accident; silence removal protects breaths, comedic timing, and sentence boundaries; ducking compiles to `sidechaincompress` keyed off the dialogue stem; provider-unavailable enhancement degrades to the documented ffmpeg fallback chain or stays out of the plan.

### 7.8 Story, transcript, and pacing

Transcript-based trimming and word/range targeting (the word-selection → `applyRemoveRange` path is the editor's core interaction already); filler/repetition/pause/false-start suggestions; hooks, cold opens, recaps, chaptering, quote extraction, highlight reels; caption generation/correction/style/emphasis; pacing analysis; B-roll and reaction inserts; long-form → short-form variants; multilingual subtitles with human review.

Required behavior: transcript edits resolve to word timestamps and then to source ranges through the TimeMap; removing speech never creates sub-frame fragments or broken phonemes without a warning; meaning-changing edits require explicit review; synthetic hooks and reordered statements are disclosed. Reordering additionally depends on export order preservation (§5.3 — the exporter currently sorts ranges by source time).

### 7.9 Generated and retrieved media

Generate or retrieve background plates, B-roll, stills, textures, graphics, short inserts; match output aspect, palette, camera intent, motion; insert as normal timeline media with provenance (the `sourceKind`/`sourceId` discriminator and `GeneratedMedia` authorization already exist); regenerate without invalidating unrelated operations.

Required behavior: cost and provider shown before generation — and **actually enforced**: the audit found no credit debit, entitlement check, or spend cap anywhere on the AI-media path; the harness adds hard ceilings backed by the existing workspace-scoped `ugc_credits` ledger (balance/reserve/refund/topup). Assets store prompt, model, provider, seed where available, moderation outcome, and license metadata. Generation happens before live-draft commit. Failed generation does not block independent edits unless declared required.

### 7.10 Automated verification

Structural timeline validation; missing-asset/URL/duration/handle/zero-length checks; duplicate-audio, accidental-mute, gap, overlap, black-frame, frozen-frame detection; caption/text safe-zone, crop, font, contrast, spelling checks; mask edge, tracking drift, retouch flicker, transition boundary sampling; loudness, clipping, silence, click/pop, dialogue-audibility checks; preview/export representative-frame comparison. Details in §20.

---

## 8. Initial compound recipes

### 8.1 Recipe A: Subject behind text

**User intents:** "Put me behind this title," "make the text pass behind the speaker," or the selection quick action.

**Parameters:** source range, subject, text, typography style, placement, animation preset, mask quality (`faster`=u2netp / `better`=BiRefNet, mirroring the segmentation quality tiers), and whether the text is fixed or tracked.

**Capability preconditions (checked before the plan is shown):** segmentation provider ready for the required capability (`auto_matte` or `point_prompt`+`propagate`); clip length within the provider's cap; text-overlay export path proven for the chosen style; queue healthy.

**Compiled steps:**

1. Resolve the selected clip and source range through the TimeMap.
2. Analyze candidate foreground subjects; select one above the confidence threshold or ask.
3. Create a composite group with reserved ids.
4. Keep the existing clip as `base` and retain its audio ownership.
5. Add a linked visual-only duplicate above the text layer as `foreground` (`audioEnabled: false`, same source reference — the Director's B-roll item shape is the template, `director_compile.py:262-288`).
6. Stage background removal (or propagated subject mask) for the foreground duplicate as an effect-job-backed staged asset.
7. Add text between base and foreground as `overlay`, placed by saliency/face/caption/safe-zone constraints.
8. Add constrained enter/exit or emphasis keyframes.
9. Link trim/move/disable/delete behavior across the group.
10. Render representative frames at start, middle, end, and detected motion peaks.
11. Verify mask quality, text visibility, layer order, audio ownership, and export parity.

**Failure behavior:** if segmentation or export verification fails, the compound edit does not commit. Offer text over a dimmed background as an explicit fallback; never silently substitute it.

### 8.2 Recipe B: Polish talking head

**Parameters:** intensity, output profile, brand style, pacing preference, caption style, music policy, retouch strength.

**Compiled steps:**

1. Analyze speech, pauses, scenes, faces, framing, exposure, noise, loudness (reusing `analyze_wav_speech` and the review-frame sampler).
2. Propose protected silence/filler removals with a transcript diff (compiling the existing `CleanupProposal` engine into harness operations rather than a parallel system).
3. Apply dialogue enhancement and the output-specific loudness target.
4. Apply conservative exposure/white-balance matching with skin protection.
5. Apply bounded retouching to selected faces.
6. Add captions; emphasize only high-value words.
7. Add restrained punch-ins to conceal selected jump cuts (whole-range only until sub-range attributes exist — `clipAttributeKey` is per-range, so a sub-range punch-in requires `splitRangesAt` plus attribute re-keying, the exact migration `docs/ai_creative_director.md` §16 deferred).
8. Add music and ducking only when policy requests it.
9. Straight cuts unless a transition solves a visible continuity problem.
10. Verify facial stability, speech continuity, captions, loudness, and cut boundaries.

### 8.3 Recipe C: Tracked product callout

Blocked until G7 (tracking runtime) is repaired; listed to force the architecture, not to promise a date.

**Parameters:** object/point, label text, style, screen side, tracking duration, animation intensity.

**Compiled steps:** resolve an explicit user click/box or a high-confidence detection; stage tracking or mask propagation; create a group of label + shape + leader line + optional highlight; compute a collision-free label path with the leader attached; add enter/follow/emphasis/exit keyframes; clamp to safe zones and caption exclusions; verify drift, occlusion handling, contrast, fonts, and export.

### 8.4 Follow-on recipes

Replace or stylize background; chapter opener with title, branded shape, optional generated background; clean jump cuts with reframing, room tone, selective B-roll; product-demo focus with tracked zoom and callouts; social hook variant with cold open, captions, reframing, duration target; multi-person speaker focus; before/after linked split screen; quote card; beat-synced montage with capped cut frequency; multi-format adaptation with overlay reflow and crop verification.

---

## 9. Architecture

```text
Command / Recipe / Review / Director
                 |
                 v
       Intent + Selection Resolver
                 |
          Capability Snapshot
                 |
                 v
         Planner / Model Adapter        (optional — skipped for deterministic recipes)
                 |
          Typed Plan Contract
                 |
                 v
       Policy + Schema Validation
                 |
                 v
      Deterministic Plan Compiler
                 |
          Operation DAG + Diff
                 |
         User Approval / Edit
                 |
                 v
     Asset and Effect Job Orchestrator   (staged, idempotent, cancellable)
                 |
       Preconditions + Revision Check
                 |
                 v
      Atomic Draft Transaction Engine
                 |
                 v
       Structural + Render Verification
                 |
        Result / Retry / Precise Revert
```

### 9.1 Component responsibilities, each mapped to its precedent

**Intent and selection resolver.** Normalizes natural-language, recipe, review, or API input; captures current video, clip, range, track, overlay, transcript selection, and output format; resolves ambiguous phrases, repeated clips, faces, subjects, objects. Returns evidence and confidence; mutates nothing. Precedent: `resolve_anchor` + `build_context` (`director_context.py`) for transcript targets; extend with visual targets.

**Capability service.** Reports supported operation types and limits for the live deployment: package/runtime probes, provider credentials, model availability, queue health, storage, fonts, exporter support. Cached briefly; refreshed before paid or long-running work. Precedent: the segmentation-capabilities endpoint's `(ready, reason, per-capability booleans)` shape (`ai.py:957`), generalized (§17).

**Planner/model adapter.** Accepts a reduced, redacted project context and a capability schema; emits only the versioned typed plan via **forced tool use with `strict: true`** — never free-text JSON. Precedent: `claude_client.generate_structured` / `Conversation` with refusal-before-content handling, prompt caching, and persisted usage (`claude_client.py`). Multiple providers may back it, but the harness never adopts the Gemini client's silent-fallback parsing (`ai_client.py:37-49` returns the caller's fallback on any parse failure — the exact opposite of the fail-loud contract the harness requires). Optional for curated recipes.

**Policy engine.** Enforces project permissions (write-level — `assert_write_project_content`), plan size, operation risk, cost ceilings, provider policies, accessibility constraints, auto-apply rules. Rejects unknown fields and operation types. Requires a user choice for material ambiguity.

**Deterministic compiler.** Expands high-level operations into primitives; resolves stable ids (reserved, derived from `(run_id, operation_key)`), time maps, tracks, layer order, groups, dependencies; validates handles, target existence, numeric ranges, feature compatibility. Deterministic for identical input + capability version + base revision — asserted by fixture, because the frontend replays it (the Director's determinism requirement, `docs/ai_creative_director.md` §9.3).

**Diff simulator.** Applies operations to an isolated snapshot; produces structural changes, affected ranges, overlay previews, cost, warnings, per-operation explanations. No side effects. Shares the same pure mutation functions as the executor.

**Job orchestrator.** Runs segmentation, tracking, retouch, generation, analysis, and preview renders as staged assets owned by the run; deterministic `job_id` per `(run_id, operation_key, attempt)` (the queue's only existing dedupe pattern, generalized — `queue.py:62,99-105`); progress on the operation row; cancellation propagated the way Director cancel fans out to child `GeneratedMedia`; registered with the liveness sweep (§15).

**Transaction engine.** Revalidates revision, preconditions, target fingerprints, staged assets; applies all ready mutations in one database transaction under a row lock; records before/after checksums, changed paths, created ids, removed values, asset references; never holds a transaction open across ML or provider work.

**Verifier.** Fast structural checks post-commit; representative render/audio checks for operations that can diverge from export (§20). Pass / warning / failure per requirement. Auto-revert only when the committed output is structurally invalid *and* the run exclusively owns the revision; otherwise it presents a safe revert action.

---

## 10. Canonical plan and operation contracts

### 10.1 Plan shape

```json
{
  "schemaVersion": 1,
  "runId": "ehr_...",
  "intent": "Put the speaker behind the title",
  "recipe": "subject_behind_text@1",
  "projectId": 0,
  "videoId": 0,
  "baseRevision": 42,
  "baseChecksum": "sha256:...",
  "capabilitySnapshotId": "cap_...",
  "targets": [],
  "operations": [],
  "warnings": [],
  "estimates": {
    "processingSeconds": 55,
    "providerCostUsd": 0,
    "affectedDurationSeconds": 8.4
  },
  "approvalGroups": [],
  "expiresAt": "2026-08-29T12:00:00Z"
}
```

### 10.2 Primitive operation shape

```json
{
  "id": "op_foreground_mask",
  "type": "visual.apply_subject_mask",
  "schemaVersion": 1,
  "dependsOn": ["op_duplicate_foreground"],
  "target": {
    "entityType": "timeline_media_item",
    "entityId": "ehr_run7-op_duplicate_foreground",
    "fingerprint": "sha256:...",
    "timeBasis": "clip_local",
    "range": {"startUs": 0, "endUs": 8400000}
  },
  "anchor": {"segmentId": "s3", "quote": "the two-day tax"},
  "preconditions": [],
  "params": {},
  "evidence": [],
  "confidence": 0.94,
  "risk": "reversible",
  "estimatedCostUsd": 0,
  "rollback": {"strategy": "inverse_manifest"},
  "idempotencyKey": "ehr_run7:op_foreground_mask:v1"
}
```

### 10.3 Required fields

Every operation defines: a stable operation id and idempotency key; namespaced type and schema version; dependencies; exact target with fingerprint and time basis (integer microseconds — §11.1); an optional transcript anchor for speech-derived targets, which re-resolves at apply time and drops on inexactness; preconditions; validated bounded parameters; evidence and confidence for AI-resolved choices; risk category and approval group; estimated time and cost where non-trivial; rollback strategy; a user-facing explanation key rendered by trusted UI templates (never model prose injected into the DOM).

### 10.4 Operation namespaces

- `analysis.*` — detect scenes, subject, face, object, beats, quality, safe placement.
- `timeline.*` — split, trim, ripple remove, insert, duplicate linked, move, group, ungroup, enable, disable, change layer.
- `audio.*` — mute, gain, fade, crossfade, enhance, duck, normalize, add media.
- `visual.*` — adjust, retouch, remove background, mask, crop, reframe, stylize, blur.
- `overlay.*` — create/update/delete text, lower third, caption, shape, icon, grid.
- `motion.*` — apply preset, set keyframes, track anchor, transition.
- `media.*` — generate, retrieve, stage, insert, replace, relink.
- `metadata.*` — marker, chapter, label, provenance, recipe instance, note.
- `verification.*` — structural, render, mask, tracking, typography, audio, accessibility check.

High-level recipe operations are allowed in a submitted plan but are fully expanded before approval. Only primitives reach the transaction engine.

### 10.5 Forbidden model output

- Raw database ids invented by the model (targets come from the resolver's evidence set).
- Raw draft JSON or JSON Patch.
- File paths, shell commands, FFmpeg fragments, shaders, SQL, executable expressions.
- Arbitrary URLs or provider names outside the capability snapshot.
- Unbounded numerical values.
- Hidden side effects such as publishing or deletion.

Schema enums are generated from the same constants as the capability manifest so the prompt and the validator cannot drift — the Director's manifest-from-source-of-truth trick, kept.

---

## 11. Time, identity, groups, and ownership

### 11.1 Canonical time model

Four bases, named on every operation:

- `source_time` — position in the original media. (`timelineMediaItems.start/end` are source seconds today — proven by the export intersection, `rough_cut_export.py:527`.)
- `program_time` — position in the edited output (the Director's "director time").
- `clip_local_time` — position relative to a timeline item after trim.
- `asset_time` — position in a generated or derived asset.

A canonical **TimeMap** service maps between them, built by *unifying* the three implementations that already exist — `CutMap` (Python, `director_context.py`), `sourceToRenderedTime`/`renderedToSourceTime` (TS, `rough-cut-utils.ts:207,233`), and the export remap — into one fixture-tested spec implemented in both languages, exactly as the easing curves already are (`tests/fixtures/easing_curves.json` pattern).

Numeric discipline: operation contracts carry **integer microseconds**; the harness converts at the draft boundary (floats) and the render boundary. Frame quantization happens only at commit/render, using a per-draft fps probed by ffprobe and stored on the draft — closing G8. The exporter's `round(x,3)`-key joins are replaced by tolerant matching everywhere (`audioRanges` already has it; `colorRanges`/`videoRanges`/`processedRanges` do not — `rough_cut_export.py:1197-1201`).

### 11.2 Stable identity (prerequisite for everything)

Range and item ids are minted once (short random ids) at creation and never derived from times. Migration path: `ensureRangeIds` keeps assigning ids on load, but `stableRangeId`'s time-derived form (`editor-shared.ts:679`) becomes a legacy fallback; all mutation paths (`splitRangeAt`, trim, ripple) preserve ids or record the old→new mapping so `clipAttributes` keys and `transitions.leftClipId/rightClipId` re-key deterministically. Target fingerprints then contain the relevant source id, source range, track id, type, and selected mutable attributes — tolerant of irrelevant changes, rejecting changes that alter operation meaning. Track index and array position are never targets.

### 11.3 Linked media and composite groups

Timeline media items gain persisted fields:

- `groupId`
- `linkId`
- `semanticRole`: `base` | `foreground` | `background` | `overlay` | `matte` | `audio` | `support`
- `linkPolicy`: which of source, trim, timing, speed, enable, delete propagate
- `audioEnabled` (exists today)
- `createdByRunId`

Group behavior: moving/trimming a group preserves relative offsets; a linked duplicate references the same source (never copies the media object); compound delete removes group-owned entities but protects reused or detached ones; a user can detach an item, after which recipe updates cannot mutate it silently; audio ownership is validated at compile and verification.

**The allow-list tax is budgeted, not discovered.** Every new field must be threaded through the full round trip in the same change: the TS type, `draftPayload`, the relevant loader (`parseTimelineMediaItems` et al.), `clipAttrsActive` if applicable, the export body + builders, and the exporter's layer-settings whitelist (`rough_cut_export.py:717-721`). The Director's M0 counted eight touchpoints for two fields; a round-trip golden fixture (save → load → save must be byte-stable, and export projection must contain the field) is added in Phase 0 so a missed touchpoint fails CI instead of production.

---

## 12. Persistence model

### 12.1 Draft substrate first: `rough_cut_drafts` + `rough_cut_draft_revisions`

The draft moves out of `ai_results` into a dedicated table, fixing G1's causes rather than patching symptoms:

`rough_cut_drafts`
- `id`, `project_id` (unique — the draft is per-project already in practice; pinning it here removes the unstable newest-video owner resolution), `video_id` (the current canonical source video, updatable explicitly, never inferred from `updated_at` ordering)
- `revision` (integer, monotonic), `checksum` (sha256 of canonical JSON)
- `payload` JSONB
- `user_edited_at`, `last_writer` (`editor` | `auto_edit` | `effect_job` | `director` | `harness:<run_id>`) — replacing the `rangeEditVersion`-presence heuristic with explicit provenance
- `created_at`, `updated_at`

`rough_cut_draft_revisions`
- `id`, `draft_id`, `revision`, `parent_revision`, `checksum`
- `snapshot` JSONB (full compressed snapshots first; deltas only after measured storage pressure)
- `created_by`, `source_type`, `source_id` (run id, job id, or user id)
- `created_at`

Rules:

- **One writer path.** A `draft_store` service is the only code that reads or writes the draft; the five current writers are migrated onto it. Background writers do read-modify-write with `expected_revision` and a bounded retry loop; the editor PUT supplies `expected_revision` and receives `409 Conflict` with the current revision and checksum on mismatch.
- **Reads never write.** The `workspacePersistence` legacy merge moves to an explicit one-time migration; GET is side-effect-free (also unblocking read-replica routing).
- **Partial-body writes are forbidden.** The PUT validates that structural keys are present or explicitly unchanged; the wizard's 7-key seed becomes a dedicated seed endpoint rather than a draft overwrite.
- Compatibility: `GET/PUT /videos/{id}/ai/rough-cut-draft` keeps working during migration, backed by the new store, now returning `revision` and `checksum`; clients that do not echo `expected_revision` get a deprecation window, then enforcement.
- The draft PUT switches to `assert_write_project_content`.

### 12.2 `editing_harness_runs`

`id`, `project_id`, `video_id`, `workspace_id`, `created_by`; `state`, `intent`, `recipe_id`, `recipe_version`; `base_draft_revision`, `applied_draft_revision`, `base_checksum`, `result_checksum`; `capability_snapshot` JSONB; `selection_snapshot` JSONB; `plan` JSONB, `diff` JSONB, `estimates` JSONB; `applied_manifest` JSONB, `inverse_manifest` JSONB, `verification_report` JSONB; `model_provider`, `model_name`, `prompt_version`, `token_usage` JSONB, `cost_usd`; `error_code`, `error_detail`, `request_id`, `cancel_requested`; timestamps per transition (`created/planned/approved/applied/verified/reverted/updated`).

Unlike `director_plans`, the inverse manifest **survives revert** (revert stamps `reverted_at` and appends to an audit list; it never nulls the record), and warnings are per-transition rather than an ever-growing list.

### 12.3 `editing_harness_operations`

`id`, `run_id`, `operation_key`, `type`, `schema_version`; `sequence`, `depends_on` JSONB; `state`, `risk`, `approval_group`; `target` JSONB, `preconditions` JSONB, `params` JSONB; `evidence` JSONB, `confidence`; `result` JSONB, `rollback` JSONB; `job_id`, `idempotency_key`, `attempt_count`; `error_code`, `error_detail`; `started_at`, `completed_at`, timestamps.

Unique constraints: `(run_id, operation_key)`, `idempotency_key`. Progress lives on the operation row (the existing pattern — progress in DB, not RQ meta).

### 12.4 Recipe definitions

Built-in recipes live in version-controlled code as typed definitions. Team/user recipes later get `editing_recipes` + `editing_recipe_versions`. An existing recipe version is never mutated; historical runs stay reproducible.

---

## 13. API contract

Mounted under the existing `/videos` prefix convention, run-addressed (fixing the Director's `_latest`-only defect):

- `GET  /videos/{video_id}/editing/capabilities`
- `GET  /projects/{project_id}/editing/recipes`
- `POST /videos/{video_id}/editing/runs` — create and plan
- `GET  /videos/{video_id}/editing/runs` — list
- `GET  /editing/runs/{run_id}`
- `GET  /editing/runs/{run_id}/diff`
- `PATCH /editing/runs/{run_id}/plan` — toggle operations or edit allowed parameters
- `POST /editing/runs/{run_id}/approve` — includes the plan checksum the user reviewed
- `POST /editing/runs/{run_id}/apply` — includes `expected_revision`
- `POST /editing/runs/{run_id}/cancel`
- `POST /editing/runs/{run_id}/retry` — selected failed operation and eligible dependents
- `POST /editing/runs/{run_id}/revert`
- `GET  /editing/runs/{run_id}/verification`

Requirements:

- All mutation requests accept an `Idempotency-Key`.
- Responses expose operation-level status without leaking provider secrets; errors follow the existing split of user-facing `error` vs machine `errorDetail` (`ai.py:1066-1152`).
- Every route uses `assert_write_project_content` for mutations; a worker re-checks run ownership and project existence before commit.
- Transport is polling, matching the editor (no WebSocket, no SSE anywhere in the frontend today): active runs poll at 2–3 s with backoff, consistent with the existing per-feature intervals (director 3000 ms, review 2500, effects 2500, mask-track 1500). GET may reconcile, but a server-side sweep also advances abandoned runs. SSE is added only when profiling shows polling cost or latency is unacceptable.
- Server-side limits: maximum intent length, plan size, operation count, target duration, generated-media count, and provider spend.

---

## 14. Staged execution and rollback

Long-running effects make a single transaction impossible. Two phases:

### Phase A — Stage

1. Freeze plan checksum, base revision, reserved ids, and operation graph.
2. Run analysis, generation, masks, tracking, retouch, and preview-render jobs as staged assets owned by the run — enqueued with deterministic job ids, "already exists" treated as successful dedupe (the queue's proven pattern, `queue.py:99-105`).
3. Verify staged asset accessibility, dimensions, duration, format, and operation-specific quality.
4. The live draft is untouched.

### Phase B — Commit

1. Lock the draft row briefly (`SELECT … FOR UPDATE`).
2. Check `expected_revision` and target preconditions; re-resolve transcript anchors; drop inexact operations with warnings.
3. Apply pure deterministic mutations to the canonical snapshot.
4. Validate the entire resulting draft.
5. Save the new draft, increment revision, write the revision snapshot, and record the **inverse manifest** — for additive operations, created ids (the Director's filter model); for modifying operations, `{path, before}` pairs; for deleting operations, the full removed values — in one database transaction.
6. Release the lock; run post-commit rendered verification.

### Rollback and revert

- A failed commit leaves the previous revision intact.
- Post-commit verification failure marks the run failed and offers precise revert.
- Revert creates a **new revision** (never deletes history) by replaying the inverse manifest; it is refused or becomes a conflict-resolution flow if later manual edits overlap paths the run owns.
- The inverse manifest is retained after revert (audit trail — the Director's nulling behavior is explicitly not inherited).
- Unreferenced staged assets are garbage-collected after a retention window.

---

## 15. Job and capability infrastructure repairs

Prerequisite hardening the harness sits on, generalizing what exists rather than inventing:

1. **Idempotent enqueue helper.** One `enqueue(job_fn, *, job_id, timeout, meta)` wrapper replacing the 28 copy-pasted enqueue functions' pattern, with deterministic ids and dedupe-on-collision.
2. **Liveness sweep.** Generalize `_reconcile_dead_effect` (`ai.py:1066`) into a periodic reconciler for *all* job-backed rows: RQ status `failed|canceled|stopped` or missing → row failed with reason. Today only effects have this; transcription, mask-track, export, and generation rows can hang in `processing` forever.
3. **Uniform cancellation.** Every harness job gets the full effect-job treatment: queued → `job.cancel()`, started → `send_stop_job_command` + cooperative flag checked at bounded strides, isolated children with the orphan watchdog where torch is involved (`segmentation/child.py:31`), cancelled ≠ failed in status.
4. **Storage unification.** Harness staged assets use the `app.storage` abstraction (R2-capable) exclusively; the Cloudinary-or-`UPLOADS_DIR` direct path remains legacy for existing jobs until migrated. Export's hard Cloudinary requirement (`rough_cut_export.py:3140`) becomes a capability entry, not a job-time crash.
5. **Worker profile honesty.** Capability probes must reflect the *worker* environment, not the API's: the ML venv discipline (Python 3.12 gate, `SimpleWorker` fork-safety, the DeepFilterNet NumPy<2 venv split) is real and per-host. Probes run in-worker and report through a heartbeat row, so the capability service reports what the fleet can actually do.
6. **Queue separation when needed, not before.** One `default` queue is fine for v1 with per-project concurrency caps enforced in the orchestrator; a heavy-ML queue is introduced when measured contention demands it.

---

## 16. Frontend architecture and UX

### 16.1 Module layout — match the repo's actual conventions

There is no `_features/` convention in this codebase; the rough-cut convention is paired domain folders — pure logic in `_lib/<domain>/` with colocated tests, UI in `_components/<domain>/` (fourteen existing pairs: adjust, animation, captions, director, elements, keyframes, mask, …). The harness follows it:

```text
rough-cut/_lib/harness/
  harness-types.ts          // generated from the backend contract
  harness-api.ts
  harness-view.ts           // pure action/label resolvers, tested (directorActions pattern)
  operation-labels.ts
  diff-overlays.ts
  capability-guards.ts
rough-cut/_components/harness/
  harness-command-bar.tsx
  harness-plan-panel.tsx
  harness-operation-row.tsx
  harness-diff-summary.tsx
  harness-timeline-diff.tsx
  harness-recipe-picker.tsx
  harness-run-status.tsx
  harness-verification-report.tsx
  harness-conflict-panel.tsx
rough-cut/_hooks/
  use-harness-run.ts        // React Query + polling, useDirectorRun pattern
  use-harness-plan-editor.ts
```

Harness state does **not** enter `rough-cut-page.tsx`. The page provides only selection, transport, draft-handover, and focus callbacks through a narrow adapter interface — the same containment `DirectorSection` achieved (`director-section.tsx` owns its run, polling, and mutations; the host only says which video). The 7,173-line page and its ~560-prop wall are the reason this rule is absolute.

### 16.2 UI requirements

- Compact right-side inspector or the existing AI panel surface; no oversized modal.
- One-line intent field with recipe/selection context and progressive detail.
- Plan header: affected duration, operation count, processing estimate, cost, risk.
- Operation rows: concise label, target range, confidence where relevant, status, expand-on-demand — the cleanup popover's row grammar (icon, text struck when skipped, per-row accept/reject toggle, summary footer, single commit button) reused directly.
- Independent operations toggleable; dependency effects immediate and visible.
- Timeline shows ghost clips, removals, added overlays, and affected ranges before application. (No content-diff visuals exist anywhere today — this is new UI, built once in the harness module and reusable by Director migration.)
- Player switches between current and simulated/staged preview.
- Warnings attach to the affected operation, not a generic alert.
- Progress is operation-based, with the effect inspector's proven affordances: stall detection ("no progress for 2 minutes — the worker may have stopped", `rough-cut-inspector-panel.tsx:1071-1091`), elapsed clock, Retry-with-recorded-settings, Dismiss for settled failures, cancelled as a calm settled state.
- Success focuses the affected range and exposes Revert; the receipt follows `AutoEditHud` (result summary + Undo, auto-dismissing).
- No long AI prose, huge headings, pill overload, or gratuitous containers; use the editor tokens (`PANEL`, `PANEL_HEAD`, `TOOL_BUTTON` — `editor-shared.ts:264-267`) and semantic colors.
- Full keyboard navigation, focus restoration, accessible names, contrast, reduced-motion.

### 16.3 Draft handover protocol (fixes G5)

The contract between a server-side apply and a live editor:

1. Before apply, the client **flushes** any dirty draft (`flushDraft` exists) and suspends autosave (a `harnessApplyInFlight` guard alongside `localEditRef`).
2. Apply returns the new draft revision + payload (or the client refetches by revision).
3. The editor ingests it through a `restoreSnapshot`-shaped path wrapped in `beginHistoryEntry`/`endHistoryEntry`, so the entire run is **one undo step** — the same mechanism mask-tracking acceptance already uses.
4. Autosave resumes carrying the new `expected_revision`.
5. If the user edited during the run (`localEditRef` set while staging), apply is blocked client-side before the server even sees a conflict, and the run offers re-plan.
6. Cross-tab: the editor subscribes to revision changes via its poll; a revision bump it did not produce triggers the conflict panel, never a silent overwrite. Long-term, the editor's draft load moves onto React Query key `["rough-cut-draft", videoId]` so invalidation actually reaches it.

### 16.4 Frontend state rules

- The server owns run and operation state; React Query owns fetching, polling (the `refetchInterval` predicate pattern), invalidation, retry.
- Local state owns only unsaved plan controls and UI expansion.
- Draft state reloads from the canonical server response after commit; the harness never patches a shadow copy into the editor.
- Frontend diff rendering consumes the server's simulated result; there is no second mutation engine. Golden fixtures validate that the frontend visualizes the backend result correctly (the `director_compile.json` pattern).

---

## 17. Capability registry

Backend module reports, per capability: operation type + schema versions; availability and the reason when unavailable (the segmentation probe's `(ready, reason)` shape); provider/runtime identity; supported media kinds, codecs, duration, resolution, fps, aspect limits; whether preview, export, cancel, retry, revert are supported; expected latency and cost model; quality tier and known limitations.

Concrete checks, all grounded in audited failure modes:

- Redis reachable, worker connected, backlog depth (`GET /health/queue` exists).
- Segmentation provider ready per capability (`auto`/`custom`/`propagate` — exists).
- **Tracking:** absent until a supported runtime lands (G7); never advertised because OpenCV imports.
- Retouch dependencies (cv2 + YuNet model file present) — new probe.
- Audio enhancement provider resolution (`AUDIO_ENHANCE_PROVIDER`, demucs importable, DeepFilterNet venv reachable) — new probe.
- Fonts required by a recipe present on preview and export workers (today: one server-side family; the registry says so).
- Image/video generation providers configured and allowed for the project (exists).
- Export dependencies: ffmpeg present + version, Cloudinary/storage configured.
- **Every overlay/effect operation has an enabled export implementation** — the §5.3 ledger, as code.
- Per-capability duration caps (120 s segmentation, 180 s retouch) surfaced as limits, not stage-time failures.

Capability snapshots are stored with each plan so a later failure distinguishes environmental drift from a bad plan.

---

## 18. Security, privacy, and cost

- Reuse project membership checks — at **write** level (`assert_write_project_content`) for every mutating route and job; a worker re-checks before commit. (The current draft PUT's read-level check is fixed in Phase 0.)
- Media resolved by id within the project, never by client URL — the existing exporter and effect-approval property, preserved (`rough_cut_export.py:727,828`; SSRF guard precedent `mask_track.py:112`).
- Signed media URLs are short-lived and never stored in plan prompts.
- Only the minimum transcript, thumbnails, metadata, and selected ranges go to external providers; provider and retention policy visible at the point of use.
- Prompt injection: transcripts, filenames, captions, and metadata are data, never instruction — the Director's `_TRANSCRIPT_RULE` delimiter discipline, kept everywhere.
- Model output is schema-parsed via forced tool use, length-limited, rejected on unknown types. No silent-fallback parsing.
- Logs and errors redact credentials, signed URLs, and transcript content per retention policy; the user-facing/machine error split is standard.
- Per-user/project rate limits and concurrent-run limits.
- Cost: estimated before approval, **reserved** via the `ugc_credits` ledger for paid generation, reconciled from actual usage; token usage persisted per run (the `ClaudeUsage` pattern) plus a USD conversion table — closing the audit finding that AI-media generation currently has no cost enforcement at all. Hard ceilings that cannot be bypassed by splitting work across operations.
- Generated-asset provenance and moderation decisions retained (`GeneratedMedia.director_plan_id`'s ON DELETE SET NULL discipline — deleting a run never deletes paid-for assets).

---

## 19. Reliability and performance targets

Initial SLTs, to be confirmed by load testing:

- Capability response: p95 < 500 ms cached.
- Deterministic recipe compilation: p95 < 2 s.
- Model-assisted plan for a selected range: p95 < 20 s excluding provider outage.
- Diff simulation: p95 < 2 s for 200 primitive operations.
- Atomic draft commit: p95 < 1 s.
- Status freshness while active: < 3 s via polling.
- Duplicate mutation from client retry: zero.
- Silent draft overwrite on revision conflict: zero.
- Structural validation failure accepted as success: zero.
- Preview/export parity pass rate for advertised operations: 100% on the golden fixture suite.
- Runs recoverable after worker restart (via the liveness sweep): 100% in integration tests.

Operational limits for v1: ≤200 primitive operations per run; ≤30 minutes of affected source media per plan unless project-wide mode is explicitly approved; ≤3 concurrent heavy-effect operations per project (configurable); generated-media count and spend per run bounded by plan tier and project policy. These sit alongside the exporter's own caps (64 layers, 128 transitions, 32 burn-ins, 256 mute spans, 160 chunks), which the compiler must respect rather than discover.

---

## 20. Verification specification

### 20.1 Structural

- Draft schema round trip: save → load → save is byte-stable for every field the harness writes (the anti-G2 fixture).
- Unique entity ids and valid references; valid tracks, order, ranges, durations, rates, transitions.
- No impossible overlaps, negative times, NaN/Infinity, sub-frame entities.
- Operation dependencies satisfied; group/link invariants held; exactly intended audio ownership on linked duplicates.
- Staged assets accessible and checksummed; before/after revision and manifest checksums correct.

### 20.2 Visual

- Render frames before, inside, and after each affected boundary; sample start/middle/end plus motion/scene peaks for masks and retouch (frame selection via the existing `review_frames.pick_timestamps`/`extract_frames`).
- Exact-frame comparisons use the same server chain as export (`apply_adjust_frame` for grades; segment renders for composites) — never the browser approximation.
- Black frames, frozen frames, unexpected transparency, aspect/crop mismatch.
- Text bounding boxes, safe zones, face/object collisions, font substitution, contrast, truncation.
- Preview/export comparison with perceptual thresholds (SSIM per sampled frame to start; tighten per feature as fixtures mature).
- Subject/object drift and mask edge stability over time; **mask verification fails closed for redaction intent**.

### 20.3 Audio

- Integrated loudness (EBU R128 measurement pass) and true peak against the output profile, on the **final mix**.
- Dialogue audibility during music; duplicate or phase-cancelled audio after duplication; click/pop around cuts and fades; unexpected silence, truncation, channel-layout change; crossfade and J/L handle validity.

### 20.4 Result policy

**Pass** — all required checks pass. **Pass with warnings** — structurally valid with disclosed subjective risk. **Fail** — structural invalidity, missing required asset, exporter failure, severe mask/tracking issue, broken audio, or required parity mismatch. An operation-specific verification failure blocks that capability from auto-apply until resolved.

---

## 21. Test strategy

### 21.1 Unit

Schema rejection and bounds per operation version; DAG ordering, cycle rejection, disabled-dependency behavior, deterministic compilation; TimeMap conversion across trims, speed changes, transitions, inserted media — including the cut-boundary word cases that broke the Director's first real run (`docs/ai_creative_director.md` §16b); group/link propagation and detach; fingerprints and optimistic concurrency; idempotent stage/apply/retry/cancel/revert; pure mutation + inverse-manifest correctness (property test: apply ∘ revert ≡ identity on the draft, modulo revision metadata); capability and policy filtering.

### 21.2 Golden contract tests

Per operation and recipe: base draft fixture; input plan; compiled primitives; expected simulated draft and diff; expected saved/reloaded draft (the round-trip fixture); expected export projection; representative preview/export frame and audio metrics where appropriate. These catch allow-list data loss and preview/export divergence — the two failure classes the audit found live in production. The high-value house pattern to copy: `test_easing_parity.py` doesn't just compare formulas, it **evaluates the generated ffmpeg expression in ffmpeg** — every harness feature with an export expression gets the same treatment.

### 21.3 Integration

FastAPI + PostgreSQL + Redis + RQ flow; worker crash and retry between stage and commit (liveness sweep asserted); provider timeout, invalid output, moderation rejection, quota exhaustion; concurrent manual edit during planning, staging, and commit (409s asserted, no silent overwrite); cancellation at every state; cross-session revert; staged-asset GC; permission changes before commit.

### 21.4 Media fixtures

Small licensed fixtures: single/multiple faces; hair, glasses, motion blur, occlusion, hard segmentation edges; static and moving products; landscape/portrait/square, variable frame rate, rotated metadata; stereo/mono/clipped/noisy/reverberant/quiet/music-heavy audio; short handles, jump cuts, transitions, captions, missing fonts.

### 21.5 End-to-end (Playwright)

Plan → inspect diff → edit parameters → approve → apply → refresh → revert; disable a dependency; conflict after manual timeline edit; paid-generation approval and failure; keyboard-only and screen-reader-critical flows; all launch recipes in 16:9 and 9:16; **two-tab conflict** (the currently-silent clobber, asserted fixed).

### 21.6 Evaluation set

A versioned corpus of real editing requests paired with acceptable plans and forbidden outcomes. Score: target-resolution accuracy, valid-plan rate, unsupported-operation rate, human operation acceptance, apply success and revert rate, export verification pass rate, cost per accepted operation, quality preference vs. the unedited baseline and a simple deterministic baseline. A model or prompt upgrade cannot ship if it regresses hard safety constraints, plan validity, or unsupported-operation rate. Offline regression suite, not CI (the Director's model-behaviour discipline).

---

## 22. Observability and product analytics

### 22.1 Operational telemetry

Run/operation state transitions; queue latency, stage/compile/commit/verification durations; provider/model, capability snapshot, retry count, error taxonomy, cost; revision conflicts and rebase outcomes; verification failures by operation type; staged-asset leaks and GC age.

### 22.2 Product events

Use the existing feature-analytics system rather than a parallel one: register `editing_harness` in `fe:lib/analytics/feature-registry.ts` and emit the standard lifecycle (`exposed | opened | started | completed | failed | canceled | result_used`) via `trackFeature`, plus `editing_harness_operation_toggled`, `_plan_approved`, `_run_reverted`, `_result_kept`, `_recipe_saved`. Backend jobs already emit `job_*` events through `EditubeWorker`. No transcript or prompt bodies in analytics; ids, categories, duration buckets, and counts only.

User-facing completion for long runs opts into the existing `notify()` chokepoint with a new `ai_run_finished` type (free-form `Notification.type` — no migration), a `group_key` for coalescing, plus the push-title table entry and frontend presentation META the push contract requires (`app/jobs/push_notifications.py:15-16`).

### 22.3 Success metrics

Percentage of planned runs approved; percentage of applied operations kept after seven days or final export; median manual changes after application; revert rate by recipe and operation; end-to-end verified-export rate; median time from intent to accepted edit; cost per accepted operation; repeat use by active editors.

Vanity metric rejected: number of AI operations generated. A system that creates more rejected work is worse, not better.

---

## 23. Delivery phases and exit criteria

### Phase 0 — Make the editor safe to automate

Deliver:

- `rough_cut_drafts` + revisions tables, `draft_store` single writer path, `expected_revision` writes, stable project-pinned ownership, side-effect-free GET, write-level auth on the draft PUT (§12.1).
- Stable clip/range identity minted at creation with deterministic re-keying (§11.2).
- Canonical TimeMap (unified from the three existing maps) with cross-language fixtures; per-draft fps probe.
- Group/link/audio-ownership fields threaded through the complete round trip, with the byte-stable save-load-save golden fixture in CI (§11.3).
- Capability registry v1 with the new probes (retouch, audio, tracking-absent, export deps) (§17).
- Job infrastructure repairs: idempotent enqueue, liveness sweep, uniform cancellation (§15).
- Export parity ledger as code + the correctness fixes that are bugs rather than features: layer-LUT resolution, tolerant range matching everywhere, clip-order preservation, `disabled` honored, mask fail-closed option (§5.3).
- Tracking runtime either repaired (SAM2 propagation wired in) or formally gated off (§5.2 G7).

Exit criteria: manual editor behavior unchanged; every new field survives save→load→save byte-stable; apply/revert property tests pass; two tabs cannot silently clobber each other; each advertised primitive has a passing preview/export golden fixture; no job-backed row can hang in `processing` after a worker kill.

### Phase 1 — Transaction engine and Subject behind text

Deliver: run/operation tables and APIs; typed plan schemas, compiler, simulator, executor, staged job flow, verifier; the harness UI module with plan panel, diff overlay, and draft-handover protocol (§16.3); deterministic command/recipe flow **without an LLM**; the Subject-behind-text recipe end to end; selection quick action and command bar.

Exit criteria: recipe succeeds on the fixture corpus in 16:9 and 9:16; duplicate-audio rate zero; failed segmentation never mutates the live draft; refresh and cross-session revert work; export matches the approved preview within tolerances; re-clicking apply is a structural no-op.

### Phase 2 — Talking-head polish and AI Review fixes

Deliver: color, retouch, audio, caption, trim, reframe, and transition operation families (each gated by its §5.3 row turning ✅); the polish recipe; the AI-Review finding→plan adapter (structured `planTemplate` on findings); model-assisted intent resolution with typed output and evaluation gates; cost, risk, and accessibility policies with credit reservation.

Exit criteria: meaning-changing transcript edits always reviewed; face and audio verification pass on the corpus; model upgrades gated by the evaluation suite; users approve/reject individual independent fixes; final-mix loudness verified against the output profile.

### Phase 3 — Tracking, callouts, and advanced composites

Deliver: supported tracking provider with normalized data, occlusion behavior, QA; the tracked-callout recipe; shape/text groups with responsive placement, leader lines, multi-format reflow (requires the elements export path from Workstream E); background replacement, foreground effects, selective blur.

Exit criteria: drift within fixture thresholds; overlays inside safe zones across formats; missing tracking capability correctly gated with no dead-end plan.

### Phase 4 — Recipe ecosystem and Director migration

Deliver: built-in recipe library and versioning; team/user recipe parameters; project-wide and multi-format plans; Creative Director and AI-media insertion migrated onto harness operations; Director-specific mutation logic removed after a compatibility migration (existing `director_plans` rows remain readable; new runs create harness runs).

Exit criteria: Director plans use the harness operation, transaction, verification, and revert systems; recipe versions reproduce historical runs; project-wide conflicts and costs clearly reviewable; the applied-manifest-unreachable-after-new-run defect is structurally impossible (run-id addressing).

### Phase 5 — Controlled autonomy and learning

Deliver: recipe-scoped auto-apply policies (auto-edit-gate semantics: one-shot consent, spent server-side, draft-provenance-aware); preference learning from explicit accepts/edits/reverts; plan ranking and deterministic fallbacks; team policy templates.

Exit criteria: auto-apply limited to reversible, verified operations with measured low revert rates; users can inspect, disable, and reset learned preferences; no private media or feedback used outside declared governance.

---

## 24. Implementation work breakdown

### Workstream A — Draft integrity (Phase 0 core)

Dedicated draft tables + store + optimistic concurrency; migrate all five writers; provenance columns replacing the `rangeEditVersion` heuristic (with a compatibility shim so the sentinel keeps working during migration); stable ids + re-keying; TimeMap + fixtures; group/link fields through every touchpoint; the byte-stability CI fixture.

### Workstream B — Operation engine

Pydantic discriminated unions for primitives; operation registry, compiler, DAG validation, simulator, executor, inverse manifests; reserved derived ids; plan checksums and idempotency; conflict/rebase rules; anchor re-resolution.

### Workstream C — Jobs and capabilities

Idempotent enqueue, liveness sweep, uniform cancel; capability probes and the aggregate endpoint; tracking repair or gate; staged-asset ownership and GC; representative preview-render jobs; storage unification for staged assets.

### Workstream D — UI

The harness module pair (`_lib/harness` + `_components/harness`); editor adapter interface and draft-handover protocol; plan review with per-operation toggles, dependencies, progress, stall detection, conflicts, verification, revert receipt; timeline diff overlays; recipe quick actions in the selection toolbar and context menus; accessibility and reduced-motion.

### Workstream E — Export parity (runs parallel to everything; §5.3 is its backlog)

Priority order: (1) server-side caption styling (unify with or generalize the repurpose ASS engine — one caption renderer, not two); (2) elements/shapes export (server rasterization or SVG→PNG pipeline with the same layout constants as preview); (3) per-clip speed; (4) layer-LUT fix + tolerant matching + order preservation (Phase 0, they are bugs); (5) per-clip masks and fail-closed redaction; (6) audio: `acrossfade`, `sidechaincompress` ducking, two-pass final-mix `loudnorm`; (7) brand overlays; (8) reduce generation loss (single-graph or lossless-intermediate rendering to replace the 7× re-encode chain); (9) server-side text/lower-third rendering to retire the motion-lossy client-raster path; (10) worker font provisioning + probes.

### Workstream F — AI and evaluations

Provider-independent planner adapter on the Claude client's forced-tool foundation; minimal context packets and prompt-injection defenses; intent/plan evaluation corpus; prompt/model versions gated through offline evaluation and canary metrics; AI Review and Director integration only after the deterministic harness is stable.

---

## 25. Definition of done for every operation type

An operation is not supported until all of the following are true:

- Versioned schema and bounded validation exist.
- Capabilities report it accurately, including limits.
- Compiler and simulator support it deterministically.
- Preconditions, idempotency, and inverse/revert behavior are defined and property-tested.
- UI can explain, preview, configure where applicable, and report status.
- Backend save and frontend reload preserve all fields (byte-stable fixture).
- Preview and exporter both implement it — its §5.3 row is ✅ or the limitation is declared in the capability entry.
- Unit, golden, integration, and at least one end-to-end test pass.
- Verification rules exist with actionable error codes.
- Permissions, privacy, cost, analytics, and observability are covered.
- Failure, cancellation, retry, conflict, and partial-plan behavior are tested.
- Documentation and capability limitations are current.

If any item is missing, the operation remains experimental and must not appear in automatic plans.

---

## 26. Risks and mitigations

| Risk | Consequence | Mitigation |
| --- | --- | --- |
| Model emits plausible but invalid edits | User loses trust or work | Forced-tool schema, capability-constrained planning, deterministic compiler, simulation, relational validation in code |
| Preview differs from export | Approved result is not delivered | §5.3 ledger as the capability gate, golden fixtures that evaluate ffmpeg, representative render verification |
| Draft changes while jobs run | Overwrites manual edits | Base revision + fingerprints + short commit lock + conflict state; autosave suspension during handover |
| ML job partially succeeds | Broken compound edit | Stage all required assets before atomic commit; per-operation status |
| Duplicate clip duplicates sound | Echo/phase problems | Explicit audio ownership + verification invariant |
| Mask or tracking drifts | Amateur output | Confidence gates, temporal sampling, fallback, user-adjustable target; tracking gated until runtime exists |
| Beautification changes identity | Uncanny output | Conservative caps, face selection, temporal QA, easy disable/revert |
| Too many transitions/animations | Tacky, inaccessible edit | Straight-cut default, style policy, frequency/intensity caps |
| Loader allow-lists drop new data | Edits vanish after refresh | Byte-stable round-trip fixture in CI; touchpoint checklist per field |
| Worker dies mid-run | Run hangs forever | Liveness sweep marks it failed with reason; retry is idempotent |
| Provider cost grows unexpectedly | Billing surprise | Pre-approval estimates, credit reservation, hard ceilings, reconciliation |
| Monolithic frontend integration | Slow, fragile development | Isolated module pair + narrow editor adapter; the DirectorSection containment precedent |
| Recipe logic diverges from Director | Duplicate bugs | One operation engine; Director migrates in Phase 4 and its mutation code is deleted |
| Feature list grows before foundations | Permanent demo-quality system | Phase gates and per-operation DoD enforced |
| Export quality degrades compound edits | Soft, artifacted output | Generation-loss reduction in Workstream E; quality tier honesty in capabilities |

---

## 27. Explicitly rejected shortcuts

- Letting an LLM edit the rough-cut payload directly.
- Treating local undo as sufficient rollback.
- Applying draft changes incrementally while required ML jobs are still running.
- Shipping an effect because it looks correct only in the browser preview.
- Hard-coding provider availability in prompts.
- Using array indexes, track positions, or time-derived ids as operation targets.
- Duplicating source media instead of using linked references.
- Keeping duplicated audio enabled by default.
- Adding harness logic to the 7,173-line rough-cut page.
- Addressing runs by "latest" instead of run id.
- Nulling revert manifests (destroying the audit trail).
- Silent-fallback parsing of model output.
- Tightening the draft API model into an allow-list without first migrating the sentinel-key guards that depend on `extra="allow"`.
- Calling proposed edits "suggestions" (existing feature namespace).
- Advertising "one-click full edit" before acceptance, revert, and export metrics prove it safe.
- Counting generated operations as product success.

---

## 28. First engineering milestone

The first milestone does not call an LLM. It proves the harness with a deterministic Subject-behind-text recipe:

1. Revisioned draft storage, single writer path, optimistic concurrency, precise revert.
2. Stable ids + group/link/audio-ownership fields with the complete persistence/export round trip and CI fixture.
3. Capability reporting for segmentation, tracking (absent), fonts, queues, and export features.
4. Typed operation registry, compiler, simulator, staged job flow with idempotent enqueue and liveness sweep, atomic commit, verifier.
5. Compact plan inspector, timeline diff overlay, and the draft-handover protocol as an isolated frontend module.
6. Subject behind text from selection to verified export.
7. Tests for cancellation, retry, refresh, worker restart, two-tab conflict, failure, and revert.

Only after this milestone passes is natural-language planning connected. That sequence proves the product's editing intelligence is dependable rather than merely fluent.

---

## 29. Revision record (2026-08-29 codebase audit)

The previous draft was written from a partial read. Every load-bearing claim has now been checked against both repos by six parallel deep audits. What changed:

### Corrections — the draft was wrong

| # | Claim in the previous draft | Reality |
| --- | --- | --- |
| 1 | "Persistence uses explicit allow-lists" (backend) | The backend draft model is `extra="allow"` passthrough; the destructive allow-lists are the **client loaders** and the **export body**. Fields are destroyed on load-then-autosave, not on save. Partial-body PUTs additionally wipe omitted keys. Rewritten as G2. |
| 2 | "Transitions … export support — ready" (implied complete parity elsewhere) | Video transitions export; **audio dips to silence** (no `acrossfade`). The broader parity picture was far worse than assumed — captions styling, elements, speed, brand, layer-LUTs, ducking, final-mix loudness all missing or silently wrong. Now itemised as §5.3, the plan's spine. |
| 3 | "Undo is session-local" (stated as the main rollback gap) | True, but incomplete: the Director *has* server-side manifest revert — it just requires additivity, nulls its manifest on revert, and is addressed by "latest" only. The harness inherits the seam and fixes the three defects (§2, §12.2). |
| 4 | Draft revisioning framed as "add revision to the workspace response" | The draft lives in the generic `ai_results` table with no unique constraint, five uncoordinated writers, a GET that commits writes, an unstable owner resolution, and a read-level permission check on the PUT. Phase 0 moves it to dedicated tables with one writer path (§12.1). |
| 5 | Frontend module layout proposed `_features/editing-harness/` | No `_features/` convention exists; the repo's convention is paired `_lib/<domain>/` + `_components/<domain>/` folders. Layout rewritten to match (§16.1). |
| 6 | Endpoints proposed under a new `/editing-harness/` root | Rehomed under the existing `/videos/{id}/…` convention, run-addressed by id (§13). |
| 7 | Tracking "not dependable" | It is **dead code**: `TrackerCSRT_create` does not exist in the installed OpenCV build, there is no guard or fallback, and SAM2 propagation is not wired into tracking. Verified by runtime probe (G7). |
| 8 | Estimate 16–24 engineer-weeks | 18–26, re-sized against the export-parity debt and the identity/revision substrate. |

### Gaps the draft missed entirely

- The editor never picks up a server-side apply (`["rough-cut-draft"]` invalidation reaches nothing) and the autosave then clobbers it — now G5 and the §16.3 handover protocol.
- No stable clip identity: ids derived from times, attributes detach on trim — now G3/§11.2, a Phase-0 prerequisite.
- Job layer: no retries, no idempotency, dead-job reconciliation for one feature only, split storage — now G6/§15.
- Export destroys clip order; the last clip can silently lose its grade to float-key drift; masks fail open on redactions — now Phase-0 bug fixes.
- No cost enforcement on the AI-media path; `ugc_credits` identified as the reservation ledger.
- `rangeEditVersion` sentinel fragility, the "suggestions" naming collision, and the notification-contract requirements for run-completion push.

### Confirmed — the draft was right

- The five-responsibility transaction-engine framing, the operation namespaces, the staged two-phase execution, the verification taxonomy, the phase structure, the DoD checklist, and the rejected-shortcuts list all survive scrutiny unchanged in substance.
- The three launch recipes still force every hard reusable problem; Recipe C is now explicitly blocked on the tracking runtime rather than implicitly at risk.
- "Only deterministic code may mutate the project" is not aspiration — it is how the one working AI-editing system in the codebase already behaves, and the audit strengthened the case for keeping it absolute.
