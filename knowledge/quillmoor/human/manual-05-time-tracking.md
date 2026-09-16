# Haldrum User Manual — Chapter 5: Time Tracking

*Applies to Haldrum 4.3. Available on all plans; timesheet approval requires
the Team or Business plan.*

Time tracking records what was actually worked. Together with bookings
(Chapter 4) it shows whether plans and reality match, and it is the basis
for billing customers and for the *Planned vs. actual* report (Chapter 6).

## 5.1 Time entries

A **time entry** has:

- a date and a duration (hours and minutes),
- a project, and optionally a task or a ticket,
- a short description,
- a **billable** flag, set by default according to the project settings.

Members, Project Leads, Resource Managers, Administrators and the Owner can
log time. Guests cannot.

Ways to log time:

1. In *Time > My week*, type hours into the grid.
2. From a ticket, with *Log time* (Chapter 3, section 3.6).
3. With the **timer**: click the clock icon in the top bar, choose the
   project, and stop the timer when you are done. The timer rounds to the
   nearest minute.
4. Through the API with `POST /v2/time-entries` (see *API: Resources,
   Bookings and Time*).

Time can only be logged on projects in status *Active*. Projects that are
*On hold*, *Completed* or *Archived* block new entries (Chapter 2, section
2.2).

## 5.2 Weekly timesheets

All time entries of one person for one calendar week form a **timesheet**.
A timesheet has one of four states:

| State | Meaning |
|---|---|
| Open | The person is still entering time. |
| Submitted | The person has submitted the week for approval. Entries are locked for the person. |
| Approved | The week was approved. Entries are locked for everyone. |
| Rejected | The week was sent back with a comment. Entries are editable again. |

Haldrum reminds people on Friday at 15:00 in their own time zone to submit
their timesheet. Organisations that do not use approval can switch
timesheets off; entries then stay editable for 60 days.

## 5.3 Timesheet approval

Timesheet approval was introduced in Haldrum 4.2.

**Who approves:**

- A **Project Lead** approves the entries logged on projects they lead.
- A **Resource Manager** can approve any timesheet in the organisation.
- **Administrators** and the **Owner** can approve any timesheet as well.

A timesheet that contains entries from several projects is approved in
parts: each Project Lead approves their part, and the timesheet becomes
*Approved* once every part is approved. A Resource Manager can approve the
whole week at once.

**What approval changes:**

- Entries are **locked**. Nobody can edit or delete them, including
  Administrators.
- The hours count as *actual* in reports.
- The webhook event `timesheet.approved` is sent (see *API: Webhooks*).

**Correcting an approved week:** a Resource Manager or Administrator can
**reopen** an approved timesheet. Reopening requires a reason and is recorded
in the audit log on the Business plan. The person then edits the entries and
submits again.

## 5.4 Billable time and rates

Each project defines whether its time is billable by default and which
hourly rate applies. Rates can be set per project, per role or per person;
the most specific rate wins (person over role over project). Rates are only
visible to Project Leads, Resource Managers, Administrators and the Owner.

## 5.5 Time and bookings

Time entries do not change bookings, and bookings do not create time
entries. The comparison happens in reports: *Planned vs. actual* shows
booked hours next to logged hours per person and project, so deviations are
visible without anyone having to reconcile two lists by hand.

## 5.6 Exporting time

Time entries can be exported under *Reports > Time entries* as CSV or XLSX,
or as part of a full project export (Chapter 2, section 2.7). Scheduled
exports that arrive by email every week or month are described in Chapter 6.

## 5.7 Common mistakes

- **Logging on the wrong project.** Before submitting, check the weekly
  grid; after approval, only a reopen can fix it.
- **Forgetting the timer.** A running timer stops automatically after 12
  hours and marks the entry for review.
- **Submitting too early.** Submitting on Wednesday locks Thursday and
  Friday as well. Submit at the end of the week.
