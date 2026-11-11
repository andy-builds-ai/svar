# AGENTS.md

Andur is meant to grow into a secretariat, stage by stage, with a person
deciding at every stage. Its parts each have a folder: Taug (`taug/`), what
the others stand on (documents, knowledge base, keys, regions and models);
Svar (`svar/`), a chatbot that answers questions from a company's own
documents; Vard (`vard/`), an agent, measured, not deployed. Two stacks build
Taug's and Svar's parts (`infra/app.py`); they run on AWS and are defined as
code (CDK in Python).

- `README.md`: the overview, and what is where.
- `taug/README.md`, `svar/README.md`, `vard/README.md`: each part.
- `docs/status.md`: what runs where, since when, and what waits for the next
  deploy.
- `docs/architecture.md`: how the stacks are built.
- `taug/scripts/README.md`, `svar/scripts/README.md`, `vard/scripts/README.md`,
  `scripts/README.md`: how the scripts work.

## Setup

Needs Python 3.11 or newer (CI runs 3.14, the function's runtime in Lambda),
Node.js 22 (CDK synthesizes through jsii, and some tests run
`svar/web/markdown.js`) and the CDK CLI (`npm install -g aws-cdk@2.1146.0`).

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

## The stacks

```bash
cdk synth                         # builds both templates, no AWS call
cdk diff -c region=eu-central-1   # compares both with the running stacks; needs an AWS login, changes nothing
```

`infra/app.py` builds two stacks in one account and region: `TaugStack`
(`taug/infra/stack.py`) and Svar's, class `SvarStack` in `svar/infra/stack.py`,
deployed as `AndurStack`, which imports the knowledge base from `TaugStack`.
`cdk synth` without a login writes `TaugStack.template.json` and
`AndurStack.template.json`. Svar's answer function bundles `svar/app/` with
Taug's library `taug/access/`, locally without Docker, so a change in
`taug/access/` alone reaches Lambda. `cdk deploy --all` is the operator's
(see Rules). Never pass `--exclusively`: the CLI then skips the bundling of
the stacks not named, and the function's code key would change.

With an AWS login, `cdk synth` writes the account into its output: the CLI
reads it from the login for `CDK_DEFAULT_ACCOUNT` (`infra/app.py`). To compare
templates, synthesize without a login, as CI does; then no account appears.

## Rules

- **Only the operator deploys.** Never run `cdk deploy` or `cdk destroy`.
- **Region is `eu-west-1` by default** (`DEFAULT_REGION` in
  `taug/infra/answer_models.py`). `cdk synth -c region=eu-central-1` and each
  script's `--region` pick Frankfurt. Frankfurt has its own models, checks
  the denied topics with gpt-oss-120b in the region (`svar/app/topic_model.py`),
  keeps masking and grounding off the EU profile, and has no CloudFront: the
  function's URL serves page and `/ask` (`svar/app/page.py`). Svar runs in
  Frankfurt; Ireland's stack stands at older code and is not carried on
  (`docs/status.md`), so a command against the running stacks names
  `eu-central-1`. `cdk diff` compares both stacks without `--all`, which it
  does not know.
- **Use EU inference profiles, never a global one.**
- **Model IDs and prices live only in `taug/infra/answer_models.py`.** Add a model
  there first; do not copy an ID or a price anywhere else.
- **No secrets in code, tests, logs or messages:** no API keys, no parameter
  values, no account IDs. Local secrets go in `.env`, which git ignores. The
  output of `cdk diff` names the account; do not paste it.
- **Commit only when asked,** as Conventional Commits (`feat:`, `fix:`,
  `docs:`).

## Documentation

- **What changes with a deploy lives only in `docs/status.md`:** which stacks
  run where, at which commit, since when, the date of the last measurement in
  each region, and what waits for the next deploy. Update it with every deploy
  and every measurement run; other documents link there and do not repeat it.
- **Every measured number carries its date.** No counts of tests or decisions
  in prose; they change with every commit.
- **Link a decision by its anchor, never by a line number.** Every entry in
  `docs/decisions.md` has `<a name="YYYY-MM-DD-a-few-words"></a>` on the line
  above it. A new entry gets one; an anchor, once written, never changes.
  `tests/test_docs_links.py` checks every relative link in every tracked
  Markdown file.
- **Every module and every public class, function and method outside the
  tests has a docstring:** one sentence on what it does, a second only for a
  why the name does not give, longer reasons in `docs/decisions.md`. ruff
  checks it (D100 to D104, D106 in `pyproject.toml`).
- **A changed byte in a code package changes the template.** `svar/app/`,
  `taug/__init__.py`, `taug/access/`, `taug/infra/api_keys/`,
  `svar/infra/deploy_drain/` and `svar/web/` (each `README.md` aside) go into
  Lambda or the page as they are. A change there, even a comment, waits for
  the next deploy of what it changes; list it in `docs/status.md` meanwhile.
  `svar/app/` is exempt from ruff's docstring rules until then.

## Scripts that call AWS

Run the measuring scripts only when asked. A cost is what a run cost on the
date given.

| Script | What it calls or creates | Cost | After a change to, ask the operator for a run |
|---|---|---|---|
| `python -m svar.scripts.evaluate` | real models on AWS | real money, no figure given | |
| `python -m taug.scripts.ingest`, `taug.scripts.retrieve`, `taug.scripts.workspace`, `svar.scripts.ask` | the deployed stacks; see `taug/scripts/README.md` and `svar/scripts/README.md` | | |
| `python -m taug.scripts.workspace remove --yes` | deletes a workspace's key, budget and documents: never without the operator's word | | |
| `python -m svar.scripts.topic_probe` | creates and deletes a guardrail named `andur-topic-probe` | 0.113 USD a run on 10.11.2026 | `DENIED_TOPICS` |
| `python -m svar.scripts.topic_probe --region eu-central-1` | measures the Frankfurt topic check | three runs of 319 checks, 0.30 USD on 07.10.2026, 0.302 USD on 10.11.2026 | that prompt, and `check_topics_with_model` in `svar/app/topic_check.py` |
| `python -m svar.scripts.grounding_probe` | creates and deletes a guardrail named `andur-grounding-probe` | 0.22 USD a run on 06.10.2026 | the grounding check or the prompt |
| `python -m svar.scripts.control_eval` | creates and deletes up to three guardrails named `andur-control-*-probe` in eu-central-1 | 42 questions: 0.063 to 0.086 USD per Frankfurt model, 0.11 USD for Nova Pro, 0.33 USD for Claude Sonnet 4.6 on 06.10.2026 | |
| `python -m vard.scripts.tool_probe` | models of `FRANKFURT_MODELS` | fractions of a cent per model | |
| `python -m scripts.rehearsal` | sets up a workspace with its key on the deployed stacks and runs the measurements above against it; `--list` calls nothing | 0.70 USD on 07.10.2026 (at most 0.75) | |
| `python -m vard.scripts.receiving_probe` | every model of `FRANKFURT_MODELS` checks 24 delivery notes through Vard's loop (`vard/`), `--repeat` rounds of them; no call that could take the run past `--max-usd` (2.25 USD by default) | a full run of the eleven models on the first twelve cases cost 0.488 and 0.490 USD on 20.10.2026; on 21.10.2026 the 24 cases in three rounds cost 0.599 USD for the four candidates, 0.708 USD for Claude Sonnet 4.6 (one round) and 0.382 USD for three models with `--vendor-settings` | `vard/` or its cases |
