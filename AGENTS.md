# AGENTS.md

Andur answers questions from a company's own documents, on AWS, defined as
code (CDK in Python). `README.md` says what it does, `docs/architecture.md`
how it is built, `scripts/README.md` how the scripts work.

## Setup

Needs Python 3.11 or newer, Node.js (CDK synthesizes through jsii, and some
tests run `web/markdown.js`) and the CDK CLI.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Checks

Run all three before you finish. CI (`.github/workflows/ci.yml`) runs the
same. They need no AWS; the tests took about a minute.

```bash
ruff check
ruff format --check
pytest -q
```

## The stack

```bash
cdk synth    # builds the template, no AWS call
cdk diff     # compares it with the deployed stack; needs an AWS login, changes nothing
```

## Limits

- **Only the operator deploys.** Never run `cdk deploy` or `cdk destroy`.
- `python -m scripts.evaluate` calls real models on AWS and costs real money.
  Run it only when asked. `ingest`, `retrieve`, `ask` and `workspace` also
  talk to the deployed stack; see `scripts/README.md`. `workspace remove --yes`
  deletes a workspace's key, budget and documents: never without the
  operator's word. `python -m scripts.topic_probe` creates and deletes a
  guardrail named `andur-topic-probe` and costs about 0.05 USD a run; run it
  only when asked, and after every change to `DENIED_TOPICS` ask for it.
- **Region is `eu-west-1`,** pinned in `infra/app.py`. Use EU inference
  profiles, never a global one.
- **Model IDs and prices live only in `infra/answer_models.py`.** Add a model
  there first; do not copy an ID or a price anywhere else.
- **No secrets in code, tests, logs or messages:** no API keys, no parameter
  values, no account IDs. Local secrets go in `.env`, which git ignores. The
  output of `cdk diff` names the account; do not paste it.
- Commit only when asked, as Conventional Commits (`feat:`, `fix:`, `docs:`).
