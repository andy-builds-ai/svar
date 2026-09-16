# Quillmoor Systems — Company Profile

*Fictional company, written for the Andur demo. Reference document: where
another document in this collection states a version, a price, a role or a
name, it follows this profile.*

## Who we are

Quillmoor Systems GmbH builds and runs **Haldrum**, a cloud platform for
workflow and resource planning in small and medium-sized companies. Haldrum
brings three things that usually live in three separate tools into one
place: projects, support tickets and the planning of people's time.

The company was founded in 2014 in Leipzig by Nora Ellison-Pike and Tobias
Wrenfield. Both had spent years in IT service companies where projects were
planned in one spreadsheet, tickets were handled in a second tool and nobody
could say on Monday morning who actually had time that week. Haldrum started
as an internal tool for a single agency and became a product in 2016.

As of September 2026 Quillmoor Systems has 86 employees and serves about
1,900 customer organisations in 14 countries. Most customers have between
10 and 250 users.

## Leadership and contacts

| Role | Name | Contact |
|---|---|---|
| CEO and co-founder | Nora Ellison-Pike | via office@quillmoor.example |
| CTO and co-founder | Tobias Wrenfield | via office@quillmoor.example |
| Head of Product | Samuel Okonkwo-Reed | via office@quillmoor.example |
| Head of Customer Support | Priya Oduya | support@quillmoor.example |
| Head of Security | Lena Fairholm | security@quillmoor.example |
| Data Protection Officer | Jonas Merritt-Kahl | privacy@quillmoor.example |
| Head of People | Carla Dunmore | people@quillmoor.example |

## The product: Haldrum

Haldrum is sold as a subscription and runs only as a cloud service. There is
no on-premises edition.

**Current version:** 4.3, released on 4 August 2026.
**Previous versions:** 4.2 (16 March 2026), 4.1 (1 December 2025),
4.0 (15 September 2025). **Next version:** 4.4, planned for November 2026.

Haldrum consists of five modules:

- **Projects** — plans, milestones, files and project members.
- **Tickets** — requests and incidents, boards, ticket SLA timers.
- **Resources** — people, skills, capacity and bookings.
- **Time** — time entries, weekly timesheets and their approval.
- **Reports** — built-in reports, exports and scheduled exports.

Addresses customers use every day:

- Web application: `app.haldrum.example`
- Help centre: `help.haldrum.example`
- API: `https://api.haldrum.example/v2`
- Status page: `status.haldrum.example`

## Roles in Haldrum

Every user in a Haldrum organisation has exactly one role. There are six:
**Owner**, **Administrator**, **Resource Manager** (since 4.2),
**Project Lead**, **Member** and **Guest**. Each organisation has exactly one
Owner. Guests are free and do not count as paid users. The full permission
description is in *Administration: Users and Roles*.

## Plans and prices

Prices are per paid user per month, billed annually. Monthly billing costs
15 % more.

| Plan | Price | Users | Highlights |
|---|---|---|---|
| Starter | €9 | up to 25 | Projects, Tickets, Time, Reports; API read-only (60 requests/minute); 10 GB storage |
| Team | €19 | unlimited | all five modules; Resource Manager role; API read and write (120 requests/minute); 100 GB storage; 99.5 % uptime target |
| Business | €34 | unlimited | everything in Team; SAML single sign-on; audit log; OAuth apps; API 600 requests/minute; 1 TB storage; sandbox organisation; priority support; 99.9 % uptime target |

Every new organisation starts with a 30-day trial with Team features. The
largest single file that can be uploaded is 250 MB on every plan.

## Where the data lives

Haldrum is hosted in the European Union. The primary data centre region is
Frankfurt; encrypted backups are kept in a second region in Dublin. Customer
data does not leave the EU. Details are in the *Data Protection Policy* and
the *Information Security Policy*. Quillmoor Systems has been certified to
ISO/IEC 27001 since 2021.

## Support

Support is reachable at support@quillmoor.example and, on Team and Business,
through the chat inside the application. Business hours are Monday to Friday,
08:00 to 18:00 CET. Business customers receive priority support, including a
24/7 response for P1 incidents. Response times and severity levels are
described in the *Support Process* and the *Service Level Agreement*.

## What we value

- **Plans that match reality.** A plan that ignores who is on holiday is a
  wish list. Haldrum was built around the question "who has time?".
- **Few tools, not many.** Projects, tickets and time belong together.
- **Boring reliability.** We release four times a year, not every day, and
  announce breaking changes at least six months in advance.
- **Data stays in the EU and stays the customer's.** Every project can be
  exported at any time, through the interface or the API.
