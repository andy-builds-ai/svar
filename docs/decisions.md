# Decisions

Why the code is the way it is. One entry per decision: when, what, why.
Measurements behind them are in the READMEs.

## Storage and retrieval

**2026-09-17 — S3 Vectors behind a Bedrock knowledge base, not OpenSearch.**
Nothing with a monthly base fee: an idle month costs cents of storage, and
chunking, embedding and search are managed, so there is no index code to keep.

**2026-09-17 — Titan Text Embeddings V2 at 1,024 dimensions, cosine.**
The largest size for retrieval quality; cosine matches how Titan normalises.
The index must use the same numbers, so they are one constant each.

**2026-09-17 — Only `workspace` and `layer` are filterable metadata.**
Bedrock's own keys and our `source` and `license` are declared
non-filterable; otherwise they count against the filterable-metadata budget
of S3 Vectors and ingestion fails.

**2026-09-17 — Fixed-size chunks of 300 tokens with 13 % overlap, and
documents uploaded as sections.** For documents with Markdown tables Bedrock
returned whole documents under every chunking tried, and the
foundation-model parser changed nothing. Splitting at headings before the
upload does not depend on why: a file returned whole is a section.

**2026-09-17 — Resource names carry a digest of their create-only
settings.** A changed create-only setting replaces the resource, and
CloudFormation creates the replacement before deleting the old one; under a
fixed name the two collide.

**2026-09-17 — The documents bucket is retained and versioned, old versions
expire after 30 days.** Documents are the one thing that cannot be rebuilt;
vectors, the index and the knowledge base are derived and go with the stack.

**2026-09-23 — The fill script prunes what the folder no longer has, as a
dry run unless told `--delete`.** Upload only adds and replaces, so a
document removed from the corpus would stay in the bucket, and every sync
would keep it in the index. Listing first shows what would go; with
`--workspace` only that workspace is compared, so a partial upload never
takes the others for stale. A deleted object stays recoverable for 30 days
as a noncurrent version.

## Workspaces and keys

**2026-09-17 — One installation, workspaces separated by a metadata filter,
with a second check in the handler.** The filter keeps a query inside one
workspace; the handler drops any passage of another workspace before the
prompt is built, so a filter mistake cannot reach the model.

**2026-09-17 — One key per workspace, generated inside AWS during the
deploy, stored as a SecureString.** CloudFormation cannot create a
SecureString parameter, and a key written into the template would sit in
`cdk.out` and in CloudFormation. A small function inside the stack generates
and stores each key instead.

**2026-09-22 — The key resources run on every deploy.** A property that
changes with every synthesis makes CloudFormation call them each time, so
deleting a key's parameter and deploying gives a new key instead of none.

**2026-09-17 — Keys are read at most every 5 minutes, and again at once for
an unknown key, no more than every 10 seconds.** A new key works at once, a
deleted one stops within 5 minutes, and a flood of wrong keys is not a flood
of Parameter Store reads. Keys that cannot be reread keep working; a key
path that reads empty ends them.

**2026-09-18 — The key travels in `X-Andur-Key`.** CloudFront signs every
request to the function URL and overwrites `Authorization` with its own
signature.

## Budgets

**2026-09-17 — A key is a budget: 10 questions and 48 hours from the first.**
A leaked key is a leak of ten questions, not an open tap.

**2026-09-17 — Counted in DynamoDB with one conditional write, found by the
key's SHA-256.** The write counts and checks in one step, so two parallel
questions cannot both take the last one. The table holds no key, only its
hash. On demand, because a public URL has no rate to provision for.

**2026-09-17 — A spent key gets 402.** Not 401 (the key is valid), not 403
(it opens this workspace), not 429 (waiting does not help).

**2026-09-17 — An item is kept 365 days.** The item is what keeps a spent key
spent; once it is gone, the key starts over.

## The answer

**2026-09-15 — Nemotron 3 Super answers.** The model id lives in the stack,
never in the handler, so a change of model is a deploy, not a code change.

**2026-09-17 — The layers of an answer come from its citations, not from the
model.** Asked to flag unconfirmed sources, the model ignored the rule or
applied it everywhere.

**2026-09-17 — The model call is tried once, everything else twice.** A
converse that timed out may have produced its answer; a second attempt pays
for the input again.

**2026-09-17 — Every AWS call has a connect and a read timeout, and the sum
of all of them stays under the function's timeout.** A hanging call ends as a
502 with a reason, not as a bare Lambda timeout. Today 171 of 180 seconds,
with one page of keys, which holds up to ten workspaces.

