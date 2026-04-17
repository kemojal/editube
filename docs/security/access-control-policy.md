# Access Control Policy

## Scope
- Applies to all Editube production systems, staging systems, and data stores.
- Covers employee, contractor, service account, and customer access.

## Core Controls
- Enforce least privilege for database, cloud storage, and application admin actions.
- Require unique user identities for all staff and disallow shared admin accounts.
- Require MFA for privileged accounts and support workspace-level MFA enforcement.
- Support workspace SSO policies and optional local-login disablement.

## Access Lifecycle
- Access requests are approved by system owner and documented in tickets.
- Role changes and membership changes are logged in `security_audit_logs`.
- Access is reviewed quarterly and revoked immediately for offboarding.

## Technical Enforcement
- JWT session IDs are validated against `user_sessions`.
- Review-link public access is controlled by password/email/NDA/geofence/expiry policies.
- Security-significant actions are recorded in immutable audit entries.

## Evidence
- Export CSV from `/security/audit/export`.
- Attach quarterly access-review checklist and offboarding records.
