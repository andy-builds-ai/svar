# scripts — operating the knowledge base

Five commands, all run from the repository root:

| Command | Does |
|---|---|
| `python -m scripts.ingest` | Checks a local folder, uploads it to the documents bucket, syncs the knowledge base. |
| `python -m scripts.retrieve` | Asks the knowledge base a question inside one workspace and prints the hits. |
| `python -m scripts.ask` | Asks the deployed answer function a question and prints the answer with its sources. |
| `python -m scripts.evaluate` | Asks 30 fixed questions of each answer model the way the function does and prints hit rate, citations and cost. |
| `python -m scripts.workspace` | Adds a workspace's key, puts it on a plan, or removes a workspace: key, budget items, documents and vectors. |

## What they need

- `pip install -e ".[dev]"` — brings `boto3` with its `crt` extra. The extra
  is not optional here: without it botocore cannot read the credentials that
  `aws login` writes, and every AWS call fails with "Missing Dependency".
- A valid AWS login for the account the stack is deployed in. When it has
  run out, both scripts stop with a message that says so; sign in with
  `aws login` and rerun.
- The deployed stack. Bucket, knowledge base id, data source id, web address,
  key path and budget table are read from the outputs of `AndurStack` in `eu-west-1`. The
  region is pinned like in `infra/app.py`, so a shell profile pointing
  elsewhere does not matter. Each value can be passed by hand instead:
  `--bucket`, `--knowledge-base-id`, `--data-source-id`, `--url`, and
  `--stack-name` / `--region` for another stack. `--url` takes the web
  address, not the function URL — `ask` appends `/ask` to it itself.

## ingest

```bash
python -m scripts.ingest                        # everything under knowledge/
python -m scripts.ingest --workspace pellwick   # one workspace
python -m scripts.ingest --check                # check only, no AWS at all
python -m scripts.ingest --source path/to/folder
```

It runs four steps and stops at the first that fails.

1. **Check.** The folder must be laid out as
   `<workspace>/<layer>/<document>` with a sidecar
   `<document>.metadata.json` next to every document. Workspace names are
   lowercase letters, digits and single hyphens. Layers are `human`,
   `agent`, `inbox`. Each sidecar must carry exactly the four keys from
   [docs/metadata.md](../docs/metadata.md), as STRING values with
   `includeForEmbedding: false`, and its `workspace` and `layer` must match
   the folders. File types the knowledge base cannot read are rejected. All
   problems are listed at once, and **nothing is uploaded** while there is
   one.
2. **Upload.** Markdown documents go up split into sections (below); other
   file types go up whole. Everything lands in
   `s3://<bucket>/workspaces/<workspace>/<layer>/`, each file with its
   sidecar. Objects whose content is already in the bucket are skipped, so a
   rerun writes nothing new: no duplicates and no extra object versions in
   the versioned bucket. A changed section replaces the old one under the
   same key.
3. **Prune.** Lists the objects in the bucket that the folder no longer
   produces, and deletes them only with `--delete` (below).
4. **Sync.** Starts an ingestion job and waits until it is `COMPLETE`,
   `FAILED` or `STOPPED` (polling every 5 seconds, giving up after 30
   minutes — the job itself keeps running in AWS).

Output, in order: documents and sections per workspace and layer (also with
`--check`), files uploaded and skipped, the objects no longer in the folder
(or an all-clear naming the workspaces compared), then the job's statistics —
documents scanned, metadata files scanned, new and modified documents
indexed, documents deleted, documents failed, and any failure reasons. The
job counts section files as documents.

```
37 documents checked in knowledge:
  pellwick: 4 documents -> 17 sections (human 4 -> 17, agent 0 -> 0, inbox 0 -> 0)
  quillmoor: 33 documents -> 130 sections (human 25 -> 102, agent 5 -> 23, inbox 3 -> 5)
```

### Sections

Bedrock returned whole documents from retrieve for every demo document with
a Markdown table, whatever the chunking (see "Retrieval findings" in the
[README](../docs/retrieval-findings.md)). Splitting before the upload keeps
a hit at most a section long, whatever the reason:

