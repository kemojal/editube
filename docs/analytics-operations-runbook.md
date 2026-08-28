# Product analytics operations runbook

**Status:** implementation handoff  
**Source specification:** `docs/product-analytics-requirements-and-implementation-plan.md`  
**Last updated:** 2026-08-29

Acceptance status and live-provider evidence are tracked separately in `docs/product-analytics-acceptance-matrix.md` and `docs/analytics-staging-qa-evidence.md`. The managed resources have been applied, but an apply is not evidence that captured data passed privacy review or that the configured projects are correctly isolated by environment.

This runbook turns the product-analytics specification into deployable provider configuration, named reports, alerting, QA, and incident procedures. PostHog is the behavior exploration surface, Sentry is the engineering reliability surface, Stripe/PostgreSQL are subscription truth, and `review_events` remains the detailed review-watch source. Do not build a parallel in-product analytics dashboard until at least 4–8 weeks of reconciled data proves a recurring gap.

## 1. Deployment order

1. Create separate PostHog and Sentry projects for staging and production. Never point local or staging at the production projects.
2. Complete the privacy/security review and select the approved PostHog data region.
3. Configure environment variables below. Use secret management; never put personal API keys in browser variables.
4. Apply the Alembic migration before deploying code that writes analytics rows:

   ```bash
   cd editube
   .venv/bin/alembic upgrade head
   .venv/bin/alembic current
   ```

5. Deploy the API, then the RQ worker using `python -m app.rq_worker`, then the frontend.
6. Verify `/analytics/config`, a consent decision, one authoritative API event, one worker event, and one deliberately raised staging Sentry error.
7. Run the privacy red-team in section 8 before enabling `NEXT_PUBLIC_POSTHOG_KEY` for production traffic.
8. Keep replay disabled by default. Enable it only after the replay-specific sample review passes.

Rollback is provider-safe: removing the public PostHog key stops new browser capture; removing the server key stops delivery while leaving the outbox retryable. Do not delete queued outbox rows during rollback.

## 2. Required configuration

### Frontend

| Variable | Purpose | Production rule |
|---|---|---|
| `NEXT_PUBLIC_POSTHOG_KEY` | Public project ingestion key | Set only after consent/privacy QA |
| `NEXT_PUBLIC_POSTHOG_HOST` | Event ingestion host | Approved region or first-party proxy |
| `NEXT_PUBLIC_POSTHOG_UI_HOST` | PostHog links/UI host | Approved regional UI host |
| `NEXT_PUBLIC_SENTRY_DSN` | Browser error project | Production browser project only |
| `NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE` | Browser tracing | Start at `0.1`, tune by cost/utility |
| `NEXT_PUBLIC_APP_ENV` | Environment tag | `staging` or `production` |
| `NEXT_PUBLIC_RELEASE` | Release correlation | Immutable Git SHA/build ID |
| `SENTRY_ORG`, `SENTRY_PROJECT`, `SENTRY_AUTH_TOKEN` | Source-map upload | Build-time secret; never public |

### API and workers

