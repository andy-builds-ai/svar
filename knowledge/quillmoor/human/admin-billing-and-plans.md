# Haldrum Administration: Billing and Plans

*Applies to Haldrum 4.3. Only the Owner of an organisation can change the
plan or payment details; Administrators can view the current plan.*

## The three plans

All prices are per paid user per month, billed annually, excluding VAT.
Billing monthly instead of annually costs 15 % more.

| | Starter | Team | Business |
|---|---|---|---|
| Price | €9 | €19 | €34 |
| Paid users | up to 25 | unlimited | unlimited |
| Guests | free, unlimited | free, unlimited | free, unlimited |
| Modules | Projects, Tickets, Time, Reports | all five | all five |
| Resource Manager role | — | yes | yes |
| Ticket SLA timers | — | yes | yes |
| Timesheet approval | — | yes | yes |
| Scheduled exports | — | up to 20 | up to 100 |
| Storage | 10 GB | 100 GB | 1 TB |
| API | read-only, 60 requests/minute | read and write, 120 requests/minute | read and write, 600 requests/minute |
| OAuth apps | — | — | yes |
| SAML single sign-on | — | — | yes |
| Configurable session timeout | — | — | yes |
| Audit log | — | — | yes, 400 days |
| Sandbox organisation | — | — | yes |
| Uptime target (SLA) | none | 99.5 % | 99.9 % |
| Support | standard | standard, plus in-app chat | priority, 24/7 for P1 |

MFA enforcement, CSV import, project export and the Reports module are
available on every plan.

## Trial

Every new organisation starts with a **30-day trial** with Team features and
up to 50 users. No payment details are needed to start. At the end of the
trial, the Owner chooses a plan. If no plan is chosen, the organisation
becomes read-only for another 30 days, during which data can still be
exported, and is then deleted as described in *Data Retention and Deletion*.

## What counts as a paid user

A paid user is any active user with a role other than Guest: Owner,
Administrator, Resource Manager, Project Lead or Member. Deactivated users
are not counted from the next billing period.

On annual billing, users added during the year are charged pro rata for the
remaining months on the next invoice. Users removed during the year are not
refunded, but the seat stays available for someone else until renewal.

## Changing the plan

**Upgrading** (Starter to Team, Team to Business) takes effect immediately.
The difference is charged pro rata.

**Downgrading** takes effect at the end of the current billing period.
Before the downgrade, Haldrum lists what will stop working, for example:

- Business to Team: single sign-on is switched off, and users are asked to
  set a password; the audit log stops recording, but existing entries can be
  exported until the downgrade date; OAuth apps stop working.
- Team to Starter: the Resources module is hidden and bookings are kept
  read-only; Resource Managers become Members; scheduled exports are paused;
  API tokens lose write access; organisations with more than 25 paid users
  cannot downgrade until they have deactivated users.

No data is deleted by a downgrade. If you upgrade again, hidden data
becomes visible again.

## Payment

Annual plans can be paid by credit card or by bank transfer against invoice.
Monthly plans require a credit card. Invoices are issued in EUR and sent to
the billing email address set by the Owner; they are also available under
*Settings > Billing > Invoices*.

Payment is due 14 days after the invoice date. If a payment is 30 days
overdue, the Owner and Administrators are notified; after 45 days the
organisation becomes read-only until payment is received.

## Cancelling

The Owner cancels under *Settings > Billing > Cancel subscription*. The
subscription runs until the end of the paid period. After that the
organisation is read-only for 30 days so data can be exported, and deleted
90 days after the end of the contract. The full timeline is in *Data
Retention and Deletion*.

## Nonprofit and education discount

Registered nonprofits and schools receive 40 % off Team and Business. The
discount is requested through support@quillmoor.example with proof of
status, and applies from the next invoice.

## Sandbox organisation

Business customers can create one **sandbox organisation** under
*Settings > Billing > Sandbox*. It is a free copy of the organisation's
settings and users, without project data, for testing rules, webhooks,
single sign-on changes and API integrations. Sandboxes have the same API
rate limit as Business but only 10 GB of storage, and are deleted after 90
days without a sign-in.

## Questions about invoices

Contact support@quillmoor.example with the invoice number. Billing questions
are handled as severity P3 in the *Support Process*.
