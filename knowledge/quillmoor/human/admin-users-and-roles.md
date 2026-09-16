# Haldrum Administration: Users and Roles

*Applies to Haldrum 4.3. For Administrators and Owners.*

This document explains how to add and remove users, what each of the six
roles may do, and how ownership of an organisation is transferred.

## Adding users

Administrators and the Owner invite users under *Settings > Users > Invite*.
Enter one or more email addresses and choose a role. The invitation link is
valid for 14 days. Users can also be invited through the API with
`POST /v2/users/invite`.

On the Business plan with SAML single sign-on, users can alternatively be
created on their first sign-in ("just-in-time" provisioning). They receive
the default role the Administrator set for single sign-on, which is
*Member* unless changed. See *Administration: Authentication and Single
Sign-On*.

Starter organisations are limited to 25 paid users. Guests never count
toward that limit or toward billing.

## The six roles

Every user has exactly one role in an organisation.

### Owner

Exactly one per organisation. The Owner can do everything an Administrator
can do, and in addition:

- manage the subscription, plan and payment details,
- see and download invoices,
- delete the organisation,
- transfer ownership.

### Administrator

Administrators run the organisation day to day:

- invite, deactivate and remove users, and change their roles (except the
  Owner's),
- configure security: MFA enforcement, single sign-on, session timeout,
- manage every project, including deleting projects,
- revoke any user's API tokens,
- read the audit log (Business plan),
- manage integrations, webhooks and OAuth apps (OAuth apps on Business).

### Resource Manager

Available on Team and Business since Haldrum 4.2.

- sees all projects and all bookings,
- creates, changes and deletes bookings in any project,
- maintains capacity, absences and skills,
- approves and reopens timesheets for anyone,
- may override booking conflicts, with a reason (since 4.3),
- cannot change users, roles, security settings, billing or project
  settings.

### Project Lead

- creates projects,
- manages the projects they lead: members, settings, templates, export,
  archiving,
- books people for their own projects within free capacity,
- approves timesheet entries logged on their projects,
- shares boards and report views with the organisation.

### Member

- works in the projects they belong to,
- creates and works on tickets,
- logs time and submits timesheets,
- sees their own bookings.

### Guest

- sees only the projects they were invited to, read-only,
- may comment on tickets in those projects,
- cannot log time, is not a resource, and is free of charge.

## Changing a role

Open *Settings > Users*, select the user and choose a new role. The change
takes effect immediately; open sessions pick it up within one minute.

Things to know:

- Downgrading a Project Lead to Member does not remove them as lead of their
  existing projects automatically. Haldrum lists the affected projects and
  asks you to choose new leads before saving.
- Downgrading a user to Guest stops their bookings from counting as
  capacity. Existing bookings stay visible but are flagged.
- Scheduled exports created by a user whose new role no longer allows the
  report are paused (see User Manual, Chapter 6).
- On Starter, the Resource Manager role is not offered.

## Deactivating and removing users

**Deactivate** a user who leaves the company. A deactivated user cannot sign
in, their API tokens stop working immediately, and they no longer count as a
paid user from the next billing period. Their tickets, comments and time
entries stay attributed to them.

**Remove** is only offered for users who never logged time or created
content, for example a mistaken invitation.

Deactivated users can be reactivated at any time with their previous role.

## Transferring ownership

The Owner opens *Settings > Organisation > Transfer ownership* and chooses an
Administrator. The chosen Administrator must confirm by email within 7 days.
After the transfer, the previous Owner becomes an Administrator. If the
Owner has left the company and cannot act, Quillmoor Support can transfer
ownership after written confirmation from the customer's management; see the
*Support Process*.

## Roles and the API

An API token acts with the role of the user who created it, restricted
further by the token's scope (read or read-write). A Member's token can
never do what the Member cannot do in the interface. The details are in
*API: Overview and Authentication*.

## Recommended setup

- Keep the number of Administrators small — two or three is enough for most
  organisations, so that one can cover for another.
- Give planners the Resource Manager role instead of Administrator.
- Invite external contacts as Guests, not as Members.
- Review the user list every quarter and deactivate people who have left.
