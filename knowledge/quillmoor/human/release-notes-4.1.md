# Haldrum 4.1 — Release Notes

**Released:** 1 December 2025
**Previous version:** 4.0 (15 September 2025)

Haldrum 4.1 is about tickets and account security. Support teams get SLA
timers, swimlanes and a CSV import; Administrators can now make
multi-factor authentication mandatory for the whole organisation.

## New

### Ticket SLA timers (Team and Business)

Organisations can set target times for first response and resolution, per
ticket priority. Timers count calendar hours or business hours from a
working calendar and pause while a ticket is in status *Waiting*. Tickets
close to breaching show an amber clock, breached tickets a red one.

A new report, *Ticket SLA*, lists breaches per week, category and assignee.
The API supports the filter `sla_breached=true` on `GET /v2/tickets`.

Documentation: User Manual, Chapter 3, section 3.4.

### Swimlanes on boards

Boards can now be split into horizontal swimlanes by assignee, priority or
category. Dragging a card between swimlanes changes the corresponding field.

Documentation: User Manual, Chapter 3, section 3.3.

### CSV import for tickets

Tickets can be imported from CSV files of up to 10,000 rows under
*Tickets > Import*. The import shows a preview, maps columns to fields and
reports rows it could not import. Imported tickets run through the
organisation's rules like any new ticket and trigger `ticket.created`.

This was the most requested feature from customers moving from another
ticket tool.

### Enforced MFA (all plans)

Administrators can require multi-factor authentication for every user under
*Settings > Security > Require MFA*, with an optional grace period of up to
7 days. Guests are included unless exempted.

Documentation: *Administration: Authentication and Single Sign-On*.

## Improved

- **Global search** finds tickets by number (`T-1042`) and jumps directly to
  them.
- **Email tickets** keep inline images from the original email as
  attachments.
- **Notifications:** daily digest emails are now sent at 07:00 in each
  user's own time zone instead of the organisation's time zone.
- **Rules:** the limit was raised from 25 to 50 active rules per
  organisation.
- **Performance:** boards with more than 1,000 cards load about three times
  faster.

## Fixed

- Resolved tickets sometimes did not reopen when the requester replied from
  a different email alias of the same address.
- The *Ticket volume* report counted tickets moved between projects twice.
- Invitations to Guests could expire after 7 instead of 14 days.
- Time entries created from a ticket did not inherit the ticket's project if
  the project had been set after the ticket was created.

## API changes

- New: `sla_breached` filter on `GET /v2/tickets`.
- New: ticket objects contain `first_response_due_at` and
  `resolution_due_at` when SLA timers are active.
- No breaking changes.

## Known issues

- SLA timers based on business hours do not yet take public holidays from
  the organisation calendar into account when the holiday was added after
  the ticket was created. Planned fix in 4.2.

## Upgrade notes

Haldrum is a cloud service; every organisation was updated automatically on
1 December 2025. No action is required. Administrators who want to use SLA
timers or enforced MFA must switch them on; both are off by default.

## Looking ahead

Haldrum 4.2, planned for March 2026, will introduce the Resource Manager
role, a capacity heatmap and timesheet approval, and will deprecate API v1.
