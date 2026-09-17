# scripts — operating the knowledge base

Three commands, all run from the repository root:

| Command | Does |
|---|---|
| `python -m scripts.ingest` | Checks a local folder, uploads it to the documents bucket, syncs the knowledge base. |
| `python -m scripts.retrieve` | Asks the knowledge base a question inside one workspace and prints the hits. |
| `python -m scripts.ask` | Asks the deployed answer function a question and prints the answer with its sources. |

## What they need

- `pip install -e ".[dev]"` — brings `boto3` with its `crt` extra. The extra
  is not optional here: without it botocore cannot read the credentials that
  `aws login` writes, and every AWS call fails with "Missing Dependency".
- A valid AWS login for the account the stack is deployed in. When it has
  run out, both scripts stop with a message that says so; sign in with
  `aws login` and rerun.
- The deployed stack. Bucket, knowledge base id, data source id, Function URL
  and key path are read from the outputs of `AndurStack` in `us-east-1`. The
  region is pinned like in `infra/app.py`, so a shell profile pointing
  elsewhere does not matter. Each value can be passed by hand instead:
  `--bucket`, `--knowledge-base-id`, `--data-source-id`, `--url`, and
  `--stack-name` / `--region` for another stack.

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
[README](../README.md#retrieval-findings)). Splitting before the upload keeps
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

It sends what any client sends: a `POST` to the Function URL with the
workspace key as `Authorization: Bearer <key>`. The key comes from Parameter
Store (`/andur/api-keys/<workspace>`, decrypted) and is never printed. To use
a key you already have, set `ANDUR_API_KEY`; with `--url` as well, the script
needs no AWS access at all.

Output: the answer, the layers of the sources it cites, one line per source
— number, `cited` or blank, layer, score, words, workspace and file — and
then model, tokens in and out, and latency. For example (17.09.2026):

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
| 1 | The function refused or failed; the status and its reason are printed (401 wrong key, 403 key of another workspace, 400 bad question, 502 Bedrock failed). |
| 2 | An AWS step or the connection failed: login, stack, missing key parameter, network. |
