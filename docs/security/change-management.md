# Change Management

## Requirements
- All production changes require peer review and traceability to issue/ticket.
- Database migrations must include downgrade path.
- Security-affecting changes must include test coverage and rollout guardrails.

## Deployment Controls
- Use feature flags for MFA enforcement, geofencing, and recording alerts.
- Roll out in staged environments before production.
- Validate migration health and endpoint smoke tests after deploy.

## Verification Checklist
- Auth flows: login, refresh, logout, MFA challenge.
- Review-link flows: gate, NDA acceptance, geofence handling, expiry behavior.
- Audit log ingestion and export endpoint.

## Evidence
- Keep deployment logs, migration logs, and test reports for each release.
