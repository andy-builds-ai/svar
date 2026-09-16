# Haldrum Data Retention and Deletion

*Owner: Jonas Merritt-Kahl, Data Protection Officer, together with the
operations team. Applies to Haldrum 4.3. Last updated 4 August 2026.*

This document states how long Haldrum keeps data, what happens when
something is deleted, and what happens when a subscription ends. The legal
framework is described in the *Data Protection Policy*.

## 1. Data a customer deletes

| Object | What happens | Recoverable |
|---|---|---|
| Project | Moved to the trash with its tasks, tickets, files and time entries. | 30 days, by an Administrator or the Owner under *Projects > Trash* |
| Ticket | Moved to the trash. | 30 days |
| File | Moved to the trash. Previous versions (up to 20) go with it. | 30 days |
| Comment | Deleted immediately; the history shows "comment deleted". | no |
| Time entry | Deleted immediately. Approved entries cannot be deleted. | no |
| Booking | Deleted immediately; a line remains in the audit log on Business. | no |
| User | Users are deactivated, not deleted, once they have created content. See *Administration: Users and Roles*. | reactivation at any time |

After the 30-day trash period, the objects are permanently deleted from the
live database. They remain in backups until those backups expire (section
3).

Emptying the trash manually deletes its contents permanently at once.

## 2. Data a customer keeps

As long as a subscription is active, Haldrum keeps all data without a time
limit, except:

- **Audit log entries** (Business): 400 days.
- **Webhook delivery logs:** 30 days.
- **Export download links:** 7 days; the export can be created again at
  any time.
- **Sign-in history** shown in the user's profile: 90 days.
- **Idempotency keys** for API requests: 24 hours.

## 3. Backups

- Continuous backups allow a point-in-time restore for the last **7 days**.
- Daily full backups are kept for **35 days**.
- Backups are encrypted with AES-256 and stored in Dublin, separate from the
  primary region in Frankfurt.

Backups exist to recover from failures on Quillmoor's side. **We do not
restore individual objects from backups on customer request**, because a
restore affects the whole organisation and would overwrite changes made
since. If data was lost through an error of Quillmoor Systems, the Owner
can request a restore through support (*Support Process*, section 9).

## 4. End of a subscription

The timeline after a subscription ends — through cancellation, a trial
ending without a plan, or termination for non-payment:

| Day | State |
|---|---|
| 0 | Paid period (or trial) ends. The organisation becomes **read-only**. Users can still sign in, view and export everything. |
| 30 | Sign-in is disabled. The Owner can still request a full export through support until day 90. |
| 90 | All organisation data is **permanently deleted** from the live database. |
| up to 125 | The data has expired from all backups (90 + 35 days). |

The Owner receives email reminders on day 0, day 25 and day 80.

To keep data, use the project export (User Manual, Chapter 2, section 2.7,
or `GET /v2/projects/{project_id}/export`) for every project before day 30,
and report exports for organisation-wide data.

A subscription renewed before day 90 restores the organisation fully.

## 5. Deletion on request

An Owner who wants the organisation deleted before the end of the timeline
can use *Settings > Organisation > Delete organisation*. This requires
confirmation by email and takes effect after 7 days, during which it can be
cancelled. Data then follows the timeline from day 90, meaning it is removed
from the live database at once and from backups within 35 days.

A written confirmation of deletion is available on request from
privacy@quillmoor.example.

## 6. Data we keep after deletion

After an organisation is deleted, Quillmoor Systems keeps only what the law
requires or what does not identify the customer's employees:

- **Invoices and billing records:** 10 years, as required by German
  commercial and tax law.
- **Support cases:** 2 years after closing, then anonymised.
- **Aggregated usage statistics** without personal data, for example the
  number of tickets per month across all customers.

## 7. Individual people

When an employee of a customer asks to be erased, the customer as controller
decides. Administrators can **anonymise** a deactivated user under
*Settings > Users > Anonymise*. The name is replaced with "Former user" and a
number, the email address is removed, and the user's content stays in
projects so that history and reports remain correct. Anonymisation cannot be
undone.

## 8. Summary of key periods

| Period | Length |
|---|---|
| Trash | 30 days |
| Point-in-time restore | 7 days |
| Daily backups | 35 days |
| Read-only after subscription end | 30 days |
| Deletion after subscription end | 90 days |
| Audit log (Business) | 400 days |
| Invoices | 10 years |
