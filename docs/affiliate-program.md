# Affiliate program operations

The cash affiliate program is a separate bounded context from refer-a-friend.
It never awards guest passes or product credits. Its accounting source is
Stripe cash collection, not subscription status.

## Commercial defaults

- Terms version: `v1`
- Commission: 30% (`3000` basis points)
- Basis: invoice cash collected after discounts, excluding tax
- Eligibility: 12 months from the referred account's first paid invoice
- Attribution: first eligible click claimed within 60 days
- Availability: 30 days after the paid invoice's month end
- Payout threshold/currency: $50 USD

These values are seeded as a **draft**. Migration alone does not launch the
program.

## Launch gates

1. Legal reviews the exact `PROGRAM_TERMS_TEXT` in
   `app/services/affiliate_program.py`.
2. Legal, finance, product, and engineering each record an MFA-backed approval
   against the draft checksum in `/admin/affiliates`. A previously published,
   fail-closed terms row may receive the same approvals during an upgrade.
   Production requires four distinct administrators;
   `AFFILIATE_ALLOW_MULTIROLE_APPROVER=1` is only for local rehearsals.
3. Set `AFFILIATE_LEGAL_APPROVED=1` in the API runtime after those signatures.
4. Set `AFFILIATE_SUPPORTED_COUNTRIES` only to recipient countries approved for
   this specific Stripe platform and funds flow. An empty allowlist keeps
   applications closed.
5. An admin publishes the draft at `/admin/affiliates/terms/{id}/publish`.
   Publication fails if the stored immutable legal text, its database checksum,
   and the deployed text differ.
6. Configure Stripe Connect and verify webhook delivery before approving
   partners.
7. Finance records the partner's tax-residency evidence, sanctions result, and
   statutory withholding rate. Only keyed evidence references are stored.
8. Finance tests a transfer in Stripe test mode and signs off dual control.
9. Set `AFFILIATE_PAYOUTS_ENABLED=1` only in the runtime authorized to create
   real transfers.

Legal approval and transfer approval are independent on purpose. Opening
applications must not silently authorize money movement.

For the current Singapore platform, leave `AFFILIATE_SUPPORTED_COUNTRIES`
empty until Stripe and counsel approve the exact recipient-country and tax
flow. If only Singapore is approved, use `AFFILIATE_SUPPORTED_COUNTRIES=SG`.
Do not copy `US` from a generic example: country support is platform-specific.

## Required Stripe events

The `/billing/webhook` endpoint must receive:

- `invoice.paid` (and `invoice.payment_succeeded` for API-version compatibility)
- `charge.refunded`
- `charge.dispute.created`
- `charge.dispute.closed`
- the existing subscription, invoice failure, Product, and Price events

`invoice.paid` creates an immutable positive commission entry. Refunds and
disputes create signed negative entries. A won dispute creates a positive
reinstatement. Source keys and Stripe event claims make replays idempotent.

## State machines

Application: `pending -> approved | rejected | withdrawn`.

Partner: `pending_terms -> active -> suspended | closed`. A closed profile is
terminal. Risk is independent: `clear | review | held`. Only an active, clear
partner can be paid.

Payout: `draft -> approved -> processing -> paid`. Failure moves processing to
`failed` and the same batch can be retried with its original Stripe idempotency
key; a late negative balance adjustment moves a stale batch to `canceled`. The
admin who drafts a payout cannot approve it.

## Attribution and privacy

The public click endpoint accepts an approved partner code and returns a random
opaque token. The browser preserves one valid first touch in first-party local
storage through the server-issued expiry, then removes it when claimed.
Email/password registration submits that token inline; Google OAuth claims it
after the authenticated callback. The server verifies expiry, partner status,
one-attribution-per-account, and self-referral rules.

