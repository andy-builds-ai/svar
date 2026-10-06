# Decisions

Why the code is the way it is. One entry per decision: when, what, why.
Measurements behind them are in the READMEs.

## Region

**2026-09-28 — eu-west-1 (Ireland), not us-east-1.** A company asks where
its documents go. In Ireland the documents, the vectors, the question and the
answer stay in the EU. S3 Vectors, Bedrock Knowledge Bases, Titan Text
Embeddings V2, Guardrails and Nemotron 3 Super are all offered there (the
Bedrock model list of eu-west-1, 28.09.2026). The stack in us-east-1 is left
standing; removing it is a decision of its own.

**2026-09-28 — Names that hold for the whole account carry the region.**
IAM role names and CloudFront policy and origin access control names are
account-wide. Without the region, the stack in eu-west-1 would have collided
with the one in us-east-1 at four names: the knowledge base role, the
response headers policy and both origin access controls.

**2026-09-28 — A model runs in the region or behind an EU profile, never a
global one.** An EU inference profile routes only to EU regions; a global
profile may route anywhere. For an EU profile the answer role may invoke the
profile with the guardrail condition, and the model itself in `eu-*` only
through that profile (`bedrock:InferenceProfileArn`). Checked in the IAM
policy simulator on 28.09.2026: the model in us-east-1, a direct call and a
global profile are all denied.

**2026-09-28 — Prices from the AWS Price List API.** It is the
machine-readable source of the pricing pages, one row per model, region and
tier. An EU profile is billed at the regional price, which for Claude is a
tenth above the global one.

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

