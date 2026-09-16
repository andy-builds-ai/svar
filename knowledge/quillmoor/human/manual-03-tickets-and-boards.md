# Haldrum User Manual — Chapter 3: Tickets and Boards

*Applies to Haldrum 4.3.*

Tickets are requests that need an answer or a fix: a customer question, an
incident, an internal change request. In Haldrum, tickets can stand on their
own or belong to a project (Chapter 2), and the time spent on them can be
logged directly from the ticket (Chapter 5).

## 3.1 Anatomy of a ticket

Every ticket has:

- a **number** such as `T-1042`, unique within the organisation,
- a **title** and a description,
- a **status**, a **priority** and a **category**,
- a **requester** (who asked) and an **assignee** (who works on it),
- an optional **project**,
- comments, attachments and a history of every change.

### Statuses

| Status | Meaning |
|---|---|
| New | Received, nobody has looked at it yet. |
| In Progress | Someone is working on it. |
| Waiting | Blocked on the requester or a third party. SLA timers pause. |
| Resolved | A solution was provided. Reopens automatically if the requester replies within 7 days. |
| Closed | Final. Closed tickets cannot be reopened; a reply creates a new, linked ticket. |

Resolved tickets close automatically after 7 days without a reply.

### Priorities

`Low`, `Normal`, `High` and `Urgent`. New tickets are `Normal` unless a rule
says otherwise (see 3.5). Ticket priorities in Haldrum are something your
organisation sets for its own work; they are not the same as the P1–P4
severity levels Quillmoor Support uses for your support cases with us (see
*Support Process*).

## 3.2 Creating tickets

Tickets can be created in four ways:

1. In the interface, under *Tickets > New ticket*.
2. By email: each organisation receives an inbound address of the form
   `<organisation>@inbound.haldrum.example`. Emails become tickets, replies
   become comments.
3. Through the API with `POST /v2/tickets` (see *API: Projects and
   Tickets*).
4. By **CSV import** under *Tickets > Import* (since 4.1). The import accepts
   up to 10,000 rows per file and shows a preview before anything is
   created.

## 3.3 Boards

A board shows tickets as cards in columns. The default board has one column
per status. You can create your own boards with filters, for example "all
Urgent tickets of project ONB-2026".

**Swimlanes** (since 4.1) split a board into horizontal rows by assignee,
priority or category. Dragging a card to another column changes its status;
dragging it to another swimlane changes the assignee, priority or category
accordingly.

Boards are personal by default. Project Leads and Administrators can share a
board with the whole organisation.

## 3.4 Ticket SLA timers

Since Haldrum 4.1, organisations on the Team and Business plans can define
**ticket SLA timers**: a target time for the first response and a target
time for resolution, per priority. For example:

| Priority | First response | Resolution |
|---|---|---|
| Urgent | 1 hour | 8 hours |
| High | 4 hours | 2 days |
| Normal | 1 day | 5 days |
| Low | 3 days | 15 days |

The values above are only an example; each organisation sets its own under
*Settings > Tickets > SLA timers*. Timers can count calendar hours or
business hours from a working calendar. Timers pause while a ticket is in
status *Waiting*.

A ticket that is close to breaching its target shows an amber clock; a
breached ticket shows a red one. Breaches appear in the *Ticket SLA* report
(Chapter 6).

These timers measure how your organisation serves its own requesters. The
commitments Quillmoor Systems makes to you as a customer are in the
*Service Level Agreement*.

## 3.5 Rules

Rules act on tickets automatically when they are created or changed. A rule
has a condition and an action, for example:

- If the email subject contains "outage", set priority to `Urgent`.
- If category is "Billing", assign to the finance team.
- If status is `Waiting` for more than 5 days, add a reminder comment.

Up to 50 active rules are allowed per organisation. Rules run in the order
shown on the rules page; the first matching rule wins when two rules set the
same field.

## 3.6 Linking tickets to work

- **To a project:** set the *Project* field. The ticket then appears in the
  project's ticket list and in project reports.
- **To time:** click *Log time* on the ticket. The entry is linked to the
  ticket and, if set, to its project.
- **To other tickets:** use *Link* to mark duplicates or related tickets.

## 3.7 Webhooks for tickets

Systems outside Haldrum can react to ticket changes through webhooks. The
events `ticket.created` and `ticket.status_changed` are described in
*API: Webhooks*.
