# What it costs

Prices for `eu-west-1` (Ireland), read 28.09.2026 from the AWS Price List API,
the machine-readable source of the pricing pages, unless dated otherwise.
Order of magnitude, not a quote; the cost per question below is measured.

**Idle:** cents per month. S3 Vectors storage is 0.06 USD per GB, the
documents bucket is a handful of megabytes, and nothing else is running.

**Per question, measured.** Every answer carries `cost_usd` in its log line
and its response: tokens and guardrail text units, each times its price
(standard tier; the prices sit in the stack next to their source).

In `eu-west-1` on 28.09.2026, at six passages per question, the 30 evaluation
questions per model through the answer function's own path (`python -m scripts.evaluate`, see
[scripts/README.md](../scripts/README.md)), each model at its own token
price:

| Model | Answered question, mean (lowest–highest) | Guardrail share | Refused question |
|---|---|---|---|
| Nova 2 Lite | 0.0019 USD (0.0015–0.0028) | 59–68 % | 0.00015 USD |
| Nova Pro | 0.0028 USD (0.0019–0.0045) | 38–52 % | 0.00015 USD |
| Claude Haiku 4.5 | 0.0034 USD (0.0023–0.0053) | 32–44 % | 0.00015 USD |
| Claude Sonnet 4.6 | 0.0077 USD (0.0048–0.0127) | 13–21 % | 0.00015 USD |
| Nemotron 3 Super | not measured: the model did not answer in `eu-west-1` on 28.09.2026 | — | 0.00015 USD |

Nova Pro is the configured model since 28.09.2026. On the page the same
day it cost 0.00342 USD for an answered question (1,868 / 82 tokens, units
2 / 3 / 8), 0.003015 for one with a masked name, 0.0012 for one the grounding
check withheld (the response reports no model tokens then) and 0.00015 for a
refused one. Its evaluation run after the switch: 0.0029 USD per answered
question on average (0.0019–0.0046, 20 answers).

**Ten passages since 28.09.2026** (`NUMBER_OF_RESULTS`, was 6; see
[decisions](decisions.md)). Nova Pro, the 22 answerable questions, two runs
through the function's own path: 0.0043 USD per answered question on average
(0.0032–0.0062, 43 answers; the grounding check withheld one more) against
0.0029 USD in two runs at six passages (0.0019–0.0045, 39 answers). Tokens in
per answer: 2,691 against 1,718, out 13.6 against 13.1. Guardrail units
(topic / PII / grounding): 2 / 4.1 / 10.3 against 2 / 3.0 / 6.5, so the
guardrail part rose from 0.00125 to 0.00173 USD, because the grounding check
reads every passage. The refused question (0.00015 USD, below) was measured
at six passages and not again at ten.

The guardrail part is the same for every model, 0.0012 USD per answered
question on average at six passages, because it reads the same question, passages and
answer length; what differs is the model. A refused question costs one topic
unit and no model call, whichever model is set.

In `us-east-1` on 23.09.2026, before the move, three questions to `pellwick`
with Nemotron 3 Super, whose token prices there were 0.00015 and 0.00065 USD
per 1,000 (a fifth below eu-west-1; the guardrail prices are the same in
both regions):

| Question | Tokens in / out | Guardrail units (topic / PII / grounding) | Model | Guardrail | `cost_usd` |
|---|---|---|---|---|---|
| Lead time from order to fitting — answered | 1,326 / 32 | 2 / 1 / 5 | 0.00022 USD | 0.00090 USD | 0.001120 USD |
| Office phone number — answered, a name masked | 1,717 / 40 | 2 / 1 / 7 | 0.00028 USD | 0.00110 USD | 0.001384 USD |
| Income tax deduction — refused by the topic filter | 0 / 0 | 1 / 0 / 0 | — | 0.00015 USD | 0.000150 USD |

There an answered question cost **about 0.0011 to 0.0014 USD — 1.1 to 1.4 USD
per 1,000 questions**, and four fifths of it was the guardrail, not the model.
The grounding check is the largest part: it reads every retrieved passage as
its reference, one unit per 1,000 characters, so it grows with the passages,
not with the answer. A refused question costs one topic unit and no model
call. Not in `cost_usd`: the question's embedding, the vector query and the
Lambda time, together under 0.0001 USD.

The excerpts of the sources pass a second guardrail that masks personal
details, built after these three questions. Measured on the phone question
again, us-east-1, 23.09.2026: six excerpts in one call are two text units, 0.0002 USD,
and the question cost 0.001579 USD in all (1,717 / 33 tokens, units
2 / 3 / 7, the 3 being one for the answer and two for the excerpts). A
refused question costs nothing more: it returns no sources.

**The cap:** the web address is open to anyone who has it, so the answer
function runs at most 5 requests at the same time and Lambda turns the rest
away with HTTP 429 before any code runs. A request without a valid key never reaches the model.
That bounds the rate at which anything can arrive; what one key may spend in
total is its budget — ten questions and 48 hours, so about 0.04 USD with
Nova Pro at ten passages before the key is refused (0.02 to 0.08 across the
models above, measured at six passages). A leaked key is a leak of ten questions, not of an
open tap. A key on a plan may spend a month's quota: at 0.0043 USD per
answered question, about 26 USD a month on `betrieb` (6,000 questions) and
65 USD on `firma` (15,000), if every question is used and answered.

**Counting it:** one conditional write per question, which is one write unit
on a table billed per request — 0.705 USD per million writes in `eu-west-1`.
A question on a plan is two, the refused demo write and the count; still a
fraction of a cent per thousand questions.
That is 0.71 USD per million questions, against roughly 1,900 to 7,700 USD
per million at the model and the guardrail. The counter is not what the bill is made of. Note that the permanent free tier of
25 write and 25 read units is for provisioned capacity, not for on-demand
tables; this table is on-demand, because a public URL has no rate to
provision for and a throttled counter would refuse honest questions.

**Filling it:** 40 documents of 2,000 words is roughly 110,000 tokens of
embedding at 0.026 USD per million (Titan Text Embeddings V2) — about 0.003
USD. Embedding a corpus is
not what costs money; asking questions is, and barely.

**Serving the page:** nothing worth counting. CloudFront's permanent free
tier is 1 TB of data transfer out and 10 million requests per month; the page
is a few kilobytes and a demo month is a few thousand requests, so a month of
it stays inside the free tier several thousand times over. Beyond it the
prices are 0.085 USD per GB and 0.010 USD (United States) or 0.012 USD
(Europe) per 10,000 HTTPS requests. Fetching from an AWS origin is free.
Cache invalidation is free for the first 1,000 paths a month, and a deploy
uses one. (CloudFront pricing page, read 17.09.2026; CloudFront is priced by
the viewer's location, not the stack's region.) The site bucket holds a few
kilobytes at 0.023 USD per GB-month.
