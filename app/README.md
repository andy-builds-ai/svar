# app — the answer function

`handler.py` is the Lambda function behind Andur's `/ask` path. It takes a
question for one workspace, counts it against the budget of the key that
asked, retrieves passages from the knowledge base with a `workspace` filter,
asks the answering model to answer from those passages only, and returns the
answer with its sources and the layer of each source.

It has a Function URL, but that is not a way in from outside: the URL is set
to `AWS_IAM`, and its resource policy names CloudFront alone, only for this
stack's distribution. Inside the account a role with
`lambda:InvokeFunctionUrl` can sign a call itself; the key, the workspace and
the budget are checked in the function either way.
Everything arrives through the web address — see *One address for everything*
in [architecture](../docs/architecture.md#one-address-for-everything).

It runs only while a question is in flight; there is nothing to pay while
nobody asks. `aws.py` holds the AWS clients with their timeouts and retries.
`plans.py` holds the plans a paying customer's key can carry (*Plans* below).
Lambda packages this folder and imports the modules by their plain names,
`handler`, `aws` and `plans`; the tests import them the same way
([tests/conftest.py](../tests/conftest.py)). Nothing else is needed but the
standard library and the boto3 the Lambda runtime brings.

## Asking

```bash
python -m scripts.ask "How do I export project data?" --workspace quillmoor
```

That is the short way: the script reads the address from the stack outputs
and the key from Parameter Store, and never prints the key
([scripts/README.md](../scripts/README.md)). The request underneath is plain
HTTP:

```bash
BODY='{"question": "How do I export project data?", "workspace": "quillmoor"}'

curl -s -X POST "$WEB_URL/ask" \
  -H "X-Andur-Key: $KEY" \
  -H "Content-Type: application/json" \
  -H "x-amz-content-sha256: $(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)" \
  -d "$BODY"
```

`$WEB_URL` is the stack output `WebUrl`; `/ask` is the one path that reaches
this function. `workspace` must be the workspace the key belongs to. A
question is 1 to 1,000 characters. On macOS use `shasum -a 256` in place of
`sha256sum`.

The body may also carry `"language": "de"` or `"language": "en"`: the
language of the sentences Andur says itself, see *Language* below. Leave it
out and they are English; the page sends it with every question, and
`python -m scripts.ask` with `--language`.

Two headers are not the function's idea but the way in's:

- **`X-Andur-Key`** carries the key. Not `Authorization`: CloudFront signs
  every request it passes to the function URL and replaces that header with
  its own signature, so a key sent there would never arrive.
- **`x-amz-content-sha256`** carries the SHA-256 of the body, in hex. A
  signed request to a Lambda function URL may not have an unsigned payload
  ("Lambda doesn't support unsigned payloads"). Without the header the
  request never reaches the function. `printf '%s'` and not `echo`: a
  trailing newline would be part of the body for the hash but not for curl,
  and the two would not match.

## Getting a key

Each workspace has one key, stored as a SecureString parameter
`/andur/api-keys/<workspace>`. `python -m scripts.workspace add <workspace>`
generates it, stores it and prints it once
([scripts/README.md](../scripts/README.md#workspace--adding-and-removing-a-workspace)).
It is not in the repository, the template or any log. To copy it to the
clipboard later without it showing up on screen:

```bash
printf '%s' "$(aws ssm get-parameter --region eu-west-1 --name /andur/api-keys/quillmoor \
  --with-decryption --query Parameter.Value --output text)" | clip   # macOS: pbcopy
```

Or keep it in a shell variable for curl: `KEY=$(aws ssm get-parameter ... --output text)`.

In Git Bash on Windows, put `MSYS_NO_PATHCONV=1` in front of any command with
`/andur/api-keys/...`: otherwise Git Bash turns the parameter name into a
Windows path and AWS rejects it.

No deploy touches a key. To replace one, delete the parameter and add it
again; `add` refuses to overwrite a key that is there:

```bash
MSYS_NO_PATHCONV=1 aws ssm delete-parameter --region eu-west-1 --name /andur/api-keys/quillmoor
python -m scripts.workspace add quillmoor
```

The function keeps the keys it has read for 5 minutes
(`KEY_CACHE_SECONDS`), so **a deleted or replaced key keeps working for up to
5 minutes** — and longer if Parameter Store cannot be *read* at the moment the
5 minutes are up, because expired keys that cannot be replaced go on deciding
rather than turning every request into a 500.

An empty path is not that case. Delete every parameter under
`/andur/api-keys` and the store still answers, it just has nothing to say: the
function throws away the keys it holds and answers 500 until keys exist again
(`NoWorkspaceKeys`). So retiring every workspace at once does take effect
within the 5 minutes, outage or none. To revoke a single key at once, change
the function after deleting it —
every running instance is replaced, and the new ones read the keys fresh:

```bash
aws lambda update-function-configuration --region eu-west-1 --function-name andur-answer --description "keys rotated"
```

A new key works at once: a key the function does not know makes it read the
keys again before it answers 401. It does that at most every 10 seconds per
running instance (`KEY_RELOAD_MIN_SECONDS`), so a stream of wrong keys cannot
turn into a stream of Parameter Store reads. A key stored for two workspaces
opens neither; the log names both.

## Keys are budgets, not passwords

A key does not open its workspace without end. A key without a plan is a
demo key and carries an allowance: **10 questions**, and **48 hours** counted
from the first of them. A key that has spent either is refused with 402
before anything is paid for. A key that is never used never starts its
clock. A paying customer's key carries a plan instead: a quota per calendar
month and per day that comes back by itself, and no end (*Plans* below).

The count lives in the DynamoDB table `andur-key-budgets` (stack output
`BudgetTableName`), one item per key. The item is found by the **SHA-256 of
the key**, never by the key, so the table can be read, exported or pasted
into a ticket without a key falling out of it. Each item holds:

| Attribute | Meaning |
|---|---|
| `key_hash` | Partition key: the SHA-256 of the key, as hex. |
| `workspace` | The workspace the key opened on its first question. |
| `questions_used` | How many questions have passed the budget. |
| `question_limit` | How many may. Default `QUESTION_LIMIT`. |
| `first_used_at` | When the window started, epoch seconds. |
| `window_seconds` | How long the window is. Default `WINDOW_HOURS`. |
| `expires_at` | `first_used_at + window_seconds`. |
| `keep_seconds` | How long the item is kept. Default `KEEP_DAYS`; raised to `window_seconds + KEEP_DAYS` if it would not outlast the window. |
| `delete_after` | `first_used_at + keep_seconds`, raised past `expires_at` when that is not enough; DynamoDB's TTL watches this. |

Checking and counting are one conditional write, so five questions arriving
together cannot all see the same count and all pass:

```
ConditionExpression:
  attribute_not_exists(#plan) AND (
    attribute_not_exists(first_used_at)
    OR (
         (attribute_not_exists(questions_used)
          OR attribute_not_exists(question_limit)
          OR questions_used < question_limit)
         AND (attribute_not_exists(expires_at) OR expires_at > :now)
       )
  )
```

`#plan` stands for the attribute `plan` (PLAN is one of DynamoDB's reserved
words). The check in front refuses an item that names a plan and so hands
the key to its plan's count (*Plans*); a demo item never has a `plan`, so for
it the condition says what it said before plans existed, and a test keeps
the rest of the string as it was.

The two `attribute_not_exists` in the middle are for an item that lost an
attribute — a half-finished edit by hand. In DynamoDB a comparison against a
missing attribute is false, not an error, so without them such an item made
both sides of the `OR` false and the key was refused every question it would
ever ask, until the TTL removed the item a year later. Letting that question
through costs one question and repairs the item: the update writes the
missing attribute from the defaults in the same call. While the item is whole
the condition says exactly what it said before.

**A question counts once it has passed the budget**, even if Bedrock then
fails with a 502. Nothing is given back. That is one rule instead of two, and
a caller who asks again knows what they paid.

### Setting a budget for one key

Both commands need the key's hash, which you can produce without the key ever
reaching the screen. Set `WORKSPACE` first; on macOS use `shasum -a 256` in
place of `sha256sum`:

```bash
HASH=$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --region eu-west-1 \
  --name "/andur/api-keys/$WORKSPACE" --with-decryption \
  --query Parameter.Value --output text | tr -d '\r\n' | sha256sum | cut -d' ' -f1)
```

`tr -d '\r\n'` and not `tr -d '\n'`: in Git Bash on Windows the AWS CLI ends
its output with a carriage return, and a hash of the key plus a stray `\r` is
a different hash than the function computes. The function hashes the key it
authorised, which is the key stripped of surrounding whitespace, so a key
sent with trailing whitespace still finds its own budget. A key is 43
characters; if the hash does not match, check that first.

**Set** a different budget — two questions and six hours, here — **before the
key's first question**. Once the window is open the item carries its own
numbers, and the condition refuses this write rather than resetting a
counter that is already running:

```bash
aws dynamodb put-item --region eu-west-1 --table-name andur-key-budgets \
  --condition-expression 'attribute_not_exists(first_used_at)' \
  --item "{\"key_hash\":{\"S\":\"$HASH\"},\"question_limit\":{\"N\":\"2\"},\"window_seconds\":{\"N\":\"21600\"},\"keep_seconds\":{\"N\":\"604800\"}}"
```

**Reset** a budget — delete the item, and the key starts over with the
defaults:

```bash
aws dynamodb delete-item --region eu-west-1 --table-name andur-key-budgets \
  --key "{\"key_hash\":{\"S\":\"$HASH\"}}"
```

`keep_seconds` has to outlast `window_seconds`: an item that disappears while
its own window is still open would hand the key a fresh budget. The function
checks that for **its own defaults** and refuses to start if they break the
rule. For an item written by hand it cannot check before the fact — a
DynamoDB `SET` action takes two operands, so the update cannot write the
later of the two numbers by itself. So it checks afterwards: the counting
write hands back what it wrote, and if `delete_after` does not outlast
`expires_at`, one more write raises **both** `keep_seconds` (to
`window_seconds + KEEP_DAYS`) and `delete_after` (to `expires_at +
KEEP_DAYS`). Raising `keep_seconds` is what makes it stick: the counting
update rewrites `delete_after` from `keep_seconds` at every question, so a
repair that touched only `delete_after` would be undone by the next question
and written again by the next repair, for as long as the key lives. With both
raised it happens once, for an item that is wrong; an ordinary question is
still one write. The log line is `budget-ttl-repaired`, and it should appear
once per item, never once per question. Writing both numbers together in the
set command above is still the better way round.

On a key that has not asked yet, set `question_limit`, `window_seconds` and
`keep_seconds` as above; `expires_at` and `delete_after` follow with its
first question. Never write `first_used_at` by hand — that attribute is what
the condition reads to tell "not used yet" from "running", so writing it
would start a window that nobody opened.

On a key that has asked, the numbers do not all follow. The condition reads
the **stored** `expires_at` and `question_limit`, and only a question that
passes it rewrites `expires_at` from `first_used_at + window_seconds` and
`delete_after` from `first_used_at + keep_seconds`:

- `question_limit` takes effect with the next question, higher or lower.
- A longer `window_seconds` works on a running key from its next question,
  but never on one whose window has closed: that key is refused before
  anything is rewritten. Set `expires_at` as well
  (`first_used_at + window_seconds`).
- A shorter `window_seconds` lets one more question through, because the
  condition still reads the old `expires_at`. Set `expires_at` as well to
  close it at once.
- `keep_seconds` works the same way on `delete_after`, which is what the
  table's TTL reads: on a key that asks no more questions, set
  `delete_after` (`first_used_at + keep_seconds`) with it, and keep it past
  `expires_at`.

The simplest way to give a used key a fresh start is to reset it with
`delete-item` above; a changed budget for it is then a preset item as for a
new key. On a key with a plan, `delete-item` deletes the plan too and leaves
a demo key; use `python -m scripts.workspace plan` to change a plan.

**A replaced key has a fresh budget by itself.** Deleting the parameter and
adding it again gives the workspace a new key, and a new key has a different
hash and therefore no item — the old item stays behind and expires on its
own. That also means the reverse: a key stays spent only as long as its item
exists. After `KEEP_DAYS` (365 by default, counted from the first question)
the item is gone and the key would work again. To end a key for good, replace
it.

### Plans

A paying customer's key carries a plan in its budget item, and the plans are
defined in one place, `plans.py`:

| Plan | Questions per calendar month | At most per day |
|---|---|---|
| `betrieb` | 6,000 | 600 |
| `firma` | 15,000 | 1,500 |

Change a number there and deploy, and every key on that plan has the new
number from its next question. `python -m scripts.workspace` puts a key on a
plan, when it adds the key or later, and takes it off again
([scripts/README.md](../scripts/README.md#workspace--adding-and-removing-a-workspace)).
The quota is there for a key in the wrong hands, not for the bill: at the
measured mean of 0.0043 USD per answered question with Nova Pro at ten
passages ([docs/costs.md](../docs/costs.md)), a month used to the last
question is about 26 USD on `betrieb` and 65 USD on `firma`.

**German time.** A month and a day are German calendar months and days: they
begin at midnight in Germany, which is 23:00 UTC in winter and 22:00 UTC in
summer. The function works German time out itself (`german_date`): UTC+1,
and UTC+2 from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last
Sunday of October, the EU rule. It does not ask `zoneinfo`, which needs a
time zone database that nothing promises the Lambda runtime brings. A test
compares the rule with the database every half hour from 2026 to 2030
wherever a database is installed, as in CI; on 01.10.2026 it agreed with
`tzdata` on every one.

**The item** of a key on a plan:

| Attribute | Meaning |
|---|---|
| `plan` | The plan's name. Written by `python -m scripts.workspace`, never by the function. |
| `workspace` | As for a demo key. |
| `month_<YYYY-MM>` | Questions counted in that German month, such as `month_2026-10`. |
| `day_<YYYY-MM-DD>` | Questions counted on that German day, such as `day_2026-10-01`. |

It has no `delete_after`. The TTL would delete the item one day, and the key
would fall back to the demo budget; every question on a plan removes the
attribute, should one be there from the key's demo days or by hand. Attributes
of the demo budget may stay in the item; nothing reads them while it names a
plan.

**Counting.** The first write of every question is the demo's above, and its
condition refuses an item with a `plan`. DynamoDB sends the refused item back
with the refusal, so the function knows the plan without a read, and counts
with a second write:

```
UpdateExpression:
  SET #month = if_not_exists(#month, :zero) + :one,
      #day = if_not_exists(#day, :zero) + :one,
      workspace = if_not_exists(workspace, :workspace)
  REMOVE delete_after[, counts older than last month and yesterday]
ConditionExpression:
  (attribute_not_exists(#month) OR #month < :month_limit)
  AND (attribute_not_exists(#day) OR #day < :day_limit)
```

`#month` and `#day` are the counts of the month and the day the question set
out in. A new month or day is a new attribute that starts from nothing:
nothing is reset at midnight and nothing is locked. Two questions at the
limit cannot both pass, because the condition reads the count as it is when
the write arrives, not as it was when the plan was read. A question that set
out at 23:59:59 and arrives after one from 00:00:00 is counted in its own
day, and finds that day's count still there: each write removes the counts
older than last month and yesterday, at most 10 at a time, and keeps those
two for exactly this case. So an item holds four counts, or a few more for a
moment.

A question on a plan is two writes, the refused one and the count. That is
the number the worst case of the timeouts already allows for (*Configuration*:
for a demo key it is the count and the TTL repair, which a plan item never
needs). A demo question is still one write.

**A full quota** is a **429** with one sentence, in the language of the
request:

- *This key's quota of 6,000 questions for this month is reached. Please get
  in touch with your contact person.* (*Das Kontingent von 6.000 Fragen für
  diesen Monat ist erreicht. Bitte melden Sie sich bei Ihrem
  Ansprechpartner.*)
- the same for *today* (*für heute*) with the day's number.

When both are full, the month's sentence wins: tomorrow would not help. 429
and not 402, because the quota comes back by itself and a 402 says it does
not; the page shows the sentence alone, without the line about a new key it
adds to a 402 ([web/README.md](../web/README.md#what-an-error-says)).

**What fails closed.** No item gives a key on a plan a question that nothing
counts:

- A `plan` that `plans.py` does not know — a typo, a plan since removed,
  `demo` written by hand, a number — is a 500 with the log line
  `budget-plan-unknown`, and nothing is asked. Put the key on a plan again
  with the script.
- A count of this month or this day that is not a number is a 500 with
  `budget-item-broken`, until that month or day is over and its count is no
  longer read. Delete the attribute to end it sooner.
- A count that is missing is the start of its month or day.

**Changing a plan** keeps the key and the counts, so a new plan is no way to
a fresh month: from `firma` to `betrieb` with 8,000 questions in the month,
the next one is refused. A demo key put on a plan asks again at once,
whatever its demo budget says. A key taken off its plan (`plan <name> demo`)
is a demo key with what is left of its demo budget — nothing, if it ever
spent it.

## The answer

An answer from us-east-1, 23.09.2026, before the move to eu-west-1:

```json
{
  "answer": "Open the project and go to Settings > Export, then choose CSV ... [2]",
  "sources": [
    {
      "number": 2,
      "file": "faq--03-projects-and-exports.md",
      "layer": "human",
      "workspace": "quillmoor",
      "score": 0.8147,
      "words": 156,
      "cited": true,
      "excerpt": "# Haldrum — Frequently Asked Questions ## Projects and exports ..."
    }
  ],
  "layers": ["human"],
  "model": "nvidia.nemotron-super-3-120b",
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 1883, "output_tokens": 75},
  "guardrail": {"action": "none", "policies": []},
  "cost_usd": 0.001031,
  "price_date": "2026-09-23",
  "latency_ms": 2483
}
```

| Field | Meaning |
|---|---|
| `answer` | The model's answer. It cites sources as `[n]`; `[2][3]`, `[2, 3]`, `[2; 3]` and ranges such as `[1-3]` count too. The prompt tells the model to answer exactly *The sources do not answer this question.* (in German: *Die Quellen beantworten diese Frage nicht.*) when they do not. |
| `sources` | Every retrieved passage, numbered as the model saw them. `file` is the section file in the documents bucket; `words` its length; `cited` whether the answer cites it; `excerpt` its opening, cut at a word boundary at about 300 characters, with personal details masked by the excerpt guardrail — `null` when that guardrail could not be asked. Empty when the guardrail refused the question. |
| `layers` | The layers of the **cited** sources, most trusted first: `human`, `agent`, `inbox`. An answer with `inbox` here rests partly on documents nobody has confirmed. |
| `model` | The model that answered; `null` when no passage matched and no model was asked. |
| `stop_reason` | Bedrock's stop reason. `max_tokens` means the answer was cut off, `content_filtered` that the model's own filter stopped it, `guardrail_intervened` that the guardrail blocked or masked something — `guardrail` says which. |
| `usage` | Tokens in and out of the model call. |
| `guardrail` | What the guardrail did: `action` is `none`, `masked` (personal details replaced by their type, such as `{PHONE}`, and a note at the end of the answer), `blocked` (the answer is a sentence of Andur's, which replaced the guardrail's own message) or `intervened` (Bedrock says it stepped in, the trace does not say how). `policies` names what acted: `topic:<name>`, `pii:<type>`, `grounding`, `relevance`. |
| `cost_usd` | What this answer cost: model tokens and guardrail text units, each times its price, rounded to six places. `null` when the guardrail reported no units, because a cost without them would be too low. Retrieval, embedding and Lambda are not in it — together under a hundredth of a cent. |
| `price_date` | The day the prices behind `cost_usd` were read off the AWS pricing page. A price changes at AWS without a deploy here; the date says how old the number is. |
| `latency_ms` | Time inside the function, from request to response. |

No passage in the workspace means no model call: the answer is *No sources in
this workspace match the question.* (*Keine Quelle in diesem Arbeitsbereich
passt zur Frage.*), with empty `sources`, zero tokens and a `cost_usd` of 0.

## Language

Andur speaks the visitor's language, not the company's, and knows two:
German and English. The request names it in the body field `language`
(`"de"` or `"en"`, case and surrounding spaces ignored). A missing field, a
field that is not text and any other value mean English; there is no error
for them. The page sends the language it is set to, which is the browser's
until the visitor uses the DE/EN switch ([web/README.md](../web/README.md)).

Every sentence the function says itself comes in both languages, from the
tables at the top of `app/handler.py`:

- the answer when no passage matched, and the sentence the prompt tells the
  model to give when the sources do not answer;
- the notes at the end of an answer the model did not finish
  (`STOP_NOTES`) or whose personal details were masked (`GUARDRAIL_NOTES`);
- the sentence for a question the guardrail refused and for an answer it
  withheld (below);
- the `error` of every status in *Errors*: the 400, 401, 402, 403, 429 and
  502 sentences, the fixed sentence of a 500, and the one for a crash.

What stays as it is: the model's answer is in the language of the
**question**, as before, so an English question on the German page gets an
English answer, and the sentence for "not in the sources" comes in the
language of the **request**. The part of a 502 sentence after the colon is
AWS's own wording and stays English. And the log is English, whatever the
caller asked in: `error` in a `refused` or `failed` line is the English
sentence.

Two errors are always English: a method other than `POST` (405) and a body
that is not JSON or not a JSON object (400). Both are found before the body
has been read, so the language is not known yet, and the page never causes
either. A client that does reads English.

## The guardrail

Every model call goes through one Bedrock guardrail, on the same `converse`
call (`guardrailConfig`), so there is no separate `ApplyGuardrail` call for
the answer. Nemotron 3 Super lists Guardrails among its features on the
`bedrock-runtime` endpoint (its model card, read 23.09.2026); with Nova Pro
through its EU profile the same guardrail refused, masked and withheld on
the live page on 28.09.2026.
The execution role may invoke the model only with the guardrail's deployed
version named in the call; a call without it is refused.

Three policies, set in `infra/stacks/andur_stack.py`:

- **Refused topics:** legal, tax and medical advice, and questions about
  public figures or a person's private life. Checked on the question and on
  the answer. A refused question never reaches the model — the answer is the
  guardrail's message, the status is still 200, and the question is spent.
  It comes back without sources: nothing was answered from them, and their
  excerpts would put the documents' text next to a refusal. An answer the
  grounding check withholds keeps its sources. The topics run in the
  Standard tier, which reads German; the Classic tier reads only English,
  French and Spanish and let 5 of 16 German refusals through (measured with
  `ApplyGuardrail` on 01.10.2026). The Standard tier needs a guardrail
  profile: the EU one, `eu.guardrail.v1:0`, which from eu-west-1 routes the
  checks to six EU regions and no others (`GUARDRAIL_PROFILE_REGIONS`). The
  answer role may apply the guardrail through that profile in those six
  regions only. The four topics are `Legal action`, `Tax returns`,
  `Medical advice` and `Private lives`, named and defined narrowly so that
  a customer's questions about the business's own prices, contracts,
  payment terms and warranty pass: under the names "Legal advice" and "Tax
  advice", 21 of 64 harmless questions were refused. Every change to them is
  measured first with `python -m scripts.topic_probe` (`scripts/README.md`).
- **Masked personal details:** names, e-mail addresses, phone numbers and
  addresses in the answer are replaced by their type, such as `{EMAIL}`. The
  question is not checked, and neither are the passages: the model reads the
  documents as they are, the reader gets the answer masked.
- **Contextual grounding:** an answer the passages do not carry (grounding)
  or that does not answer the question (relevance) is withheld. Both
  thresholds are 0.7, the value the user guide works its example with; AWS
  names no default. Every answered log line carries both scores, which is
  what a threshold is tuned by. The user guide lists English, French and
  Spanish for this check and no tier that adds German; how it scores a
  German answer is not measured yet.

How the message tells them apart: the passages go in a `guardContent`
block qualified `grounding_source` — the reference for the grounding check
and nothing the other policies read. The question goes in a block qualified
`query` and `guard_content` — the query for the relevance check, and text
the topic filter reads. A query may be at most 1,000 characters, the same
limit as a question; the grounding source at most 100,000, which is why a
deployment may ask for at most 30 passages (`MAX_RESULTS`).

"Private lives" is an approximation: the topic filter does
not see the documents, so it cannot know who is in them. It refuses
questions about public figures and private lives; an answer about somebody
the passages do not mention is caught by the grounding check instead.

The guardrail's version is a number, never `DRAFT`. A changed setting
changes a digest in the version's description, and since that description is
create-only, the deploy makes a new version and hands its number to the
function — not at the same instant: CloudFormation updates the execution
role, whose model permission names the version, before the function. For
those seconds the running function still calls with the old version, the
role refuses it, and a question ends in 502 after it was counted. See
[docs/operating.md](../docs/operating.md#deploy).

What the trace says goes to the log, never what it matched: a masked phone
number is logged as `pii:PHONE`, not as the number, and the trace's copy of
the unmasked answer is not read at all.

### What a block says, in the visitor's language

When the guardrail blocks, Bedrock puts its own message in place of the text,
and that message is one fixed English sentence set on the guardrail
(`BLOCKED_INPUT_MESSAGE` and `BLOCKED_OUTPUT_MESSAGE` in the stack). The
handler replaces it with its own sentence in the language of the request, and
the stack's sentences stay as the fallback: they are what a caller reads when
the handler cannot tell what happened. It tells by two things in the same
response, both needed:

1. `stopReason` is `guardrail_intervened`, and
2. the trace holds a policy with the action `BLOCKED` — a topic in either
   assessment, or the grounding or relevance check on the answer
   (`read_guardrail`). A policy that only *masked* something is `ANONYMIZED`
   and does not count.

Then `question_refused` — a topic `BLOCKED` in the **input** assessment, the
only place the question is read — says which sentence: the question was
refused, or the answer was withheld. A refused question never reaches the
model, so there is no output assessment next to it. A stop reason without a
blocking policy in the trace (`intervened`) keeps Bedrock's text and gets the
note of `STOP_NOTES`, and a `BLOCKED` in the trace on a response Bedrock did
not stop is the model's own text: neither is guessed at.

What is measured and what is read off the shape: on the deployed function a
refused question came back with one topic unit and no tokens, and a withheld
one with no model tokens and `grounding` and `relevance` in
`guardrail_policies` ([docs/costs.md](../docs/costs.md); the log of
30.09.2026). Nobody has read the raw trace of a *German* question, and the
test traces in `tests/test_handler.py` are hand-built in Bedrock's shape. The
condition does not look at the language of the question, only at the trace
and the stop reason.

### The excerpts

The guardrail on the model call reads the answer. The `excerpt` of every
source is the opening of a passage as the documents have it, and it goes to
the page too — before build step 6 was finished, a name the answer had
masked stood unmasked in the excerpt next to it (live probe, 23.09.2026). So
the excerpts pass a second guardrail, `andur-excerpt-guardrail`, with one
policy: the same four details, masked. No topics, so an excerpt about VAT is
not refused as tax advice; no grounding, which has nothing to check an
excerpt against; and one price, the sensitive information filter's.

All excerpts go in one `ApplyGuardrail` call, one per line in a single text
block: an excerpt has no line break of its own, and one block of 1,800
characters is two text units where six blocks would be six. The units count
into `cost_usd` as PII units.

It fails closed: a source without its opening is less useful, an unmasked
one is what this is for. Only `NONE` (nothing to mask) and
`GUARDRAIL_INTERVENED` with a text that splits back into as many excerpts as
went in keep them. When the call fails, every `excerpt` is `null` and so is
`cost_usd`, since nothing says what was billed — the log line is
`excerpt-guard-failed` with the error code. When the action is anything
else or missing, or the text does not split back, every `excerpt` is `null`
too, but the call went through and its units are counted, so `cost_usd`
stays an amount — the log line is `excerpt-guard-unusable` with the action
and the counts.

### Where the masking stops

The PII filter recognises personal details statistically, from the words
around them; it keeps no list of the names in the documents. It misses. In
the browser acceptance on 23.09.2026 (the phone question to `pellwick`) the
answer was masked and the excerpts carried `{NAME}` — `excerpt_policies`
in the log line says `pii:NAME` — yet the same first name stood unmasked in
two excerpts, in one of them a few words from a `{NAME}`. A first name
without a surname can slip through, in the answer as much as in an excerpt.

So the masking lowers the chance that a reader sees a personal detail; it
does not rule it out. The log cannot show a miss either: it names what was
masked, never what was not. What must never reach a reader must not be in
the documents.

Behind the retrieval filter sits a second lock: a passage whose `workspace`
metadata is not the requested workspace is dropped before the prompt is
built, and counted as `dropped` in the log line. Each passage goes into the
prompt with at most 3,000 characters (`MAX_SOURCE_CHARACTERS`); a longer one
is cut with a note. The longest passage the demo corpus returns whole has
2,874 characters, so the limit only catches outliers.

`layers` is derived from the citations, not from the model's wording. In
tests on 17.09.2026 Nemotron did not say when an answer rested on an inbox
document, and when told to add a fixed sentence, it added it to answers
without any inbox source too. Citations had one flaw — the model wrote "[3]
and [4] do not contain pricing information", and both counted as cited —
which stopped once the prompt forbade commenting on the sources (4 test
questions, then 10). Citations are still the model's; `layers` is only as
good as they are.

## Errors

Every error is a JSON body `{"error": "<one sentence>"}`, never a traceback.
The sentence is in the language of the request (*Language* above); the tables
below describe the English one.

| Status | When |
|---|---|
| 400 | The body is not a JSON object, has no valid `workspace`, or the question is empty or longer than 1,000 characters. |
| 401 | No `X-Andur-Key` header, or a key that belongs to no workspace. No `WWW-Authenticate` in the response: that header names an authentication scheme for `Authorization`, and the key does not travel there. |
| 402 | A valid key for this workspace with nothing left: its questions are used up, or its window has closed. The sentence says which. Waiting does not help — a new key does. |
| 403 | A valid key, but for another workspace. |
| 405 | Any method other than `POST`. The response carries `Allow: POST`. |
| 429 | With an `error` sentence: a key on a plan has used its questions for this month or for today (*Plans*); the sentence says which, and the quota comes back with the next day or month. Without one: more requests at once than the function may run (5). Lambda refuses those before any code runs, so the body is Lambda's own: `{"Reason": "ReservedFunctionConcurrentInvocationLimitExceeded", "Type": "User", "message": "Rate Exceeded."}`. Five calls from a stranger can hold the service up for everyone; that is a price taken on purpose, see *Configuration*. |
| 500 | The function is set up wrong (an environment variable, unreadable keys), the budget table could not be reached or was throttled (log line `budget-unreadable`, and there a retry may well work), or a bug. The body then only says *The service is not configured.* (*Der Dienst ist nicht eingerichtet.*); the cause is in the log. |
| 502 | Bedrock failed: throttled or busy twice in a row, timed out, access denied (for example a model not enabled in the account), or the model returned an empty answer. The sentence names the cause. |

Two more can arrive that the function never saw. Neither body is
`{"error": ...}`, which is how a client tells them from a refusal:

| Status | When |
|---|---|
| 403 | The request was not accepted before the handler ran. Three ways to earn it, all measured 17.09.2026: a call sent **straight to the function URL**, which answers `{"Message": "Forbidden"}` because only CloudFront may invoke it; a call through CloudFront **without `x-amz-content-sha256`**, which comes back as Lambda's `{"message": "The request signature we calculated does not match the signature you provided. ..."}`; or a broken deployment, where CloudFront lacks the permission. None of them is a wrong key — that is a 401 with a sentence. |
| 504 | The function did not answer within the 120 seconds CloudFront waits, and CloudFront returns an HTML error page of its own. The function itself may run 180 seconds and carries on to the end; the question is counted either way. See *Configuration*. |

The checks run in this order: method, body, workspace, settings, key,
question, budget. Settings sit in the middle because everything after them
needs them, and a deployment set up wrong is a 500 whatever the request looks
like. A request without a valid key learns nothing about its question, and a
question the handler refuses anyway costs its key nothing.

402 is none of the three it could be mistaken for. 401 means the key is
unknown, 403 that it belongs to another workspace — this key is valid and
opens this workspace. 429 is Lambda's concurrency limit and means *too many
at once, try again in a moment*; a spent budget means the opposite, that
trying again will not help. A key on a plan never gets a 402: its quota
comes back, so a full one is a 429, with a sentence that Lambda's has not.

## Logs

One JSON line per request in the log group `/aws/lambda/andur-answer`, kept
30 days (these from us-east-1, 23.09.2026; the `answered` line is from before
the excerpt guardrail, so it has no `excerpt_policies` yet):

```json
{"event": "answered", "status": 200, "workspace": "pellwick", "model": "nvidia.nemotron-super-3-120b", "input_tokens": 1326, "output_tokens": 32, "hits": 5, "dropped": 0, "cited": 2, "guardrail": "none", "guardrail_policies": [], "grounding_score": 0.99, "relevance_score": 1.0, "guardrail_units": {"topicPolicyUnits": 2, "sensitiveInformationPolicyUnits": 1, "contextualGroundingPolicyUnits": 5}, "cost_usd": 0.00112, "price_date": "2026-09-23", "latency_ms": 3430}
{"event": "refused", "status": 401, "workspace": "quillmoor", "error": "The key is not valid.", "latency_ms": 0}
{"event": "refused", "status": 402, "workspace": "pellwick", "error": "This key's 10 questions are used up; a new key is needed.", "latency_ms": 31}
```

The question is never logged, nor the key, nor its hash — with one
exception: a `crashed` line carries a traceback, and an exception message
inside it can quote what the code choked on.

What the excerpt guardrail masked is in `excerpt_policies` of the
`answered` line, by type. `hits` counts the passages that reached the model,
also for a refused question that returns none of them.

A Bedrock failure adds a `retrieve-failed` or `converse-failed` line with
the exception class and the AWS error code, never the service's own message:
a message quotes what the call choked on, and for `converse` that is the
prompt — the question and passages out of a company's documents. A model
call AWS turned away gives a `converse-rejected` line first, with the error
code, the time into the request (`elapsed_ms`) and whether the second call is
made (`retry`); counting these lines tells how often it happens. Parameter
Store and the budget table do add the full exception (`keys-unreadable`,
`budget-unreadable`); neither call carries a question. A budget item with a
plan nobody defined gives a `budget-plan-unknown` line, one whose count of
this month or day is not a number a `budget-item-broken` line (*Plans*). A key shared by two
workspaces gives a `duplicate-key` line. A failed reread of the keys
is a `keys-reload-failed` line, with `stale: true` when the keys were already
past their five minutes and the old ones went on deciding. A key path that is
readable and empty is not a failed reread: it ends the request as a `failed`
line with status 500 and the path in its `error` field. A bug is one
`crashed` line with the traceback in a field. The first line after a cold
start names the boto3 and botocore versions of the runtime.

```bash
aws logs tail /aws/lambda/andur-answer --region eu-west-1 --follow
```

`cost_usd` in the `answered` line is the answer's cost at the prices of
`price_date`: tokens and guardrail units, each times the price per thousand
the stack hands over. Prices change at AWS without a deploy here, and the
token and unit counts in the same line do not — so an old line can always be
priced again. `guardrail_units` is what the trace reported per policy;
`null` there, and in `cost_usd`, means the runtime returned no units.

## Configuration

The stack sets everything through environment variables; nothing is read
from files.

| Variable | Set to |
|---|---|
| `KNOWLEDGE_BASE_ID` | The stack's knowledge base. |
| `MODEL_ID` | `ANSWER_MODEL_ID` in `infra/stacks/andur_stack.py`: `eu.amazon.nova-pro-v1:0`, Nova Pro through its EU inference profile. |
| `REGION` | The stack's region, `eu-west-1`. |
| `NUMBER_OF_RESULTS` | Passages per question, `10`. Retrieve may return fewer. |
| `API_KEY_PARAMETER_PATH` | `/andur/api-keys`. |
| `BUDGET_TABLE_NAME` | `andur-key-budgets`, the table that counts per key. |
| `QUESTION_LIMIT` | Questions a key gets, `10`, unless its item says otherwise. |
| `WINDOW_HOURS` | How long its window stays open, `48`, from the first question. |
| `KEEP_DAYS` | How long an item is kept, `365`, from the first question. Has to outlast the window. |
| `GUARDRAIL_ID` | The stack's guardrail. |
| `GUARDRAIL_VERSION` | Its deployed version, a number. `DRAFT` is refused. |
| `EXCERPT_GUARDRAIL_ID` | The stack's excerpt guardrail. |
| `EXCERPT_GUARDRAIL_VERSION` | Its deployed version, a number. |
| `PRICE_INPUT_PER_1K_TOKENS` | USD per 1,000 input tokens of the answering model, `0.00092`. |
| `PRICE_OUTPUT_PER_1K_TOKENS` | USD per 1,000 output tokens, `0.00368`. |
| `PRICE_TOPIC_PER_1K_UNITS` | USD per 1,000 text units of the topic filter, `0.15`. |
| `PRICE_PII_PER_1K_UNITS` | USD per 1,000 text units of the sensitive information filter, `0.10`. |
| `PRICE_GROUNDING_PER_1K_UNITS` | USD per 1,000 text units of the grounding check, `0.10`. |
| `PRICE_DATE` | The day those prices were read, `2026-09-28`, as `YYYY-MM-DD`. |

The prices are the eu-west-1 standard-tier prices of Amazon Bedrock, read
28.09.2026 from the AWS Price List API, the source of the pricing page; the
source is in a comment next to them in the stack. The token prices come
from `infra/answer_models.py`, looked up by the model id the stack deploys,
so a model never goes out with another model's prices, and a model without
a row there does not synthesise. The guardrail prices are the same as in
us-east-1. A text unit is up to 1,000 characters of text one policy reads.

There is no variable for the address or the key header: the function reads
the key from `KEY_HEADER` in `handler.py`, and nothing in it knows what it is
reached through.

A model that is only offered through an inference profile, such as the
Claude and Nova models in eu-west-1, is given by its EU profile id
(`eu.…`). The stack then allows `InvokeModel` on that profile, with the
guardrail condition, and on the model itself in the EU regions (`eu-*`) only
when the call comes through that profile (`bedrock:InferenceProfileArn`).
A global profile (`global.…`) is never used: it may route the question out
of the EU.

Timeouts: every AWS call has a connect and a read timeout (`CLIENT_LIMITS`
in `aws.py`). Reading the keys, counting the budget, retrieving and masking
the excerpts are tried twice; the model call once, because a converse
that timed out may already have produced its answer, and a second attempt
would pay for the input tokens again. The exception is a call AWS documents
as not run — throttled over a quota, a service unable to handle it, a model
not ready (`MODEL_REJECTION_CODES` in `handler.py`; an internal error is not
among them, the model may have run). The handler makes it once more after 2
seconds, but only up to second 50 of the request (`latest_model_retry_start()`
in `aws.py`): the pause, a model call that runs into its 3 + 40 seconds and
the excerpt guardrail's 20 after it end by second 115, and the 5 seconds to
CloudFront's 120 are for the cold start and the way in, which the handler's
clock does not see (`ORIGIN_MARGIN_SECONDS`). A retried count can charge one question
twice, which is the safe direction: the count runs ahead, never behind. The
budget call waits the shortest of all, since it sits in front of every
question. The excerpt guardrail is a `bedrock-runtime` call with limits of
its own (2 + 5 seconds): under the model's 40 it would push the worst case
past the function's timeout. Added up, the worst case is 171 seconds —
Parameter Store counts twice, because an
unknown key makes the function read the keys again before it answers 401, and
the budget table counts twice, because an item whose own window outlasts it is
put right with a second write, and a key on a plan is counted with a second
write after the demo's refused it. A request with the second model call ends
by second 115, so it does not add to the 171. The 171 hold for up to ten
workspaces: Parameter Store hands out the keys ten to a page, and every
further page is one more call, read twice, 44 seconds more in the worst case. The
function's timeout is 180, so a hanging Bedrock call still ends as a 502 with
a reason, not as a bare timeout.

One layer does not nest, and knowingly so. **CloudFront waits 120 seconds**
(`ORIGIN_RESPONSE_TIMEOUT_SECONDS` in the stack) against the function's 180.
That is the whole of what the response-timeout quota allows without an
approved increase — the quota is 1 to 120 seconds. So an answer that took
longer than 120 seconds reaches the browser as CloudFront's 504 while the
function runs it to the end and the question stays counted. The alternative
would be a function timeout under 120, which would cut off answers that are
merely slow instead of merely reporting them late. An answer took 0.7 to 3
seconds in us-east-1 before there were guardrails (18.09.2026), 1.8 to 3.8
seconds there with both guardrails (four answered questions, 23.09.2026),
and 0.7 to 4.4 seconds inside the function with Nova Pro on the page in
eu-west-1 (four questions, 28.09.2026), so this is a corner, not a case.

At most 5 requests run at the same time (`ANSWER_MAX_CONCURRENCY` in the
stack); Lambda answers the rest with 429. Five concurrent calls from a
stranger therefore hold the service up for everyone — taken on purpose:
capping the bill matters more here than keeping a demo reachable, and a WAF in
front of the URL is the answer if that ever becomes a real problem.

## Packaging

The function package is this folder without `README.md` and caches. boto3 is
not packaged: the `python3.14` runtime brought boto3 and botocore 1.42.97 in
`eu-west-1` on 28.09.2026 (the cold-start log line), the same as in
`us-east-1` on 17.09.2026; both know `bedrock-agent-runtime` and `converse`.
