# Haldrum API: Overview and Authentication

*API version 2, as of Haldrum 4.3.*

The Haldrum REST API gives programmatic access to projects, tickets,
resources, bookings, time entries, users, webhooks and the audit log. The
endpoints themselves are documented in *API: Projects and Tickets*,
*API: Resources, Bookings and Time* and *API: Webhooks*.

## Base URL and format

```
https://api.haldrum.example/v2
```

All requests and responses use JSON encoded as UTF-8, except the project
export, which can return CSV in a ZIP archive. Timestamps are ISO 8601 in
UTC, for example `2026-08-04T09:30:00Z`. Dates without a time, such as
booking ranges, use `YYYY-MM-DD`. Identifiers are opaque strings; do not
parse them.

## Availability by plan

| Plan | Access | Rate limit |
|---|---|---|
| Starter | read-only | 60 requests per minute |
| Team | read and write | 120 requests per minute |
| Business | read and write, plus OAuth apps | 600 requests per minute |

Rate limits apply per organisation, not per token.

## Authentication with API tokens

Personal API tokens are the simplest way to call the API.

1. Sign in and go to *Settings > Developer > API tokens*.
2. Click *Create token*, give it a name, choose the scope and the lifetime.
3. Copy the token. It is shown **once** and cannot be displayed again.

Tokens start with the prefix `hdr_`. Send the token in the `Authorization`
header:

```http
GET /v2/projects HTTP/1.1
Host: api.haldrum.example
Authorization: Bearer hdr_...
```

### Scopes

| Scope | Allows |
|---|---|
| `read` | All `GET` requests the user is permitted to make. |
| `read-write` | Additionally `POST`, `PATCH` and `DELETE`. Not available on Starter. |

A token always acts with the permissions of the user who created it. A
Member's `read-write` token cannot create bookings, because a Member cannot
create bookings in the interface either (see *Administration: Users and
Roles*).

### Lifetime

Tokens expire after at most 365 days; Administrators can set a shorter
maximum (see *Administration: Authentication and Single Sign-On*). Haldrum
emails the token owner 14 days before expiry. When a user is deactivated,
all their tokens stop working immediately.

Never put tokens into source code. Store them in your platform's secret
store or in environment variables.

## OAuth 2.0 apps

On the Business plan, integrations can use OAuth 2.0 with the authorisation
code flow instead of personal tokens. Administrators register an app under
*Settings > Developer > OAuth apps* and receive a client ID and a client
secret.

- Authorisation URL: `https://app.haldrum.example/oauth/authorize`
- Token URL: `https://api.haldrum.example/oauth/token`
- Scopes: `read`, `read-write`
- Access tokens are valid for 1 hour; refresh tokens for 90 days.

## Pagination

List endpoints use cursor pagination.

- `limit` — number of items per page, default **50**, maximum **200**.
- `cursor` — the value of `next_cursor` from the previous response.

```json
{
  "data": [ ... ],
  "next_cursor": "c_8f2a...",
  "has_more": true
}
```

When `has_more` is `false`, you have reached the last page.

## Errors

Errors return a JSON body with a machine-readable code and a message:

```json
{
  "error": {
    "code": "booking_conflict",
    "message": "Booking exceeds capacity on 2026-10-06.",
    "request_id": "req_41c9..."
  }
}
```

| Status | Meaning |
|---|---|
| 400 | Malformed request, for example invalid JSON. |
| 401 | Missing, invalid or expired token. |
| 403 | The token is valid, but the user or scope does not allow this. Also returned for write requests on Starter. |
| 404 | The object does not exist or the user may not see it. |
| 409 | Conflict, for example a booking conflict (since 4.3) or a duplicate project code. |
| 422 | The request is well-formed but a field value is invalid. |
| 429 | Rate limit exceeded. Wait the number of seconds in the `Retry-After` header. |
| 500, 503 | Error on our side. Retry with exponential backoff; check `status.haldrum.example`. |

Include the `request_id` when contacting support.

## Rate limit headers

Every response carries:

- `X-RateLimit-Limit` — requests allowed per minute,
- `X-RateLimit-Remaining` — requests left in the current window,
- `X-RateLimit-Reset` — Unix time when the window resets.

## Idempotency

`POST` requests accept an `Idempotency-Key` header. If a request with the
same key is repeated within 24 hours, Haldrum returns the original response
instead of creating a second object. Use it for bookings and time entries
created by automated jobs.

## Versioning and the end of API v1

API v2 is the current version. **API v1 was deprecated with Haldrum 4.2 on
16 March 2026 and will be switched off on 31 December 2026.** Requests to v1
return the header `Deprecation: true` and a `Sunset` date. The main
differences:

- v1 used page numbers; v2 uses cursors.
- v1 had no bookings endpoints; resources were read-only.
- v1 accepted tokens in the query string; v2 accepts them only in the
  `Authorization` header.

Breaking changes to v2 will be announced at least six months in advance, in
the release notes and by email to Administrators.

## Sandbox

Business customers should develop against their sandbox organisation (see
*Administration: Billing and Plans*). The sandbox uses the same base URL;
the token decides which organisation is addressed.
