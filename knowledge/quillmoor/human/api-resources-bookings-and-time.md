# Haldrum API: Resources, Bookings and Time

*API version 2, as of Haldrum 4.3. The resource and booking endpoints require
the Team or Business plan. Authentication, pagination and errors are
described in API: Overview and Authentication.*

All paths are relative to `https://api.haldrum.example/v2`.

## Resources

A resource is a person with the role Member or higher (User Manual,
Chapter 4).

### List resources

```
GET /resources
```

| Parameter | Description |
|---|---|
| `skill` | Only people with this skill. Repeat the parameter to require several skills, e.g. `?skill=network&skill=German`. Added in Haldrum 4.3. |
| `project_id` | Only people booked on this project. |
| `include_inactive` | `true` includes deactivated users. Default `false`. |
| `limit`, `cursor` | Pagination. |

Each item contains the user ID, display name, capacity in hours per day and
the list of skills.

### Availability

```
GET /resources/{resource_id}/availability?from=2026-10-05&to=2026-10-16
```

Returns one entry per day:

```json
{
  "date": "2026-10-06",
  "capacity_hours": 8,
  "booked_hours": 6,
  "tentative_hours": 2,
  "absence": null
}
```

`tentative_hours` are reported separately; the capacity heatmap counts them
at 50 %. The range may be at most 92 days.

### Skills

```
GET  /skills
POST /skills                          {"name": "accounting"}
PUT  /resources/{resource_id}/skills  {"skills": ["network", "German"]}
```

Writing skills requires a Resource Manager, Administrator or Owner.

## Bookings

### List bookings

```
GET /bookings?project_id=...&resource_id=...&from=...&to=...
```

### Create a booking

```
POST /bookings
```

```json
{
  "resource_id": "usr_7c02...",
  "project_id": "prj_0a44...",
  "start_date": "2026-10-05",
  "end_date": "2026-10-16",
  "hours_per_day": 4,
  "tentative": false
}
```

Returns `201 Created` and triggers `booking.created`.

**Conflicts (since 4.3).** If the booking would push the person above 100 %
of capacity on any day, or falls on an absence, the API returns
`409 Conflict` with the code `booking_conflict` and the conflicting days:

```json
{
  "error": {
    "code": "booking_conflict",
    "message": "Booking exceeds capacity on 2 days.",
    "conflicts": [
      {"date": "2026-10-06", "capacity_hours": 8, "booked_hours": 10},
      {"date": "2026-10-07", "capacity_hours": 0, "absence": "holiday"}
    ]
  }
}
```

A Resource Manager's token may save the booking anyway by adding
`"override_reason": "..."` to the body. Project Leads cannot override; the
field is rejected with `403`. Both the prevented and the overridden conflict
trigger the webhook event `booking.conflict`.

Tentative bookings never return `409`; they return `201` with a `warnings`
array.

### Change or delete a booking

```
PATCH  /bookings/{booking_id}
DELETE /bookings/{booking_id}
```

A `PATCH` that changes dates or hours is checked for conflicts the same way
as a new booking.

### Absences

```
GET  /absences?resource_id=...&from=...&to=...
POST /absences  {"resource_id": "...", "start_date": "...", "end_date": "...", "type": "holiday"}
```

`type` is `holiday`, `sick`, `training` or `other`. Absences set capacity to
zero for those days.

## Time entries

Time endpoints are available on every plan. On Starter, they are read-only
like the rest of the API.

### List time entries

```
GET /time-entries?from=2026-09-01&to=2026-09-30&project_id=...&user_id=...
```

Additional filters: `billable=true|false`, `approved=true|false`,
`ticket_id`.

### Create a time entry

```
POST /time-entries
```

```json
{
  "date": "2026-09-15",
  "minutes": 90,
  "project_id": "prj_0a44...",
  "ticket_id": "T-1042",
  "description": "Set up VPN profile",
  "billable": true
}
```

The entry is created for the token's user. Returns `422` if the project is
not *Active* and `409` if the week's timesheet is already submitted or
approved. Use an `Idempotency-Key` header for automated imports.

### Timesheets

```
GET  /timesheets?user_id=...&week=2026-W38
POST /timesheets/{timesheet_id}/submit
POST /timesheets/{timesheet_id}/approve
POST /timesheets/{timesheet_id}/reject   {"comment": "..."}
POST /timesheets/{timesheet_id}/reopen   {"reason": "..."}
```

Approval endpoints were added in Haldrum 4.2 and follow the rules in User
Manual, Chapter 5: Project Leads approve the parts belonging to their
projects, Resource Managers, Administrators and the Owner approve whole
weeks. Approving triggers `timesheet.approved`.

## Typical integration: payroll

A payroll system that needs approved hours once a month:

1. `GET /time-entries?from=...&to=...&approved=true`, following
   `next_cursor` until `has_more` is `false`.
2. Group by `user_id` and sum `minutes`.
3. Optionally subscribe to `timesheet.approved` instead of polling.

## Related

- Project export, which includes bookings and time entries:
  *API: Projects and Tickets*.
- Webhook payloads: *API: Webhooks*.