Raw IP addresses and user-agent strings are not stored in affiliate records.
They are converted to keyed SHA-256 hashes for limited velocity and audit use.
Campaign names, landing paths, referrer hosts, timestamps, and risk flags are
stored. Rotate the dedicated `AFFILIATE_HASH_SECRET` under a documented privacy
retention process because rotation prevents future correlation with old hashes.

Anonymous cross-device attribution is not claimed. Once an account claims an
attribution, renewals are server-linked across sessions and devices.

The signup page does not create affiliate attribution state until the visitor
chooses **Allow**, and it honors Global Privacy Control by declining capture.
Declining never blocks account creation. The first-party token is removed when
claimed.

Set `AFFILIATE_PRIVACY_RETENTION_INTERVAL_HOURS=24` to run data minimization.
After `AFFILIATE_CLICK_DETAIL_RETENTION_DAYS` (minimum 60), expired clicks keep
only the partner/time/relationship needed for financial evidence; token,
campaign, path, referrer, network/browser hashes, and risk details are scrubbed.
Acceptance network/browser hashes are cleared after
`AFFILIATE_ACCEPTANCE_HASH_RETENTION_DAYS` (minimum 30). Account deletion runs
the same targeted scrub immediately for that customer's attribution while
preserving required ledger and contract rows. Raw addresses on unclaimed
expired referral invites are removed after
`REFERRAL_INVITE_EMAIL_RETENTION_DAYS` (default 30 days after expiry).

Campaign links are managed records with a stable slug, internal destination,
and `active -> paused -> archived` lifecycle. Arbitrary campaign query strings
are rejected, and archived campaigns cannot be reopened. Partner reporting
includes active, paused, archived, zero-click, and direct-link performance.

Attribution automatically places a partner into review when the referred
account shares a business email domain, Stripe customer, workspace, or the
network hash used for the partner's terms acceptance. These are investigation
signals rather than automatic accusations; finance must record a resolution
before clearing the hold.

Affiliate attribution and refer-a-friend guest passes do not stack. The account
row is locked while either program claims a signup; the guest pass takes
precedence when both are supplied by the signup UI.

## Payout runbook

1. Review partner status, risk reason, terms acceptance, Connect readiness, and
   ledger reversals in `/admin/affiliates`. Tax evidence and sanctions clearance
   must be current.
2. Draft a payout. The service locks eligible, unassigned ledger entries and
   snapshots gross commission, withholding rate, withholding amount, net
   transfer, and the threshold.
3. A second administrator approves it. Late refunds are folded into a draft;
   the batch is canceled if the adjusted amount drops below threshold.
4. Execute. Any negative adjustment that arrived after approval cancels the
   stale batch and requires a new review.
5. Stripe Transfer creation uses payout-id idempotency. Store the returned
   `tr_` id on the payout and reconcile it against Stripe exports.

If transfer creation fails, the payout is marked `failed` with a truncated
failure reason. Investigate Connect capability, platform balance, sanctions/tax
verification, and Stripe logs, then retry the same batch. Do not create a
replacement: execution reuses `affiliate-payout-{id}`, so a timeout after Stripe
accepted a transfer cannot pay twice. The database row stays locked in one
transaction through transfer creation; a process crash rolls the state back to
`approved` while Stripe retains the idempotency result.

## Refund and dispute behavior

Partial refunds reverse commission proportionally using Stripe's cumulative
`amount_refunded`. Subsequent events append only the difference needed to reach
the new cumulative target. A full qualifying-invoice refund also reverses the
fixed refer-a-friend credit reward tied to that invoice.

A referral reward is also held with an idempotent negative entry when its
qualifying charge is disputed. A Stripe dispute win restores it with a positive
entry; a loss makes the reversal permanent. A later full refund recognizes the
existing reversal without debiting twice.

## Referral email operations

Each provider attempt is recorded as queued, sent, failed, or suppressed.
Failures retry twice with bounded exponential backoff; exhausted failures and
suppressed recipients release the guest-pass reservation. Invites sent to an
existing account retain a generic sender-visible state but release capacity,
so the finite pass pool is not stranded.

