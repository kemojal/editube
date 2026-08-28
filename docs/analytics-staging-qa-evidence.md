# Analytics staging QA evidence

**Release:** _not recorded_  
**Environment:** staging  
**Tester:** _not recorded_  
**Started/finished:** _not recorded_  
**PostHog project:** configured; environment designation still requires human verification  
**Sentry projects:** configured for frontend, API, and worker  
**Stripe mode:** test

This is an evidence record, not a checklist that can be marked complete from repository tests. Use synthetic data only. Keep replay disabled until every replay/privacy row passes.

## Provider resources

| Check | Status | Evidence |
|---|---|---|
| PostHog dashboard sync applied: 8 dashboards / 25 insights | Applied | Apply completed 2026-08-29; API read-back found exactly 25 active managed insights and no active duplicate. Attach provider links before release sign-off. |
| Sentry monitor sync applied: 5 detectors / 1 workflow | Applied | Apply completed 2026-08-29. Attach project/workflow links before release sign-off. |
| Staging and production projects are separate | Not verified | Project settings screenshot/link required |
| Release/source maps resolve a frontend stack | Not run | Sentry issue link required |

## Consent, identity, and route privacy

| Scenario | Expected result | Status | Evidence |
|---|---|---|---|
| Unknown consent | No PostHog request, persistence, session ID, or replay | Not run | Network/storage capture |
| Essential-only rejection | Capture remains off after route change and reload | Not run | Network/storage capture |
| Analytics accepted, replay rejected | Normalized events only; no replay | Not run | Raw event + network capture |
| Replay accepted on allowed marketing route | Inputs/text masked; no media/canvas | Not run | Recording sample link |
| Blocked account/editor/player/review/billing routes | No replay snapshots | Not run | Provider search/sample link |
| Anonymous signup then identify | Allowed anonymous history joins exactly one user | Not run | Distinct-ID/person evidence |
| Logout then second account | Reset occurs before second identify; no mixed history | Not run | Two-person event evidence |
| Dynamic/secret route | Only normalized `route_template`; token absent | Not run | Raw event with sentinel search |
| Consent withdrawn | Capture stops immediately and stays off after reload | Not run | Network/storage capture + consent audit ID |

## Funnel and authority checks

| Journey | Required evidence | Status |
|---|---|---|
| Signup/onboarding | Client intent plus authoritative account/step completion, ordered once | Not run |
| Checkout success | Client start, API session, Stripe webhook completion, local subscription ledger, outbox event | Not run |
| Checkout cancel/failure/abandonment | Explicit cancel is recorded separately; return is not payment; only an open first-party attempt emits modeled abandonment after 24 hours | Not run |
| Cancellation scheduled/reversed/churned | Stripe cancellation details retained; scheduled and effective states separate | Not run |
| Project/source/transcription/activation | Operational row counts reconcile to authoritative events | Not run |
| Representative successful and failed jobs | Queued/started/terminal ordering, stable IDs, safe feature/provider context | Not run |
| Export | Start/terminal/result-use are distinct; no client success substituted for failed server work | Not run |
| Review playback | 25/50/75/100 once; seeks bounded; unique heatmap and replay counts differ correctly | Not run |

## Synthetic privacy red-team

Use recognizable values such as `SECRET_SENTINEL`, `person@example.test`, and `TRANSCRIPT_SENTINEL` in a staging-only account.

| Destination | Search targets | Status | Evidence |
|---|---|---|---|
| PostHog events/persons | sentinel strings, email, names, raw URLs, tokens, signed query values, transcript/comment/prompt text | Not run | Search export or screenshots |
| PostHog replay | inputs, media, canvas, thumbnails, transcript, comments, prompts, billing/security pages | Not run | Reviewed sample links |
| Sentry frontend/API/worker | bodies, authorization/cookie values, signed URLs, user-authored content, provider payloads | Not run | Issue/event links |
| Application logs | all sentinels and secret field names | Not run | Redacted log search output |

Sample at least 100 events across browser, API, worker, review, and Stripe sources. Record sample size, time range, query, and every discrepancy; “looked okay” is not evidence.

## Reconciliation and sign-off

| Source comparison | Tolerance/exclusion | Observed result | Owner sign-off |
|---|---|---|---|
| Users vs `account_created` | Document test/internal users and acceptable delivery lag | Not run | — |
| Projects vs `project_created` | Document deleted/test projects | Not run | — |
| Transcriptions/jobs vs lifecycle events | Document retries and terminal dedupe | Not run | — |
| Stripe vs subscription ledger/outbox/PostHog | Stripe and PostgreSQL control truth | Not run | — |
| Review sessions/events vs milestones | First-party review tables control truth | Not run | — |
| Sentry affected users vs normalized impact events | State sampling differences | Not run | — |

Final sign-off requires Product, Engineering, and Privacy/Security names, dates, unresolved discrepancies, replay decision, and the exact analytics enablement scope.
