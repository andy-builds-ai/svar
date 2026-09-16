# Haldrum Onboarding Guide for New Customers

*For the Owner and Administrators of a new Haldrum organisation. Applies to
Haldrum 4.3.*

This guide takes a new organisation from the first sign-in to everyday use
in about 30 days — the length of the trial. It points to the detailed
documents instead of repeating them.

## Before you start

Decide three things with your team:

1. **Who administers Haldrum.** Choose two or three Administrators. One of
   them becomes Owner when the trial turns into a subscription, unless the
   person who signed up keeps that role.
2. **Which modules you start with.** Most customers start with Projects and
   Time, add Tickets in week two and Resources in week three. Starting with
   everything at once is possible, but people learn faster one module at a
   time.
3. **Which plan you are likely to need.** The trial has Team features. If
   you need single sign-on or the audit log, plan for Business; if you have
   fewer than 25 users and do not plan resources, Starter may be enough.
   See *Administration: Billing and Plans*.

## Week 1: foundation

**Day 1 — organisation settings.**
Under *Settings > Organisation*, set the organisation name, default time
zone, working week and public holidays. The time zone matters: reports are
calculated in it (User Manual, Chapter 6).

**Day 1 — security.**
- Switch on *Require MFA*, or on Business set up single sign-on
  (*Administration: Authentication and Single Sign-On*).
- Verify your email domain.
- Set the maximum API token lifetime.

**Day 2 — invite the first users.**
Start with a pilot group of five to ten people from one team. Assign roles
carefully (*Administration: Users and Roles*):
- team leads as **Project Leads**,
- planners as **Resource Managers**, not Administrators,
- everyone else as **Members**,
- external contacts as **Guests**.

**Days 3–5 — first project.**
Create one real project, not a test project. Add milestones and members.
Ask the pilot group to log their time on it for the rest of the week
(User Manual, Chapters 2 and 5).

## Week 2: tickets

**Set up the inbound email address.** Every organisation has an address of
the form `<organisation>@inbound.haldrum.example`. Forward your existing
support mailbox to it, or publish it directly.

**Define categories and priorities.** Keep the list short; five to eight
categories are enough for most teams.

**Create two or three rules.** For example, route billing questions to one
person and mark emails containing "outage" as `Urgent` (User Manual,
Chapter 3, section 3.5).

**Import open tickets.** Existing tickets can be imported from a CSV file,
up to 10,000 rows per file.

**Set ticket SLA timers** if you promise response times to your own
requesters.

## Week 3: resources and time approval

**Maintain capacity.** Resource Managers check every person's working hours
and enter known holidays for the next three months.

**Add skills.** Start with ten to twenty skills that people actually ask
for when staffing projects.

**Book the pilot project.** Book people for the coming weeks and look at the
capacity heatmap together in the Monday meeting. Since Haldrum 4.3, booking
conflicts are detected automatically (User Manual, Chapter 4, section 4.5).

**Switch on timesheet approval** if your organisation needs approved hours
for billing or payroll. Agree on a weekly deadline; Haldrum sends a reminder
on Friday at 15:00.

## Week 4: roll-out and integration

**Invite everyone else.** Use the pilot group as first contacts for
questions in each team.

**Reports.** Save report views for the questions your management asks every
week, and schedule exports where people want numbers by email (User Manual,
Chapter 6).

**Integrations.** If other systems need Haldrum data, create API tokens or,
on Business, OAuth apps. Payroll and invoicing systems usually need
`GET /v2/time-entries` with `approved=true`; chat tools usually subscribe to
webhooks such as `ticket.created` and `booking.conflict`. See *API: Overview
and Authentication* and *API: Webhooks*.

**Choose a plan** before day 30. After the trial ends without a plan, the
organisation becomes read-only for 30 days.

## Moving data in

| From | How |
|---|---|
| Spreadsheet of projects | Create projects through `POST /v2/projects`, or by hand for fewer than 20 projects. |
| Previous ticket tool | CSV import under *Tickets > Import*. |
| Previous time tracking | `POST /v2/time-entries` with an `Idempotency-Key`, so a repeated import does not create duplicates. |
| Staff list | Invite by email, or on Business use just-in-time provisioning with single sign-on. |

Historical time entries can only be imported into projects with status
*Active*; set old projects to *Active*, import, then set them to *Completed*.

## Getting help during onboarding

- Help centre: `help.haldrum.example`
- Support: support@quillmoor.example, or in-app chat on Team and Business
- Questions that come up often are answered in the *FAQ*.

Customers who sign a Business contract for 100 or more users receive two
free onboarding sessions of 90 minutes with a Quillmoor onboarding
specialist. These are booked through support and scheduled within 10
business days.

## Checklist

- [ ] Time zone, working week and holidays set
- [ ] MFA enforced or single sign-on configured
- [ ] Two or more Administrators
- [ ] First real project running with time entries
- [ ] Inbound email address in use, categories defined
- [ ] Capacity and holidays maintained, skills added
- [ ] Timesheet approval decided
- [ ] Report views saved, exports scheduled
- [ ] Plan chosen before the trial ends