Configure `REFERRAL_EMAIL_WEBHOOK_SECRET` and have the email provider post
delivered, bounce, complaint, and unsubscribe events to
`/public/referrals/email-events`. Bounce, complaint, and unsubscribe events add
a keyed suppression and stop retries without storing another raw address.
Administrators can review delivery attempts, clear suppressions, revoke or
restore codes, and change pass capacity from the referral operations panel;
every change is appended to `referral_admin_audit_events`.
Use a dedicated, stable `REFERRAL_EMAIL_HASH_SECRET` for suppression hashes.
Do not rotate it with the short-lived click-correlation key unless existing
suppressions are re-hashed from the provider's protected suppression export.

Negative entries already paid carry forward against future commissions. They
are never deleted and old positive entries are never edited.

Refund and dispute targets share a per-invoice projection. A dispute can hold
the remaining commission while a refund is recorded, and a later dispute win
restores only the amount that remains eligible after that refund. This prevents
event-order-dependent double reversals or reinstatements.

## Monitoring

`GET /health/affiliate` returns only status, counts, and checked-row totals. It
returns HTTP 503 for a critical invariant failure and does not expose partner,
customer, invoice, or transfer identifiers. Point an uptime monitor at it.

Set `AFFILIATE_MONITOR_INTERVAL_MINUTES` on one designated API instance to run
the same database checks in-process. `AFFILIATE_ALERT_WEBHOOK_URL` receives a
sanitized summary when the result is degraded or critical;
`AFFILIATE_ALERT_COOLDOWN_MINUTES` suppresses identical repeated alerts. In a
multi-replica deployment, prefer an external scheduler or enable the loop on
only one replica so every pod does not send the same alert.

Alert on:

- webhook handler failures or repeated event-claim releases;
- repeated `commission.no_eligible_subscription_cash` events (long Stripe
  invoices are auto-paginated; an incomplete line fetch is retried, never
  estimated);
- invoices in an unsupported currency;
- partners entering `review`/`held`;
- payout batches in `failed`, `processing`, or unexpected `canceled` states;
- Connect accounts losing `payouts_enabled`;
- high click velocity flags or abnormal click-to-paid conversion;
- a non-zero difference between ledger payout items and Stripe transfers.

The sanitized `affiliate_audit_events` table records admin actions, terms,
clicks, attribution, commission, Connect, risk, and payout transitions.

## Reconciliation and repair

The admin Reconciliation panel has three distinct operations:

1. Database verification checks immutable terms hashes, per-invoice ledger
   projections, referral-credit reward balances, payout gross/withholding/net
   equations, payout item totals and scope, transfer references, partner terms
   acceptance, Connect readiness, stale processing batches, and risk reasons.
2. Stripe verification rehydrates complete invoice lines, reproduces accruals,
   verifies transfers, and scans a bounded 90-day paid-invoice window for an
   attributed invoice that has neither a ledger projection nor an audited
   exclusion decision.
3. Invoice repair first previews one `in_...` invoice. Applying the repair is
   MFA-gated, idempotent, uses the same accounting function as the webhook, and
   writes `reconciliation.invoice_backfill` to the audit log. Never apply a
   repair until the preview's account and Stripe invoice have been independently
   checked.

Partners can export their signed ledger and per-payout CSV statements. Admins
can export the same payout statement for support and finance review. The CSV
writer neutralizes spreadsheet formula prefixes; exports contain no raw IP or
user-agent data.

Paid invoices dated before the account's affiliate attribution are explicitly
excluded and audited. A historical subscription cannot become commissionable
merely because its owner later follows an affiliate link.

## Rollback

Turning off `AFFILIATE_PAYOUTS_ENABLED` immediately stops new transfers without
destroying ledger state. Suspend a partner to stop new tracking while retaining
history. Do not delete financial rows. If application intake must stop, retire
the active terms; public configuration will report applications closed.
