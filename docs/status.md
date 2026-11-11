# Status

What runs where, since when, and what waits for the next deploy. This is the one place for
facts that change with a deploy; every other document links here instead of repeating them.
The operator updates it with every deploy and every measurement run.

## Deployed

| Region | Stacks | Code | Last deploy | What it is |
|---|---|---|---|---|
| `eu-central-1` (Frankfurt) | `TaugStack`, `AndurStack` | `b97596a` | 11.11.2026 | The stack that runs. Taug's seven resources were moved into `TaugStack` that night and both stacks deployed; `cdk diff` against this code then showed no difference in either stack ([decisions.md, 11.11.2026](decisions.md#2026-11-11-move-ran-in-frankfurt)). |
| `eu-west-1` (Ireland) | `AndurStack`, `TaugStack` | `037086b` | 10.11.2026 | Not carried on and to be deleted. It stands at the last commit before the move: Taug's seven resources are still in `AndurStack`, and `TaugStack` holds only the key retirer. Do not deploy the current code there ([Deploy](operating.md#deploy)). |

The code is named by its commit on `main`; later commits that change only documents or tests
leave the deployed templates as they are. The stacks themselves are described in
[architecture.md](architecture.md).

## Last measured

| Region | Date | What | Where the numbers are |
|---|---|---|---|
| `eu-central-1` | 11.11.2026 | The rehearsal of a new workspace after the move, with every measurement it runs | [svar/README.md](../svar/README.md#measured) |
| `eu-central-1` | 14.10.2026 | The 42 questions built to tempt a model (`control_eval`), not repeated since | [svar/README.md](../svar/README.md#measured) |
| `eu-central-1` | 21.10.2026 | The models Vard is measured with | [vard/README.md](../vard/README.md) |
| `eu-west-1` | 16.10.2026 | The English questions with Nova Pro | [svar/README.md](../svar/README.md#ireland-16102026-nova-pro) |

## Waiting for the next deploy

Each of these sits in the answer function's code package or in a template, so changing it
changes what AWS gets. They wait for the next deploy that changes the function or the
template anyway.

- `taug/infra/keys.py`: the key retirer's description, "Answers the Delete of retired Andur
  key resources", is in `TaugStack`'s template; in `TaugStack` no resource sends it a Delete.
- `taug/infra/api_keys/handler.py`: its module docstring still speaks of the one stack that
  held the key resources.
- `svar/app/clocks.py`: the comment on `clock` names the age of a cached key; the key cache
  reads its own clock in `taug/access/keys.py`.
- `svar/app/sentences.py`: the comment above `QUESTION_REFUSED_ANSWERS` names
  `infra/stacks/guardrails.py`, now `svar/infra/guardrails.py`.
- Five public definitions in `svar/app/` without a docstring: three in `errors.py`, one each
  in `budget.py` and `plans.py`. `pyproject.toml` exempts `svar/app/` from ruff's docstring
  rules until then; the exemption goes with that deploy.
