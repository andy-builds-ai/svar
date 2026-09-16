# Generated summary of the onboarding path for new Haldrum organisations

*This is a generated summary, produced automatically on 7 August 2026 by
extracting every action item from the Onboarding Guide and linking each one
to the document that explains it. It is meant as a quick task list for
Administrators. It has not been reviewed by a person; the Onboarding Guide is
the authoritative source.*

## The path in four weeks

| Week | Focus | Outcome |
|---|---|---|
| 1 | Foundation | Settings, security, pilot users, first real project |
| 2 | Tickets | Inbound email, categories, rules, import, SLA timers |
| 3 | Resources and approval | Capacity, skills, bookings, timesheet approval |
| 4 | Roll-out | All users, reports, integrations, plan decision |

The four weeks match the 30-day trial with Team features.

## Extracted action items

### Before starting

1. Choose two or three Administrators.
   → *Administration: Users and Roles*
2. Decide the order of modules (typical: Projects and Time, then Tickets,
   then Resources).
   → *Onboarding Guide*
3. Estimate the plan: Business if single sign-on or audit log is needed;
   Starter only for fewer than 25 users without resource planning.
   → *Administration: Billing and Plans*

### Week 1

4. Set organisation name, default time zone, working week, public holidays.
   → User Manual, Chapter 6, section 6.8 (why the time zone matters)
5. Switch on *Require MFA*, or configure SAML single sign-on on Business.
   → *Administration: Authentication and Single Sign-On*
6. Verify the email domain.
7. Set the maximum API token lifetime (suggested: 90 days).
8. Invite a pilot group of 5–10 people; team leads as Project Leads, planners
   as Resource Managers, others as Members, externals as Guests.
9. Create one real project with milestones and members; pilot group logs
   time on it.
   → User Manual, Chapters 2 and 5

### Week 2

10. Put the inbound address `<organisation>@inbound.haldrum.example` into
    use.
    → User Manual, Chapter 3, section 3.2
11. Define five to eight ticket categories.
12. Create two or three rules.
    → User Manual, Chapter 3, section 3.5
13. Import open tickets by CSV (max 10,000 rows per file).
14. Set ticket SLA timers if response times are promised to requesters.
    → User Manual, Chapter 3, section 3.4

### Week 3

15. Check capacity of every person; enter holidays for three months ahead.
    → User Manual, Chapter 4, section 4.1
16. Create ten to twenty skills.
17. Book the pilot project; review the capacity heatmap in the Monday
    meeting.
    → User Manual, Chapter 4, sections 4.4 and 4.5
18. Decide on timesheet approval; agree on a weekly deadline.
    → User Manual, Chapter 5, section 5.3

### Week 4

19. Invite remaining users; name pilot users as first contacts.
20. Save report views and schedule exports.
    → User Manual, Chapter 6
21. Set up integrations: tokens or OAuth apps; payroll via
    `GET /v2/time-entries?approved=true`; chat tools via webhooks
    `ticket.created` and `booking.conflict`.
    → *API: Overview and Authentication*, *API: Webhooks*
22. Choose a plan before day 30.
    → *Administration: Billing and Plans*

## Data migration shortcuts

| Source | Method |
|---|---|
| Project list | `POST /v2/projects`, or manually below 20 projects |
| Ticket tool | CSV import |
| Time tracking | `POST /v2/time-entries` with `Idempotency-Key` |
| Staff list | Email invitations, or just-in-time provisioning with single sign-on (Business) |

Historical time entries need the project set to *Active* during the import.

## Things that are easy to miss

- SLA timers, MFA enforcement and timesheet approval are **off by default**.
- The trial ends in a **read-only** state, not in deletion; deletion follows
  90 days after the end (*Data Retention and Deletion*).
- Business customers with 100 or more users can book **two free 90-minute
  onboarding sessions** through support.
- Resource planning and timesheet approval are not available on Starter; an
  organisation that tests them in the trial and then chooses Starter loses
  access to them.

## Generation notes

- 22 action items were extracted; the checklist at the end of the Onboarding
  Guide has 9 lines because it groups several items.
- Cross-references to section numbers were added automatically and checked
  against the headings of the User Manual.
