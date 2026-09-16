# Haldrum User Manual — Chapter 1: Getting Started

*Applies to Haldrum 4.3.*

This chapter explains how Haldrum is organised, how you sign in and how to
find your way around the interface. The following chapters cover the
modules one by one: Chapter 2 Projects, Chapter 3 Tickets and Boards,
Chapter 4 Resource Planning, Chapter 5 Time Tracking and Chapter 6 Reports
and Exports.

## 1.1 Organisations

Everything in Haldrum belongs to an **organisation**. An organisation is
usually one company. It has its own users, projects, settings and
subscription plan. A person can be a member of several organisations with
the same email address — for example a freelancer who works for two
customers — and switches between them with the organisation switcher in the
top left corner.

Each organisation has exactly one **Owner**. The Owner holds the
subscription and can transfer ownership to an Administrator. How roles work
is described in *Administration: Users and Roles*.

## 1.2 Signing in

Open `app.haldrum.example` and sign in with your email address and password.
Depending on how your Administrator has set up the organisation, you may
also be asked for:

- **A one-time code (MFA).** Haldrum supports authenticator apps that
  generate time-based codes. Administrators can make MFA mandatory for
  everyone in the organisation (available since 4.1).
- **Single sign-on.** On the Business plan, your organisation can sign you
  in through its own identity provider using SAML 2.0. In that case you
  click *Sign in with your company account* and never enter a Haldrum
  password.

Your session ends after 12 hours of inactivity by default. On the Business
plan, Administrators can set this between 1 and 72 hours (since 4.3).

If you have forgotten your password, use *Forgot password* on the sign-in
page. The reset link is valid for 60 minutes. Users who sign in with single
sign-on reset their password with their own IT department, not in Haldrum.

## 1.3 The interface

The screen has four areas:

1. **Navigation bar (left).** One entry per module: Projects, Tickets,
   Resources, Time and Reports. Modules not included in your plan are
   hidden. On the Starter plan you will not see Resources.
2. **Top bar.** Organisation switcher, global search, notifications and your
   profile menu.
3. **Main area.** The list, board or plan you are working on.
4. **Detail panel (right).** Opens when you select a project, ticket or
   booking, so you do not lose the list you came from.

### Global search

The search in the top bar finds projects, tickets, people and files. Type a
ticket number such as `T-1042` to jump straight to a ticket. Search only
returns items you have permission to see; a Guest will not find projects
they were not invited to.

### Notifications

The bell icon collects mentions, ticket assignments, booking changes and
timesheet decisions. You choose which of them you also receive by email
under *Profile > Notifications*. Daily digest emails are sent at 07:00 in
your own time zone.

## 1.4 Your profile

Under *Profile* you set:

- your display name and photo,
- your **time zone** and language (English and German are available),
- your **working hours per day**, which Resource Planning uses as your
  capacity unless a Resource Manager has set a different value,
- your MFA device.

Personal **API tokens** are not part of the profile; they are created under
*Settings > Developer > API tokens*, if your role and plan allow API access
(see *API: Overview and Authentication*).

## 1.5 Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `g` then `p` | Go to Projects |
| `g` then `t` | Go to Tickets |
| `g` then `r` | Go to Resources |
| `c` | Create a new item in the current module |
| `/` | Focus the global search |
| `?` | Show all shortcuts |

## 1.6 Mobile use

Haldrum works in the browser on phones and tablets. There is no separate app
to install. On small screens the navigation bar collapses into a menu and
the detail panel opens full screen. Logging time and updating tickets work
well on a phone; planning resources is easier on a larger screen.

## 1.7 Where to go next

- New to Haldrum as an Administrator? Follow the *Onboarding Guide* first.
- Starting a project? Read Chapter 2.
- Handling support requests? Read Chapter 3.
- Common questions are answered in the *FAQ*.
