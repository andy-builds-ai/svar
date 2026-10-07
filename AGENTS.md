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
  `python -m scripts.grounding_probe` creates and deletes a guardrail named
  `andur-grounding-probe` and cost 0.22 USD a run on 06.10.2026; run it only
  when asked, and after every change to the grounding check or the prompt ask
  for it.
  `python -m scripts.control_eval` creates and deletes up to three guardrails
  named `andur-control-*-probe` in eu-central-1; a run of the 42 questions cost
  0.063 to 0.086 USD per Frankfurt model, 0.11 USD for Nova Pro and 0.33 USD
  for Claude Sonnet 4.6 on 06.10.2026;
  `python -m scripts.tool_probe` costs fractions of a cent per model. Run
  both only when asked.
- **Region is `eu-west-1` by default** (`DEFAULT_REGION` in
  `infra/answer_models.py`); `cdk synth -c region=eu-central-1` and each
  script's `--region` pick Frankfurt, whose stack has its own models, checks
  the denied topics with gpt-oss-120b in the region (`app/topic_model.py`),
  keeps masking and grounding off the EU profile, and has no CloudFront: the
  function's URL serves page and `/ask` (`app/page.py`). Use EU inference
  profiles, never a global one. `python -m scripts.topic_probe --region
  eu-central-1` measures that check, three runs of 319 checks for 0.30 USD
  on 07.10.2026; run it only when asked, and after every change to that
  prompt or to `check_topics_with_model` in `app/handler.py` ask for it.
- **Model IDs and prices live only in `infra/answer_models.py`.** Add a model
  there first; do not copy an ID or a price anywhere else.
- **No secrets in code, tests, logs or messages:** no API keys, no parameter
  values, no account IDs. Local secrets go in `.env`, which git ignores. The
  output of `cdk diff` names the account; do not paste it.
- Commit only when asked, as Conventional Commits (`feat:`, `fix:`, `docs:`).
