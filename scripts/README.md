# scripts — operating the knowledge base

Four commands, all run from the repository root:

| Command | Does |
|---|---|
| `python -m scripts.ingest` | Checks a local folder, uploads it to the documents bucket, syncs the knowledge base. |
| `python -m scripts.retrieve` | Asks the knowledge base a question inside one workspace and prints the hits. |
| `python -m scripts.ask` | Asks the deployed answer function a question and prints the answer with its sources. |
| `python -m scripts.evaluate` | Asks 30 fixed questions of each answer model the way the function does and prints hit rate, citations and cost. |

## What they need

- `pip install -e ".[dev]"` — brings `boto3` with its `crt` extra. The extra
  is not optional here: without it botocore cannot read the credentials that
  `aws login` writes, and every AWS call fails with "Missing Dependency".
- A valid AWS login for the account the stack is deployed in. When it has
  run out, both scripts stop with a message that says so; sign in with
  `aws login` and rerun.
- The deployed stack. Bucket, knowledge base id, data source id, web address
  and key path are read from the outputs of `AndurStack` in `eu-west-1`. The
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

It runs three steps and stops at the first that fails.

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
3. **Sync.** Starts an ingestion job and waits until it is `COMPLETE`,
   `FAILED` or `STOPPED` (polling every 5 seconds, giving up after 30
   minutes — the job itself keeps running in AWS).

Output, in order: documents and sections per workspace and layer (also with
`--check`), files uploaded and skipped, then the job's statistics —
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

Two things it deliberately does not do:

- **Delete.** A file removed locally stays in the bucket and in the index.
  The same happens to sections: renaming, adding or removing a `## `
  heading can change section names, and the sections under the old names
  stay. Remove them from the bucket by hand (`aws s3 rm` on the document's
  `<document>--` files), then rerun; the sync drops their vectors.
- **Sync one workspace only.** `--workspace` limits the upload. The ingestion
  job always covers the whole data source.

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

| Exit code | Meaning |
|---|---|
| 0 | Answered. |
| 1 | The function refused or failed; the status and its reason are printed (401 wrong key, 402 key budget used up, 403 key of another workspace, 400 bad question, 502 Bedrock failed). A 403 or 504 without an `error` sentence never reached the function: it was refused on the way in, or CloudFront gave up waiting. |
| 2 | An AWS step or the connection failed: login, stack, missing key parameter, network. |

## evaluate — which model answers best

```bash
python -m scripts.evaluate                              # all five models
python -m scripts.evaluate --model "Nova 2 Lite" --out results.json
```

It asks the 30 questions in `evaluation-questions.json` — 22 that the
documents answer, 4 that the guardrail must refuse, 4 that no document
answers — of every model in `MODELS`, through `answer_question` in
`app/handler.py` with the deployed function's own settings. Only the model
and its token prices change; see *Evaluation* in
[docs/decisions.md](../docs/decisions.md). No key and no budget are used.
Only models that run in the region or behind an EU profile are allowed; the
script refuses any other.

A question is **right** when the answer contains every expected fact, each
standing on its own ("12" is not found in "120"), or, for the other eight,
when Andur declines. **Cites the document** means a
cited source comes from one of the documents that carry the answer; five
facts stand in more than one, and the question lists them all. A model call
that AWS documents as not run (throttled, unavailable, model not ready) is
made once more by the function itself; a question that still comes back busy is asked again by the script
after 10, 30 and 60 seconds. **Rejected on the first try** counts the
questions whose first model call AWS turned away, whether the second call
saved it or not; **Right on the first try** counts the questions answered
right without the script asking again — what the page would have shown.

The comparison, run on 28.09.2026 in eu-west-1 before the function made
its own second call, one question at a time, 19 minutes and 0.38 USD for
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
- **One question no model answers:** q07, how long an invitation link is
  valid. Retrieval does not return the passage that says 14 days, so every
  model answers that the sources do not say — correctly, from what it was
  given.
- Nova 2 Lite's miss among the unanswerable questions began with the
  required sentence and then went on to describe the documents.
- **USD per question** is the mean over all 30, refusals (0.00015 each)
  included; per answered question see [docs/costs.md](../docs/costs.md).

