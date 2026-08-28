# Product analytics acceptance matrix

**Audited:** 2026-08-29  
**Controlling specification:** `docs/product-analytics-requirements-and-implementation-plan.md`  
**Verdict:** the analytics implementation builds and passes its regression suites, but the product analytics program is not yet fully accepted for production.

This distinction is deliberate. Code can implement consent, event contracts, delivery, reconciliation, dashboards-as-code, and monitors, and the managed PostHog/Sentry resources have now been applied. It cannot inspect real captured staging payloads, complete a privacy sign-off, conduct a weekly review, or generate a 2–4 week baseline honestly.

## Status definitions

- **Implemented:** present in code and covered by automated checks.
- **Staging gate:** implementation exists, but live provider or privacy evidence is required.
- **Operational gate:** requires elapsed production data or a named human decision.
- **Partial:** useful coverage exists, but the acceptance statement is broader than the evidence.

## Acceptance criteria

| # | Criterion | Status | Repository evidence | Remaining proof or action |
|---:|---|---|---|---|
| 1 | Normalized page routes; no raw secret tokens | Implemented | Frontend `route-template.ts`, page tracker, exhaustive route/privacy tests | Repeat the sentinel check against raw staging events. |
| 2 | Consent-governed anonymous-to-user acquisition | Implemented | Consent provider/context, PostHog adapter, identity transition helper and tests | Verify the identity merge in the approved staging PostHog project. |
| 3 | Logout/account switching cannot mix identities | Implemented | Reset-before-identify logic and identity isolation tests | Run one two-account staging browser scenario. |
| 4 | Authoritative signup, onboarding, checkout, activation, export, review, and churn completion | Implemented | API/webhook/worker emitters, subscription ledger, activation service, review summary service, export/job lifecycle events, and a first-party checkout-attempt ledger with 24-hour abandonment maturity and explicit-cancel exclusion | Reconcile a complete Stripe test-mode journey and representative jobs in staging. |
| 5 | Major features have eligibility and open/start/complete/result-use definitions | Implemented | Shared frontend/backend feature registry; visibility/open tracking; explicit editor, repurpose, UGC, export, integration, review, and worker lifecycle instrumentation; bounded editor-session summaries | Sample each currently reachable feature lifecycle in staging. Registry entries for future/unreachable features remain ineligible and must not be reported as zero adoption. |
| 6 | Cancellation reasons and churn classifications | Implemented | Subscription analytics service/model/migration consumes Stripe cancellation feedback/comment and separates scheduled, reversed, effective, voluntary, and involuntary states | Enable cancellation reasons in the Stripe portal and verify a test webhook. |
| 7 | Provider downtime cannot fail product or billing requests | Implemented | Transactional analytics outbox; async RQ delivery; stable insert IDs; retry/backoff/stale-claim/dead-letter tests | Exercise a staging provider outage and record the recovery evidence. |
| 8 | Frontend, FastAPI, and RQ errors reach Sentry with safe context | Staging gate | Next.js/FastAPI/RQ initialization, error boundaries, request/job tags, media/WebGL/WebSocket/autosave capture, Sentry privacy filter/tests; five managed monitors and one workflow applied | Trigger one safe error in each deployed runtime and verify release/source-map resolution. |
| 9 | Sensitive content is absent from analytics/replay/logs/Sentry | Staging gate | Prohibited-key/value validation, Sentry scrubber, replay denylist, block/mask components, automated privacy tests | Inspect captured staging events and replay with synthetic sentinels; replay stays off until this passes. |
| 10 | Consent accept/reject/change/withdraw/audit is immediate | Implemented | First-party consent and append-only audit models/API, local enforcement, preferences UI, provider opt-in/out handling | Confirm network behavior across reloads in staging. |
| 11 | Review heatmaps preserve granular first-party data without PostHog progress spam | Implemented | Bounded watch ranges, unique-session heatmap, separate replay counts, one-time milestones, sequence dedupe, migration and tests | Compare one known playback trace with the review UI in staging. |
| 12 | Event counts reconcile against PostgreSQL and Stripe | Staging gate | Scheduled first-party quality monitor reconciles users, projects, activation, transcription, subscriptions, and review ledgers; admin quality endpoint exposes discrepancies | Run and record a live Stripe/PostgreSQL/PostHog comparison with real staging data. |
| 13 | Dashboards state definition, denominator, freshness, owner, and limitations | Staging gate | Eight PostHog dashboards with 25 active managed insights and five Sentry monitors/one workflow applied through idempotent sync scripts; active PostHog resources read back without duplicates | Attach provider links/screenshots and validate populated definitions against staging events. |
| 14 | A weekly product review and engineering review make an explicit decision | Operational gate | Review cadence and owners are defined in the operations runbook | Hold and record both reviews using live data. |
| 15 | Baseline precedes business targets | Operational gate | Dashboards intentionally contain no invented targets | Collect 2–4 complete reconciled weeks, then approve cohort-specific targets. |

## Verification snapshot

| Check | Result on 2026-08-29 |
|---|---|
| Frontend focused analytics/editor-session tests | 35 passed |
| Frontend full Vitest suite | 1,596 passed across 133 files |
| Frontend production build | Passed with `npm run build:docker`; existing font-override and ambiguous Tailwind-class warnings remain |
| Frontend lint | Passed; existing hook-dependency and unoptimized-image warnings remain |
| Frontend full TypeScript check | Not green: 34 pre-existing errors in legacy project/player/comment/annotation code; no errors matched the analytics, observability, or newly instrumented editor files |
| Backend focused checkout/provider analytics tests | 96 passed; provider-manifest subset 4 passed |
| Backend broad regression suite | 1,362 passed, 17 skipped, 618 subtests passed; only `tests/test_video_backend.py` was excluded because the optional Torch dependency is absent |
| Python compile check | Passed for `app` and `scripts` |
| Alembic | One head; database current at `ag2908290003` |
| PostHog provider apply/read-back | Applied: 8 dashboards, exactly 25 active managed insights, no active managed duplicate |
| Sentry provider apply | Applied: 5 monitors, 1 workflow |
| Environment configuration | Required frontend/backend PostHog, Sentry, delivery, quality, consent, retention, and encryption variables are populated; only documented optional overrides remain unset |
| Diff hygiene | `git diff --check` passed in backend and frontend repositories |

The TypeScript failures are not caused by the analytics implementation, but they are still repository debt and should not be hidden under a “bulletproof” label. The production build currently skips type validation by configuration.

Repository checks do not turn staging or operational gates green. Evidence for those gates belongs in `docs/analytics-staging-qa-evidence.md` and must contain provider links or immutable screenshots, safe event/request IDs, discrepancies, owners, and sign-off.

## Release decision

The safe release sequence is:

1. deploy with PostHog browser capture and replay disabled;
2. apply migrations and deploy API/worker/frontend observability;
3. verify that the configured provider projects are staging-only and remain separate from production (managed resources are already applied);
4. complete the sentinel privacy and reconciliation QA;
5. enable consented analytics capture;
6. keep replay disabled until its separate sample audit passes;
7. collect baseline data before setting targets.

Any claim that all fifteen criteria are complete before steps 3–7 is false.
