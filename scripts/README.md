# scripts — operating the knowledge base

Two commands, both run from the repository root:

| Command | Does |
|---|---|
| `python -m scripts.ingest` | Checks a local folder, uploads it to the documents bucket, syncs the knowledge base. |
| `python -m scripts.retrieve` | Asks the knowledge base a question inside one workspace and prints the hits. |

## What they need

- `pip install -e ".[dev]"` — brings `boto3` with its `crt` extra. The extra
  is not optional here: without it botocore cannot read the credentials that
  `aws login` writes, and every AWS call fails with "Missing Dependency".
- A valid AWS login for the account the stack is deployed in. When it has
  run out, both scripts stop with a message that says so; sign in with
  `aws login` and rerun.
- The deployed stack. Bucket, knowledge base id and data source id are read
  from the outputs of `AndurStack` in `us-east-1`. The region is pinned like
  in `infra/app.py`, so a shell profile pointing elsewhere does not matter.
  Each value can be passed by hand instead: `--bucket`,
  `--knowledge-base-id`, `--data-source-id`, and `--stack-name` / `--region`
  for another stack.

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
2. **Upload.** Every document and sidecar goes to
   `s3://<bucket>/workspaces/<workspace>/<layer>/<file>`. Files whose content
   is already in the bucket are skipped, so a rerun writes nothing new: no
   duplicates and no extra object versions in the versioned bucket. A changed
   file replaces the old one under the same key.
3. **Sync.** Starts an ingestion job and waits until it is `COMPLETE`,
   `FAILED` or `STOPPED` (polling every 5 seconds, giving up after 30
   minutes — the job itself keeps running in AWS).

Output, in order: documents per workspace and layer, files uploaded and
skipped, then the job's statistics — documents scanned, metadata files
scanned, new and modified documents indexed, documents deleted, documents
failed, and any failure reasons.

| Exit code | Meaning |
|---|---|
| 0 | Done, no document failed. |
| 1 | The local folder has problems; nothing was uploaded. |
| 2 | An AWS step failed: login, stack, permissions, network. |
| 3 | The ingestion job failed, stopped, or could not index every document. |

Two things it deliberately does not do:

- **Delete.** A file removed locally stays in the bucket and in the index.
  Remove it from the bucket by hand, then rerun; the sync drops its vectors.
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

Each hit prints as two lines — score, `workspace`, `layer`, file name, then
the start of the chunk. The shape, with made-up values:

```
1. score 0.612  workspace=quillmoor  layer=human  faq.md
   ## Projects and exports **How do I export project data?** Open the project ...
```

What to expect from the three commands above:

1. Hits only with `workspace=quillmoor`.
2. The same question in `pellwick`: hits only with `workspace=pellwick`, or
   none. The filter is applied inside the vector search, so a joinery
   workshop's price list may come back as a weak match — a Quillmoor
   document never does.
3. Hits only with `layer=agent`, all from `quillmoor`.

This is retrieval only; no model writes an answer. That comes in build step
3.
