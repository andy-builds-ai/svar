# Haldrum User Manual — Chapter 2: Projects

*Applies to Haldrum 4.3.*

A project in Haldrum is a piece of work with a beginning and an end: a
customer implementation, an office move, a software release. Projects hold
milestones, tasks, files and the people who work on them. Tickets (Chapter
3), bookings (Chapter 4) and time entries (Chapter 5) can all be linked to a
project.

## 2.1 Who can do what

- **Project Leads** create projects and manage the projects they lead.
- **Administrators** and the **Owner** can manage every project.
- **Resource Managers** can see every project, but change only bookings.
- **Members** work inside the projects they belong to.
- **Guests** see only the projects they were invited to, read-only, and may
  comment on tickets.

The complete permission list is in *Administration: Users and Roles*.

## 2.2 Creating a project

1. Go to *Projects* and click *New project*, or press `c`.
2. Enter a name and an optional project code, for example `ONB-2026`. The
   code appears in reports and exports.
3. Choose a **template** or start empty. Templates copy milestones, task
   lists and ticket categories, but never files or people.
4. Set a start date and a planned end date.
5. Pick the Project Lead. By default this is you.
6. Click *Create*.

The project is created in status **Planned**. It becomes **Active** when the
first milestone starts or when the Project Lead sets it manually.

### Project statuses

| Status | Meaning |
|---|---|
| Planned | Created, work has not started. |
| Active | Work is going on. Time can be logged. |
| On hold | Paused. Time entries are blocked, bookings stay. |
| Completed | Finished. Time entries are blocked, the project is read-only except for comments. |
| Archived | Hidden from lists and search by default. Can be restored. |

## 2.3 Milestones and tasks

Milestones are dates that matter: a go-live, a delivery, a review. Each
milestone can hold task lists. A task has an assignee, a due date and an
estimate in hours. Estimates feed the *Planned vs. actual* report in Chapter
6.

Tasks can depend on each other. If task B depends on task A, moving A's due
date shows a warning on B. Haldrum does not move dependent dates
automatically; the Project Lead decides.

## 2.4 Project members

Open the project, go to *Settings > Members* and add people. Adding someone
to a project does **not** book their time. Booking time is a separate step
in Resource Planning (Chapter 4), because being on a project and having
hours for it are two different things.

External people, such as a customer's contact person, can be invited as
**Guests**. They receive an email invitation and see only this project.

## 2.5 Files

Files can be attached to the project, a milestone, a task or a ticket. The
largest single file is 250 MB. The storage for the whole organisation
depends on the plan: 10 GB on Starter, 100 GB on Team, 1 TB on Business.
When you upload a file with the same name again, Haldrum keeps the previous
version; the last 20 versions are kept per file.

## 2.6 Archiving and deleting

**Archiving** hides a completed project from everyday lists. Archived
projects keep all data and can be restored at any time under *Projects >
Filter > Archived*. Archiving can also be triggered through the API with
`POST /v2/projects/{project_id}/archive`.

**Deleting** moves a project to the trash. Projects in the trash can be
restored for **30 days**; after that they are deleted permanently, including
tickets, files and time entries linked only to that project. Only
Administrators and the Owner can delete projects. The rules for what happens
to data after deletion are in *Data Retention and Deletion*.

## 2.7 Exporting a project

Any project can be exported, at any time, by its Project Lead, an
Administrator or the Owner. Open the project and go to *Settings > Export*.
You can choose:

- **CSV** — one file per object type (tasks, tickets, time entries,
  bookings), packed as a ZIP archive.
- **JSON** — a single file with the full project structure, suitable for
  importing into another system.

Files attached to the project are included as a separate ZIP download.
Exports of large projects are prepared in the background; you receive a
notification with a download link, valid for 7 days.

The same export is available through the API as
`GET /v2/projects/{project_id}/export?format=csv` or `format=json` (added in
Haldrum 4.2, see *API: Projects and Tickets*). Organisation-wide reports and
scheduled exports are covered in Chapter 6.

## 2.8 Project templates

Administrators and Project Leads can save any project as a template under
*Settings > Save as template*. Templates are shared across the organisation.
A good template contains the milestones and ticket categories you use every
time, and nothing that belongs to a single customer.
