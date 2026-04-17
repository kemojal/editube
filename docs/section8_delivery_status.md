# Section 8 Delivery & Handoff Status

Source: `docs/future_plan.md` section 8 (Delivery & Handoff).

## Status Matrix

| Item | Status | Evidence |
|---|---|---|
| Delivery packages (approved + source + captions + thumbnails zip) | Not implemented | No delivery package DB model, API route, or zip job currently exists. |
| Multi-format export (4K master, 1080p YT, 720p social, auto) | Partial | `video_aspect_exports` and `aspect_export_job` exist, but only aspect-ratio outputs. |
| Client-branded delivery page | Partial | Public branded review page exists; no dedicated package-focused delivery route/page. |
| Expiring download links (30-day, renewable) | Partial | `expires_at` and signed URLs exist for review links; no default 30-day policy + renew flow for delivery links. |
| Delivery receipt tracking (who/what/when) | Partial | Review session/event analytics exist; no file-level delivery receipt ledger. |
| Archive + cold storage after 90 days | Not implemented | No retention policy model, archive scheduler, or cold-tier transition job. |

## Execution Note

The implementation sequence follows the approved plan:
1. Foundation schema and migrations
2. Multi-format export pipeline
3. Delivery package assembly
4. Client delivery page + renewable expiring links
5. Delivery receipt analytics
6. Archive/cold storage automation