**2026-09-17 — At most 5 requests at a time.** The cap on the bill matters
more than keeping a demo reachable; a stranger with five parallel calls can
hold it up, and a WAF on the distribution is the answer if that happens.

**2026-09-17 — The log never carries the question, the key or its hash, and
a Bedrock failure is logged by its code only.** A service message quotes
what it choked on, and for converse that is the prompt.

## The way in

**2026-09-18 — One CloudFront distribution for the page and `/ask`.** The
page calls a relative path, so it needs no configuration and no CORS; and
there is one place in front of everything.

**2026-09-18 — The function URL is `AWS_IAM`, reached through an origin
access control; the site bucket is private.** Neither origin answers from
outside on its own. Every request carries the SHA-256 of its body, because a
signed request to a function URL may not have an unsigned payload.

**2026-09-17 — CloudFront waits 120 seconds, the function may run 180.** 120
is the quota without an approved increase. A slower answer reaches the
browser as a 504 while the function finishes it; cutting the function under
120 would stop answers that are only slow.

**2026-09-22 — Security headers on the distribution, with a strict CSP.**
The page loads its own script and stylesheet and posts to its own origin,
nothing else; `frame-ancestors 'none'` keeps it out of other pages' frames.

## Guardrails and cost

**2026-09-23 — The guardrail rides on the converse call.** Nemotron 3 Super
supports Guardrails on `bedrock-runtime` (its model card), so no separate
`ApplyGuardrail` call for the answer.

**2026-09-23 — Passages go in as `grounding_source`, the question as
`query` and `guard_content`.** The topic and PII filters read the question
and the answer, not the documents; the model sees the documents unmasked.

**2026-09-23 — Grounding and relevance thresholds of 0.7.** AWS names no
default; the user guide works its example with 0.7, and a higher threshold
withholds more correct answers. Both scores are logged for tuning.

**2026-09-23 — "People outside the documents" is approximated.** The topic
filter cannot see the documents; it refuses public figures and private
lives, and the grounding check withholds answers about people the passages
do not name.

**2026-09-23 — The model may only be invoked with the deployed guardrail
version.** An IAM condition on `bedrock:GuardrailIdentifier`: a call that
drops the guardrail fails instead of answering unguarded.

**2026-09-23 — At most 30 passages.** The grounding source takes at most
100,000 characters.

**2026-09-23 — A second guardrail for the excerpts, PII masking only.** The
answer's guardrail never sees an excerpt. With topics an excerpt about VAT
would be refused as tax advice, and each question would pay for topic units.

**2026-09-23 — Excerpts in one text block, one per line; anything unexpected
drops them.** Six blocks would be six text units, one block is two. Only
`NONE` and `GUARDRAIL_INTERVENED` with a text that splits back keep the
excerpts; an unmasked excerpt is what this is there to stop.

**2026-09-23 — The excerpt guardrail has its own client, 2 + 5 seconds, two
attempts.** Under the model's 40 seconds the worst case would pass the
function's timeout.

**2026-09-23 — Guardrail versions follow a digest of the resolved
properties.** A version's description is create-only, so a digest in it
replaces the version on every change. Taken over the properties themselves,
not a summary, so a changed action or flag cannot be missed.

**2026-09-23 — A refused question returns no sources.** Nothing was answered
from them, and their excerpts would put document text next to a refusal.

**2026-09-23 — The cost of every answer is computed in the handler, with the
date of its prices.** Prices sit in the stack as environment variables with
their source; the log line carries tokens and units as well, so an old line
can be priced again. Earlier the code had no prices at all; a number per
answer was worth more than the risk of an old price, as long as the date
travels with it.

**2026-09-23 — The PII masking is a filter, not a guarantee.** It is
statistical; a first name without a surname slipped through in the browser
acceptance. What a reader must never see must not be in the documents.

## Code

**2026-09-23 — Files over 500 lines, and why they stay whole.** Measured
23.09.2026. `app/handler.py` (1,105 lines) is the Lambda function: one
module with one entry point keeps the package, its imports and the test
fakes simple, and its parts are the steps of one request in order.
`infra/stacks/andur_stack.py` (964) is one CloudFormation stack whose
resources reference each other and deploy together. `scripts/ingest.py`
(605) is one command with four steps: check, upload, prune, sync. The test
files over 500 — `tests/test_handler.py` (2,198), `tests/test_stack.py`
(1,306), `tests/test_ingest.py` (631) — follow the module they test and
share its fakes. A split is a step of its own, not part of a change that
must not change behaviour.

**2026-09-23 — Comments say what the code cannot.** Docstrings are one
sentence; reasons live here, measurements in the READMEs.
