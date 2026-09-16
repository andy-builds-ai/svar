# Haldrum User Manual — Chapter 4: Resource Planning

*Applies to Haldrum 4.3. The Resources module is available on the Team and
Business plans.*

Resource planning answers the question Haldrum was built around: who has
time for what, and when? It connects people, their capacity and the
projects that need them.

## 4.1 Resources, capacity and skills

A **resource** in Haldrum is a person. Every user with the role Member or
higher is automatically a resource. Guests are not.

**Capacity** is how many hours a person can work per day. It starts with the
working hours the person entered in their profile (Chapter 1, section 1.4).
A Resource Manager can override it, for example for part-time employees, and
enter absences such as holidays and training days. Absences reduce capacity
to zero for those days.

**Skills** are tags such as "network", "accounting", "German" or "trainer".
Resource Managers maintain the list of skills and assign them to people.
Since Haldrum 4.3, you can search resources by skill, both in the interface
and through the API (`GET /v2/resources?skill=`).

## 4.2 Bookings

A **booking** reserves a number of hours per day of one person for one
project over a date range. Example: "Anna, project ONB-2026, 4 hours per
day, 5 to 16 October".

Who can book:

- **Resource Managers** can create, change and delete any booking.
- **Project Leads** can book people for projects they lead, as long as the
  booking fits within the person's free capacity.
- **Members** see their own bookings and cannot change them.

Create a booking under *Resources > Planner*: select a person's row, drag
across the days and choose the project. Bookings can also be created with
`POST /v2/bookings` (see *API: Resources, Bookings and Time*).

## 4.3 The Resource Manager role

The Resource Manager role was introduced in Haldrum 4.2. Before 4.2, only
Administrators could plan across all projects, which meant giving planners
far more rights than they needed. A Resource Manager:

- sees every project and every person's bookings,
- creates, changes and deletes bookings in any project,
- maintains capacity, absences and skills,
- approves timesheets (Chapter 5),
- may **override booking conflicts** (section 4.5),
- cannot change project settings, users, roles or billing.

## 4.4 The capacity heatmap

Since Haldrum 4.2 the planner has a **capacity heatmap**. Each cell shows how
much of a person's capacity is booked on that day:

| Colour | Booked |
|---|---|
| White | 0 % |
| Light green | 1–79 % |
| Dark green | 80–100 % |
| Red | above 100 % |

Switch between day, week and month view in the top right. In week and month
view, a cell shows the average across the period and turns red if any single
day is above 100 %.

## 4.5 Booking conflicts

Since Haldrum 4.3, Haldrum detects **booking conflicts**. A conflict exists
when a new or changed booking would push a person above 100 % of their
capacity on at least one day, or when a booking falls on a registered
absence.

What happens then depends on who is booking:

- A **Project Lead** cannot save the booking. Haldrum shows the conflicting
  days and suggests other people with the same skills who are free.
- A **Resource Manager** sees the same warning but may save anyway. An
  override requires a reason, which is stored with the booking and shown in
  the audit log on the Business plan.

Every conflict — prevented or overridden — triggers the webhook event
`booking.conflict` (see *API: Webhooks*). Through the API, a conflicting
booking request returns HTTP status `409 Conflict` unless the token belongs
to a Resource Manager and the request sets `"override_reason"`.

Bookings created before 4.3 were not checked. To find old overbookings, open
the heatmap and filter for red cells, or run the *Overbooking* report
(Chapter 6).

## 4.6 Tentative bookings

A booking can be marked **tentative**, for example while a customer has not
yet signed. Tentative bookings are shown with a striped pattern and count
toward capacity at 50 % in the heatmap. They do not trigger conflict
blocking, but they do show a warning.

## 4.7 Good practice

- Keep capacity honest: enter holidays as soon as they are approved.
- Book in whole or half hours; smaller units make the plan harder to read.
- Review the heatmap once a week, ideally before the Monday planning
  meeting.
- Use skills instead of names when asking for people. "Two people with
  network skills" gives the Resource Manager room to plan.
