# app — the answer function

`handler.py` is the Lambda function behind Andur's Function URL. It takes a
question for one workspace, counts it against the budget of the key that
asked, retrieves passages from the knowledge base with a `workspace` filter,
asks the answering model to answer from those passages only, and returns the
answer with its sources and the layer of each source.

It runs only while a question is in flight; there is nothing to pay while
nobody asks. `aws.py` holds the AWS clients with their timeouts and retries.
Lambda packages this folder and imports both modules by their plain names,
`handler` and `aws`; the tests import them the same way
([tests/conftest.py](../tests/conftest.py)). Nothing else is needed but the
standard library and the boto3 the Lambda runtime brings.

## Asking

```bash
python -m scripts.ask "How do I export project data?" --workspace quillmoor
```

That is the short way: the script reads the URL from the stack outputs and
the key from Parameter Store, and never prints the key
([scripts/README.md](../scripts/README.md)). The request underneath is plain
HTTP:

```bash
curl -s -X POST "$URL" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"question": "How do I export project data?", "workspace": "quillmoor"}'
```

`$URL` is the stack output `FunctionUrl`. `workspace` must be the workspace
the key belongs to. A question is 1 to 1,000 characters.

## Getting a key

Each workspace has one key, generated inside AWS during `cdk deploy` and
stored as a SecureString parameter `/andur/api-keys/<workspace>`. It is not in
the repository, the template or any log. To copy it to the clipboard without
it showing up on screen:

```bash
printf '%s' "$(aws ssm get-parameter --region us-east-1 --name /andur/api-keys/quillmoor \
  --with-decryption --query Parameter.Value --output text)" | clip   # macOS: pbcopy
```

Or keep it in a shell variable for curl: `KEY=$(aws ssm get-parameter ... --output text)`.

In Git Bash on Windows, put `MSYS_NO_PATHCONV=1` in front of any command with
`/andur/api-keys/...`: otherwise Git Bash turns the parameter name into a
Windows path and AWS rejects it.

A deploy keeps existing keys. To replace one, delete the parameter and deploy
again. The function keeps the keys it has read for 5 minutes
(`KEY_CACHE_SECONDS`), so **a deleted or replaced key keeps working for up to
5 minutes** — and longer if Parameter Store cannot be read at the moment the
5 minutes are up, because expired keys that cannot be replaced go on deciding
rather than turning every request into a 500. To revoke a key at once, change
the function after deleting it —
every running instance is replaced, and the new ones read the keys fresh:

```bash
aws lambda update-function-configuration --region us-east-1 --function-name andur-answer --description "keys rotated"
```

A new key works at once: a key the function does not know makes it read the
keys again before it answers 401. It does that at most every 10 seconds per
running instance (`KEY_RELOAD_MIN_SECONDS`), so a stream of wrong keys cannot
turn into a stream of Parameter Store reads. A key stored for two workspaces
opens neither; the log names both.

## Keys are budgets, not passwords

A key does not open its workspace without end. It carries an allowance: **10
questions**, and **48 hours** counted from the first of them. A key that has
spent either is refused with 402 before anything is paid for. A key that is
never used never starts its clock.

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
| `keep_seconds` | How long the item is kept. Default `KEEP_DAYS`. |
| `delete_after` | `first_used_at + keep_seconds`; DynamoDB's TTL watches this. |

Checking and counting are one conditional write, so five questions arriving
together cannot all see the same count and all pass:

```
ConditionExpression:
  attribute_not_exists(first_used_at)
  OR (questions_used < question_limit AND expires_at > :now)
```

**A question counts once it has passed the budget**, even if Bedrock then
fails with a 502. Nothing is given back. That is one rule instead of two, and
a caller who asks again knows what they paid.

### Setting a budget for one key

Both commands need the key's hash, which you can produce without the key ever
reaching the screen. Set `WORKSPACE` first; on macOS use `shasum -a 256` in
place of `sha256sum`:

```bash
HASH=$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --region us-east-1 \
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
aws dynamodb put-item --region us-east-1 --table-name andur-key-budgets \
  --condition-expression 'attribute_not_exists(first_used_at)' \
  --item "{\"key_hash\":{\"S\":\"$HASH\"},\"question_limit\":{\"N\":\"2\"},\"window_seconds\":{\"N\":\"21600\"},\"keep_seconds\":{\"N\":\"604800\"}}"
```

