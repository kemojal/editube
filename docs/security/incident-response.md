# Incident Response Runbook

## Severity Levels
- `SEV-1`: Data exposure, auth bypass, production outage affecting all users.
- `SEV-2`: Partial outage, high-risk vulnerability with compensating controls.
- `SEV-3`: Limited impact bug or low-risk policy violation.

## Response Workflow
1. Detect and triage alert.
2. Assign incident commander and communications owner.
3. Contain impact (revoke sessions, disable links, block suspicious traffic).
4. Eradicate root cause and validate remediation.
5. Recover service and monitor for recurrence.
6. Publish postmortem with corrective actions.

## Security-Specific Procedures
- Use audit exports to reconstruct timeline by actor/action/resource.
- For leaked review links, revoke link and rotate access policies.
- For suspected recording misuse, inspect `recording_signal` events and forensic asset mapping.
- For auth compromise, revoke user sessions and enforce MFA reset.

## SLA Targets
- Initial triage: within 15 minutes for `SEV-1`.
- Containment start: within 30 minutes for `SEV-1`.
- Postmortem: within 5 business days.
