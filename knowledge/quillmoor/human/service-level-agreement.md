# Haldrum Service Level Agreement

*Part of the Haldrum terms of service. Version 2.1, effective 1 January
2026. Applies to organisations on the Team and Business plans.*

This agreement defines the availability Quillmoor Systems GmbH commits to
for Haldrum, how it is measured, and the service credits customers receive
if the commitment is missed. Support response times are described
separately in the *Support Process*; they are targets, not part of the
credit calculation.

## 1. Uptime commitment

| Plan | Monthly uptime target |
|---|---|
| Starter | no commitment |
| Team | **99.5 %** |
| Business | **99.9 %** |

For a 30-day month, 99.5 % allows about 3 hours 36 minutes of downtime;
99.9 % allows about 43 minutes.

## 2. Definitions

**Service.** The Haldrum web application at `app.haldrum.example` and the
API at `https://api.haldrum.example/v2`.

**Downtime.** A period of at least one minute in which more than 5 % of
requests to the service fail with an error on our side (HTTP 5xx) or time
out after 30 seconds, as measured by our external monitoring from at least
two locations in the EU.

**Monthly uptime** = (total minutes in the month − downtime minutes) ÷
total minutes in the month × 100.

**Not counted as downtime:**

1. Scheduled maintenance announced at least **5 business days** in advance
   on `status.haldrum.example`, limited to 4 hours per month and scheduled
   between Saturday 22:00 and Sunday 06:00 CET.
2. Emergency maintenance to fix a critical security vulnerability,
   limited to 2 hours per month.
3. Problems caused by the customer, such as exceeding the API rate limit,
   invalid requests, or a customer's identity provider being unavailable for
   single sign-on.
4. Failures of the customer's own network or of the public internet outside
   our control.
5. Unavailability of a single feature while the rest of the service works,
   for example delayed webhook deliveries or delayed scheduled exports.
6. Events outside reasonable control (force majeure).

API v1 is not covered by this agreement since its deprecation with Haldrum
4.2.

## 3. Service credits

If the monthly uptime falls below the target, the customer receives a credit
on the monthly subscription fee of the affected organisation:

| Plan | Monthly uptime | Credit |
|---|---|---|
| Team | below 99.5 % | 10 % |
| Team | below 98.5 % | 25 % |
| Business | below 99.9 % | 10 % |
| Business | below 99.0 % | 25 % |

For annual subscriptions, the monthly fee is the annual fee divided by
twelve. Credits for one month are capped at 25 % of that month's fee.

## 4. Claiming a credit

1. The Owner or an Administrator sends a claim to support@quillmoor.example
   within **30 days** after the end of the affected month.
2. The claim names the organisation and the dates and times of the
   observed downtime.
3. Quillmoor Systems confirms or rejects the claim within 10 business days,
   based on its monitoring data, and explains a rejection.
4. Confirmed credits are deducted from the next invoice. Credits are not
   paid out in cash and expire when the subscription ends.

Service credits are the customer's sole financial remedy for missed uptime,
except where the law does not allow such a limitation.

## 5. Termination for repeated failure

If uptime falls below the credit threshold of the second row for the
customer's plan (98.5 % on Team, 99.0 % on Business) in **three consecutive
months**, the customer may terminate the subscription with 30 days' notice
and receives a pro rata refund of prepaid fees for the remaining period.

## 6. Reporting

- The current state and the history of incidents are published on
  `status.haldrum.example`.
- Business customers can request a monthly uptime report for their
  organisation through support.
- Every P1 incident receives a post-incident review; Business customers
  affected by it receive a summary (*Information Security Policy*,
  section 8).

## 7. Data durability

Independent of uptime, Quillmoor Systems commits to the recovery objectives
in the *Information Security Policy*: a recovery point objective of 15
minutes and a recovery time objective of 4 hours for the full platform.
Restores from backup follow the rules in *Data Retention and Deletion*.

## 8. Past performance

For information only, not part of the commitment:

| Quarter | Measured uptime |
|---|---|
| Q3 2025 | 99.97 % |
| Q4 2025 | 99.94 % |
| Q1 2026 | 99.89 % |
| Q2 2026 | 99.98 % |

The lower value in Q1 2026 was caused by a P1 incident on 24 February 2026,
when sign-in was unavailable for 71 minutes after a faulty certificate
rotation. Business customers received a 10 % credit for February without
having to claim it.

## 9. Changes

Quillmoor Systems may change this agreement with 90 days' notice by email to
Owners. Changes never reduce the uptime target or the credits during a
customer's current annual term.