**Reset** a budget — delete the item, and the key starts over with the
defaults:

```bash
aws dynamodb delete-item --region us-east-1 --table-name andur-key-budgets \
  --key "{\"key_hash\":{\"S\":\"$HASH\"}}"
```

`keep_seconds` has to outlast `window_seconds`: an item that disappears while
its own window is still open would hand the key a fresh budget. The function
checks that for **its own defaults** and refuses to start if they break the
rule — it does not check an item somebody wrote by hand. That is why the set
command above writes both numbers together, and why they are worth a second
look before you paste it.

`expires_at` and `delete_after` are not yours to set. The function derives
both from `first_used_at` plus the two lengths and rewrites them on every
question, so a value put there by hand is gone with the next one. Set
`question_limit`, `window_seconds` and `keep_seconds`; the rest follows. And
a preset item never carries `first_used_at` — that attribute is what the
condition reads to tell "not used yet" from "running", so writing it by hand
would start a window that nobody opened.

**A replaced key has a fresh budget by itself.** Deleting the parameter and
deploying again gives the workspace a new key, and a new key has a different
hash and therefore no item — the old item stays behind and expires on its
own. That also means the reverse: a key stays spent only as long as its item
exists. After `KEEP_DAYS` (365 by default, counted from the first question)
the item is gone and the key would work again. To end a key for good, replace
it.