| Variable | Purpose | Production rule |
|---|---|---|
| `POSTHOG_PROJECT_API_KEY` | Server ingestion token | Secret management only |
| `POSTHOG_HOST` | Batch ingestion endpoint | Same approved region as frontend |
| `POSTHOG_API_HOST` | Personal API endpoint | Required only for deletion jobs |
| `POSTHOG_PROJECT_ID` | Deletion target project | Required for provider deletion |
| `POSTHOG_PERSONAL_API_KEY` | PostHog deletion API | `person:read` + `person:write` only; secret management |
| `POSTHOG_WORKSPACE_GROUP_TYPE_INDEX` | Workspace aggregation index | Required by managed dashboard sync; verify in the provider project |
| `ANALYTICS_CONSENT_VERSION` | Notice version | Change whenever purposes/controls materially change |
| `ANALYTICS_REGION_POLICY` | Region-specific policy key | Stable short identifier |
| `ANALYTICS_DELIVERY_INTERVAL_SECONDS` | Outbox scheduling window | Default `30` |
| `ANALYTICS_DELIVERY_TIMEOUT_SECONDS` | Provider request timeout | Default `10` |
| `ANALYTICS_RETENTION_INTERVAL_HOURS` | First-party retention sweep | Default `24`; `0` disables |
| `ANALYTICS_QUALITY_INTERVAL_SECONDS` | Reconciliation/quality cadence | Default `300`; minimum `60`; empty disables |
| `ANALYTICS_RAW_EVENT_RETENTION_DAYS` | Delivered/dead-letter outbox retention | Default `456` (about 15 months) |
| `ANALYTICS_FEEDBACK_RETENTION_DAYS` | Restricted feedback retention | Default `365` |
| `ANALYTICS_AUDIT_RETENTION_DAYS` | Anonymized consent/deletion audit retention | Default `760` (about 25 months) |
| `ANALYTICS_FEEDBACK_ENCRYPTION_KEY` | Restricted free-text encryption | Dedicated Fernet key preferred |
| `SENTRY_DSN` | API/worker error project | Server DSN only |
| `SENTRY_TRACES_SAMPLE_RATE` | API/worker tracing | Start at `0.1` |
| `SENTRY_PROFILES_SAMPLE_RATE` | Profiling | Default `0` until approved |
| `SENTRY_FRONTEND_PROJECT`, `SENTRY_API_PROJECT`, `SENTRY_WORKER_PROJECT` | Managed monitor targets | Separate project slugs are recommended |
| `SENTRY_ALERT_OWNER` | Managed alert ownership | Optional `team:<id>` or `user:<id>` |
| `APP_ENV`, `RELEASE` | Environment/release | Match frontend values |

The PostHog personal API key is never required for ingestion. If deletion credentials are absent, the first-party request remains `pending_configuration` and visible rather than being marked complete. Redis must be available for both delivery and provider-deletion jobs.

Provider resources are configuration-as-code. Validate them without credentials on every release:

```bash
.venv/bin/python scripts/sync_posthog_dashboards.py
.venv/bin/python scripts/sync_sentry_monitors.py
```

After the staging project, workspace group index, destinations, and access scopes are reviewed, apply them explicitly:

```bash
.venv/bin/python scripts/sync_posthog_dashboards.py --apply
.venv/bin/python scripts/sync_sentry_monitors.py --apply
```

The sync scripts are idempotent by managed name. They do not create sample events or invent baseline targets. A successful dry run proves only that checked-in definitions are valid; a successful apply and the provider links belong in the release QA record.

## 3. Provider settings

### PostHog

- Disable automatic pageview/pageleave capture; Editube sends normalized `page_viewed` and `page_left` events.
- Keep autocapture enabled for consented clickmaps, with element text and inputs masked by the client configuration.
- Do not capture network request/response bodies, console logs, canvas, video, audio, or exceptions.
- Set person profiles to identified users only.
- Configure session replay retention to 30 days, heatmap coordinate retention to 90 days, and raw product-event retention to 15 months, subject to the approved contract/plan.
- Add `localhost`, automated tests, staff smoke tests, and staging identities to exclusion cohorts for business dashboards. Maintain a separate internal-QA dashboard that includes them.
- Enable Stripe portal cancellation reasons. Restrict free-text cancellation access to the approved support/privacy role; do not copy it into PostHog.
- Use one canonical event name per behavior. Do not re-enable PostHog `$pageview` reports alongside `page_viewed`.

### Sentry

- Keep Sentry Session Replay disabled because PostHog owns replay.
- Enable release/source-map association and suspect commits.
- Scrub cookies, authorization headers, request bodies, query values, signed URLs, form data, user-authored text, and provider payloads.
- Retain error events no longer than 90 days; use shorter retention/sampling for high-volume traces.
- Group by normalized route and fingerprint, never raw URL IDs.

## 4. Canonical dashboards

Every saved insight must include its owner role, definition, denominator, freshness, exclusions, and known limitations in the description. Targets remain unset until 2–4 complete baseline weeks are available.

### A. Acquisition and signup — owner: Growth/Product, weekly