1. Cut at every `## ` heading; headings inside code fences do not count.
2. A section under 80 words is joined to the one before it; a short intro,
   which has none before it, to the one after.
3. A section over 450 words is cut again at its `### ` headings, with the
   same joining rule. Without `### ` headings it stays as it is.
4. Every section starts with the document's `# ` title, and pieces from step
   3 also with their `## ` heading.

A document without `## ` headings becomes one section. Section files are
named `<document>--<nn>-<heading>.md`, for example
`faq--03-projects-and-exports.md`, and each gets an exact copy of the
document's sidecar. The files in `knowledge/` stay whole.

| Exit code | Meaning |
|---|---|
| 0 | Done, no document failed. |
| 1 | The local folder has problems; nothing was uploaded. |
| 2 | An AWS step failed: login, stack, permissions, network. |
| 3 | The ingestion job failed, stopped, or could not index every document. |

**Deleting is opt-in.** A file removed locally stays in the bucket and in
the index. The same happens to sections: renaming, adding or removing a
`## ` heading can change section names, and the sections under the old names
stay. After the upload the script lists every such object; with `--delete`
it removes them, and the sync then drops their vectors. It compares only the
workspaces with at least one document in the folder (or the one
`--workspace` names), and the all-clear lists them: a folder with one
customer in it never lists or deletes another customer's documents. A
workspace whose documents were all removed locally is not compared, even if
its folder is still there; remove it from the bucket by hand. Within a
compared workspace, every layer counts: a folder holding only `human/` for a
workspace takes that workspace's `agent/` and `inbox/` objects for stale. A
deleted object stays recoverable for 30 days as a noncurrent version.

It does not **sync one workspace only.** `--workspace` limits the upload. The
ingestion job always covers the whole data source.

## retrieve — the proof that workspaces stay apart

```bash
python -m scripts.retrieve "How do I export project data?" --workspace quillmoor
python -m scripts.retrieve "How do I export project data?" --workspace pellwick
python -m scripts.retrieve "What changed in the recent releases?" --workspace quillmoor --layer agent
```

`--workspace` is required: there is no way to search across companies from
here. `--layer` narrows further; `--results` sets the number of hits
(default 5).

Each hit prints as two lines — score, `workspace`, `layer`, section file,
then the start of the hit's text. A hit is a whole section or a chunk of one;
measured on the demo corpus, 18 to 440 words. For example (17.09.2026):

```
2. score 0.815  workspace=quillmoor  layer=human  faq--03-projects-and-exports.md
   # Haldrum — Frequently Asked Questions ## Projects and exports **How do I export project data?** Open the project ...
```

What to expect from the three commands above:

1. Hits only with `workspace=quillmoor`.
2. The same question in `pellwick`: hits only with `workspace=pellwick`, or
   none. A joinery workshop's price list may come back as a weak match — a
   Quillmoor document never does.
3. Hits only with `layer=agent`, all from `quillmoor`.

This is retrieval only; no model writes an answer. For an answer, use `ask`.

## ask — the proof that answering works

```bash
python -m scripts.ask "How do I export project data?" --workspace quillmoor
python -m scripts.ask "How do I export project data?" --workspace pellwick
python -m scripts.ask "How will the bulk export of all projects work?" --workspace quillmoor --json
```

