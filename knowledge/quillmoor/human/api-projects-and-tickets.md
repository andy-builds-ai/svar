# Haldrum API: Projects and Tickets

*API version 2, as of Haldrum 4.3. Authentication, pagination and errors are
described in API: Overview and Authentication.*

All paths are relative to `https://api.haldrum.example/v2`.

## Projects

### List projects

```
GET /projects
```

Query parameters:

| Parameter | Description |
|---|---|
| `status` | `planned`, `active`, `on_hold`, `completed` or `archived`. Archived projects are excluded unless requested. |
| `lead_id` | Only projects led by this user. |
| `updated_since` | ISO 8601 timestamp. |
| `limit`, `cursor` | Pagination. |

Returns only projects the token's user may see.

### Get a project

```
GET /projects/{project_id}
```

Returns name, code, status, start and end dates, lead, member count and
links to the project's tickets, bookings and time entries.

### Create a project

```
POST /projects
```

```json
{
  "name": "Customer onboarding 2026",
  "code": "ONB-2026",
  "template_id": "tpl_19ab...",
  "start_date": "2026-10-01",
  "end_date": "2026-12-18",
  "lead_id": "usr_7c02..."
}
```

Requires a `read-write` token of a Project Lead, Administrator or Owner.
Returns `201 Created`. A duplicate `code` returns `409 Conflict`.

### Update a project

```
PATCH /projects/{project_id}
```

Send only the fields to change. Changing `status` follows the same rules as
the interface (User Manual, Chapter 2).

### Archive a project

```
POST /projects/{project_id}/archive
```

Archives a project. Restoring is done with
`PATCH /projects/{project_id}` and `"status": "completed"`. Triggers the
webhook event `project.archived`.

### Export a project

```
GET /projects/{project_id}/export?format=csv
GET /projects/{project_id}/export?format=json
```

Added in Haldrum 4.2. Exports all data of one project: tasks, tickets,
bookings and time entries.

- `format=csv` returns a ZIP archive with one CSV file per object type.
- `format=json` returns a single JSON document.
- `include_files=true` adds a link to a separate ZIP archive with the
  project's files.

Requires a token with scope `read` of the project's Project Lead, an
Administrator or the Owner. The export works on every plan, including
Starter.

Small projects are returned directly with `200 OK`. Large projects return
`202 Accepted` with a `Location` header pointing to
`GET /exports/{export_id}`. Poll that URL; when `status` is `ready`, it
contains a `download_url` valid for 7 days.

```json
{
  "export_id": "exp_5d31...",
  "status": "ready",
  "download_url": "https://api.haldrum.example/v2/exports/exp_5d31.../file",
  "expires_at": "2026-09-24T10:00:00Z"
}
```

### Project members

```
GET    /projects/{project_id}/members
POST   /projects/{project_id}/members      {"user_id": "...", "as_guest": false}
DELETE /projects/{project_id}/members/{user_id}
```

Adding a member does not book their time; use `POST /bookings` for that.

## Tickets

### List tickets

```
GET /tickets
```

| Parameter | Description |
|---|---|
| `status` | `new`, `in_progress`, `waiting`, `resolved`, `closed`. Several values separated by commas. |
| `priority` | `low`, `normal`, `high`, `urgent`. |
| `project_id` | Only tickets of this project. |
| `assignee_id` | Only tickets assigned to this user. |
| `sla_breached` | `true` returns tickets that breached a first-response or resolution target (Team and Business). |
| `updated_since`, `limit`, `cursor` | As above. |

### Get a ticket

```
GET /tickets/{ticket_id}
```

`ticket_id` is either the internal identifier or the ticket number such as
`T-1042`.

### Create a ticket

```
POST /tickets
```

```json
{
  "title": "VPN access for new colleague",
  "description": "Starts on 1 October.",
  "priority": "normal",
  "category": "Access",
  "project_id": "prj_0a44...",
  "requester_email": "someone@customer.example"
}
```

Returns `201 Created` with the ticket number. Rules (User Manual, Chapter 3)
run on tickets created through the API exactly as on tickets created in the
interface. Triggers `ticket.created`.

### Update a ticket

```
PATCH /tickets/{ticket_id}
```

Change `status`, `priority`, `assignee_id`, `category` or `project_id`. A
status change triggers `ticket.status_changed`. Setting `status` to `closed`
is final; closed tickets cannot be changed.

### Comments

```
GET  /tickets/{ticket_id}/comments
POST /tickets/{ticket_id}/comments   {"body": "...", "internal": true}
```

`internal: true` makes a comment visible only to users of the organisation,
not to the requester or Guests.

## Example: export every active project

```bash
curl -s -H "Authorization: Bearer $HALDRUM_TOKEN" \
  "https://api.haldrum.example/v2/projects?status=active&limit=200"
```

Then, for each `id` in `data`:

```bash
curl -s -H "Authorization: Bearer $HALDRUM_TOKEN" -o "$id.zip" \
  "https://api.haldrum.example/v2/projects/$id/export?format=csv"
```

Respect the rate limit of your plan; on Starter, 60 requests per minute is
enough for 30 projects per minute including the listing.

## Related

- Resource, booking and time endpoints: *API: Resources, Bookings and Time*.
- Events for projects and tickets: *API: Webhooks*.
