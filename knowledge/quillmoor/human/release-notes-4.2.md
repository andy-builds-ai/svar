# Haldrum 4.2 — Release Notes

**Released:** 16 March 2026
**Previous version:** 4.1 (1 December 2025)

Haldrum 4.2 is the planning release. It introduces the Resource Manager
role, a capacity heatmap and timesheet approval, adds a project export
endpoint to the API, and starts the end of API v1.

## New

### Resource Manager role (Team and Business)

A new role for the people who plan. Resource Managers see every project and
every booking, create and change bookings in any project, maintain capacity,
absences and skills, and approve timesheets — without receiving the user,
security and billing rights of an Administrator.

Until now, planners across several projects had to be Administrators. We
recommend reviewing your Administrators and moving planners to the new role.

Documentation: User Manual, Chapter 4, section 4.3; *Administration: Users
and Roles*.

### Capacity heatmap

The resource planner shows each person's booked share of capacity per day,
week or month: white for 0 %, light green up to 79 %, dark green from 80 to
100 %, red above 100 %. Tentative bookings count at 50 %.

Documentation: User Manual, Chapter 4, section 4.4.

### Timesheet approval (Team and Business)

Weekly timesheets can be submitted, approved, rejected and reopened. Project
Leads approve the entries on their projects; Resource Managers,
Administrators and the Owner approve whole weeks. Approved entries are
locked. A reminder is sent on Friday at 15:00.

New webhook event: `timesheet.approved`.

Documentation: User Manual, Chapter 5, section 5.3.

### Project export through the API

```
GET /v2/projects/{project_id}/export?format=csv|json
```

Exports tasks, tickets, bookings and time entries of a project, as a ZIP of
CSV files or as one JSON document. Large exports run in the background and
are fetched through `GET /v2/exports/{export_id}`. Available on every plan,
including Starter, for the project's Project Lead, Administrators and the
Owner.

The same export has been available in the interface under *Settings >
Export*; it now also offers the JSON format.

Documentation: *API: Projects and Tickets*; User Manual, Chapter 2,
section 2.7.

## Deprecated

### API v1

API v1 is deprecated as of this release and **will be switched off on
31 December 2026**. From today, every v1 response carries the headers
`Deprecation: true` and `Sunset: Thu, 31 Dec 2026 23:59:59 GMT`.
Administrators of organisations that still call v1 receive a monthly email
listing the tokens used.

What to change when moving to v2: cursors instead of page numbers, tokens in
the `Authorization` header only, and the new bookings endpoints instead of
the read-only v1 resources. See *API: Overview and Authentication*.

## Improved

- **Absences** can now be entered by Resource Managers for anyone, not only
  by the person themselves.
- **Planned vs. actual** counts only approved hours as actual when approval
  is switched on.
- **Project templates** can include ticket categories.
- **Session handling:** role changes reach open sessions within one minute
  instead of at the next sign-in.

## Fixed

- SLA timers based on business hours now respect public holidays added after
  a ticket was created (known issue from 4.1).
- The CSV import rejected files with a byte order mark.
- Deactivated users still appeared in the resource planner for one day.
- The *Utilisation* report showed 0 % for part-time employees whose capacity
  was set by an Administrator instead of in their profile.

## API changes

- New: `GET /v2/projects/{project_id}/export`, `GET /v2/exports/{export_id}`.
- New: `POST /v2/timesheets/{timesheet_id}/submit`, `/approve`, `/reject`,
  `/reopen`.
- New: `POST /v2/absences`.
- New webhook event: `timesheet.approved`.
- Deprecated: API v1 (sunset 31 December 2026).

## Upgrade notes

All organisations were updated automatically on 16 March 2026. Timesheet
approval is off by default and is switched on under *Settings > Time*.
Organisations on Starter do not see the Resource Manager role or timesheet
approval.

## Looking ahead

Haldrum 4.3, planned for summer 2026, will bring booking conflict detection,
skill search and scheduled report exports.