1. **Consented sessions by landing/channel**: unique `analytics_session_id` on `page_viewed`; break down by `route_template`, `utm_source`, `utm_medium`, `utm_campaign`. Explicitly label this “consented traffic,” not all visitors.
2. **Landing CTA rate**: unique sessions with `landing_cta_clicked` or `pricing_plan_selected` / unique sessions with `page_viewed`, filtered to the same landing route and date window.
3. **Visitor-to-account funnel**: `page_viewed` on a marketing route → `landing_cta_clicked` or `pricing_plan_selected` → `signup_viewed` → `signup_submitted` → authoritative `account_created`. Use ordered steps, a 7-day window, and unique persons.
4. **Signup failure**: `signup_failed` and authoritative `login_failed`, broken down by normalized `error_code` and auth method. Link release regressions to Sentry.
5. **Content engagement**: median/p75 `engagement_ms`, `max_scroll_percent`, exits, and section reach by route template.

### B. Onboarding and checkout — owner: Growth/Product + Billing Engineering, weekly/release

1. **Onboarding funnel**: `account_created` → `onboarding_step_completed(step_key=profile)` → workflow → plan → `onboarding_completed`; unique users, 7-day window.
2. **Checkout funnel**: `checkout_clicked` → authoritative `checkout_session_created` → Stripe-authoritative `checkout_completed` → `subscription_activated`; unique users with a matured 48-hour conversion window. `checkout_returned` is diagnostic only.
3. **Checkout loss**: `checkout_abandoned` after the first-party attempt ledger's 24-hour maturity window, plus explicit cancellation and failures at each stage; break down by source, plan, interval, campaign, safe error code, and preceding API/Sentry failure.
4. **Reported checkout reason**: `abandonment_feedback_submitted(prompt_key=checkout_canceled)` by `reason_code`. Label it reported and show response rate; never treat non-responders as the same reason.
5. **Matured paid conversion opportunity**: eligible checkout/trial cohorts whose conversion window has closed and became paid active / all eligible matured opportunities. Exclude still-open cohorts.

### C. Activation and time to value — owner: Product, weekly

1. **7-day new-workspace activation**: workspaces with `first_value_achieved` within seven days of workspace/account creation / eligible new workspaces. First-value logic is workflow-specific and deduplicated by `workspace_activations`.
2. **Activation path**: `project_created` → `upload_completed` or successful integration import → `transcription_completed` where required → `project_setup_completed` → `first_value_achieved`.
3. **Time to value**: median/p75 time from account/workspace creation to first project, source ready, successful feature completion, and first value.
4. **Activation cuts**: onboarding workflow, source type, plan, acquisition campaign, and workspace size. Do not expose a cut with fewer than the approved minimum cohort size.
5. **Project setup loss**: `form_abandoned(form_key=create_project)`, `project_setup_failed`, and repeated-failure feedback. Separate observed step/failure from reported reason.

### D. Feature adoption — owner: Product/feature owners, biweekly

For every `feature_key`, report eligible, exposed/discovered, started, completed, result-used, 28-day repeat workspaces, median duration, and failure rate. The core lifecycle views are:

- exposure-to-start = unique workspaces `feature_started` / unique eligible workspaces `feature_exposed`;
- start-to-complete = unique workspaces `feature_completed` / unique workspaces `feature_started`;
- complete-to-result-use = unique workspaces `feature_result_used` / unique workspaces `feature_completed`;
- failure rate = failed starts / all terminal starts, with retried successes shown separately;
- retained adoption = successful use in at least two distinct weeks within 28 days.

Never rank features on raw clicks alone. Filter denominators for plan, permission, workflow, source readiness, and feature availability. A feature absent from a user’s UI is ineligible, not “unused.”

### E. Subscription and revenue — owner: Finance/Product, weekly/monthly

Use Stripe/webhook-backed events and `subscription_lifecycle_events`, not browser return pages.

1. Trials started, ending, converted, expired.
2. Active paid subscriptions and plan mix.
3. Plan upgrades/downgrades and resubscriptions.
4. Invoice paid, payment failed, past due, recovered.
5. Cancel scheduled → cancel reversed or effective churn.
6. Voluntary/involuntary churn and resubscription within 30/90 days.
7. Normalized cancellation `reason_code` plus restricted-comment review outside PostHog.
8. MRR/ARR/GRR/NRR only after currency and amount history is reconciled; do not infer revenue from plan labels.

### F. Reliability and user impact — owner: Engineering, daily/release

