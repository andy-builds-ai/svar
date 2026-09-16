# Haldrum Glossary

*Terms used across the Haldrum documentation, in alphabetical order. Applies
to Haldrum 4.3.*

**Absence.** A period in which a person cannot work, of type holiday, sick,
training or other. Absences set capacity to zero for those days and cause a
booking conflict if a booking falls on them. See User Manual, Chapter 4.

**Administrator.** A role that manages users, roles, security settings and
all projects, but not billing. See *Administration: Users and Roles*.

**API token.** A secret starting with `hdr_` that lets a program call the
Haldrum API with the permissions of the user who created it, limited by its
scope (`read` or `read-write`). Valid for at most 365 days. See *API:
Overview and Authentication*.

**API v1.** The previous version of the API. Deprecated with Haldrum 4.2,
switched off on 31 December 2026.

**Archived.** A project status that hides a project from everyday lists
without deleting anything. Can be reversed.

**Audit log.** A record of security-relevant actions — sign-ins, role
changes, MFA resets, token changes, booking overrides, support access. Kept
for 400 days. Business plan only.

**Board.** A view of tickets as cards in columns, optionally split into
swimlanes.

**Booking.** A reservation of a number of hours per day of one person for
one project over a date range. See User Manual, Chapter 4, section 4.2.

**Booking conflict.** A booking that would push a person above 100 % of
capacity on at least one day, or that falls on an absence. Detected since
Haldrum 4.3. Blocked for Project Leads, overridable by Resource Managers.

**Business.** The largest plan, €34 per user per month.

**Capacity.** The number of hours a person can work per day, taken from
their profile or set by a Resource Manager.

**Capacity heatmap.** The colour view in the resource planner that shows how
much of each person's capacity is booked. Since Haldrum 4.2.

**Guest.** A free, read-only role for external people, limited to the
projects they are invited to.

**Idempotency key.** A header on `POST` requests that prevents duplicates
when a request is repeated within 24 hours.

**Inbound address.** The email address `<organisation>@inbound.haldrum.example`
that turns emails into tickets.

**Member.** The standard role for people who work in projects, handle
tickets and log time.

**MFA.** Multi-factor authentication with time-based one-time codes. Can be
enforced by Administrators since Haldrum 4.1.

**Organisation.** The top-level container in Haldrum, usually one company,
with its own users, projects, settings and plan.

**Override reason.** The mandatory explanation a Resource Manager gives when
saving a booking despite a conflict.

**Owner.** The one user per organisation who holds the subscription and can
change plan and payment, delete the organisation and transfer ownership.

**P1–P4.** The severity levels of support cases with Quillmoor Support, from
Critical to Low. Not the same as ticket priorities inside Haldrum. See
*Support Process*.

**Project export.** A complete export of one project as CSV files in a ZIP
archive or as one JSON document, in the interface or through
`GET /v2/projects/{project_id}/export` (API since 4.2).

**Project Lead.** A role that creates projects and manages the projects it
leads, including booking people within free capacity and approving their
time.

**Quillmoor Systems.** The company that develops and operates Haldrum,
founded 2014 in Leipzig.

**Resource.** A person with the role Member or higher, who can be booked.

**Resource Manager.** A role for planners, introduced in Haldrum 4.2: manages
bookings, capacity, absences and skills in all projects and approves
timesheets.

**Sandbox organisation.** A free test copy of a Business organisation's
settings and users, without project data.

**Scheduled export.** A saved report view sent automatically by email as
CSV, XLSX or PDF. Since Haldrum 4.3, Team and Business.

**Service credit.** A discount on the next invoice when monthly uptime falls
below the target in the *Service Level Agreement*.

**Skill.** A tag on a person, such as "network" or "German", used to search
for suitable people. Skill search since Haldrum 4.3.

**Starter.** The smallest plan, €9 per user per month, up to 25 users,
read-only API.

**SLA timer (ticket).** A target time for first response or resolution of a
ticket, set per priority by the customer's organisation. Since Haldrum 4.1.

**Swimlane.** A horizontal row on a board, grouping cards by assignee,
priority or category. Since Haldrum 4.1.

**Team.** The middle plan, €19 per user per month, with all five modules.

**Tentative booking.** A booking that is not yet certain. Counts at 50 % in
the heatmap and never blocks.

**Ticket.** A request or incident with a number such as `T-1042`, a status,
a priority and an assignee.

**Timesheet.** All time entries of one person for one calendar week. States:
Open, Submitted, Approved, Rejected.

**Trash.** Where deleted projects, tickets and files stay for 30 days before
permanent deletion.

**Webhook.** An HTTPS request Haldrum sends to another system when an event
occurs, signed with `X-Haldrum-Signature`. See *API: Webhooks*.
