# AGENTS.md

Svar is a chatbot that answers questions from a company's own documents.
Vard is an agent (`vard/`), measured, not deployed. Both are parts of a
larger system called Andur, which is meant to grow into a secretariat, stage
by stage, with a person deciding at every stage. The stack runs on AWS and
is defined as code (CDK in Python).

- `README.md`: what the repository holds.
- `docs/architecture.md`: how the stack is built.
- `scripts/README.md`: how the scripts work.

## Setup

Needs Python 3.11 or newer (CI runs 3.14, the function's runtime in Lambda),
Node.js 22 (CDK synthesizes through jsii, and some tests run
`web/markdown.js`) and the CDK CLI (`npm install -g aws-cdk@2.1142.0`).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]" -c constraints.txt
```

`constraints.txt` pins every package to the version the tests pass on; its
head says how to renew it.

## Checks

Run all three before you finish. CI (`.github/workflows/ci.yml`) runs the
same, and then `cdk synth` for both regions. They need no AWS; the tests took
about a minute.

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

## Rules

- **Only the operator deploys.** Never run `cdk deploy` or `cdk destroy`.
- **Region is `eu-west-1` by default** (`DEFAULT_REGION` in
  `infra/answer_models.py`). `cdk synth -c region=eu-central-1` and each
  script's `--region` pick Frankfurt. Frankfurt has its own models, checks
  the denied topics with gpt-oss-120b in the region (`app/topic_model.py`),
  keeps masking and grounding off the EU profile, and has no CloudFront: the
  function's URL serves page and `/ask` (`app/page.py`).
- **Use EU inference profiles, never a global one.**
- **Model IDs and prices live only in `infra/answer_models.py`.** Add a model
  there first; do not copy an ID or a price anywhere else.
- **No secrets in code, tests, logs or messages:** no API keys, no parameter
  values, no account IDs. Local secrets go in `.env`, which git ignores. The
  output of `cdk diff` names the account; do not paste it.
- **Commit only when asked,** as Conventional Commits (`feat:`, `fix:`,
  `docs:`).

## Scripts that call AWS

Run the measuring scripts only when asked. A cost is what a run cost on the
date given.

| Script | What it calls or creates | Cost | After a change to, ask the operator for a run |
|---|---|---|---|
| `python -m scripts.evaluate` | real models on AWS | real money, no figure given | |
| `ingest`, `retrieve`, `ask`, `workspace` (as `python -m scripts.<name>`) | the deployed stack; see `scripts/README.md` | | |
| `python -m scripts.workspace remove --yes` | deletes a workspace's key, budget and documents: never without the operator's word | | |
| `python -m scripts.topic_probe` | creates and deletes a guardrail named `andur-topic-probe` | about 0.05 USD a run | `DENIED_TOPICS` |
| `python -m scripts.topic_probe --region eu-central-1` | measures the Frankfurt topic check | three runs of 319 checks, 0.30 USD on 07.10.2026 | that prompt, and `check_topics_with_model` in `app/topic_check.py` |
| `python -m scripts.grounding_probe` | creates and deletes a guardrail named `andur-grounding-probe` | 0.22 USD a run on 06.10.2026 | the grounding check or the prompt |
| `python -m scripts.control_eval` | creates and deletes up to three guardrails named `andur-control-*-probe` in eu-central-1 | 42 questions: 0.063 to 0.086 USD per Frankfurt model, 0.11 USD for Nova Pro, 0.33 USD for Claude Sonnet 4.6 on 06.10.2026 | |
| `python -m scripts.tool_probe` | models of `FRANKFURT_MODELS` | fractions of a cent per model | |
| `python -m scripts.rehearsal` | sets up a workspace with its key on the deployed stack and runs the measurements above against it; `--list` calls nothing | 0.70 USD on 07.10.2026 (at most 0.75) | |
| `python -m scripts.receiving_probe` | every model of `FRANKFURT_MODELS` checks 24 delivery notes through Vard's loop (`vard/`), `--repeat` rounds of them; no call that could take the run past `--max-usd` (2.25 USD by default) | a full run of the eleven models on the first twelve cases cost 0.488 and 0.490 USD on 20.10.2026; on 21.10.2026 the 24 cases in three rounds cost 0.599 USD for the four candidates, 0.708 USD for Claude Sonnet 4.6 (one round) and 0.382 USD for three models with `--vendor-settings` | `vard/` or its cases |
