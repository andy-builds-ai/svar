# Quillmoor Systems — Information Security Policy

*Version 5.0, effective 1 January 2026. Owner of this policy: Lena
Fairholm, Head of Security. Approved by Tobias Wrenfield, CTO. Review cycle:
yearly, and after every P1 security incident.*

## 1. Purpose

This policy sets out how Quillmoor Systems protects the confidentiality,
integrity and availability of Haldrum and the data our customers entrust to
it. It is the top-level document of our information security management
system, certified to ISO/IEC 27001 since 2021. The handling of personal data
is described in the *Data Protection Policy*.

## 2. Responsibilities

- **CTO** — accountable for security of the platform.
- **Head of Security** — owns this policy, runs risk assessments, leads
  incident response.
- **Team leads** — make sure their teams follow the policy.
- **Every employee** — follows the policy and reports suspected incidents
  immediately to security@quillmoor.example or the internal security
  channel.

## 3. Asset management and classification

All systems, repositories and data stores are recorded in an asset register
with an owner. Information is classified in four levels:

| Level | Examples | Handling |
|---|---|---|
| Public | Website, help centre, release notes | No restriction |
| Internal | Internal wiki, meeting notes | Employees only |
| Confidential | Contracts, financial data, source code | Need-to-know |
| Customer data | Everything customers store in Haldrum | Strictly need-to-know, access logged |

## 4. Access control

- Access follows **least privilege** and is granted by role.
- Production access requires a hardware security key and is granted per
  request for at most **8 hours**. Standing production access does not
  exist.
- Access to customer data follows section 6 of the *Data Protection
  Policy*.
- Access rights are reviewed every **quarter** by team leads and the Head of
  Security.
- Accounts of leaving employees are disabled on their last working day,
  at the latest by 18:00.

## 5. Encryption

- **In transit:** TLS 1.2 or higher for all external connections, including
  the API and webhooks. Older protocols are rejected.
- **At rest:** customer data, backups and files are encrypted with AES-256.
- **Keys** are managed in a dedicated key management service, rotated
  yearly, and never stored alongside the data they protect.
- **Secrets** such as API signing keys are kept in a secret store, never in
  source code or configuration files.

## 6. Secure development

- Every change to Haldrum is reviewed by at least one other engineer before
  it is merged.
- Automated tests, dependency checks and static analysis run on every
  change; a failing check blocks the merge.
- Dependencies with known critical vulnerabilities are updated within
  **7 days**, high within **30 days**.
- An independent firm performs a **penetration test once a year** and after
  major architectural changes. Findings rated critical or high are fixed
  before the report is closed.
- Customer data is never used in development or test environments.

## 7. Operations

- Production runs in the EU, primary region Frankfurt, backups in Dublin.
- **Backups** of all databases are taken continuously and allow
  point-in-time restore for the last **7 days**; daily full backups are kept
  for **35 days**. A restore is tested every month.
- Recovery objectives: **RPO 15 minutes**, **RTO 4 hours** for the full
  platform.
- Systems are monitored 24/7. The on-call engineer is paged within 5
  minutes of a critical alert.
- Operating systems and runtime images are patched at least monthly.

## 8. Incident response

A security incident is any event that threatens the confidentiality,
integrity or availability of Haldrum or customer data.

1. **Detect and report.** Anyone who notices a possible incident reports it
   immediately. It is better to report ten false alarms than to miss one.
2. **Triage.** The on-call engineer and the Head of Security assess the
   severity within 1 hour. Security incidents are rated with the same P1–P4
   scale as support cases (*Support Process*); any incident affecting
   customer data is at least P2.
3. **Contain.** Limit the damage: revoke credentials, isolate systems, sign
   out sessions.
4. **Notify.** Personal data breaches are reported to affected customers
   within **48 hours**, as described in the *Data Protection Policy*. Service
   impact is published on `status.haldrum.example`.
5. **Recover.** Restore normal operation, from backup if necessary.
6. **Learn.** Every P1 and P2 incident receives a written post-incident
   review within 10 business days. Business customers affected by a P1
   receive a summary.

## 9. Suppliers

Suppliers with access to customer data are assessed before contract
signature and yearly afterwards, and must provide equivalent security
commitments. The list of subprocessors is public (*Data Protection Policy*,
section 5).

## 10. Employees

- Background checks, as permitted by law, before employees receive
  production access.
- Security awareness training when joining and yearly; phishing simulations
  every quarter.
- Company laptops use full-disk encryption, automatic screen lock after 5
  minutes and managed updates.
- Customer data must not be copied to personal devices, email or chat.

## 11. Responsible disclosure

Security researchers can report vulnerabilities to security@quillmoor.example.
We confirm receipt within 2 business days, do not take legal action against
good-faith research that respects customer data, and publish a
thank-you list with the researcher's consent.

## 12. Customer-side controls

Haldrum gives customers controls they are responsible for using: enforced
MFA, SAML single sign-on, session timeout, API token lifetime and the audit
log. They are described in *Administration: Authentication and Single
Sign-On*.

## 13. Exceptions

Exceptions to this policy require written approval by the Head of Security,
are limited to at most 90 days and are recorded in the risk register.
