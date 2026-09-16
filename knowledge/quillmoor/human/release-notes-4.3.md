# Haldrum 4.3 — Release Notes

**Released:** 4 August 2026
**Previous version:** 4.2 (16 March 2026)

Haldrum 4.3 makes plans harder to break. Bookings that overload a person are
now detected, people can be found by skill, and reports can arrive by email
on a schedule. Business customers get a configurable session timeout and
better audit log filtering.

## New

### Booking conflict detection (Team and Business)

Haldrum now checks every new or changed booking. A **conflict** exists when
the booking would push a person above 100 % of their capacity on at least
one day, or when it falls on a registered absence.

- **Project Leads** cannot save a conflicting booking. Haldrum shows the
  conflicting days and suggests people with the same skills who are free.
- **Resource Managers** see the warning and may save anyway, with a
  mandatory reason. On Business, the override is written to the audit log.
- Through the API, a conflicting request returns `409 Conflict` with the
  code `booking_conflict`. A Resource Manager's token may add
  `override_reason` to save anyway.
- Tentative bookings are not blocked, but return a warning.

New webhook event: `booking.conflict`, sent for prevented and overridden
conflicts.

Bookings created before 4.3 are not checked retroactively. Use the
*Overbooking* report or the red cells of the capacity heatmap to find them.

Documentation: User Manual, Chapter 4, section 4.5; *API: Resources,
Bookings and Time*; *API: Webhooks*.

### Skill search

The resource planner can be filtered by one or more skills, and the API
accepts `GET /v2/resources?skill=` (repeat the parameter for several
skills). The conflict dialog uses the same search to suggest alternatives.

Documentation: User Manual, Chapter 4, section 4.1.

### Scheduled exports (Team and Business)

Any saved report view can be sent automatically as CSV, XLSX or PDF, daily,
weekly or monthly. Recipients must be users of the organisation. Schedules
run with the permissions of their creator and pause if that person loses
access. Up to 20 schedules on Team and 100 on Business.

Documentation: User Manual, Chapter 6, section 6.5.

### Configurable session timeout (Business)

Administrators can set the inactivity timeout between 1 and 72 hours. The
default stays at 12 hours, which also remains fixed on Starter and Team.

Documentation: *Administration: Authentication and Single Sign-On*.

## Improved

- **Audit log API:** `GET /v2/audit-log` accepts the filters `actor_id`,
  `action`, `from` and `to`.
- **Timer:** a running timer stops automatically after 12 hours and marks
  the entry for review, instead of running indefinitely.
- **Exports:** email attachments above 20 MB are replaced by a download link
  valid for 7 days.
- **Planner:** week view loads twice as fast for organisations with more than
  200 resources.
- **Guests** can now be exempted from enforced MFA individually.

## Fixed

- Moving a booking by drag and drop across a month boundary shortened it by
  one day.
- Rejected timesheets did not notify the person when the rejection came from
  a Resource Manager.
- The JSON project export omitted time entries linked to a ticket but not to
  a task.
- Webhook deliveries were retried 6 instead of 5 times.

## API changes

- New: `409 Conflict` with `booking_conflict` on `POST /v2/bookings` and
  `PATCH /v2/bookings/{booking_id}`.
- New: optional `override_reason` on booking requests.
- New: `skill` filter on `GET /v2/resources`.
- New: filters `actor_id`, `action`, `from`, `to` on `GET /v2/audit-log`.
- New webhook event: `booking.conflict`.

**Possible impact on integrations:** automated jobs that created bookings
without checking capacity may now receive `409` responses. Check your
integration logs after the update. If a planning tool must keep writing
overbookings, it needs a Resource Manager token and an `override_reason`.

## Reminder: API v1

API v1 will be switched off on **31 December 2026**, as announced with 4.2.

## Upgrade notes

All organisations were updated automatically on 4 August 2026. Conflict
detection is on for every Team and Business organisation and cannot be
switched off.

## Looking ahead

Haldrum 4.4 is planned for November 2026. Details will follow in the
release notes for that version.