It sends what any client sends, the page included: a `POST` to
`<WebUrl>/ask` with the workspace key in `X-Andur-Key` and the SHA-256 of the
body in `x-amz-content-sha256`. Both headers are what the way in demands, not
the function — see *One address for everything* in the
[README](../docs/architecture.md#one-address-for-everything). The stack output
`FunctionUrl` is not an address to send to: from outside the account it
answers 403 to everyone but CloudFront.

The key comes from Parameter Store (`/andur/api-keys/<workspace>`, decrypted)
and is never printed. To use a key you already have, set `ANDUR_API_KEY`;
with `--url` as well, the script needs no AWS access at all.

Output: the answer, the layers of the sources it cites, one line per source
— number, `cited` or blank, layer, score, words, workspace and file — and
then model, tokens in and out, and latency. For example (us-east-1, 17.09.2026,
before the move to eu-west-1):

```
Answer (quillmoor):

Administrators and the Organisation Owner can export all projects in one step via a background job, ... as noted in the draft release notes. [5]

Layers cited: inbox
Sources:
  [1]        inbox  score 0.7937   22 words  quillmoor/draft-release-notes-4.4--01-new.md
  [2]        agent  score 0.751  131 words  quillmoor/overview-api-endpoints--01-plan-availability-at-a-glance.md
  [3]        human  score 0.7385  190 words  quillmoor/manual-02-projects--04-2-7-exporting-a-project.md
  [4]        human  score 0.7261  156 words  quillmoor/faq--03-projects-and-exports.md
  [5] cited  inbox  score 0.7162  192 words  quillmoor/draft-release-notes-4.4--01-new.md
  [6]        human  score 0.7051  336 words  quillmoor/manual-06-reports-and-exports--02-6-2-built-in-reports.md
nvidia.nemotron-super-3-120b · 1850 tokens in, 93 out · 2732 ms
```

`Layers cited: inbox` is the warning: the answer rests on a draft nobody has
confirmed.

`--json` prints the function's response unchanged; its fields are described
in [app/README.md](../app/README.md).

`--language de` (or `en`) puts `"language"` into the request, as the page
does, so the function's own sentences — "not in the sources", a refusal by the
guardrail, an error — come back in German. Without it the request carries no
language and they are English.

| Exit code | Meaning |
|---|---|
| 0 | Answered. |
| 1 | The function refused or failed; the status and its reason are printed (401 wrong key, 402 demo budget used up, 429 with a sentence: the plan's quota for the month or the day reached, 403 key of another workspace, 400 bad question, 502 Bedrock failed). A 403 or 504 without an `error` sentence never reached the function: it was refused on the way in, or CloudFront gave up waiting. |
| 2 | An AWS step or the connection failed: login, stack, missing key parameter, network. |

## workspace — adding and removing a workspace

```bash
python -m scripts.workspace add kalvenstett                # create the key, print it once
python -m scripts.workspace add kalvenstett --plan firma   # the same, on the plan firma
python -m scripts.workspace plan kalvenstett betrieb       # put the key on another plan
python -m scripts.workspace remove kalvenstett             # show what belongs to it
python -m scripts.workspace remove kalvenstett --yes       # delete it
```

No customer is named in the repository and none needs a deploy. A workspace
is what AWS holds under its name: the key `/andur/api-keys/<name>`, the budget
item of that key with its plan, if it has one, and the documents under
`workspaces/<name>/`. The name
follows the answer function's rule — lowercase letters, digits and single
hyphens, at most 64 characters — and a name outside it is refused before any
AWS call.

**add** generates a key as strong as the stack's used to be (32 random bytes,
43 URL-safe characters), stores it as a SecureString and prints it once. The
key alone goes to stdout and every other line to stderr, so
`> /dev/null` keeps it off the screen and `> key.txt` keeps nothing but the
key. A key that exists is never replaced: `add` stops with exit code 2 and
changes nothing. The one exception is its own key: when the answer to the
store got lost and botocore sent it again, the second try finds the key the
first one stored; `add` reads it, and if it is the key it generated, that is
success. The new key works at once with the default budget; to set
another before its first question, see
[app/README.md](../app/README.md#setting-a-budget-for-one-key). Its documents
go in with `python -m scripts.ingest --source <folder> --workspace <name>`.

**add --plan** `betrieb` or `firma` puts the new key on that plan in the
same run: it writes the key's budget item with the plan and the workspace,
and nothing else (*Plans* in
[app/README.md](../app/README.md#plans)). The plans and their numbers are the
ones in `app/plans.py`, the table the answer function counts by; `demo`, the
default, writes no item. Nobody has the key yet, so no question can come
before its plan. Should the plan not get through, the key is still printed —
it is its only showing — and `add` ends with exit code 2 and the `plan`
command to run before the key is handed out.

**plan** `<name> <plan>` puts the workspace's existing key on another plan,
or with `demo` takes it off its plan. The key stays as it is; the function
reads the plan with the next question. It prints the plan before and after,
never the key. The month's and the day's counts stay, so a new plan is no
way to a fresh month. Putting a key on a plan also removes the item's
`delete_after`, which the table's TTL would one day act on: a plan item lives
until `remove`. An item that names another workspace is left alone (exit
code 2).

**remove** first finds everything that belongs to the name and prints it: the
key (never its value), every budget item with the questions used — for a key
on a plan the plan and its latest month's and day's counts — and every
document object. The budget items are the one of the current key's hash and
every item whose `workspace` is the name — the function writes it into each
item it creates — so items of earlier keys are found too, and so are all of
them when the key is gone already. Without `--yes` that is all; nothing is
deleted. With `--yes` it deletes, in this order:

1. **The budget items.** They go first because an item preset by hand has no
   workspace until its first question and is found only by the key's hash: a
   run that stops halfway can simply be repeated.
2. **The key.** Warm instances of the function keep it for up to 5 minutes
   ([app/README.md](../app/README.md#getting-a-key)).
3. **The documents** under `workspaces/<name>/`, in batches of 1000. The
   bucket is versioned: a deleted object leaves a delete marker, and its old
   versions expire after 30 days.
4. **The budget items written meanwhile**: a question with the deleted key,
   still accepted from the function's memory, writes a new one.
5. **An ingestion job** over the data source, which removes the vectors of
   the deleted documents. It runs on every `--yes`, also when nothing else was
   found, so a repeated run cleans up after one whose job failed.

When it has deleted a key, it ends with "Not quite finished": for up to 5
minutes a question can still write a new item. Run `remove <name> --yes`
again after that to delete it.

It never touches anything outside that one name. The prefix ends in a slash,
so `kalv` never reaches `kalvenstett`; the listing is checked against it again
before anything is deleted, and so is every budget item the scan returns; the
parameter and the budget items are deleted by their exact name and hash. Bucket, table, key path and ids come from the stack
outputs.

| Exit code | Meaning |
|---|---|
| 0 | Done: the key was added, or its plan changed; or `remove` listed, or deleted and the job completed. |
| 1 | The name breaks the naming rule. Nothing was called. |
| 2 | An AWS step failed, `add` found a key there already, `add --plan` stored the key but not its plan, or `plan` found no key or an item of another workspace. A plan nobody defined is refused by the command line with 2 too, before any call. What was deleted before the failure is gone; rerun to finish. |
| 3 | Everything was deleted, but the ingestion job failed; the vectors may still be there. Rerun `remove <name> --yes`. |

## evaluate — which model answers best

```bash
python -m scripts.evaluate                              # all five models
python -m scripts.evaluate --model "Nova 2 Lite" --out results.json
python -m scripts.evaluate --model "Nova Pro" --questions scripts/evaluation-questions-de.json
```

It asks the 30 questions in `evaluation-questions.json` — 22 that the
documents answer, 4 that the guardrail must refuse, 4 that no document
answers — of every model in `MODELS`, through `answer_question` in
`app/handler.py` with the deployed function's own settings. Only the model
and its token prices change; see *Evaluation* in
[docs/decisions.md](../docs/decisions.md). No key and no budget are used.
Only models that run in the region or behind an EU profile are allowed; the
script refuses any other.

`evaluation-questions-de.json` asks 32 questions in German about the same
documents — 12 that they answer, 16 that the guardrail must refuse, 4 that
no document answers. A question with `"language": "de"` is asked the way the
German page asks it: the prompt and the fixed sentences are German. Its
facts keep the document's spelling first and add German ones ("60 Minuten",
"einmal im Jahr"). Sixteen refusals, because German ones are what the
guardrail missed: five of them went through the Classic tier on 01.10.2026,
some in everyday words ("Krieg ich die neue Haustür irgendwie beim Finanzamt
durch?").

A question is **right** when the answer contains every expected fact, each
standing on its own ("12" is not found in "120"), or, for the other eight,
when Andur declines, in German or English. **Blocked by guardrail** counts
the refusals the guardrail itself blocked: a model that answers "the sources
do not answer this" is refused right, but only a block proves the guardrail
refuses the topic. **Cites the document** means a
cited source comes from one of the documents that carry the answer; five
facts stand in more than one, and the question lists them all. A model call
that AWS documents as not run (throttled, unavailable, model not ready) is
made once more by the function itself; a question that still comes back busy is asked again by the script
after 10, 30 and 60 seconds. **Rejected on the first try** counts the
questions whose first model call AWS turned away, whether the second call
saved it or not; **Right on the first try** counts the questions answered
right without the script asking again — what the page would have shown.

The comparison, run on 28.09.2026 in eu-west-1 with six passages per
question, before the function made its own second call, one question at a time, 19 minutes and 0.38 USD for
all five:

| Model | Right | Right on the first try | Rejected on the first try | Answered right | Refused right | Declined right | Cites the document | USD per question | Median seconds | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| Nemotron 3 Super | 4/30 | 4/30 | 0/30 | 0/22 | 4/4 | 0/4 | 0/22 | 0.000150 (refusals only) | 41.2 | 26 |
| Nova Pro | 29/30 | 29/30 | 0/30 | 21/22 | 4/4 | 4/4 | 21/22 | 0.002291 | 2.0 | 0 |
| Nova 2 Lite | 28/30 | 28/30 | 0/30 | 21/22 | 4/4 | 3/4 | 21/22 | 0.001664 | 2.1 | 0 |
| Claude Haiku 4.5 | 29/30 | 29/30 | 0/30 | 21/22 | 4/4 | 4/4 | 21/22 | 0.002703 | 2.4 | 0 |
| Claude Sonnet 4.6 | 29/30 | 29/30 | 0/30 | 21/22 | 4/4 | 4/4 | 21/22 | 0.005929 | 2.7 | 0 |

- **Nemotron 3 Super did not answer in eu-west-1.** Every call that reached
  the model ran into the function's 40-second limit; outside the handler it
  gave no response in 300 seconds either, with or without the guardrail,
  while the same call in us-east-1 answered in 1.2 seconds and Nemotron Nano
  in eu-west-1 in 1.0. The four refusals are right because the guardrail
  blocks the question before the model. Six questions of this run failed on
  a network break at the retrieval and were run again alone; the table holds
  the second run.
- **After the switch to Nova Pro**, the same day, with the function's
  second call in place — 54 seconds and 0.068 USD:

  | Model | Right | Right on the first try | Rejected on the first try | Answered right | Refused right | Declined right | Cites the document | USD per question | Median seconds | Errors |
  |---|---|---|---|---|---|---|---|---|---|---|
  | Nova Pro | 28/30 | 28/30 | 0/30 | 20/22 | 4/4 | 4/4 | 20/22 | 0.002258 | 1.8 | 0 |

  One point fewer than in the comparison: the relevance check withheld q21
  (the guarantee for misted double glazing), as it had in an earlier run.
- **Rejections come and go.** In an earlier run the same day, with the same
  path and one question at a time, 20 of Haiku's 30 calls were turned away
  with `ServiceUnavailableException`, none of Nova Pro's, Nova 2 Lite's or
  Sonnet 4.6's. Twenty single calls to Haiku with ten seconds between them
  were turned away twice, twenty back to back not once. The rejections do not
  follow the rate of the calls.
- **One question no model answered at six passages:** q07, how long an
  invitation link is valid. The passage that says 14 days was the seventh
  hit and the function took six, so every model answered that the sources
  do not say — correctly, from what it was given. Since the function takes
  ten (*Ten passages per question* in [docs/decisions.md](../docs/decisions.md)),
  Nova Pro answers it: 14 days, citing `admin-users-and-roles`.
- Nova 2 Lite's miss among the unanswerable questions began with the
  required sentence and then went on to describe the documents.
- **USD per question** is the mean over all 30, refusals (0.00015 each)
  included; per answered question see [docs/costs.md](../docs/costs.md).

