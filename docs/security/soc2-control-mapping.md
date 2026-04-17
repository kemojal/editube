# SOC 2 Control Mapping (Draft)

| Control Area | Implemented Capability | Evidence Source |
| --- | --- | --- |
| Logical Access | MFA enrollment/challenge + workspace auth policy + SSO providers | `security_audit_logs`, workspace auth policy endpoints |
| Change Management | Migration/versioned release process | migration files, release pipeline logs |
| Logging & Monitoring | Immutable security audit events + export API | `/security/audit`, `/security/audit/export` |
| Data Protection | Review-link policies (password/email/NDA/geofence/expiry) | review-link config and audit traces |
| Incident Response | Documented runbook and incident timelines | incident docs + audit timeline exports |
| Availability | Auto-revoke maintenance jobs and queue-based async operations | job logs and queue traces |

## Gaps To Close
- Complete provider-backed GeoIP integration for production country resolution.
- Add tamper-evident log signing/retention guarantees.
- Add formal quarterly access review evidence package automation.
