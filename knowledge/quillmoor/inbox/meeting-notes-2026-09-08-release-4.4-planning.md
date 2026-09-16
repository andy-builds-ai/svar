notes – 4.4 planning – tue 8 sep 2026, 14:00–15:10
present: Samuel (product), Tobias (CTO), Priya (support), Lena (security),
2 people from engineering (planning team, platform team)
notes taken by: planning team, not yet cleaned up

---

**1. date**

- target release still november. Tobias: realistic is 17 nov, code freeze
  27 oct. Samuel ok with that.
- nobody wants a december release because of api v1 switch-off on 31 dec,
  support will be busy with that. Priya agrees strongly.
- -> 4.4 on 17 nov 2026, freeze 27 oct. to be confirmed in next week's
  meeting (not final!)

**2. scope – what is in**

a) bulk project export
- top open request since q2 (see support summary q2). Samuel wants it.
- idea: GET /v2/exports?project_status=... creates one archive with all
  projects. background job only.
- Tobias: size is the problem, some orgs have 300+ projects with files.
  proposal: without files, files stay per project.
- permission: only admins and owner (project leads would only get their own
  projects which makes it confusing)
- -> IN, without files. engineering to estimate.

b) soft mode for booking conflicts
- 3 customer mails since august asking to switch off conflict check. one
  from last week (business, nightly sync script, gets 409s).
- Samuel: no global switch-off, that was a conscious decision in 4.3.
- option discussed: per-project setting "warn only" that Project Leads
  cannot change, only resource managers / admins.
- Lena: override_reason must still be logged, even in soft mode, otherwise
  audit log has gaps.
- open: does a "warn only" booking via API return 201 with warnings (like
  tentative) or 409? Tobias prefers 201 + warnings.
- -> probably IN, Samuel to write short spec by 15 sep.

c) custom fields on bookings
- requested often, but touches export format, reports, api.
- -> OUT for 4.4, look again for 4.5.

d) native mobile app
- -> OUT. no change, browser is fine.

e) SCIM user provisioning (business)
- Lena wants it, JIT provisioning with SSO does not deactivate users who
  leave. security risk at big customers.
- Tobias: 3–4 weeks work. tight.
- -> maybe. decide next week depending on estimate of (a).

**3. api v1 switch-off**

- still ~140 orgs calling v1 in last 30 days (was ~390 in june).
- Priya: send second reminder mail 1 oct, third 1 dec.
- idea from platform team: short "brownouts" in november, v1 returns 503 for
  1 hour on two announced days so people notice. Priya worried about
  tickets. Lena ok with it.
- sla: v1 is not covered by sla anymore since 4.2, so brownouts are not
  downtime. (check wording in SLA doc – yes, section 2 says v1 not covered)
- -> brownouts: 10 nov and 24 nov, 10:00–11:00 CET. announce with 1 oct
  mail. Priya to prepare help article.

**4. support topics**

- resource manager permissions still unclear for some customers: can admins
  override booking conflicts? docs only mention RM. Samuel: admins CAN'T
  override today, that's intended – they should use RM role. but docs don't
  say it clearly.
- -> Priya/Samuel: add a sentence to users & roles doc + manual ch. 4.
  (the generated roles overview also has this as open question)

**5. security**

- pentest for 2026 scheduled for october, after freeze. Lena will send scope.
- session timeout config (4.3) – some business customers set 72h. Lena would
  like max 24h. Samuel: we promised 1–72 in release notes, can't reduce
  without notice. -> leave it, maybe add recommendation in admin doc.

---

**action items**

- [ ] Tobias – engineering estimate bulk export + SCIM – by 15 sep
- [ ] Samuel – spec soft mode booking conflicts – by 15 sep
- [ ] Priya – v1 reminder mail + brownout article – by 25 sep
- [ ] Priya/Samuel – clarify admin vs RM override in docs – by 30 sep
- [ ] Lena – pentest scope – by 22 sep
- [ ] planning team – draft release notes 4.4 skeleton – started, see drafts

next meeting: tue 15 sep, same time
