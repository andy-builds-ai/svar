# What it costs

Prices taken from AWS pricing pages on 16.09.2026 and later, each dated
where it is used, `us-east-1`. Order of magnitude, not a quote; the cost per
question below is measured.

**Idle:** cents per month. S3 Vectors storage is 0.06 USD per GB, the
documents bucket is a handful of megabytes, and nothing else is running.

**Per question, measured.** Every answer carries `cost_usd` in its log line
and its response: tokens and guardrail text units, each times its price
(Amazon Bedrock pricing page, read 23.09.2026, standard tier; the prices sit
in the stack next to their source). Three questions to `pellwick` on
23.09.2026:

| Question | Tokens in / out | Guardrail units (topic / PII / grounding) | Model | Guardrail | `cost_usd` |
|---|---|---|---|---|---|
| Lead time from order to fitting — answered | 1,326 / 32 | 2 / 1 / 5 | 0.00022 USD | 0.00090 USD | 0.001120 USD |
| Office phone number — answered, a name masked | 1,717 / 40 | 2 / 1 / 7 | 0.00028 USD | 0.00110 USD | 0.001384 USD |
| Income tax deduction — refused by the topic filter | 0 / 0 | 1 / 0 / 0 | — | 0.00015 USD | 0.000150 USD |

An answered question costs **about 0.0011 to 0.0014 USD — 1.1 to 1.4 USD per
1,000 questions**, and four fifths of it is the guardrail, not the model.
The grounding check is the largest part: it reads every retrieved passage as
its reference, one unit per 1,000 characters, so it grows with the passages,
not with the answer. A refused question costs one topic unit and no model
call. Not in `cost_usd`: the question's embedding, the vector query and the
Lambda time, together under 0.0001 USD.

The excerpts of the sources pass a second guardrail that masks personal
details, built after these three questions. Measured on the phone question
again, 23.09.2026: six excerpts in one call are two text units, 0.0002 USD,
and the question cost 0.001579 USD in all (1,717 / 33 tokens, units
2 / 3 / 7, the 3 being one for the answer and two for the excerpts). A
refused question costs nothing more: it returns no sources.

**The cap:** the web address is open to anyone who has it, so the answer
function runs at most 5 requests at the same time and Lambda turns the rest
away with HTTP 429 before any code runs. A request without a valid key never reaches the model.
That bounds the rate at which anything can arrive; what one key may spend in
total is its budget — ten questions and 48 hours, so about 0.014 USD before
the key is refused. A leaked key is a leak of ten questions, not of an
open tap.

**Counting it:** one conditional write per question, which is one write unit
on a table billed per request — 0.625 USD per million writes in `us-east-1`
(DynamoDB on-demand pricing page, read 17.09.2026). That is 0.63 USD per
million questions, against roughly 1,100 to 1,400 USD per million at the
model and the guardrail. The counter is not what the bill is made of. Note that the permanent free tier of
25 write and 25 read units is for provisioned capacity, not for on-demand
tables; this table is on-demand, because a public URL has no rate to
provision for and a throttled counter would refuse honest questions.

**Filling it:** 40 documents of 2,000 words is roughly 110,000 tokens of
embedding at 0.02 USD per million — about 0.002 USD. Embedding a corpus is
not what costs money; asking questions is, and barely.

**Serving the page:** nothing worth counting. CloudFront's permanent free
tier is 1 TB of data transfer out and 10 million requests per month; the page
is a few kilobytes and a demo month is a few thousand requests, so a month of
it stays inside the free tier several thousand times over. Beyond it the
prices are 0.085 USD per GB and 0.010 USD (United States) or 0.012 USD
(Europe) per 10,000 HTTPS requests. Fetching from an AWS origin is free.
Cache invalidation is free for the first 1,000 paths a month, and a deploy
uses one. The site bucket holds a few kilobytes at 0.023 USD per GB-month.
(CloudFront and S3 pricing pages, read 17.09.2026.)