1. API 5xx rate and p95 duration by route template, release, plan, and environment.
2. Job queue wait, p50/p95 duration, retry rate, terminal failure rate by job/feature/provider.
3. Feature failure rate and affected unique users/workspaces.
4. Media playback failures, WebSocket disconnects, export/render failures, and payment failures.
5. Sentry new/regressed issues by affected users/workspaces and release.
6. Analytics outbox pending age, retry volume, rejection rate, and dead-letter count.

The first-party control-plane view is `GET /analytics/quality?window_hours=24` (admin only). Its scheduled monitor checks schema drift, missing required dimensions, environment contamination, event-volume anomalies, backlog/dead letters, and PostgreSQL-to-outbox reconciliation. Treat PostHog/Sentry as downstream analysis surfaces; this endpoint is the ingestion-integrity source.

### G. Review and delivery — owner: Review/Collaboration Product, biweekly

Use summary events for workspace reporting and preserve `review_events` for granular watch analysis. Report links created/opened/unopened, time to first open/comment/decision, watch milestones, skip/rewatch aggregates, approvals, signoffs, review-cycle completion, delivery readiness, and downloads. Do not send raw seek ranges or comments to PostHog.

## 5. Cohorts and segments

Create these shared cohorts before building comparisons:

- newly created workspace: age 0–7 days;
- activated in seven days;
- not activated after seven days;
- free, trialing, active paid, past due, cancel scheduled, churned;
- selected onboarding workflow: auto edit, repurpose, review;
- successful feature adopter: completed plus result used;
- repeated feature adopter: success in two distinct weeks/28 days;
- collaborator workspace: at least one accepted workspace invite;
- internal/test/staging exclusion.

All cohort descriptions must state whether membership is user- or workspace-based.

## 6. Sentry alert rules

Create environment-scoped alerts:

| Alert | Initial condition | Destination |
|---|---|---|
| New production regression | New issue in production release affecting at least 3 users in 15 minutes | Engineering on-call |
| Conversion route 5xx | Any error on signup, onboarding, checkout-session, Stripe webhook, or consent routes above the agreed noise floor | Engineering + Billing where relevant |
| Worker terminal failures | 5 failures for one `job_type`/`feature_key` in 15 minutes or sharp release regression | Engineering on-call |
| Payment processing | Stripe webhook or subscription transition errors | Billing Engineering |
| Privacy filter rejection | Any prohibited-property or provider-deletion repeated failure | Privacy/Security + Engineering |

Tune thresholds only after baseline collection. Never silence a funnel error solely because total event volume is low.

## 7. Outbox operations

Useful read-only checks:

```sql
select delivery_status, count(*)
from analytics_outbox
group by delivery_status;

select min(occurred_at) as oldest_pending, count(*) as pending
from analytics_outbox
where delivery_status in ('pending', 'failed');

select event_name, last_error_code, count(*)
from analytics_outbox
where delivery_status = 'dead_letter'
group by event_name, last_error_code
order by count(*) desc;
```

`failed` rows are retryable until the eighth attempt promotes them to `dead_letter`.
`suppressed` rows were prevented from provider delivery by a privacy deletion request; retention may remove them, and operators must never retry them.

The admin outbox endpoint is restricted to administrators and returns aggregate health, not event payloads. Provider downtime must never fail checkout, project creation, review, worker, or webhook requests. Retry uses stable event IDs as PostHog `$insert_id` values for deduplication.

### Data-rights requests

- `DELETE /analytics/me` disables first-party linkage immediately and returns an auditable request ID.
- `GET /analytics/me/deletions/{request_id}` exposes only the authenticated request owner's coarse provider states; PostHog person UUIDs are never returned.
- The dedicated privacy job calls PostHog `persons/bulk_delete` with `delete_events=true` and `delete_recordings=true`.
- A request remains `provider_processing` until PostHog's deletion-status API verifies every queued event deletion. HTTP acceptance alone is not completion.
- Alert on `pending_configuration` in production, any repeated `failed` status, and requests still `provider_processing` beyond the approved SLA.

## 8. Privacy red-team and staging QA

Run with synthetic values containing recognizable sentinels, for example `SECRET_SENTINEL`, `person@example.test`, and `TRANSCRIPT_SENTINEL`. Do not use real customer data.

