# Haldrum User Manual — Chapter 6: Reports and Exports

*Applies to Haldrum 4.3.*

Reports turn the data from projects, tickets, bookings and time entries into
answers. This chapter lists the built-in reports, explains how to export
data, and describes scheduled exports, which were added in Haldrum 4.3.

## 6.1 Who sees which reports

Reports respect permissions. A Member sees reports about their own work and
the projects they belong to. Project Leads see everything about the projects
they lead. Resource Managers, Administrators and the Owner see reports for
the whole organisation. Reports that show hourly rates or revenue are hidden
from Members.

## 6.2 Built-in reports

| Report | Module | What it answers |
|---|---|---|
| Project status | Projects | Which projects are on track, late or over budget? |
| Planned vs. actual | Projects, Resources, Time | How do booked hours compare with logged hours, per person and project? |
| Ticket volume | Tickets | How many tickets were created, resolved and closed per week? |
| Ticket SLA | Tickets | Which tickets breached their first-response or resolution target? (Team and Business) |
| Utilisation | Resources | What share of each person's capacity is booked and logged? (Team and Business) |
| Overbooking | Resources | Where is someone booked above 100 % of capacity? (Team and Business) |
| Time entries | Time | Every entry, filterable by person, project, date and billable flag |
| Unsubmitted timesheets | Time | Who has not submitted last week? |

Every report can be filtered by date range, project, person and — where it
makes sense — ticket category or skill. Filters can be saved as a **view**
and shared with the organisation.

## 6.3 Exporting a report

Open any report and click *Export*. Available formats:

- **CSV** — plain data, one row per record, UTF-8 encoded, comma separated.
- **XLSX** — the same data as a spreadsheet, with column headers formatted.
- **PDF** — the report as shown on screen, including charts. Useful for
  sharing with people outside Haldrum.

Exports contain only the data you are allowed to see.

## 6.4 Exporting a whole project

To export all data of a single project — tasks, tickets, bookings, time
entries and files — use the project export described in Chapter 2, section
2.7, or the API endpoint `GET /v2/projects/{project_id}/export`. The project
export is the right tool when data has to move to another system or be kept
after a contract ends. A report export is the right tool when someone needs
numbers.

## 6.5 Scheduled exports

Since Haldrum 4.3, any saved report view can be sent automatically as an
export by email.

1. Open the report and save your filters as a view.
2. Click *Schedule export*.
3. Choose the format (CSV, XLSX or PDF), the frequency (daily, weekly on a
   chosen weekday, or monthly on the first day of the month) and the time of
   day.
4. Add recipients. Recipients must be users of the organisation; external
   email addresses are not allowed, so that data only reaches people with a
   Haldrum account.

Each export is generated with the permissions of the person who created the
schedule. If that person loses access — for example because their role
changes or their account is deactivated — the schedule is paused and the
Administrators are notified.

Organisations can have up to 20 scheduled exports on the Team plan and up
to 100 on the Business plan. Scheduled exports are not available on
Starter.

Attachments larger than 20 MB are not sent by email; the email then contains
a download link valid for 7 days instead.

## 6.6 Reports through the API

Report data is not a separate API resource. Build what you need from the
underlying endpoints — `GET /v2/time-entries`, `GET /v2/tickets`,
`GET /v2/resources/{resource_id}/availability` — or use the project export.
The endpoints are listed in *API: Projects and Tickets* and *API: Resources,
Bookings and Time*.

## 6.7 Dashboards

A dashboard is a page with up to twelve report widgets. Each user has a
personal dashboard; Administrators can publish dashboards for the whole
organisation. Dashboards refresh every 15 minutes, or immediately with the
refresh button.

## 6.8 Accuracy notes

- Reports are calculated in the organisation's default time zone, set by
  Administrators under *Settings > Organisation*.
- *Planned vs. actual* counts only **approved** time as actual when
  timesheet approval is switched on (Chapter 5).
- Tentative bookings count at 50 % in *Utilisation*, as in the heatmap
  (Chapter 4, section 4.6).
- Data deleted from the trash disappears from reports, including past
  periods.
