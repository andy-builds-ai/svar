# Taug

Taug is what Andur's other parts stand on ([the overview](../README.md)). It
keeps each company's documents in a workspace of its own, in three layers:
`human` (written by a person), `agent` (produced by a machine) and `inbox`
(arrived and not sorted yet). Over them lies one knowledge base, and every
search in it is filtered to one workspace. Taug also holds the workspace keys
and the table of regions and models with their prices.

## What is here

| File | What it does |
|---|---|
| [access/](access/__init__.py) | Taug's access library: the key check (`keys.py`), the workspace rules (`workspaces.py`: names, layers, key header, the filter of every search) and what a botocore error says (`aws_errors.py`). It imports nothing from Svar; Svar's answer function packs it. |
| [infra/stack.py](infra/stack.py) | `TaugStack`: the documents, the vectors, the knowledge base, the key retirer, and the outputs the scripts read. |
| [infra/knowledge_base.py](infra/knowledge_base.py) | Builds the documents bucket (`workspaces/<workspace>/<layer>/`), the vector store, the knowledge base and its data source. |
| [infra/answer_models.py](infra/answer_models.py) | The regions a stack may run in, the models each region may use, and their token prices. Model ids and prices live here and nowhere else. |
| [infra/keys.py](infra/keys.py) | Where the workspace keys live in Parameter Store, and the key retirer, which answered the Delete of the key resources Andur's one stack used to have; in `TaugStack` no resource calls it, it is the stack's first content. |
| [infra/api_keys/](infra/api_keys/handler.py) | That function's code, a code package of its own. |
| [infra/shared.py](infra/shared.py) | What the stack modules of both stacks share: the Lambda runtime, how code is packaged, log groups. |
| [scripts/](scripts/README.md) | `ingest` (check a folder, upload it, sync the knowledge base), `retrieve` (search one workspace), `workspace` (add a workspace's key, put it on a plan, remove a workspace), with `layout` and `aws` underneath. |
| [docs/metadata.md](docs/metadata.md) | The sidecar file every document carries: workspace, layer, source, licence. |
| [docs/retrieval-findings.md](docs/retrieval-findings.md) | Why documents are uploaded as sections. |
| [tests/](tests/) | Tests of all of the above, without AWS. |

## What runs with Svar

Taug has a stack of its own, `TaugStack` ([infra/stack.py](infra/stack.py)).
Svar's stack, `AndurStack`, depends on it and reads the knowledge base from it.
Some of Taug runs where Svar needs it:

- The key check runs in Svar's answer function. It is Taug's library
  ([access/](access/__init__.py)), which the function packs next to its own
  code (`ANSWER_PACKAGE` in `svar/infra/answer_function.py`) and imports as
  `taug.access`. So do the workspace filter of every search, the three layers
  and the rule for workspace names (`access/workspaces.py`), which Taug's
  scripts use too.
- A key's plan and its budget item belong to Svar, which counts every
  question against them. `python -m taug.scripts.workspace add … --plan` and
  `… plan` call Svar's step for it (`svar/scripts/plan.py`), and `… remove`
  deletes the key's budget items in Svar's table as well.

## Commands

```bash
python -m taug.scripts.ingest --check               # validate layout and sidecars, no AWS
python -m taug.scripts.ingest                       # upload the demo corpus and sync
python -m taug.scripts.retrieve "How do I export project data?" --workspace quillmoor
python -m taug.scripts.workspace add quillmoor > /dev/null   # store the workspace's key
```

All of them call the deployed stacks except `ingest --check`; options, output
and exit codes: [scripts/README.md](scripts/README.md).
