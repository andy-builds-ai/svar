# Generated overview of the Haldrum API v2 endpoints

*This is a generated overview, extracted automatically on 6 August 2026 from
the documents API: Overview and Authentication, API: Projects and Tickets,
API: Resources, Bookings and Time and API: Webhooks. It lists endpoints only;
parameters, request bodies and permission rules are in the source documents.
Not reviewed by a person.*

Base URL: `https://api.haldrum.example/v2`
Authentication: `Authorization: Bearer hdr_...` or OAuth 2.0 (Business).

## Plan availability at a glance

| Plan | API access | Requests per minute |
|---|---|---|
| Starter | read-only | 60 |
| Team | read and write | 120 |
| Business | read and write, OAuth apps | 600 |

Resources, bookings, absences, skills and webhooks require Team or Business.
The project export works on every plan.

## Projects

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/projects` | List projects | 4.0 |
| POST | `/projects` | Create a project | 4.0 |
| GET | `/projects/{project_id}` | Get a project | 4.0 |
| PATCH | `/projects/{project_id}` | Update a project | 4.0 |
| POST | `/projects/{project_id}/archive` | Archive a project | 4.0 |
| GET | `/projects/{project_id}/export` | Export a project (`format=csv` or `json`) | 4.2 |
| GET | `/exports/{export_id}` | Status and download link of a background export | 4.2 |
| GET | `/projects/{project_id}/members` | List members | 4.0 |
| POST | `/projects/{project_id}/members` | Add a member | 4.0 |
| DELETE | `/projects/{project_id}/members/{user_id}` | Remove a member | 4.0 |

## Tickets

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/tickets` | List tickets; filter `sla_breached` since 4.1 | 4.0 |
| POST | `/tickets` | Create a ticket | 4.0 |
| GET | `/tickets/{ticket_id}` | Get a ticket by ID or number | 4.0 |
| PATCH | `/tickets/{ticket_id}` | Update status, priority, assignee, category, project | 4.0 |
| GET | `/tickets/{ticket_id}/comments` | List comments | 4.0 |
| POST | `/tickets/{ticket_id}/comments` | Add a comment, optionally internal | 4.0 |

## Resources, skills and absences

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/resources` | List resources; filter `skill` since 4.3 | 4.0 |
| GET | `/resources/{resource_id}/availability` | Capacity and bookings per day, max 92 days | 4.0 |
| PUT | `/resources/{resource_id}/skills` | Set a person's skills | 4.0 |
| GET | `/skills` | List skills | 4.0 |
| POST | `/skills` | Create a skill | 4.0 |
| GET | `/absences` | List absences | 4.2 |
| POST | `/absences` | Create an absence | 4.2 |

## Bookings

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/bookings` | List bookings | 4.0 |
| POST | `/bookings` | Create a booking; `409 booking_conflict` since 4.3 | 4.0 |
| PATCH | `/bookings/{booking_id}` | Change a booking; conflict check since 4.3 | 4.0 |
| DELETE | `/bookings/{booking_id}` | Delete a booking | 4.0 |

## Time

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/time-entries` | List time entries; filters `billable`, `approved`, `ticket_id` | 4.0 |
| POST | `/time-entries` | Create a time entry for the token's user | 4.0 |
| GET | `/timesheets` | List timesheets by user and week | 4.2 |
| POST | `/timesheets/{timesheet_id}/submit` | Submit | 4.2 |
| POST | `/timesheets/{timesheet_id}/approve` | Approve | 4.2 |
| POST | `/timesheets/{timesheet_id}/reject` | Reject with comment | 4.2 |
| POST | `/timesheets/{timesheet_id}/reopen` | Reopen with reason | 4.2 |

## Users and audit

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/users` | List users | 4.0 |
| POST | `/users/invite` | Invite users with a role | 4.0 |
| GET | `/audit-log` | Audit log (Business); filters `actor_id`, `action`, `from`, `to` since 4.3 | 4.0 |

## Webhooks

| Method | Path | Purpose | Since |
|---|---|---|---|
| GET | `/webhooks` | List webhooks | 4.0 |
| POST | `/webhooks` | Register a webhook | 4.0 |
| GET | `/webhooks/{webhook_id}` | Get a webhook | 4.0 |
| PATCH | `/webhooks/{webhook_id}` | Change URL, events or description | 4.0 |
| DELETE | `/webhooks/{webhook_id}` | Delete a webhook | 4.0 |
| POST | `/webhooks/{webhook_id}/test` | Send a `ping` event | 4.0 |

Webhook events: `project.created`, `project.archived`, `ticket.created`,
`ticket.status_changed`, `booking.created` (all 4.0), `timesheet.approved`
(4.2), `booking.conflict` (4.3).

## OAuth (Business)

| URL | Purpose |
|---|---|
| `https://app.haldrum.example/oauth/authorize` | Authorisation |
| `https://api.haldrum.example/oauth/token` | Token exchange and refresh |

## Common status codes

`400` malformed, `401` token missing or invalid, `403` not permitted or
write on Starter, `404` not found or not visible, `409` conflict,
`422` invalid value, `429` rate limit (see `Retry-After`), `500`/`503`
server error.

## Generation notes

- "Since 4.0" means the endpoint existed at the start of the 4.x series; the
  source documents do not state an earlier date.
- `GET /users` and `POST /users/invite` are mentioned in *Administration:
  Users and Roles* and the *Data Protection Policy* but have no dedicated
  API page yet. Parameters are unknown to this overview.
- API v1 endpoints are not listed; v1 is switched off on 31 December 2026.
