"""The editing harness: typed, revision-safe compound edits.

Layout mirrors the Director's proven seam (docs/editing-harness-implementation-plan.md §9):

- `schemas`      — the versioned plan/operation contracts (Pydantic).
- `capabilities` — live runtime probes; nothing is planned that cannot run here.
- `mutations`    — pure draft mutations + inverse entries (dict in, dict out).
- `compiler`     — recipes → primitive operation lists, deterministic.
- `executor`     — run lifecycle: plan, approve, stage, commit, revert.
- `verifier`     — structural checks on the committed draft.
- `planner`      — optional model adapter (OpenRouter free models for evals).
"""
