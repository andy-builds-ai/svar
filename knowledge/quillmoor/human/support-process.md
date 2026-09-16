# Quillmoor Customer Support — Support Process

*Owner: Priya Oduya, Head of Customer Support. Applies to all Haldrum
customers. Last updated 4 August 2026.*

This document describes how customers reach Quillmoor Support, how we rate
the severity of a case, how quickly we respond and how cases are escalated.
The contractual uptime commitments are in the *Service Level Agreement*.

## 1. Channels

| Channel | Plans | Use for |
|---|---|---|
| Email: support@quillmoor.example | all | everything |
| In-app chat | Team, Business | questions while working in Haldrum |
| P1 phone line (number in *Settings > Billing > Support*) | Business | P1 incidents outside business hours |
| Status page: `status.haldrum.example` | all | known outages and maintenance |

Business hours are **Monday to Friday, 08:00 to 18:00 CET**, excluding public
holidays in Saxony, Germany.

Support cases can be opened by any user, but only Administrators and the
Owner can request changes to the organisation, such as an ownership transfer
or restoring deleted data.

## 2. Severity levels

The severity describes the impact on the customer, not how urgent the
question feels. Support staff may adjust the severity chosen by the customer
and explain why.

| Severity | Definition | Examples |
|---|---|---|
| **P1 — Critical** | Haldrum is unavailable or unusable for the whole organisation, or customer data is at risk. | Nobody can sign in; data visible to the wrong organisation. |
| **P2 — High** | A core function does not work for many users and there is no workaround. | Time entries cannot be saved; webhooks are not delivered for all customers. |
| **P3 — Normal** | A function does not work as documented, but a workaround exists, or a question about use, billing or configuration. | A report shows wrong totals; invoice question. |
| **P4 — Low** | General questions, feature requests, cosmetic problems. | "Can boards have custom colours?" |

## 3. Response times

Response time is the time until a support engineer replies with a first
assessment — not until the problem is solved.

| Severity | Standard support (Starter, Team) | Priority support (Business) |
|---|---|---|
| P1 | 4 business hours | **1 hour, 24/7** |
| P2 | 1 business day | 4 business hours |
| P3 | 2 business days | 1 business day |
| P4 | 5 business days | 3 business days |

For P1 cases we give an update at least every 2 hours until the service is
restored, and publish the incident on the status page.

## 4. What we need from you

Cases are solved faster with:

- the organisation name and the affected user's email address,
- what you did, what you expected and what happened,
- screenshots, ticket numbers such as `T-1042`, or project codes,
- for API problems: the endpoint, the HTTP status and the `request_id` from
  the error response (*API: Overview and Authentication*),
- for webhook problems: the event `id`.

Never send passwords, MFA recovery codes or full API tokens. Support will
never ask for them.

## 5. Access to your data

Support engineers do not look into your organisation by default. If a case
needs it, the engineer asks for permission in the case. Access is granted for
at most **72 hours** and is recorded in the audit log for Business customers.
See *Data Protection Policy*, section 6.

## 6. Lifecycle of a case

1. **New** — the case is received and a number is assigned (`QS-` followed
   by digits).
2. **Triaged** — severity checked, engineer assigned.
3. **In progress** — being worked on.
4. **Waiting for customer** — we need information. After 7 days without a
   reply we send a reminder; after 14 days the case is closed.
5. **Solved** — a solution or answer was given. Replying within 7 days
   reopens the case.
6. **Closed.**

Customers receive a short satisfaction survey after a case is solved.

## 7. Escalation

If a case is not progressing as expected, customers can ask for escalation
by replying with "escalate" in the case.

| Level | Who | When |
|---|---|---|
| 1 | Support engineer | Default |
| 2 | Support team lead | On customer request, or when a P1/P2 response time is missed |
| 3 | Head of Customer Support, Priya Oduya | P1 open longer than 4 hours; repeated escalation |
| 4 | CTO, Tobias Wrenfield | P1 open longer than 8 hours, or a security-related incident |

Security incidents are handed to the Head of Security and follow the
*Information Security Policy*.

## 8. Bugs and feature requests

- Confirmed **bugs** are passed to the product team with the case number.
  The customer is informed when a fix is released; fixes are listed in the
  release notes.
- **Feature requests** are recorded with the customer's use case. The Head
  of Product, Samuel Okonkwo-Reed, reviews them every quarter. We do not
  promise dates for features in support cases.

## 9. Special requests

| Request | Who may ask | How |
|---|---|---|
| Restore a project deleted more than 30 days ago | not possible | Data is permanently deleted after the trash period. |
| Restore data from backup | Owner | Only for data loss caused by Quillmoor; see *Data Retention and Deletion*. |
| Ownership transfer when the Owner is unavailable | Administrator, with written confirmation from management | Handled as P3. |
| MFA reset when no Administrator is left | Management of the customer, in writing | Handled as P2. |
| Nonprofit discount | Owner | Proof of status; handled as P3. |

## 10. Onboarding sessions

Business customers with 100 or more users receive two free onboarding
sessions of 90 minutes, scheduled within 10 business days of the request.
See the *Onboarding Guide*.
