# Vendor and Infrastructure Security

## Critical Dependencies
- Cloudinary (media/image storage paths).
- Redis + RQ (background job queueing).
- Email providers for transactional notifications.
- OIDC identity providers (Google, Okta, Azure AD).

## Vendor Controls
- Maintain vendor inventory with owner and data classification.
- Review security documentation annually.
- Rotate API keys/secrets on schedule and after incidents.

## Infrastructure Controls
- Encrypt data in transit via TLS.
- Restrict production DB and queue access to private network paths.
- Back up core databases daily with retention policies.
- Monitor queue and storage lifecycle for forensic asset expiry cleanup.

## Evidence
- Vendor questionnaire/attestation snapshots.
- Secret rotation logs.
- Backup restore test reports.
