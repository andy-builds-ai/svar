# app — the answer function

`handler.py` is the Lambda function behind Andur's Function URL. It takes a
question for one workspace, retrieves passages from the knowledge base with a
`workspace` filter, asks the answering model to answer from those passages
only, and returns the answer with its sources and the layer of each source.

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
5 minutes**. To revoke a key at once, change the function after deleting it —
every running instance is replaced, and the new ones read the keys fresh:

```bash
aws lambda update-function-configuration --region us-east-1 --function-name andur-answer --description "keys rotated"
```

A new key works at once: a key the function does not know makes it read the
keys again before it answers 401. It does that at most every 10 seconds per
running instance (`KEY_RELOAD_MIN_SECONDS`), so a stream of wrong keys cannot
turn into a stream of Parameter Store reads. A key stored for two workspaces
opens neither; the log names both.

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
| `sources` | Every retrieved passage, numbered as the model saw them. `file` is the section file in the documents bucket; `words` its length; `cited` whether the answer cites it; `excerpt` its first 300 characters. |
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
| 403 | A valid key, but for another workspace. |
| 405 | Any method other than `POST`. The response carries `Allow: POST`. |
| 429 | More requests at once than the function may run (5). Lambda refuses them before any code runs, so the body is Lambda's own: `{"Reason": "ReservedFunctionConcurrentInvocationLimitExceeded", "Type": "User", "message": "Rate Exceeded."}`. |
| 500 | The function is set up wrong (an environment variable, unreadable keys) — the body then only says *The service is not configured.*, the cause is in the log — or a bug. |
| 502 | Bedrock failed: throttled, timed out, access denied (for example a model not enabled in the account), or the model returned an empty answer. The sentence names the cause. |

The checks run in this order: method, body, workspace, key, question. A
request without a valid key learns nothing about its question.

## Logs

One JSON line per request in the log group `/aws/lambda/andur-answer`, kept
30 days:

```json
{"event": "answered", "status": 200, "workspace": "quillmoor", "model": "nvidia.nemotron-super-3-120b", "input_tokens": 1883, "output_tokens": 75, "hits": 6, "dropped": 0, "cited": 1, "latency_ms": 2483}
{"event": "refused", "status": 401, "workspace": "quillmoor", "error": "The key is not valid.", "latency_ms": 0}
```

The question is never logged, nor the key. Bedrock and Parameter Store
failures add a line with the full exception, a key shared by two workspaces
a `duplicate-key` line, a failed reread of the keys a `keys-reload-failed`
line. A bug is logged as one `crashed` line with the traceback in a field.
The first line after a cold start names the boto3 and botocore versions of
the runtime.

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

A model that is only offered through an inference profile, such as the
Claude models, needs a different `InvokeModel` resource in the stack than a
plain model id.

Timeouts: every AWS call has a connect and a read timeout (`CLIENT_LIMITS`
in `aws.py`). Reading the keys and retrieving are tried twice; the model
call only once, because a converse that timed out may already have produced
its answer, and a second attempt would pay for the input tokens again.
Added up, the worst case is 97 seconds; the function's timeout is 180. A
hanging Bedrock call therefore ends as a 502 with a reason, not as a bare
timeout.

At most 5 requests run at the same time (`ANSWER_MAX_CONCURRENCY` in the
stack); Lambda answers the rest with 429.

## Packaging

The function package is this folder without `README.md` and caches. boto3 is
not packaged: the `python3.14` runtime brought boto3 and botocore 1.42.97 in
`us-east-1` on 17.09.2026, which know `bedrock-agent-runtime` and `converse`.
