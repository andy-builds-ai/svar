# scripts — the rehearsal of a new workspace across Andur's parts

One command, run from the repository root. What the scripts need (install, AWS login, the deployed stacks): [taug/scripts/README.md](../taug/scripts/README.md#what-they-need).

| Command | Does |
|---|---|
| `python -m scripts.rehearsal` | Sets up a new workspace on the deployed stacks with the same commands as any other, runs the measurements against it, and times every step. |

## rehearsal — a new workspace, set up and measured, step by step

```bash
python -m scripts.rehearsal --workspace kalvenstett --source ../companies \
  --question "Was kostet eine Stunde für einen Gesellen?" --fact "68 Euro" --plan betrieb \
  --out-dir ../rehearsal --list
```

One command for what a new workspace needs once the stacks are deployed, in
this order, each step timed on its own:

1. **check** — the signed-in identity (with `--expect-role`, the run stops
   under any other role), Taug's stack and Svar's deployed and finished, how
   long the bootstrap and the last deploy of each took by their CloudFormation
   events, every
   address of `WebUrl` in the region and in no other by AWS's
   `ip-ranges.json`, and that plain HTTP serves nothing while HTTPS serves the
   page and answers a question without a key with 401, both with the security
   headers of `svar/app/page.py`. On port 80 an address that refuses the
   connection, or lets it time out before taking it, is closed, a timeout
   named apart, since a block on this computer's side looks the same; one
   that takes the connection and then breaks it off, closes it or stays silent
   without an answer fails the step, since the request, and a caller's key
   with it, had already arrived unencrypted;
2. **setup** — `ingest --check`, `workspace add` (its stdout, the key, is
   thrown away; the key stays in Parameter Store), `ingest`, `retrieve` (every
   hit from the new workspace), the first question through `ask --language
   de` (with `--fact`, its answer must contain that text); handing the key over
   and asking on the page in a browser are steps by hand;
3. **apart** — the demo workspaces' documents, which the measurements need; a
   search in `--other-workspace` that must find hits there and none from a
   document of the new workspace, by file name as well as by its metadata, so a
   document filed under the wrong workspace shows (a name with spaces is read
   as it is and with `%20`); and the new key asked there
   and refused with HTTP 401, which is what keeps a business to its own
   documents;
4. **measure** — `topic_probe --deployed` at the same time as `tool_probe` and
   `grounding_probe` (the first calls only the topic model, the other two never
   do, so they share no model quota), then `evaluate` on the English and the
   German questions and on the new workspace's own German ones,
   `evaluation-questions-<workspace>.json`, and `control_eval --workspace` on
   its questions in `evaluation-questions-control.json`, with retrieval in the
   region, all with `--model`, by default the answer model `REGIONS` gives the
   region. A workspace without such German questions has those two steps
   skipped, named as skipped in the summary, never as passed, so a run for a
   new workspace never measures another one in their place; a file of that name
   with questions of another workspace or language is refused before anything
   runs. A measurement that ends with a failed call (exit code 2) fails its
   step, since nothing was measured; wrong answers are a result and pass;
5. **after** — where the deployed stacks keep and process data as AWS reports
   it (bucket, embedding model, vector index, function, the two models, the two
   guardrails, budget table, log group, and whether Bedrock logs the model
   calls), written to `residency.md`; and the events of the answer function's
   log since this run began, written to `function-log.json`, Lambda's own
   timeouts and runtime errors counted too. Either fails on anything outside
   the region, an inference profile, a missing model, a log of the model calls,
   a crash or a timeout. Run alone with `--only`, the log step sees only its
   own seconds.

`--region` takes only a region whose function is its own address, today
`eu-central-1`: behind CloudFront the address checks could never pass. The
deploy time names when the deploy finished, since a deploy that changed nothing
leaves no event and the last one may be older. The stacks are Svar's,
`AndurStack` (`--stack-name`), and Taug's, `TaugStack` (`--taug-stack-name`);
the run reads the outputs of both.

A step by hand stands in the plan as such. With `--manual skip`, the default,
it is recorded as open and the run goes on; `--manual wait` stops there until
Enter and times it. `--list` prints the plan and calls nothing; `--from <step>`
goes on from a step, `--only <step>` runs single steps, the identity always
first. The run stops at the first step that fails (in a group of steps run at
once, the other lanes finish first); any error in a step, a network break too,
fails that step. A second full run for the same workspace
stops at `key`, since `workspace add` never replaces a key: go on with
`--from documents`. `ANDUR_API_KEY` from the shell is not handed to the
commands, so `ask` uses the new workspace's own key.

Everything goes to `--out-dir`, which must lie outside the repository: one log
per command that ran, the measurements' `--out` files, and `run-<time>.json` and `.md`
with every step's start, seconds, exit code, status and result, the seconds
per phase, and the wall clock. At the end, also after Ctrl+C, the summary is
written and the account id is replaced by `<account>` in every file there; on
the screen it never shows. It deploys nothing and deletes nothing; the
measurements create and delete their probe guardrails as they do alone, and
cost what they cost alone. On 07.10.2026 the whole run in eu-central-1, 22
steps, took 11 minutes 12 seconds and cost 0.70 USD (at most 0.75) with the
Frankfurt answer model, when `sources` still asked all 42 questions,
Pellwick's too.

| Exit code | Meaning |
|---|---|
| 0 | Every step passed, was left open by hand, or was skipped for want of the workspace's own German questions. |
| 1 | A step failed; the summary names it and its log. |
| 2 | Unknown step, `--out-dir` inside the repository, or `evaluation-questions-<workspace>.json` holding questions of another workspace or language. Nothing was called. |
