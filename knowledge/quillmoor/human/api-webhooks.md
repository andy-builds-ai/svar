# Haldrum API: Webhooks

*API version 2, as of Haldrum 4.3. Webhooks are available on the Team and
Business plans.*

Webhooks let another system react to what happens in Haldrum without
polling the API. When an event occurs, Haldrum sends an HTTPS `POST` request
with a JSON body to a URL you register.

## Registering a webhook

In the interface, Administrators go to *Settings > Developer > Webhooks >
Add webhook*. Through the API:

```
POST /v2/webhooks
```

```json
{
  "url": "https://integration.customer.example/haldrum",
  "events": ["ticket.created", "booking.conflict"],
  "description": "Forward conflicts to planning chat"
}
```

The response contains the webhook ID and a **signing secret** starting with
`whsec_`. The secret is shown once. Other endpoints:

```
GET    /v2/webhooks
GET    /v2/webhooks/{webhook_id}
PATCH  /v2/webhooks/{webhook_id}
DELETE /v2/webhooks/{webhook_id}
POST   /v2/webhooks/{webhook_id}/test
```

The target URL must use HTTPS with a valid certificate. Up to 25 webhooks
per organisation are allowed.

## Events

| Event | Sent when | Since |
|---|---|---|
| `project.created` | A project is created. | 4.0 |
| `project.archived` | A project is archived. | 4.0 |
| `ticket.created` | A ticket is created by any channel: interface, email, API or CSV import. | 4.0 |
| `ticket.status_changed` | A ticket's status changes. | 4.0 |
| `booking.created` | A booking is created. | 4.0 |
| `timesheet.approved` | A timesheet, or the last missing part of it, is approved. | 4.2 |
| `booking.conflict` | A booking conflict is prevented or overridden. | 4.3 |

## Payload

Every payload has the same envelope:

```json
{
  "id": "evt_2b7e...",
  "type": "booking.conflict",
  "created_at": "2026-09-15T08:12:44Z",
  "organisation_id": "org_91d0...",
  "data": { ... }
}
```

### `ticket.status_changed`

```json
"data": {
  "ticket_id": "T-1042",
  "previous_status": "in_progress",
  "status": "resolved",
  "changed_by": "usr_7c02..."
}
```

### `booking.conflict`

```json
"data": {
  "resource_id": "usr_7c02...",
  "project_id": "prj_0a44...",
  "requested_by": "usr_33af...",
  "outcome": "prevented",
  "override_reason": null,
  "conflicts": [
    {"date": "2026-10-06", "capacity_hours": 8, "booked_hours": 10}
  ]
}
```

`outcome` is `prevented` when the booking was not saved, or `overridden`
when a Resource Manager saved it with a reason (User Manual, Chapter 4,
section 4.5).

### `timesheet.approved`

```json
"data": {
  "timesheet_id": "ts_6610...",
  "user_id": "usr_7c02...",
  "week": "2026-W38",
  "approved_by": "usr_33af...",
  "total_minutes": 2340
}
```

## Verifying the signature

Every request carries the header `X-Haldrum-Signature`:

```
X-Haldrum-Signature: t=1789466000,v1=5f2c...
```

To verify it:

1. Take `t` (Unix timestamp) and the raw request body.
2. Compute HMAC-SHA256 over the string `t + "." + body` with your signing
   secret as the key.
3. Compare the hex result with `v1` using a constant-time comparison.
4. Reject requests where `t` is more than 5 minutes old, to prevent replays.

Never skip verification: without it, anyone who learns the URL can send
fake events.

## Delivery and retries

- Your endpoint must answer with a `2xx` status within **10 seconds**.
- Any other status, or a timeout, counts as a failure.
- Failed deliveries are retried **5 times over 24 hours** (after 1 minute,
  10 minutes, 1 hour, 6 hours and 16 hours, each counted from the previous
  attempt).
- After 5 failed retries, the event is dropped and marked failed in the
  delivery log under *Settings > Developer > Webhooks*.
- If a webhook fails continuously for 3 days, it is disabled and the
  Administrators are notified by email.

Events are delivered **at least once**. The same event can arrive twice;
use the `id` field to ignore duplicates. Events are not guaranteed to arrive
in order; use `created_at` if order matters.

## Testing

`POST /v2/webhooks/{webhook_id}/test` sends a `ping` event. Business
customers can use their sandbox organisation to produce real events without
touching production data.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No events arrive | Webhook disabled after 3 days of failures, or the event type is not subscribed. |
| Signature never matches | The body was parsed and re-serialised before hashing. Hash the raw bytes. |
| Duplicate processing | Missing deduplication on `id`. |
| `booking.conflict` missing for old overbookings | Conflicts are only detected for bookings created or changed since 4.3. |

Questions about deliveries can be sent to support@quillmoor.example with the
event `id`.