1. Before consent, confirm no PostHog request, PostHog persistence, analytics session ID, or replay request exists.
2. Reject analytics and confirm browser capture remains off after navigation/reload.
3. Accept analytics without replay. Verify normalized route templates, allowlisted campaign fields, click/scroll events, identity merge after login, workspace group, and logout reset.
4. Enable replay. Inspect marketing/onboarding samples: inputs and text are masked; media/canvas/editable/private content is absent.
5. Visit every blocked route family: account, admin, editor/project, player, review, contract, rough-cut, repurpose, and UGC. Confirm recording stops and no sensitive DOM snapshot is sent.
6. Trigger client/API/worker errors and inspect Sentry. Search payloads for all sentinels, authorization/cookie names, signed URL parameters, request bodies, emails, project names, and free text.
7. Run checkout using Stripe test mode. Reconcile client start, API checkout-session creation, webhook completion, subscription transition, and database lifecycle row. The return page is not payment truth.
8. Complete and fail representative background jobs. Reconcile job lifecycle with authoritative feature completion/failure and ensure a handled business failure is not counted as feature completion.
9. Submit reason-only and free-text abandonment feedback. Confirm PostHog contains only prompt/reason/has-comment while PostgreSQL contains encrypted text.
10. Request export and deletion. Confirm local capture stops immediately, first-party restricted data is removed/anonymized, and provider deletion reaches completed state.
11. Sample at least 100 events across browser/API/worker/Stripe sources and verify schema, identity, workspace, environment, release, timestamps, and prohibited-property absence.

Record QA evidence by release: tester, timestamp, environment, event IDs, screenshots/links to provider events, redaction result, discrepancies, and sign-off. Production replay remains off if evidence is incomplete.

## 9. Reconciliation and data-quality rules

- Subscription counts reconcile to Stripe and `subscription_lifecycle_events`; PostHog is a downstream view.
- Account/project/workspace state reconciles to PostgreSQL authoritative rows.
- Browser funnel steps may be lower because of consent, blocking, page close, or offline delivery. State the bias.
- Server events can exceed identified browser users; do not “fix” this by dropping server truth.
- Use event ID/$insert_id to investigate duplicates.
- Compare timestamps in UTC and document reporting timezone.
- State expected freshness: browser near-real-time, outbox normally under two minutes, Stripe dependent on webhook delivery, deletion asynchronous.
- Do not change report filters merely to make counts match. Name the controlling source and document legitimate exclusions.

## 10. Incident response

### Suspected sensitive-data capture

1. Disable the affected capture key or replay immediately.
2. Preserve application functionality; do not disable billing/security logging.
3. Identify event names, environments, releases, time window, affected IDs, and provider storage.
4. Delete affected provider data using approved APIs/support and record the request.
5. Patch schema/masking tests, rotate exposed credentials if any, complete privacy/security review, and only then re-enable.

### Provider outage or backlog

1. Confirm product requests remain healthy.
2. Check Redis, worker, outbox status/age, provider status, and dead letters.
3. Restore delivery and let stable insert IDs deduplicate retries.
4. Do not bulk-delete pending rows. If volume threatens the primary database, follow the approved retention/archival procedure.

### Metric regression

1. Validate instrumentation/release changes and provider freshness first.
2. Reconcile against PostgreSQL/Stripe and check consent mix.
3. Segment by release, route, plan, workflow, device, feature, and acquisition source.
4. Correlate Sentry and job failures.
5. Label explanations as reported reason, observed friction, or inferred likely cause.

## 11. Release checklist

- [ ] Migration applied and single Alembic head confirmed.
- [ ] API/frontend/worker release values match.
- [ ] Consent notice/version reviewed.
- [ ] No new event name or property bypasses the registry/sanitizer.
- [ ] Relevant funnel and feature events tested in staging.
- [ ] Stripe/webhook or worker authoritative outcomes tested when changed.
- [ ] Sentry source maps resolve and payload scrub passes.
- [ ] Replay remains off unless the current release passed privacy QA.
- [ ] Dashboard definitions/owners updated for schema changes.
- [ ] Known count discrepancies documented.
