# Haldrum — Frequently Asked Questions

*Maintained by Quillmoor Customer Support. Applies to Haldrum 4.3.*

## Accounts and access

**I forgot my password. What do I do?**
Use *Forgot password* on the sign-in page; the link is valid for 60 minutes.
If your organisation uses single sign-on, contact your own IT department
instead.

**I lost the phone with my authenticator app.**
Sign in with one of your recovery codes. If you have none left, ask an
Administrator of your organisation to reset MFA for you. Quillmoor Support
cannot reset MFA on behalf of an organisation that still has an
Administrator. See *Administration: Authentication and Single Sign-On*.

**Why was I signed out?**
Sessions end after 12 hours without activity by default. On the Business
plan your Administrator may have set a different value between 1 and 72
hours.

**Our Owner has left the company. Who can change the plan?**
If the Owner cannot transfer ownership, contact support with written
confirmation from your management. See *Administration: Users and Roles*.

## Roles

**How do I set up the Resource Manager role?**
An Administrator opens *Settings > Users*, selects the person and chooses
*Resource Manager*. The role exists since Haldrum 4.2 and requires the Team
or Business plan. It lets planners manage bookings in all projects without
giving them Administrator rights.

**Do Guests cost money?**
No. Guests are free and unlimited on every plan. They see only the projects
they are invited to and cannot log time.

**Can a Project Lead see hourly rates?**
Yes, for the projects they lead. Members never see rates.

## Projects and exports

**How do I export project data?**
Open the project and go to *Settings > Export*, then choose CSV (a ZIP
archive with one file per object type) or JSON (one file). Through the API,
use `GET /v2/projects/{project_id}/export?format=csv` or `format=json`,
available since Haldrum 4.2. Project Leads of the project, Administrators and
the Owner may export. The export works on every plan. Details: User Manual,
Chapter 2, section 2.7, and *API: Projects and Tickets*.

**Can I get a report by email every Monday?**
Yes, since Haldrum 4.3 with scheduled exports on Team and Business. Save the
report as a view, click *Schedule export* and choose *weekly*. Recipients
must be users of your organisation. See User Manual, Chapter 6.

**I deleted a project by mistake.**
Deleted projects stay in the trash for 30 days. An Administrator can restore
them under *Projects > Trash*. After 30 days they are gone.

## Resources and bookings

**Why can't I save a booking?**
Since Haldrum 4.3, bookings that would push someone above 100 % of their
capacity, or that fall on an absence, are blocked for Project Leads. A
Resource Manager can override the conflict with a reason. Through the API
you receive `409 Conflict` with the code `booking_conflict`. See User
Manual, Chapter 4, section 4.5.

**How do I find someone with a particular skill?**
In *Resources > Planner*, filter by skill. Through the API, use
`GET /v2/resources?skill=network` (since 4.3).

**The heatmap shows 50 % for a day nobody works on.**
Tentative bookings count at 50 %. Check the striped entries in the planner.

## Time tracking

**I submitted my timesheet with a mistake.**
If it is not yet approved, ask your Project Lead or Resource Manager to
reject it; you can then edit it. If it is approved, a Resource Manager or
Administrator must reopen it, with a reason.

**Who approves my timesheet?**
The Project Lead of each project you logged time on approves that part. A
Resource Manager can approve the whole week.

**Can I log time on a completed project?**
No. Only *Active* projects accept time entries.

## API and integrations

**Which API version should I use?**
API v2. API v1 was deprecated with Haldrum 4.2 and will be switched off on
31 December 2026. See *API: Overview and Authentication* for the
differences.

**I get HTTP 429.**
You have reached the rate limit of your plan: 60 requests per minute on
Starter, 120 on Team, 600 on Business. Wait the seconds given in the
`Retry-After` header.

**I get HTTP 403 when creating a ticket through the API.**
Either your token has the `read` scope, or your organisation is on Starter,
where the API is read-only.

**Our webhook stopped receiving events.**
Webhooks are disabled after three days of failed deliveries. Check the
delivery log under *Settings > Developer > Webhooks*, fix the endpoint and
enable the webhook again. See *API: Webhooks*.

## Billing

**What does Haldrum cost?**
Starter €9, Team €19, Business €34 per paid user per month, billed
annually. Monthly billing costs 15 % more. See *Administration: Billing and
Plans*.

**Do deactivated users cost money?**
Not from the next billing period.

**Is there a discount for nonprofits?**
Yes, 40 % on Team and Business for registered nonprofits and schools.

## Data and security

**Where is our data stored?**
In the EU: primary region Frankfurt, backups in Dublin. See *Data Protection
Policy*.

**What happens to our data if we cancel?**
The organisation stays read-only for 30 days after the paid period so you
can export, and is deleted 90 days after the end of the contract. See
*Data Retention and Deletion*.

**How do we report a security issue?**
Write to security@quillmoor.example. See *Information Security Policy*.

## Support

**How fast does support answer?**
It depends on severity and plan. On Business, P1 incidents receive a
response within 1 hour, 24/7. See *Support Process*.

**Where do I see outages?**
On `status.haldrum.example`.
