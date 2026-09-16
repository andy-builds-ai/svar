# Quillmoor Systems — Data Protection Policy

*Version 3.2, effective 1 June 2026. Owner of this policy: Jonas
Merritt-Kahl, Data Protection Officer. Review cycle: yearly.*

## 1. Purpose and scope

This policy describes how Quillmoor Systems GmbH handles personal data in
Haldrum and in its own operations. It applies to every employee, contractor
and subprocessor of Quillmoor Systems. It complements the *Information
Security Policy*, which describes the technical and organisational measures,
and *Data Retention and Deletion*, which describes timelines.

The policy follows the EU General Data Protection Regulation (GDPR).

## 2. Roles under data protection law

- For **customer data in Haldrum** — projects, tickets, time entries,
  bookings, files and the user accounts of a customer's employees — the
  customer is the **controller** and Quillmoor Systems is the **processor**.
  We process this data only on the customer's documented instructions, as
  set out in the data processing agreement (DPA) that is part of every
  subscription.
- For **our own business data** — customer contacts for billing, website
  visitors, job applicants and our employees — Quillmoor Systems is the
  controller.

## 3. Categories of data in Haldrum

| Category | Examples |
|---|---|
| Account data | name, email address, role, time zone, language, MFA settings |
| Work data | projects, tasks, tickets, comments, attachments |
| Planning data | capacity, absences, skills, bookings |
| Time data | time entries, timesheets, approvals, hourly rates |
| Technical data | IP addresses, sign-in times, device and browser type, audit log entries |

Haldrum is not designed for special categories of personal data such as
health data. Absence types are limited to `holiday`, `sick`, `training` and
`other`, and do not record reasons or diagnoses. Customers must not upload
special category data in free-text fields or files unless they have a legal
basis and have informed Quillmoor Systems in writing.

## 4. Principles

We process personal data:

1. **lawfully and transparently**, on the basis of the contract with the
   customer or another legal basis we document;
2. **only for the stated purpose** — to provide, secure and support Haldrum;
3. **minimally** — we do not collect data Haldrum does not need;
4. **accurately** — customers can correct all data through the interface or
   the API;
5. **no longer than necessary** — see *Data Retention and Deletion*;
6. **securely** — see the *Information Security Policy*.

Quillmoor Systems does not sell customer data, does not use customer data
for advertising and does not use the content of customer projects, tickets
or files to train machine learning models.

## 5. Where data is stored

All customer data is stored and processed in the European Union. The
primary data centre region is **Frankfurt**; encrypted backups are kept in
**Dublin**. Support staff access customer data from within the EU only.

Subprocessors are listed at `quillmoor.example/subprocessors`. Customers are
informed by email at least **30 days** before a new subprocessor starts
processing their data and may object during that period. If an objection
cannot be resolved, the customer may terminate the affected subscription
without penalty.

## 6. Access by Quillmoor employees

Employees do not look at customer data by default. Access is granted only:

- when a customer asks for help in a support case and gives permission in
  that case (*Support Process*), limited to 72 hours;
- when needed to investigate a security incident, approved by the Head of
  Security;
- when legally required.

Every access is logged, and the log is available to Business customers in
their audit log.

## 7. Rights of data subjects

Employees of our customers may ask for access, correction, deletion,
restriction or portability of their data. Because the customer is the
controller, **we forward such requests to the customer's Owner within 5
business days** and support the customer in answering them, for example
through the project export and `GET /v2/users` in the API.

Requests about data for which Quillmoor Systems is the controller are
answered by the Data Protection Officer within one month.

## 8. Personal data breaches

If Quillmoor Systems becomes aware of a personal data breach affecting
customer data, it notifies the affected customers **without undue delay and
no later than 48 hours** after becoming aware, so that customers can meet
their own 72-hour notification duty towards the supervisory authority. The
notification contains what is known at the time: the nature of the breach,
the categories and approximate number of people affected, likely
consequences and measures taken. The incident process is described in the
*Information Security Policy*.

## 9. Data processing agreement

The DPA is part of the terms of service and does not need to be signed
separately. Customers who need a signed copy can download one under
*Settings > Billing > Legal documents* or request it from
privacy@quillmoor.example.

## 10. Employees and training

Every employee signs a confidentiality agreement and completes data
protection training when joining and once a year afterwards. Completion is
tracked by the People team. Employees with access to production systems
complete an additional security training (*Information Security Policy*).

## 11. Contact

Data Protection Officer: Jonas Merritt-Kahl, privacy@quillmoor.example.

## 12. Changes to this policy

| Version | Date | Change |
|---|---|---|
| 3.0 | 1 March 2025 | Rewritten after the move of backups to Dublin. |
| 3.1 | 16 March 2026 | Added planning data (absences, skills) for the Resource Manager role in Haldrum 4.2. |
| 3.2 | 1 June 2026 | Breach notification to customers shortened from 72 to 48 hours. |
