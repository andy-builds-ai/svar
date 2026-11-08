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
| [infra/knowledge_base.py](infra/knowledge_base.py) | Builds the documents bucket (`workspaces/<workspace>/<layer>/`), the vector store, the knowledge base and its data source. |
| [infra/answer_models.py](infra/answer_models.py) | The regions a stack may run in, the models each region may use, and their token prices. Model ids and prices live here and nowhere else. |
| [infra/keys.py](infra/keys.py) | Where the workspace keys live in Parameter Store, and the function that answers the Delete of the key resources the stack used to have. |
| [infra/api_keys/](infra/api_keys/handler.py) | That function's code, a code package of its own. |
| [infra/shared.py](infra/shared.py) | What the stack modules share: the Lambda runtime, how code is packaged, log groups. |
| [scripts/](scripts/README.md) | `ingest` (check a folder, upload it, sync the knowledge base), `retrieve` (search one workspace), `workspace` (add a workspace's key, put it on a plan, remove a workspace), with `layout` and `aws` underneath. |
| [docs/metadata.md](docs/metadata.md) | The sidecar file every document carries: workspace, layer, source, licence. |
| [docs/retrieval-findings.md](docs/retrieval-findings.md) | Why documents are uploaded as sections. |
| [tests/](tests/) | Tests of all of the above, without AWS. |

## What still lies with Svar

The one stack builds Taug's parts together with Svar's
([infra/stacks/andur_stack.py](../infra/stacks/andur_stack.py)). Until it is
split, some of Taug stays where Svar needs it:

- The key check runs in Svar's answer function (`svar/app/keys.py`), because
  the function's code package is the folder `svar/app/`. So do the workspace
  filter of every search (`svar/app/retrieval.py`), the three layers
  (`svar/app/citations.py`) and the rule for workspace names
  (`svar/app/intake.py`, which a test holds equal to `taug/scripts/layout.py`).
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

All of them call the deployed stack except `ingest --check`; options, output
and exit codes: [scripts/README.md](scripts/README.md).
