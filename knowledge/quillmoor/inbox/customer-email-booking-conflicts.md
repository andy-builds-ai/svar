From: operations lead at a customer organisation (Business plan, about 140 users)
To: support@quillmoor.example
Date: 9 September 2026, 07:52
Subject: 409 errors since August + question about Resource Manager rights

Hello Haldrum support,

we have a problem since the summer update and I am not sure if it is a bug
or if we did something wrong. Sorry that this mail is a bit long, I wanted
to put everything in one place.

Background: we have a small script that runs every night and copies the
staffing plan from our sales pipeline into Haldrum. When a deal is marked as
"likely", the script creates bookings for the people that are pencilled in.
It uses POST /v2/bookings with an API token of my colleague, who is a
Project Lead. This has worked for more than a year.

Since around the beginning of August the script fails for maybe a third of
the bookings. The log says HTTP 409 and "booking_conflict". I looked into it
and most of the failing ones are for people who are already fully booked on
another project in the same weeks. Before, Haldrum just accepted them and we
sorted it out in the Monday meeting with the heatmap.

My questions:

1. Is this the new conflict check from 4.3? I read the release notes but I
   did not realise it would affect the API as well. We did not change
   anything on our side.

2. Can we switch the check off for our organisation? For us it is normal
   that sales books people double for a while, because not all deals come
   in. We would like to keep the old behaviour.

3. If we cannot switch it off, what is the right way? I saw something about
   "override_reason". My colleague tried adding that field and got a 403
   instead. Does the token have to belong to an Administrator? We do not
   want to give the script admin rights, that feels wrong.

4. Would it help to create the bookings as tentative? The pipeline data is
   not certain anyway. But I am not sure how tentative bookings show up in
   the utilisation report, because management looks at that every month and
   we do not want the numbers to jump.

5. Separate question: we also noticed that some old overbookings from
   spring are still there and nobody got warned. Is there a way to find all
   of them at once? We have about 60 active projects.

Some details in case you need them:

- organisation: (see signature in the ticket system)
- example request_id from last night: req_41c9e0...
- the script runs at 02:00 CET
- we are still using API v2, we moved from v1 in April

We are not in a hurry for tonight, the script just skips the failing
bookings and we add them by hand, but it is annoying and people start to
not trust the plan anymore. It would be good to have an answer this week
because our quarterly planning starts on 21 September.

One more thing, not really support: it would be great if the conflict check
could have a "soft" mode per project, where it only warns. Maybe for 4.4?

Thanks and best regards
(operations lead)

---

*Internal note added by first-level support, 9 September 2026, 09:14:*
not yet triaged. Looks like P3 (works as designed, workaround exists).
Answers probably: 1 yes; 2 no, cannot be switched off (release notes 4.3);
3 needs Resource Manager token, not admin — check if that is right for API;
4 tentative = 50 % in utilisation, no 409 but warnings; 5 Overbooking
report. Soft mode -> feature request, maybe mention in 4.4 planning. Need
to double-check whether the Resource Manager role is enough for the script
or if a separate integration user is better practice. Ask Priya?
