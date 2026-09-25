# Andur

[![CI](https://github.com/andy-builds-ai/ask-andur/actions/workflows/ci.yml/badge.svg)](https://github.com/andy-builds-ai/ask-andur/actions/workflows/ci.yml)

Andur answers questions from a company's own documents and shows, for every
answer, the passages it was built from and whether a person or a machine
wrote them. It is meant for a team that keeps its handbooks, policies and
notes in files and would rather ask than search, and one installation
serves several companies without one ever seeing another's documents. It
runs on AWS, defined as code, with no part that bills a monthly base fee.

![An answer in the browser: the question, the answer with a masked name and its citations, and the first source](docs/screenshot-answer.png)

A walkthrough from workspace to answer — a question about the warranty on
painted windows, answered with four sources and the cost line:

![Walkthrough: choosing the workspace, asking a question, the answer with four sources and its cost](docs/walkthrough.gif)

## Measured

- **Cost per question:** 0.0011 to 0.0016 USD for an answered question,
  0.00015 USD for one the guardrail refuses — `cost_usd` of five live
  questions on 23.09.2026, at that day's prices. Details:
  [docs/costs.md](docs/costs.md).
- **Tests:** 479, all passing, without AWS — 17.9 seconds in CI on
  23.09.2026, 24 to 37 seconds on a local laptop.
- **Answer time:** 1.8 to 3.8 seconds inside the function for the four
  answered questions, 0.7 seconds for the refused one (`latency_ms` in the
  log).
- **Documents:** 37 Markdown documents of two fictional companies, uploaded
  as 147 sections so that a hit is a passage, not a page.
- **Key budget:** a key allows 10 questions within 48 hours of its first —
  at the measured cost, at most about 0.016 USD per key.

## Decisions

Everything with a monthly base fee is left out: S3 Vectors behind a Bedrock
knowledge base instead of a search cluster. A key is a budget, not a
password, counted in one conditional DynamoDB write. Two guardrails, one on
the answer and one that masks the excerpts of its sources. Every AWS call has
a timeout, and their sum stays under the function's own. Why each of these,
and 36 more, with dates: [docs/decisions.md](docs/decisions.md).

## Limits

- **Personal details are filtered, not guaranteed.** The masking is
  statistical. In the browser acceptance a first name without a surname
  stayed readable in two excerpts, while the answer next to them was masked.
- **Citations are the model's.** One of the four answered live questions
  carried no `[n]` at all, although the grounding check scored it 1.0. The
  prompt asks for citations; the model does not always give them.
- **The grounding threshold is a starting point.** 0.7 is the value AWS
  works its example with, not a tuned one. A correct answer scored 0.73, just
  above it. Every answer logs both scores, so the threshold can be moved on
  evidence.

## How it was built

Built with Claude Code as a coding partner; the decisions, the reviews and
the measurements are the author's.

## Try it

Needs Python 3.11+, Node.js, the AWS CDK (`npm install -g aws-cdk`) and AWS
credentials. Everything goes to `us-east-1`.

```bash
python -m venv .venv && . .venv/Scripts/activate   # Linux/macOS: . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                  # 479 tests, no AWS needed

cdk bootstrap aws://<account>/us-east-1    # once per account and region
cdk deploy                                 # builds everything
python -m scripts.ingest                   # uploads the demo corpus and syncs
python -m scripts.ask "How do I export project data?" --workspace quillmoor
cdk destroy                                # removes it; the documents bucket stays
```

Then open the stack output `WebUrl` in a browser; the page asks for a
workspace and its key. How to get a key, what every field and status means,
and how to ask with curl: [docs/operating.md](docs/operating.md) and
[app/README.md](app/README.md).

## Further reading

| Topic | Where |
|---|---|
| Architecture, the way in, what the stack creates | [docs/architecture.md](docs/architecture.md) |
| Deploy, fill, ask, test, tear down | [docs/operating.md](docs/operating.md) |
| Why documents are uploaded as sections | [docs/retrieval-findings.md](docs/retrieval-findings.md) |
| What it costs, measured and priced | [docs/costs.md](docs/costs.md) |
| Every decision, dated | [docs/decisions.md](docs/decisions.md) |
| The answer function: request, answer, errors, guardrails, logs | [app/README.md](app/README.md) |
| The page | [web/README.md](web/README.md) |
| The scripts | [scripts/README.md](scripts/README.md) |
| The demo corpus and its metadata | [knowledge/README.md](knowledge/README.md), [docs/metadata.md](docs/metadata.md) |

## Licence

MIT, see [LICENSE](LICENSE).
