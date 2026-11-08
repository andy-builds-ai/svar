# Document metadata

Every document that goes into Andur carries a small sidecar file next to it.
Bedrock reads that file during a sync and attaches its contents to each chunk
the document produces. At query time those values become filters — this is
how one deployment serves several companies without ever mixing their
answers, and how an answer can say which layer it came from.

## Layout in the bucket

```
s3://<documents bucket>/workspaces/<workspace>/<layer>/<name>.<ext>
s3://<documents bucket>/workspaces/<workspace>/<layer>/<name>.<ext>.metadata.json
```

The prefix is not decoration. The data source only crawls `workspaces/`, and
`<workspace>` is the boundary between companies. The path and the metadata
say the same thing twice on purpose: the path makes it visible to a human
browsing the bucket, the metadata makes it queryable.

## The sidecar file

The file is named after the document plus `.metadata.json` — for
`handbook.md` that is `handbook.md.metadata.json`.

```json
{
  "metadataAttributes": {
    "workspace": { "value": { "type": "STRING", "stringValue": "demo-gmbh" }, "includeForEmbedding": false },
    "layer":     { "value": { "type": "STRING", "stringValue": "human" },     "includeForEmbedding": false },
    "source":    { "value": { "type": "STRING", "stringValue": "fictional" }, "includeForEmbedding": false },
    "license":   { "value": { "type": "STRING", "stringValue": "CC0-1.0" },   "includeForEmbedding": false }
  }
}
```

`includeForEmbedding: false` keeps the value out of the embedded text. A
workspace name should decide *whether* a chunk is a candidate, not nudge
*how* similar it looks.

## The four keys

| Key | What it holds | Filterable |
|---|---|---|
| `workspace` | The company this document belongs to, lowercase, no spaces — e.g. `demo-gmbh`. Matches the folder name. | yes |
| `layer` | Where the knowledge comes from: `human`, `agent` or `inbox`. | yes |
| `source` | Where the document came from: a link, or the word `fictional` for invented demo material. | no |
| `license` | The licence the document is under. `CC0-1.0` for anything written for the demo. | no |

### Why only two are filterable

S3 Vectors caps the metadata it stores per vector. Filterable metadata is
the scarce part of that budget, and Bedrock already spends some of it on the
chunk text (`AMAZON_BEDROCK_TEXT`) and its own source metadata
(`AMAZON_BEDROCK_METADATA`). Those two, plus `source` and `license`, are
declared non-filterable on the index, which leaves the budget for the two
keys a query actually narrows on.

Non-filterable does not mean invisible: `source` and `license` come back
with every hit, they just cannot appear in a filter expression. That is the
right trade — you want to *show* a licence, not search by it.

### The three layers

`layer` is what lets an answer say where it is standing:

- `human` — written by a person for people. Handbooks, policies, notes.
- `agent` — produced by a machine. Summaries, extractions, generated
  overviews. Useful, but one step removed from a human decision.
- `inbox` — arrived, not yet sorted. Read it, but do not lean on it.

A retrieval that cannot distinguish these can quote a machine summary of a
machine summary and present it as company policy. Keeping the layer on the
chunk means the answer can carry the distinction through to the reader.

## Adding a workspace

There is nothing to deploy. Give the workspace its key with
`python -m scripts.workspace add <name>`, upload under a new
`workspaces/<name>/` prefix, sync, and query with `workspace = <name>`. The
vector index, the knowledge base and the role stay as they are.
`python -m scripts.workspace remove <name> --yes` takes all of it away again
([scripts/README.md](../scripts/README.md#workspace--adding-and-removing-a-workspace)).
