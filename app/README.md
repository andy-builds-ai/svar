# app — the answer function

`handler.py` is the Lambda function behind Andur's Function URL. It takes a
question for one workspace, retrieves passages from the knowledge base with a
`workspace` filter, asks the answering model to answer from those passages
only, and returns the answer with its sources and the layer of each source.

It runs only while a question is in flight; there is nothing to pay while
nobody asks. The file stands alone — standard library plus the boto3 the
Lambda runtime brings — because Lambda packages this folder and nothing else.

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
again. The function reads the keys once per cold start, so **a deleted or
replaced key keeps working until the function starts cold again** — after a
few idle minutes, or at once after any change to the function. To revoke a
key right away, change the function after deleting it, for example with
`aws lambda update-function-configuration --function-name andur-answer
--description "keys rotated"`.

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
| `answer` | The model's answer. It cites sources as `[n]`. If the sources do not answer the question, it is exactly *The sources do not answer this question.* |
| `sources` | Every retrieved passage, numbered as the model saw them. `file` is the section file in the documents bucket; `words` its length; `cited` whether the answer cites it; `excerpt` its first 300 characters. |
| `layers` | The layers of the **cited** sources, most trusted first: `human`, `agent`, `inbox`. An answer with `inbox` here rests partly on documents nobody has confirmed. |
| `model` | The model that answered; `null` when no passage matched and no model was asked. |
| `stop_reason` | Bedrock's stop reason. `max_tokens` means the answer was cut off; the answer then says so at its end. |
| `usage` | Tokens in and out of the model call — what the call costs. |
| `latency_ms` | Time inside the function, from request to response. |

No passage in the workspace means no model call: the answer is *No sources in
this workspace match the question.*, with empty `sources` and zero tokens.

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
| 401 | No `Authorization: Bearer <key>` header, or a key that belongs to no workspace. |
| 403 | A valid key, but for another workspace. |
| 405 | Any method other than `POST`. |
| 500 | The function is set up wrong (an environment variable, unreadable keys), or a bug. |
| 502 | Bedrock failed: throttled, timed out, access denied (for example a model not enabled in the account). The sentence names the cause. |

The checks run in this order: method, body, workspace, key, question. A
request without a valid key learns nothing about its question.

## Logs

One JSON line per request in the log group `/aws/lambda/andur-answer`, kept
30 days:

```json
{"event": "answered", "status": 200, "workspace": "quillmoor", "model": "nvidia.nemotron-super-3-120b", "input_tokens": 1883, "output_tokens": 75, "hits": 6, "cited": 1, "latency_ms": 2483}
{"event": "refused", "status": 401, "workspace": "quillmoor", "error": "The key is not valid.", "latency_ms": 0}
```

The question is never logged, nor the key. Bedrock and Parameter Store
failures add a line with the full exception. The first line after a cold
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

A model that is only offered through an inference profile, such as the
Claude models, needs a different `InvokeModel` resource in the stack than a
plain model id.

Timeouts: every AWS call has a connect and read timeout and one retry
(`CLIENT_TIMEOUTS` in `handler.py`). Added up, the worst case is 146
seconds; the function's timeout is 180. A hanging Bedrock call therefore ends
as a 502 with a reason, not as a bare timeout.

## Packaging

The function package is this folder without `README.md` and caches. boto3 is
not packaged: the `python3.14` runtime brought boto3 and botocore 1.42.97 in
`us-east-1` on 17.09.2026, which know `bedrock-agent-runtime` and `converse`.
