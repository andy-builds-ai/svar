# knowledge — the demo corpus

Two **fictional** companies, written for this demo. Neither exists. Every
name, product, person, price, address and document in this folder is
invented; nothing here comes from a real company. The `.example` domains
used in the texts are reserved for documentation and lead nowhere.

## The two companies

**Quillmoor Systems** (workspace `quillmoor`) is an IT service company that
builds Haldrum, a cloud platform for workflow and resource planning in small
and medium-sized companies: projects, tickets, people's time. Its documents
are what such a company actually has lying around — a user manual in
chapters, admin and API documentation, release notes for three versions,
policies, a support process — and they refer to one another. Versions,
roles, endpoints, prices and names agree across all of them;
`quillmoor/human/company-profile.md` holds the reference values the rest
follows.

**Pellwick Joinery** (workspace `pellwick`) is a small joinery workshop:
windows, doors, staircases. It has nothing in common with the first company
on purpose. Its only job in the demo is to be the *other* workspace, so that
a query filtered to one company can be shown not to return the other.

## Layout

The folder mirrors the documents bucket:

```
knowledge/<workspace>/<layer>/<document>.md
knowledge/<workspace>/<layer>/<document>.md.metadata.json
```

Every document has a sidecar with the four keys from
[docs/metadata.md](../docs/metadata.md): `workspace` and `layer` equal to the
folder names, `source` set to `fictional`, `license` set to `CC0-1.0`.

## What is in it

| Workspace | human | agent | inbox | Total |
|---|---|---|---|---|
| `quillmoor` | 25 | 5 | 3 | 33 |
| `pellwick` | 4 | 0 | 0 | 4 |
| **Total** | 29 | 5 | 3 | **37** |

About 28,700 words; every document is between 540 and 1,020 words. With the
data source's fixed-size chunking of 800 tokens, most documents still end up
as a single chunk — the first sync on 17.09.2026 returned whole documents of
up to 976 words as one chunk.

The three layers in `quillmoor` show what each means:

- `human/` — written by people: company profile, user manual chapters 1–6,
  administration, API reference, onboarding guide, FAQ, release notes 4.1 to
  4.3, data protection and security policies, support process, SLA,
  retention rules, glossary.
- `agent/` — produced by a machine: summaries and overviews generated from
  the human documents, each marked as generated in its first lines. Two of
  them flag open questions a person has not answered yet.
- `inbox/` — arrived, not sorted: a customer email, raw meeting notes and a
  half-finished draft of release notes for a version that is not out. They
  contain tentative dates and unresolved questions; an answer built on them
  should say so.

## Checking and uploading

```bash
python -m scripts.ingest --check   # validate layout and sidecars, no AWS
python -m scripts.ingest           # upload and sync
```

See [scripts/README.md](../scripts/README.md).
