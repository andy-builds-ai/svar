# DRAFT — Haldrum 4.4 — Release Notes

> **DRAFT, NOT FOR PUBLICATION.** Skeleton started by the planning team on
> 10 September 2026 after the 4.4 planning meeting. Scope is not final —
> items marked [?] may be dropped. Dates are targets. Do not quote to
> customers.

**Released:** 17 November 2026 [target, to be confirmed]
**Previous version:** 4.3 (4 August 2026)

TODO intro paragraph — theme? "Bigger organisations"? "Fewer surprises"?
Samuel to decide.

## New

### Bulk export of all projects

TODO write properly once engineering estimate is in.

Administrators and the Owner can export all projects of the organisation in
one step, as a background job. The archive contains one folder per project
with the same CSV or JSON content as the single project export. **Files are
not included**; they stay available through the project export per project.

API:

```
POST /v2/exports   {"format": "csv", "project_status": ["active", "completed"]}
GET  /v2/exports/{export_id}
```

(endpoint shape not agreed yet — GET or POST? meeting notes say GET with
query params, platform team suggested POST because it creates a job. check
with Tobias)

Available on: all plans? or Team and Business only? [?] — single project
export is on all plans since 4.2, so probably all plans for consistency.

### "Warn only" mode for booking conflicts [?]

Depends on Samuel's spec (due 15 Sep).

Resource Managers and Administrators can set individual projects to **warn
only**. In these projects, bookings that exceed capacity are saved with a
warning instead of being blocked, also for Project Leads. Conflict detection
itself cannot be switched off for the organisation, as before.

- Through the API, bookings in warn-only projects return `201 Created` with a
  `warnings` array, like tentative bookings. [not decided, Tobias prefers
  this]
- The webhook `booking.conflict` is still sent, with `outcome` set to
  `warned` (new value).
- The audit log records every warned conflict (Business).

TODO: what happens with the rule "only Resource Managers can override"?
Meeting said Administrators cannot override today. Does "warn only" setting
belong to RM only or also admins? Notes say "resource managers / admins" —
contradiction with point 4 of the meeting? Clarify.

### SCIM user provisioning (Business) [?]

Only if estimate allows. Users created, updated and deactivated
automatically from the customer's identity provider. Would close the gap
that just-in-time provisioning does not deactivate leavers.

## Improved

- Documentation: clarified that Administrators cannot override booking
  conflicts; the Resource Manager role is required. (docs change, not
  product — keep here or not?)
- TODO collect smaller items from engineering board

## Fixed

- TODO

## API changes

- New: bulk export endpoint (shape TBD)
- New: `outcome: "warned"` in `booking.conflict` payload [?]
- New: SCIM 2.0 endpoints under `/scim/v2` [?]

## Reminder: API v1 switch-off

API v1 will be switched off on **31 December 2026**, as announced with 4.2.
Before that, v1 will be unavailable for one hour on **10 November** and
**24 November 2026, 10:00–11:00 CET** ("brownouts"), so that remaining
integrations notice. During brownouts v1 returns `503`. Brownouts are not
downtime under the SLA, since v1 is no longer covered.

(announce in 1 Oct mail first — do NOT publish before that)

## Upgrade notes

TODO. Probably: automatic update, no action needed; warn-only mode is off
by default.

## Looking ahead

TODO 4.5 — custom fields on bookings? don't promise.

---

open questions for next meeting (15 Sep):
1. bulk export plan availability
2. warn-only: who may set it
3. SCIM in or out
4. release date confirmation
