# Haldrum Administration: Authentication and Single Sign-On

*Applies to Haldrum 4.3. For Administrators and Owners.*

This document covers how people sign in to Haldrum and which settings
Administrators control: passwords, multi-factor authentication (MFA),
SAML single sign-on, session timeout and API token policy.

## Password sign-in

By default users sign in with email and password. Passwords must have at
least 12 characters. Haldrum checks new passwords against a list of known
leaked passwords and rejects matches. After 10 failed attempts within 15
minutes, the account is locked for 30 minutes and the user is notified by
email.

Administrators cannot see or set passwords. They can send a password reset
link from *Settings > Users*.

## Multi-factor authentication

Users can add an authenticator app under *Profile > Security*. Haldrum uses
time-based one-time codes (TOTP). Each user also receives ten single-use
recovery codes when setting up MFA.

### Enforcing MFA

Since Haldrum 4.1, Administrators can make MFA mandatory for the whole
organisation under *Settings > Security > Require MFA*. This is available on
all plans.

When enforcement is switched on:

- users without MFA are asked to set it up at their next sign-in and cannot
  continue until they have,
- a grace period of up to 7 days can be granted,
- Guests are included unless the Administrator explicitly exempts them.

When single sign-on is active, MFA is the job of the identity provider, and
Haldrum's own MFA enforcement does not apply to single sign-on users.

### Lost devices

A user who lost their device signs in with a recovery code. If they have no
recovery codes left, an Administrator resets MFA for that user under
*Settings > Users > Reset MFA*. The reset is recorded in the audit log on the
Business plan. Quillmoor Support does not reset MFA for users; it only helps
when no Administrator is left in the organisation.

## SAML single sign-on

Single sign-on is available on the **Business** plan. Haldrum supports
SAML 2.0 with any identity provider that follows the standard.

### Setting it up

1. Go to *Settings > Security > Single sign-on*.
2. Copy Haldrum's service provider details: entity ID and assertion consumer
   service URL.
3. Create an application in your identity provider with those values.
4. Upload the identity provider's metadata XML, or enter its entity ID,
   sign-in URL and signing certificate by hand.
5. Map the attributes: email address (required), first name, last name.
6. Click *Test connection*. Haldrum opens a test sign-in in a new window and
   shows the received attributes.
7. Choose the enforcement mode (below) and save.

### Enforcement modes

| Mode | Effect |
|---|---|
| Optional | Users may sign in with single sign-on or password. |
| Required | Users with an email address in the verified domains must use single sign-on. The Owner keeps password access as a fallback. |

Domains must be verified before *Required* can be selected, by adding a DNS
TXT record shown on the settings page.

### Just-in-time provisioning

If switched on, a person who signs in through single sign-on for the first
time is created as a user with the default role, which is *Member* unless the
Administrator chooses another. Guests cannot be created this way.

## Session timeout

A session ends after a period without activity. The default is **12 hours**.
Since Haldrum 4.3, Administrators on the Business plan can set any value
between **1 and 72 hours** under *Settings > Security > Session timeout*. The
new value applies to sessions started after the change. On Starter and Team
the timeout is fixed at 12 hours.

Administrators can also sign out every user at once with *Sign out all
sessions*, for example after a suspected incident.

## API token policy

Users create personal API tokens under *Settings > Developer > API tokens*,
as described in *API: Overview and Authentication*. Administrators control:

- **Maximum lifetime.** Tokens can live at most 365 days. Administrators can
  lower this to 30, 90 or 180 days.
- **Who may create tokens.** By default every role except Guest. It can be
  restricted to Project Leads and above.
- **Revocation.** Administrators can revoke any token, and all tokens of a
  deactivated user stop working immediately.

OAuth apps, which let a third-party integration act on behalf of users
without personal tokens, are available on the Business plan only.

## Audit log

On the Business plan, sign-ins, failed sign-ins, MFA resets, changes to
security settings, token creation and revocation, role changes and booking
overrides are written to the audit log. Entries are kept for **400 days**.
Administrators read the log under *Settings > Audit log* or through
`GET /v2/audit-log`; filters for actor, action and time range were added to
the API in Haldrum 4.3.

## Checklist for a new organisation

- Enforce MFA, or set up single sign-on on Business.
- Verify your email domain.
- Lower the maximum token lifetime to 90 days unless integrations need more.
- Keep at least two Administrators.
