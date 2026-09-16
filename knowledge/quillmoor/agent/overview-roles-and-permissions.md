# Generated overview of roles and permissions in Haldrum 4.3

*This is a generated overview, compiled automatically on 6 August 2026 from
Administration: Users and Roles, the User Manual chapters 2 to 6 and
Administration: Authentication and Single Sign-On. It condenses the rules
into one matrix. It has not been reviewed by a person; in case of doubt,
Administration: Users and Roles is authoritative.*

## Roles

| Role | Per organisation | Paid | Plans |
|---|---|---|---|
| Owner | exactly one | yes | all |
| Administrator | any number | yes | all |
| Resource Manager | any number | yes | Team, Business (since 4.2) |
| Project Lead | any number | yes | all |
| Member | any number | yes | all |
| Guest | any number | no | all |

## Permission matrix

Legend: **✓** allowed · **own** only for projects the person leads or
belongs to · **—** not allowed

### Organisation and security

| Permission | Owner | Admin | Res. Mgr | Proj. Lead | Member | Guest |
|---|---|---|---|---|---|---|
| Change plan, payment, see invoices | ✓ | — | — | — | — | — |
| Delete organisation | ✓ | — | — | — | — | — |
| Transfer ownership | ✓ | — | — | — | — | — |
| Invite, deactivate users, change roles | ✓ | ✓ | — | — | — | — |
| Enforce MFA, configure single sign-on, session timeout | ✓ | ✓ | — | — | — | — |
| Revoke other users' API tokens | ✓ | ✓ | — | — | — | — |
| Read audit log (Business) | ✓ | ✓ | — | — | — | — |
| Manage webhooks and OAuth apps | ✓ | ✓ | — | — | — | — |
| Create own API tokens (default policy) | ✓ | ✓ | ✓ | ✓ | ✓ | — |

### Projects

| Permission | Owner | Admin | Res. Mgr | Proj. Lead | Member | Guest |
|---|---|---|---|---|---|---|
| See projects | all | all | all | own | own | invited, read-only |
| Create projects | ✓ | ✓ | — | ✓ | — | — |
| Change project settings and members | ✓ | ✓ | — | own | — | — |
| Archive project | ✓ | ✓ | — | own | — | — |
| Delete project | ✓ | ✓ | — | — | — | — |
| Export project | ✓ | ✓ | — | own | — | — |
| Save and share templates | ✓ | ✓ | — | ✓ | — | — |

### Tickets

| Permission | Owner | Admin | Res. Mgr | Proj. Lead | Member | Guest |
|---|---|---|---|---|---|---|
| Create and work on tickets | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Comment on tickets | ✓ | ✓ | ✓ | ✓ | ✓ | invited projects |
| Share boards with organisation | ✓ | ✓ | — | ✓ | — | — |
| Configure SLA timers and rules | ✓ | ✓ | — | — | — | — |

### Resources and bookings

| Permission | Owner | Admin | Res. Mgr | Proj. Lead | Member | Guest |
|---|---|---|---|---|---|---|
| Is a bookable resource | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| See bookings | all | all | all | own projects | own | — |
| Create and change bookings | ✓ | ✓ | ✓ all projects | own projects, within capacity | — | — |
| Override booking conflict (since 4.3) | — ¹ | — ¹ | ✓ with reason | — | — | — |
| Maintain capacity, absences, skills | — ¹ | — ¹ | ✓ | — | — | — |

¹ The source documents name only the Resource Manager for overrides and for
maintaining capacity, absences and skills. Whether Owners and Administrators
may do the same is not stated explicitly. **Open question — needs
confirmation by a person.**

### Time

| Permission | Owner | Admin | Res. Mgr | Proj. Lead | Member | Guest |
|---|---|---|---|---|---|---|
| Log time | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Approve timesheets | all | all | all | entries on own projects | — | — |
| Reopen approved timesheets | ✓ | ✓ | ✓ | — | — | — |
| See hourly rates | ✓ | ✓ | ✓ | own projects | — | — |

### Reports

| Permission | Owner | Admin | Res. Mgr | Proj. Lead | Member | Guest |
|---|---|---|---|---|---|---|
| Organisation-wide reports | ✓ | ✓ | ✓ | — | — | — |
| Reports on own projects | ✓ | ✓ | ✓ | ✓ | own work | — |
| Scheduled exports (Team, Business) | ✓ | ✓ | ✓ | ✓ | ✓ ² | — |
| Publish organisation dashboards | ✓ | ✓ | — | — | — | — |

² Scheduled exports run with the creator's permissions, so a Member's
schedule only contains what the Member can see. The source documents do not
restrict who may create a schedule.

## API

A token acts with the role of its creator, limited by its scope. `read`
tokens allow `GET` only; `read-write` tokens are not available on Starter.

## Generation notes

- Two cells are marked as open questions (footnote ¹).
- The matrix does not cover single sign-on just-in-time provisioning, which
  creates users with the default role *Member*.
