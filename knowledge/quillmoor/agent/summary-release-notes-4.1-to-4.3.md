# Generated summary of the Haldrum release notes 4.1 to 4.3

*This is a generated summary of the release notes for Haldrum 4.1, 4.2 and
4.3, produced automatically by the internal documentation assistant on
5 August 2026. It has not been reviewed by a person. The release notes
themselves are the authoritative source; where this summary and the release
notes differ, the release notes are correct.*

## Timeline

| Version | Released | Theme |
|---|---|---|
| 4.1 | 1 December 2025 | Tickets and account security |
| 4.2 | 16 March 2026 | Planning: roles, heatmap, approval, export API |
| 4.3 | 4 August 2026 | Protecting plans: conflicts, skills, scheduled exports |
| 4.4 | planned November 2026 | not yet announced |

## Features by area

### Tickets

- **Ticket SLA timers** (4.1, Team and Business): first-response and
  resolution targets per priority; timers pause in status *Waiting*; new
  *Ticket SLA* report; API filter `sla_breached=true`.
- **Swimlanes** on boards (4.1): by assignee, priority or category.
- **CSV import** of tickets (4.1): up to 10,000 rows per file.
- **Rules limit** raised from 25 to 50 (4.1).

### Security and administration

- **Enforced MFA** (4.1, all plans), with a grace period of up to 7 days.
- **Role changes** reach open sessions within one minute (4.2).
- **Configurable session timeout** (4.3, Business): 1 to 72 hours, default 12.
- **Audit log API filters** `actor_id`, `action`, `from`, `to` (4.3).
- **Individual MFA exemption for Guests** (4.3).

### Resource planning

- **Resource Manager role** (4.2, Team and Business).
- **Capacity heatmap** (4.2): white 0 %, light green 1–79 %, dark green
  80–100 %, red above 100 %; tentative bookings at 50 %.
- **Absences for others** entered by Resource Managers (4.2).
- **Booking conflict detection** (4.3, Team and Business): blocks Project
  Leads, Resource Managers can override with a reason; API returns `409`
  with `booking_conflict`; not applied retroactively to older bookings.
- **Skill search** (4.3): in the planner and as `GET /v2/resources?skill=`.

### Time tracking

- **Timesheet approval** (4.2, Team and Business): submit, approve, reject,
  reopen; Friday 15:00 reminder; approved entries locked.
- **Timer auto-stop** after 12 hours (4.3).

### Reports and exports

- **Planned vs. actual** counts only approved hours when approval is on
  (4.2).
- **Project export API** `GET /v2/projects/{project_id}/export` in CSV or
  JSON (4.2, all plans).
- **Scheduled exports** (4.3, Team and Business): CSV, XLSX or PDF, daily,
  weekly or monthly; 20 schedules on Team, 100 on Business.
- **Large email attachments** above 20 MB replaced by a 7-day link (4.3).

## API changes in one list

| Version | Change |
|---|---|
| 4.1 | `sla_breached` filter; due-date fields on tickets |
| 4.2 | project export and exports endpoints; timesheet actions; `POST /v2/absences`; webhook `timesheet.approved` |
| 4.2 | **API v1 deprecated**, switch-off 31 December 2026 |
| 4.3 | `409 booking_conflict` on bookings; `override_reason`; `skill` filter; audit log filters; webhook `booking.conflict` |

## Changes that may need action by customers

1. **Integrations still using API v1** must move to v2 before 31 December
   2026.
2. **Automated booking jobs** may receive `409 Conflict` since 4.3 and need
   either capacity checks or a Resource Manager token with
   `override_reason`.
3. **Planners who are Administrators** can be moved to the Resource Manager
   role (recommended since 4.2).
4. **SLA timers, MFA enforcement and timesheet approval** are off by default
   and must be switched on.

## Notable fixes

- 4.1: resolved tickets not reopening on replies from email aliases; *Ticket
  volume* double counting.
- 4.2: business-hours SLA timers ignoring holidays added later (known issue
  from 4.1); CSV import rejecting files with a byte order mark.
- 4.3: drag-and-drop bookings losing a day at month boundaries; JSON project
  export omitting time entries linked only to tickets; webhook retries sent
  6 instead of 5 times.

## Generation notes

- Source documents: *Haldrum 4.1 — Release Notes*, *Haldrum 4.2 — Release
  Notes*, *Haldrum 4.3 — Release Notes*.
- Sections "Improved" were shortened; minor performance items were left out.
- Plan availability was taken from the release notes and cross-checked
  against *Administration: Billing and Plans* automatically.