**2026-09-28 — Ten passages per question, not six.** Every model answered
"the sources do not say" to q07, how long an invitation link is valid,
because retrieval ranked the right section seventh (score 0.7255; the eighth
0.7248, the first, about API tokens, 0.7704) and the function took six. With
30 hits for each of the 22 answerable questions, 20 had their document first,
q14 fifth and q07 seventh; none lay deeper. Nova Pro through the function's
own path, the 22 answerable questions: at six passages four runs on
28.09.2026 got 21, 20, 19 and 17 right (the comparison, the run after the
switch, two runs of this check), at ten passages two runs got 22 and 21. The
cost of an answered question rose from 0.0029 to 0.0043 USD (1,718 to 2,691
tokens in), the median time stayed at 1.8 seconds. What is proven is q07:
right in both runs at ten passages and in none at six. Whether ten passages
also withhold fewer answers is not proven: the grounding check withheld 5 of
44 answers at six passages and 1 of 44 at ten, but the model is not
deterministic and a different question was withheld in each run. Eight
passages would have put q07 one rank inside the cut; ten leave three.
Rerank is left for later: eu-west-1 has no rerank model, and the knowledge
base refuses one from another region ("Reranking does not support
cross-region calls", 28.09.2026). A direct call to Frankfurt works and put
q07 first (Amazon Rerank 1.0, 0.001 USD per query; Cohere Rerank 3.5, 0.002;
AWS Price List API, 28.09.2026), but it adds a region, a permission and a
network hop; it is worth building if a larger corpus pushes the right
passage past rank ten. Hybrid search is rejected: S3 Vectors answers a
similarity query with a metadata filter, and knowledge bases offer hybrid
only on Amazon RDS, OpenSearch Serverless and MongoDB (Amazon S3 Vectors and
Bedrock user guides, *Querying vectors* and *Configure and customize
queries*, read 28.09.2026).

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

**2026-09-30 — Pruning compares only the workspaces the folder holds.** Until
then, a folder without `--workspace` was compared with every workspace in the
bucket. A dry run with one customer's own folder listed all 294 objects of
the two demo workspaces as stale; with `--delete` it would have removed them.
A workspace without a document in the folder is now never compared, so the
one case it cannot prune — a workspace emptied locally — is left to a
deliberate `aws s3 rm`. The all-clear names the workspaces it compared,
because the bucket may hold others that were not looked at.

**2026-10-02 — Section names spell out umlauts.** A section's file name is
what the page lists as its source, and it kept only a–z and 0–9: a German
customer read `b-rozeiten` for *Bürozeiten*. Now `ä`, `ö`, `ü`, `ß` are `ae`,
`oe`, `ue`, `ss`, as Germans write them without umlauts, `ø`, `æ`, `œ`, `ł`,
`đ` likewise, and other letters lose their accent, so the part made from the
heading stays plain ASCII. Not the heading as written: an umlaut can be
stored as one character or as a letter and its dots, which would make two
names for one heading. The part before it is the document's own file name;
it stays as it is, only composed (NFC), for the same reason. An English
heading keeps its name, so the demo corpus is not uploaded again; a German one
is renamed, and its workspace needs one upload with `--delete` to drop the old
sections.

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

**2026-10-01 — Keys come from a command, not from the deploy; the two entries
above are replaced.** The stack created one key per name in `WORKSPACES`, so
every customer stood by name in the public repository, every new one needed a
code change and a deploy, and a stack deployed into a customer's own account
brought the demo keys along. Now `python -m scripts.workspace add <name>`
generates the key on the operator's machine and stores it as a SecureString,
and `remove <name> --yes` deletes key, budget items and documents and runs an
ingestion job. The answer function never needed the list: it reads whatever
lies under the key path. The price: the key is generated outside AWS, in the
operator's terminal, and printed there once; and `cdk destroy` no longer takes
the keys with it.

The deploy that drops the key resources must not take the keys with it.
Removing a custom resource makes CloudFormation send it a Delete, and the
old key function answered a Delete by deleting the key. CloudFormation deletes
removed resources only in the cleanup phase, after every update has succeeded
("Monitor the progress of a stack update" in the CloudFormation user guide),
so the same deploy first updates the key function — new code that deletes
nothing, a role without any Parameter Store permission — and the Delete
reaches that. A `Retain` deletion policy would have needed two deploys, since
CloudFormation applies the policy of the template already deployed, and the
first of them would have needed the customer names again. The function and
its provider have no work left after that deploy, but they stay until no
deployed stack holds a `Custom::AndurApiKey` any more, in any account and
region. Removing them from the code removes them everywhere the code is
deployed next, and on a stack that skipped this deploy the Delete would reach
the old function, which may delete keys. One such stack exists: the old one in
us-east-1, deliberately left at a version with key resources.

**2026-10-01 — Removing a workspace shows before it deletes.** Without
`--yes`, `remove` lists what it would delete and deletes nothing, because a
typo must not take a customer away. Everything it deletes is found under that
one name: the parameter `<path>/<name>`, the budget items — the one of the
key's hash, and every item whose `workspace` is the name, which also finds
those of earlier keys and those left when the key is gone already — and the
objects under `workspaces/<name>/` — with the slash, so `kalv` never reaches
`kalvenstett`. The listing and the scan are checked again before anything is
deleted. The budget items go before the key, because an item preset by hand
carries no workspace until its first question and is found only by the key's
hash: a run that stops halfway can be repeated. After the key the items are
looked for once more, because the function keeps a key for up to 5 minutes and
a question in that time writes a new one; the script says so instead of
reporting the workspace gone. Deleted documents leave a
delete marker in the versioned bucket, and their old versions expire after
30 days like every other's; within that time they can be restored.

**2026-09-17 — Keys are read at most every 5 minutes, and again at once for
an unknown key, no more than every 10 seconds.** A new key works at once, a
deleted one stops within 5 minutes, and a flood of wrong keys is not a flood
of Parameter Store reads. Keys that cannot be reread keep working; a key
path that reads empty ends them.

**2026-10-02 — The function reads only the key of the workspace a request
names.** One `GetParameter` on `<path>/<workspace>` instead of every key under
the path. Parameter Store hands out the path ten keys to a page, so from the
eleventh workspace on the read was two calls, and the worst case of the
timeouts went past the function's 180 seconds; now it is one call whatever
the number of customers, and the function never holds another customer's key.
The entry above holds per workspace: each key has its own 5 minutes and its
own 10 seconds between rereads, and a request reads at most once. A workspace
without a key is remembered as such for those 10 seconds, so the same
invented name costs one read every 10 seconds, and it is forgotten at the
first good read after them so the names do not fill the function's memory.
Three things
changed with it. The key of another workspace is a 401 now, not a 403: the
function no longer knows whose key it is, and a wrong key, a foreign key and
a workspace without a key share one sentence, so the sentence does not tell
which workspaces exist (status and timing can; the names are not a secret).
The check for one key stored for two workspaces is
gone, since it needed every key; `scripts.workspace add` draws keys from 32
random bytes, so only a key written by hand can repeat. And an empty key path
is no longer a 500: a deleted key is `ParameterNotFound`, the key stops within
its 5 minutes, and the request is a 401. The role may
`ssm:GetParameter` on `<path>/*` and nothing else in Parameter Store.

**2026-10-02 — An old key decides for at most 15 minutes, and invented names
have a cap.** A key that cannot be read again goes on deciding, as decided
on 2026-09-17, but only until 15 minutes after its last good read
(`KEY_MAX_AGE_SECONDS`, three cache times); then its workspace is a 500 until
the store answers. Without that limit a deleted key lived as long as an
outage did. Fifteen minutes ride out ten minutes of trouble at Parameter
Store and still end a revoked key on a clock. A failed read is remembered for
10 seconds also for a workspace the instance holds nothing for, so an outage
is not a read per request. And each instance reads at most 10 times in 10
seconds for workspaces it holds no key for (`UNKNOWN_KEY_READS`); beyond
that it answers 503 with `Retry-After: 10`, without a read. Five instances
are then at most 5 reads a second, an eighth of the 40 a second the account
has by default for all `GetParameter*` calls, so a stream of invented names
cannot spend the account's reads. It can still give a customer whose key the
instance does not hold yet a 503, with about one request a second and no key;
nothing tells a real name from an invented one before it is read. One answered
question took 0.7 to 4.4 seconds inside the function, so an instance answers
few real customers in 10 seconds, and only those it holds no key for yet
count; 10 leaves room. A key the instance holds never meets the cap. 503 and
not 401, because nothing was looked up, and not 429, which here is a key's own
quota. Rereads because a presented key is not the cached one have a cap of
their own, 10 in 10 seconds per instance across workspaces
(`WRONG_KEY_READS`): the brake per workspace alone would let wrong keys sent
round many real workspaces spend the same reads. Over it the cached key
decides, so a wrong key stays a 401 and a just-changed key waits a few seconds.

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

**2026-10-01 — A paying customer's key carries a plan, and the plans live in
one table.** Until now every key ended after 48 hours, and a customer would
have been locked out on day three. `betrieb` allows 6,000 questions a
calendar month and at most 600 a day, `firma` 15,000 and 1,500; the quota
renews every month and the key never ends. Generous on purpose: it guards
against a key in the wrong hands, not against the bill. The numbers stand
in `app/plans.py` alone, which the function and `scripts/workspace.py` both
read, so a change is one line and a deploy. The item names its plan; an item
without one is the demo, exactly as before, so the demo keys in use go on
unchanged.

**2026-10-01 — German calendar months and days, worked out in the code.**
The quota renews at midnight in Germany, where the customers are. `zoneinfo`
needs a time zone database that nothing promises the Lambda runtime brings
(this laptop's Python has none either), so the handler applies the EU rule
for summer time itself; a test compares it with the database every half hour
from 2026 to 2030 where one is installed. Should the EU ever drop the clock
change, this is the line to change.

**2026-10-01 — One count per month and per day, named by the period.** A key
on a plan counts in `month_2026-10` and `day_2026-10-01`. A new day is a new
attribute, so nothing is reset and nothing races at midnight, and a question
that set out before midnight counts in its own day. The other way, a single
`day` and `day_used` reset when the day changes, needs a second conditional
write whenever two questions meet at the change, and more writes than the
worst case of the timeouts allows for. Each write clears the counts older
than last month and yesterday.

**2026-10-01 — The demo write comes first and hands over the plan.** Its
condition refuses an item with a plan, and DynamoDB returns the refused item;
the second write counts against the plan. No read, and two writes per
question on a plan, which the worst case already counts for a demo key (the
count and the TTL repair). A plan item has no TTL: losing the item would turn
the key back into a demo key.

**2026-10-01 — A full quota is a 429, a spent demo key still a 402.** 402 says
nothing comes back and a new key is needed, which the page adds in a line of
its own. A quota does come back, and 429 says to come back later; its body
carries a sentence, which Lambda's own 429 does not, so the page shows it
alone. The sentence asks the customer to get in touch with their contact
person, in German or English like every other.

**2026-10-01 — A plan item that cannot be counted answers nothing.** An
unknown plan and a count that is not a number are a 500 with a log line,
never a question that nothing counted. A missing count is the start of its
period.

## The answer

**2026-09-15 — Nemotron 3 Super answers.** The model id lives in the stack,
never in the handler, so a change of model is a deploy, not a code change.

**2026-09-28 — Nova Pro answers, through its EU profile.** Nemotron 3
Super gave no response in eu-west-1 on 28.09.2026. Of the four models that
did, Nova Pro was right as often as Claude Haiku 4.5 and Sonnet 4.6 (29 of
30), cost less per answered question (0.0028 USD against 0.0034 and 0.0077)
and was never turned away (scripts/README.md, *evaluate*).

**2026-09-28 — A model and its price live in one table.**
`infra/answer_models.py` holds each model id with its token prices; the
stack looks the price up by the id it deploys, and the evaluation reads the
same table. A model without a row fails at synthesis, so a change of model
cannot carry the last model's prices.

**2026-09-29 — Temperature 0, and what it did not fix.** The function set
only `maxTokens`, so Nova Pro sampled at its default (0.7). It now sends
`temperature` 0 (`ANSWER_TEMPERATURE` in `app/handler.py`). The Nova user
guide gives the range for `temperature` as 0.00001 to 1 on its request-schema
page and uses `temperature: 0` on its structured-output page; a call to Nova
Pro through the EU profile accepted both 0 and 0.00001 (29.09.2026). AWS does
not promise identical output: its pages say only that a lower temperature
"leads to more deterministic responses" and that "model output is not
deterministic". Measured: two runs of the 30 questions, Nova Pro at ten
passages, 28 of 30 right in both, 27 of 30 questions with the same outcome
(right or wrong, cited sources, withheld) and 26 of 30 with the same answer
text. The sums hid it: q03 was right and then withheld by the relevance
check, q16 the other way round. Retrieval did not move (same files, same
scores, same input tokens). In q08 and q11 the guardrail did not intervene
and the model's own output differed (12 against 9 and 9 against 11 output
tokens; "200 [1][2]" against "200 [1]", "30 days" against "At least 30
days"), so temperature 0 does not make Nova Pro repeat itself. Not proven for
q03 and q16: the blocked text is not kept, so it is open whether the model
wrote a different answer or the guardrail's check judged the same one
differently. Not tried: `topK` (Nova takes it in `additionalModelRequestFields`).
Two runs cannot say how often the answers differ.

**2026-10-03 — An answer is a sentence, not numbers in brackets.** The open
question from 29.09. is answered for q16: rebuilt outside the function with
the same sources, prompt and temperature, Nova Pro wrote only citation marks
("[1][2]", "[2][3][4]") in 3 of 13 tries. The relevance check scores such an
answer 0 and withholds it, rightly. The system prompt now says to write the
answer as a sentence in words, because numbers in brackets alone are not an
answer; with that rule the same question gave a full sentence in 10 of 10
tries. Across both evaluation sets one run each gave 28 of 30 and 29 of 32
with the rule against 28 of 30 and 28 of 32 without it (03.10.2026): no harm,
and a difference small enough to be the model's own spread. In both runs
`topic:Private lives` withheld the answers to q15 and de11 on the way out,
which the topic probe did not measure then (see 05.10.2026 under *Guardrails
and cost*).

**2026-09-17 — The layers of an answer come from its citations, not from the
model.** Asked to flag unconfirmed sources, the model ignored the rule or
applied it everywhere.

**2026-09-17 — The model call is tried once, everything else twice.** A
converse that timed out may have produced its answer; a second attempt pays
for the input again.

**2026-09-28 — A model call AWS turns away is made once more, if it can still
end before CloudFront gives up.** Claude Haiku 4.5 was turned away with
`ServiceUnavailableException` on 20 of 30 calls one afternoon, each a 502 and
a spent question. Only the three Converse errors whose AWS description says
the request was not run are repeated: `ThrottlingException` ("Your request
was denied due to exceeding the account quotas"),
`ServiceUnavailableException` ("temporarily unable to handle the request")
and `ModelNotReadyException` ("not ready to serve inference requests") —
Converse API reference and the Bedrock user guide's *Troubleshooting API
error codes*, read 28.09.2026. `InternalServerException` is not ("The
request processing has failed"): the model may have run, and a second call
would pay twice, the same reason a timeout is never repeated. The other
throttling codes the handler knows are no Converse errors, so nothing
documents them either way. The second call waits 2 seconds and starts only
up to second 50 of the request: 2 of pause, 43 of call and 20 of excerpt
guardrail after it end at 115. Since 05.10.2026 up to second 30: the topic
check of the answer is a second guardrail call of 20. The 5 seconds to CloudFront's 120 are for
what the handler's clock does not see — the cold start (Init Duration 0.11
seconds, measured), the way from CloudFront to the function and the runtime
around the handler; they cost nothing measurable, since the model call
starts within 5 seconds of a request. The second call does not add to the
worst case. The pause is short because its length made no
measured difference: twenty calls back to back were turned away none of the
time, twenty with ten seconds between them twice. It sits in the handler,
not in botocore's retry setting, which would repeat timeouts too, and the
first rejection is a log line of its own.

**2026-09-28 — A body Python cannot parse is a 400, whatever the reason.**
`json.loads` refuses an integer of more than 4,300 digits with `ValueError`
and nesting past the recursion limit with `RecursionError`. Both used to end
as a 500 with a traceback in the log, and the body is read before the key is
checked, so anyone could cause them.

**2026-09-17 — Every AWS call has a connect and a read timeout, and the sum
of all of them stays under the function's timeout.** A hanging call ends as a
502 with a reason, not as a bare Lambda timeout. Today 169 of 180 seconds,
whatever the number of workspaces: the key is one call (2026-10-02), and the
guardrail two, the topic check of the answer and the excerpts (2026-10-05).

**2026-09-17 — At most 5 requests at a time.** The cap on the bill matters
more than keeping a demo reachable; a stranger with five parallel calls can
hold it up, and a WAF on the distribution is the answer if that happens.

**2026-09-17 — The log never carries the question, the key or its hash, and
a Bedrock failure is logged by its code only.** A service message quotes
what it choked on, and for converse that is the prompt.

**2026-10-01 — Andur speaks the visitor's language, not the company's.** Two
languages, German and English. The page starts in German when the browser's
language begins with `de`, offers a DE/EN switch and sends its language in the
body of every question; the function says every sentence of its own in it. A
language per workspace would have needed a list of customers outside the
repository, and that list (`WORKSPACES`) was still in the stack then. Which
language a request names decides nothing about which documents are read, and
nothing is checked against it: a missing or unknown one is English, not an
error, so a client written before the field existed keeps working unchanged.

**2026-10-01 — The answer follows the question, the fixed sentences follow
the request.** The prompt still says "answer in the language of the question",
as before: an English question about English documents is answered in
English, on the German page too. What the prompt fixes word for word is the
sentence for "the sources do not answer", and that one comes in the language of
the request, because the evaluation recognises it by its exact text
(`scripts/evaluate.py` knows both). The price: a German page and an English
question can give an English answer and a German "not in the sources". Tying
the answer to the page language as well would have removed that, and would
have taken away the ability to ask an English document in English from a
German browser.

**2026-10-01 — A block by the guardrail is told in the handler's words.**
Bedrock replaces a blocked text with the one English sentence set on the
guardrail, whatever the language of the question; that was the one sentence a
German visitor read in English on a borderline question. The
handler now swaps it for its own sentence when two things are in the same
response: Bedrock stopped with `guardrail_intervened`, and the trace has a
policy with the action `BLOCKED`. A stop without that, or a `BLOCKED` on an
answer that was not stopped, is left alone rather than guessed at. The
guardrail's English sentences stay in the stack as the fallback, and a test
keeps them equal to the handler's English ones. Two sentences are not
three: only the topic filter reads the question, so a topic blocked in the
input assessment means "refused", every other block means "withheld".

**2026-10-01 — What is found before the body is read is English.** The
method and the shape of the body are checked first, and the language is in the
body. Reading the language from the `Accept-Language` header instead would
have given a second source that can disagree with the switch on the page, for
errors the page cannot cause. It never sends a wrong method or a body that is
not JSON; whoever does is a script, and `scripts.ask` reads English.

**2026-10-01 — The page keeps every sentence once.** `web/language.js` holds
the English and the German text as two tables with the same keys; `index.html`
is empty where text goes and `app.js` has no sentence of its own. Under Node
the file exports its pure functions, as `markdown.js` does, so the tests run
them: both tables have the same keys and placeholders, every key that is asked
for exists and every one that exists is asked for. German uses "Sie" on the
page and in the function's sentences, because the readers are the staff and
customers of a company.

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

**2026-10-01 — Denied topics in the Standard tier, through the EU guardrail
profile.** The first customers are German, and a German tax question went
through: measured with `ApplyGuardrail` against the deployed guardrail, which
ran in the Classic tier, 11 of 16 German refusals were refused and 5 were
not, among them "Kann ich die Rechnung für den Heizungstausch von der Steuer
absetzen?". 14 harmless German questions passed, and the 4 English refusals
were refused. The user guide lists English, French and Spanish for the
Classic tier's topics, German ("optimized") for the Standard tier's. The
Standard tier needs a guardrail profile; `eu.guardrail.v1:0` routes from
eu-west-1 to eu-central-1, eu-west-1, eu-west-3, eu-north-1, eu-south-1 and
eu-south-2 only, and a stack whose region is set to one outside those six
does not synthesise (without a set region there is nothing to check;
`infra/app.py` always sets it). Both tiers cost 0.15 USD per 1,000 topic
units (Price List API and pricing page, 01.10.2026), and the profile costs
nothing extra, so a question costs what it did. Not done: German examples in
`DENIED_TOPICS`. Taken from the questions that went through, they would have
made the measurement after the deploy a test of the examples, not of the
tier; they stay the next step if German refusals still pass.

**2026-10-02 — Every change to the denied topics is measured before the
deploy, with `python -m scripts.topic_probe`.** After the Standard tier was
deployed, 15 of 16 German refusals were refused, and 4 of 14 harmless German
questions with them, among them "Wie lange habe ich Gewährleistung auf eine
Reparatur?": a business whose customers cannot ask about its own warranty
cannot use Andur. The probe builds the topics the way the stack does in a
guardrail of its own, asks 109 fixed questions (`scripts/topic-questions.json`)
three times each, and deletes the guardrail; the run before any deploy costs
0.049 USD. The topics are now `Legal action`, `Tax returns`, `Medical
advice` and `Private lives`. The names weighed most: with the old names and
new definitions one run refused 9 of 64 harmless questions, with the new
names and the same definitions 2. Definitions follow the user guide's best
practices: a subject, no instruction, no exception, no example in the
definition. Measured on 02.10.2026, three runs: all 45 refusals refused in
every run, 2 of 64 harmless questions refused in every run (cancelling a
maintenance contract, a call-out charge). Not reached: none refused. In 26
runs every change that freed one of the two refused another harmless
question or let a refusal through; a narrower legal topic did both. The
verdicts are not fixed either: the same topics gave one question two verdicts
in two runs, so a question counts as right only if it is right in every run.
The probe read the question only then; since 04.10.2026 it reads answers
too.

**2026-10-05 — The answer is checked for the denied topics without its
citations, by a guardrail of its own.** `Private lives` withheld q15 and de11
in the evaluation of 03.10, on the answer. Rebuilt outside the
function with the same passages, prompt and guardrail version, 25 tries
each: the trace put every block in `outputAssessments`, never in
`inputAssessment`, and it fell on the same wordings every time — "Warned
[1][2][4]" withheld 4 of 4 times, "Warned [1][2][4][6]" let through 16 of 16;
"Die Garantie gilt für 5 Jahre [1][4]." withheld, the same with "[4]" not.
The 0 input and 0 output tokens in the evaluation said nothing about the
side: Bedrock reported 0 for every answer it withheld in the rebuild, also
when the trace held what the model wrote. The citations decide it: with
`[1][4]` behind them `Private lives` withheld 11 of 69 harmless answers, with `[1][2][4]` 16, with `[1]`, `[2]`,
`[1][2]` or none 0 (`ApplyGuardrail` as OUTPUT on the deployed version). Not
fixed by four new definitions (each kept "Warned [1][2][4]" withheld and moved
verdicts of other topics), by other spellings of the citations (`[1] [4]`,
`[1, 4]` and the like withheld up to 48 of 69), nor by a topic of its own for
answers (it withheld the same citations). Taking `Private lives` off the
answer (built on 04.10., never deployed) freed q15 but let all 8 answers in
the probe that tell something private pass; rejected: an answer that tells
something private from the documents stays withheld. So the citations are
taken out before the topics read the answer. The answer guardrail on the
model call reads the four topics in the question only; the handler applies
`andur-topic-guardrail`, which holds the same four topics and nothing else,
as OUTPUT to the answer without its citations (`without_citations`), and
withholds the answer if it blocks. The reader gets the answer with its
citations, and masking, grounding and relevance read it on the model call as
before. A guardrail of its own because the answer guardrail cannot read one
text for its topics and another for grounding in the same call, and applying
it once more would run the masking and grounding again and pay for them. A
check that fails withholds the answer as a 502: it is never given unchecked.
An answer withheld for a topic comes back without sources, like a refused
question: an excerpt can tell what the answer was withheld for, and masking
takes out names and addresses, not "is married and has two children".
Measured with the probe (05.10.2026, three runs, answers without their
citations): questions unchanged, 45/45 refused and 2/64 harmless refused;
answers, 31 of 32 to withhold withheld — all 8 private ones, also with the
address masked as the function would show it, and not the German medical
`rmd4` ("Die Schnittwunde muss nicht genäht werden, ein Pflaster reicht."),
which passed with every topic on both sides on 04.10. too; 0 of 73 harmless
withheld (with citations, the answer side withheld q15 and de11, or de04a2 and
hd01, depending on the setting); and 0 of the 276 answers with `[1][4]`,
`[1][2][4]`, `[2][4]` or `[1][5]` behind them (32 on the deployed version 3).
It costs one more `ApplyGuardrail` call per answered question: one topic
unit, 0.00015 USD, and 0.44 seconds measured from outside AWS. de11 stays
withheld for a second reason: the
grounding check, which the user guide lists for English, French and Spanish
only, scored its German answers to English passages 0.25 to 0.54 and withheld
31 of 35 in the rebuilds; the four that passed read "Die Garantie gilt für 5
Jahre [4]." (0.85).

**2026-10-06 — German answers keep the grounding check as it is; checking
them in English is measured, not built.** Measured with `python -m
scripts.grounding_probe` (`scripts/README.md`) on 142 fixed answers, two runs:
on English documents the check withheld 3 of 19 right German answers, on
German documents none of 19. It withheld 20 of 34 made-up German answers on
English documents, 18 of their 34 English twins and 23 of 36 made-up German
answers on German documents. In the evaluation of the same day it withheld
de11 and de12 of the 12 German answer questions, and none of the 19 Kalvenstett
answer questions that reached the model; de09 is the model's own "Die Quellen
beantworten diese Frage nicht.", like the English q20. Built and measured: a
German answer the check withheld for grounding or relevance alone was
translated with its question by the answer model, through the answer
guardrail, and the translation checked on the same sources; passed, the
German answer was masked by the excerpt guardrail and given. It freed 2 of
the 3 right answers, and let 6 made-up German answers on English documents
and 3 on German documents through that the check withholds today. Not built,
since the check is what stands against made-up answers and is not weakened
without the operator's word; the code is not in the repository, the probe
keeps measuring the way. Measured too and not built: lower thresholds (the
first right answer freed at 0.5, with 7 more made-up ones through) and the
English check with a threshold of its own (at 0.9 it freed "25 % [1]" alone
and still let one made-up answer on German documents through). No score, as
written or translated, kept the right German answers apart from the made-up
ones. What the English check would cost per withheld German
answer: a model call and two guardrail calls, 1.35 to 2.13 seconds (46 checks
from outside AWS) and 0.0013 USD, and a client of its own with short limits:
under the answer call's 3 + 40 seconds, a request's worst case with the check
would end past CloudFront's 120 (`app/aws.py`).

**2026-09-23 — The model may only be invoked with the deployed guardrail
version.** An IAM condition on `bedrock:GuardrailIdentifier`: a call that
drops the guardrail fails instead of answering unguarded. Since 05.10.2026
any numbered version of that guardrail (next entry).

**2026-10-05 — A new guardrail version leaves the answer role alone, and the
deploy waits for the answers on the old one.** The role allowed the model
only with `<answer guardrail arn>:<deployed number>`. A deploy with a new
version updated the role 23 and 24 seconds before the function (CloudTrail,
02.10.2026, two deploys), and in between the function called with the old
number and ended in 502, the question already counted. The cleanup then
deleted the old version 13 and 46 seconds after the function changed, while
an answer that started on it may run 180. Now the condition is `ArnLike
<answer guardrail arn>:*`, the Bedrock user guide's policy for "a specific
guardrail and any of its numeric versions": a call without a guardrail, with
the draft (the ARN without a number), with the excerpt or topic guardrail or
any other one stays refused. The ARN itself never changes, since no property
of a guardrail replaces it. CloudFormation deletes every replaced version, so
outside a deploy the condition matches the deployed number only, and during
one the old number as well, the same guardrail with its earlier settings.
`Custom::AndurDeployDrain` comes after the function, follows the three
versions, and waits 210 seconds when one of them changes; the cleanup comes
after it. It costs about three and a half minutes per such deploy and two
functions that run only then. `tests/test_deploy_steps.py` walks every order
CloudFormation may take and checks every answer at every step; the old form
fails it. Not taken: keeping old versions (`Retain`), since a guardrail holds
at most 20, not adjustable, the answer guardrail reached version 3 in eight
days, and a deploy at the quota would fail until versions are deleted by
hand; two deploys per change, by hand every time; every guardrail in the
account, since the excerpt guardrail checks no topic and no grounding; the
draft instead of versions, since CloudFormation changes it before the
function and the topic probe measures numbered versions. A Lambda alias with
a role per published version would also close the windows that remain
(`docs/operating.md`, *Deploy*), but it moves the function URL that
CloudFront calls; a step of its own.

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

## Evaluation

**2026-09-28 — The model comparison runs the handler's own path, not a
deploy per model.** `scripts/evaluate.py` calls `answer_question` with the
deployed function's settings and changes only the model id and its token
prices. That is the same conversation — retrieval, prompt, `converse` with
the guardrail, excerpt masking, cost line — with each model priced at its
own rate, while the live default stays as it is. A deploy per model would
take minutes per model and switch the page each time. The run uses the
developer's credentials, not the function's role, so it proves the models
and the guardrail, not the role's IAM.

**2026-09-28 — An answer is right when it contains the expected facts.** Each
question names its facts with their usual spellings, and the first spelling
is checked against the document in the tests. A text check is repeatable and
needs no second model to judge the first. A fact counts only where it stands
on its own, so "12" is not found in "120" nor "4:30" in "14:30"; checked
against the saved runs of 28.09.2026, this changed one answer, q14, whose
right "27 October" had matched only as the start of a word, and the
question now lists the spelled-out month. A right answer in words the
question set did not foresee counts as wrong, so the hit rate is a lower
bound. A question that must be refused or cannot be answered is right when
Andur declines: the guardrail blocks, or the model says the sources do not
answer.

**2026-09-28 — A busy service is asked again, anything else is counted as
wrong.** Bedrock answered some calls with `ServiceUnavailableException`; a run
that counts those measures the day's capacity, not the model. A timeout is
not retried: it is what the page would show.

## Code

**2026-09-23 — Files over 500 lines, and why they stay whole.** Measured
28.09.2026. `app/handler.py` (1,217 lines) is the Lambda function: one
module with one entry point keeps the package, its imports and the test
fakes simple, and its parts are the steps of one request in order.
`infra/stacks/andur_stack.py` (1,066) is one CloudFormation stack whose
resources reference each other and deploy together. `scripts/ingest.py`
(629) is one command with four steps: check, upload, prune, sync. The test
files over 500 — `tests/test_handler.py` (2,590), `tests/test_stack.py`
(1,440), `tests/test_ingest.py` (711) — follow the module they test and
share its fakes. A split is a step of its own, not part of a change that
must not change behaviour.

**2026-09-23 — Comments say what the code cannot.** Docstrings are one
sentence; reasons live here, measurements in the READMEs.