## The answer

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
  "latency_ms": 2483
}
```

| Field | Meaning |
|---|---|
| `answer` | The model's answer. It cites sources as `[n]`; `[2][3]`, `[2, 3]`, `[2; 3]` and ranges such as `[1-3]` count too. The prompt tells the model to answer exactly *The sources do not answer this question.* when they do not. |
| `sources` | Every retrieved passage, numbered as the model saw them. `file` is the section file in the documents bucket; `words` its length; `cited` whether the answer cites it; `excerpt` its opening, cut at a word boundary at about 300 characters. |
| `layers` | The layers of the **cited** sources, most trusted first: `human`, `agent`, `inbox`. An answer with `inbox` here rests partly on documents nobody has confirmed. |
| `model` | The model that answered; `null` when no passage matched and no model was asked. |
| `stop_reason` | Bedrock's stop reason. `max_tokens` means the answer was cut off, `content_filtered` and `guardrail_intervened` that a filter stopped it; in all three cases the answer says so at its end. |
| `usage` | Tokens in and out of the model call — what the call costs. |
| `latency_ms` | Time inside the function, from request to response. |

No passage in the workspace means no model call: the answer is *No sources in
this workspace match the question.*, with empty `sources` and zero tokens.

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

| Status | When |
|---|---|
| 400 | The body is not a JSON object, has no valid `workspace`, or the question is empty or longer than 1,000 characters. |
| 401 | No `Authorization: Bearer <key>` header, or a key that belongs to no workspace. The function sends `WWW-Authenticate: Bearer`; the Function URL passes it on renamed as `x-amzn-Remapped-www-authenticate` (observed 17.09.2026). |
| 402 | A valid key for this workspace with nothing left: its questions are used up, or its window has closed. The sentence says which. Waiting does not help — a new key does. |
| 403 | A valid key, but for another workspace. |
| 405 | Any method other than `POST`. The response carries `Allow: POST`. |
| 429 | More requests at once than the function may run (5). Lambda refuses them before any code runs, so the body is Lambda's own: `{"Reason": "ReservedFunctionConcurrentInvocationLimitExceeded", "Type": "User", "message": "Rate Exceeded."}`. Five calls from a stranger can hold the service up for everyone; that is a price taken on purpose, see *Configuration*. |
| 500 | The function is set up wrong (an environment variable, unreadable keys), the budget table could not be reached or was throttled (log line `budget-unreadable`, and there a retry may well work), or a bug. The body then only says *The service is not configured.*; the cause is in the log. |
| 502 | Bedrock failed: throttled, timed out, access denied (for example a model not enabled in the account), or the model returned an empty answer. The sentence names the cause. |

The checks run in this order: method, body, workspace, settings, key,
question, budget. Settings sit in the middle because everything after them
needs them, and a deployment set up wrong is a 500 whatever the request looks
like. A request without a valid key learns nothing about its question, and a
question the handler refuses anyway costs its key nothing.

402 is none of the three it could be mistaken for. 401 means the key is
unknown, 403 that it belongs to another workspace — this key is valid and
opens this workspace. 429 is Lambda's concurrency limit and means *too many
at once, try again in a moment*; a spent budget means the opposite, that
trying again will not help.

## Logs

One JSON line per request in the log group `/aws/lambda/andur-answer`, kept
30 days:

```json
{"event": "answered", "status": 200, "workspace": "quillmoor", "model": "nvidia.nemotron-super-3-120b", "input_tokens": 1883, "output_tokens": 75, "hits": 6, "dropped": 0, "cited": 1, "latency_ms": 2483}
{"event": "refused", "status": 401, "workspace": "quillmoor", "error": "The key is not valid.", "latency_ms": 0}
{"event": "refused", "status": 402, "workspace": "pellwick", "error": "This key's 10 questions are used up; a new key is needed.", "latency_ms": 31}
```

The question is never logged, nor the key, nor its hash — with one
exception: a `crashed` line carries a traceback, and an exception message
inside it can quote what the code choked on.

Bedrock and Parameter Store failures add a line with the full exception, a
budget table that cannot be reached a `budget-unreadable` line, and a key
shared by two workspaces a `duplicate-key` line. A failed reread of the keys
is a `keys-reload-failed` line, with `stale: true` when the keys were already
past their five minutes and the old ones went on deciding. A bug is one
`crashed` line with the traceback in a field. The first line after a cold
start names the boto3 and botocore versions of the runtime.

```bash
aws logs tail /aws/lambda/andur-answer --region us-east-1 --follow
```

There is no price calculation in the code: prices change, token counts do
not. Multiply `input_tokens` and `output_tokens` by the model's current price.

## Configuration

The stack sets everything through environment variables; nothing is read
from files.

| Variable | Set to |
|---|---|
| `KNOWLEDGE_BASE_ID` | The stack's knowledge base. |
| `MODEL_ID` | `ANSWER_MODEL_ID` in `infra/stacks/andur_stack.py`: `nvidia.nemotron-super-3-120b`. |
| `REGION` | The stack's region, `us-east-1`. |
| `NUMBER_OF_RESULTS` | Passages per question, `6`. Retrieve may return fewer. |
| `API_KEY_PARAMETER_PATH` | `/andur/api-keys`. |
| `BUDGET_TABLE_NAME` | `andur-key-budgets`, the table that counts per key. |
| `QUESTION_LIMIT` | Questions a key gets, `10`, unless its item says otherwise. |
| `WINDOW_HOURS` | How long its window stays open, `48`, from the first question. |
| `KEEP_DAYS` | How long an item is kept, `365`, from the first question. Has to outlast the window. |

A model that is only offered through an inference profile, such as the
Claude models, needs a different `InvokeModel` resource in the stack than a
plain model id.

Timeouts: every AWS call has a connect and a read timeout (`CLIENT_LIMITS`
in `aws.py`). Reading the keys, counting the budget and retrieving are tried
twice; the model call only once, because a converse that timed out may
already have produced its answer, and a second attempt would pay for the
input tokens again. A retried count can charge one question twice, which is
the safe direction: the count runs ahead, never behind. The budget call waits
the shortest of the four, since it sits in front of every question. Added up,
the worst case is 135 seconds — Parameter Store counts twice, because an
unknown key makes the function read the keys again before it answers 401. The
function's timeout is 180, so a hanging Bedrock call still ends as a 502 with
a reason, not as a bare timeout.

At most 5 requests run at the same time (`ANSWER_MAX_CONCURRENCY` in the
stack); Lambda answers the rest with 429. Five concurrent calls from a
stranger therefore hold the service up for everyone — taken on purpose:
capping the bill matters more here than keeping a demo reachable, and a WAF in
front of the URL is the answer if that ever becomes a real problem.

## Packaging

The function package is this folder without `README.md` and caches. boto3 is
not packaged: the `python3.14` runtime brought boto3 and botocore 1.42.97 in
`us-east-1` on 17.09.2026, which know `bedrock-agent-runtime` and `converse`.
