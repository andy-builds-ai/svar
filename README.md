# Andur

[![CI](https://github.com/andy-builds-ai/ask-andur/actions/workflows/ci.yml/badge.svg)](https://github.com/andy-builds-ai/ask-andur/actions/workflows/ci.yml)

Andur answers questions from a company's own documents and shows, for every
answer, the passages it was built from and whether a person or a machine
wrote them. It is meant for a team that keeps its handbooks, policies and
notes in files and would rather ask than search, and one installation
serves several companies without one ever seeing another's documents. It
runs on AWS, defined as code, with no part that bills a monthly base fee, in
either of two regions: Ireland (`eu-west-1`), behind CloudFront, or Frankfurt
(`eu-central-1`), where everything stays in Germany.

![An answer in the browser to "What is the office phone number, and who answers it?": the name is masked as {NAME}, a note says personal details are masked, and the first two sources are listed, the first one cited](docs/screenshot-answer.png)

A walkthrough of one question, recorded on 28.09.2026 in eu-west-1 with Nova
Pro and six passages per question, the workspace and key already entered: what the guarantee on painted
windows covers and what to do to keep it, answered with two citations out of
four sources, and the cost line under them (1357 tokens in, 73 out, 0.002717
USD, 3425 ms in the function):

![Walkthrough: typing a question about the guarantee on painted windows, asking it, and the answer with four sources and its cost line](docs/walkthrough.gif)

## Measured

Each number names the region and the day it was measured in.

### Frankfurt (`eu-central-1`), 07.10.2026

A rehearsal set up a new business on the deployed stack, a fictional craft
firm with seven German documents, and measured it there
(`python -m scripts.rehearsal`, see
[scripts/README.md](scripts/README.md#rehearsal--a-new-business-set-up-and-measured-step-by-step)).
The answers below come from Qwen3 235B in the region, through the function's
own path against the deployed stack, with the prompt in this repository.

- **Where the data is:** in `eu-central-1` at all 11 places AWS reports for
  the stack — documents bucket, embedding model, vector index, function,
  answer model, topic model, both guardrails, budget table and log. No
  inference profile, no guardrail profile, and Bedrock keeps no log of the
  model calls. Every address of the page lies in `eu-central-1` by AWS's own
  list (`ip-ranges.json`), and plain HTTP got no connection on any of them.
- **A new business:** 31 seconds of machine time from checking its folder to
  the first right answer — 3 for its workspace and key, 16 to upload and
  index its documents, 8 for the first question, "Was kostet eine Stunde für
  einen Gesellen?", answered "68 Euro" with the right document cited. Its key
  was refused in another workspace with 401, and a search there found none of
  its documents. The whole rehearsal, 22 steps with the measurements below,
  took 11 minutes 12 seconds and 0.70 USD of AWS cost (at most 0.75).
- **How often the answer is right:** 30 of 30 English questions on English
  documents, 26 of 26 German questions on German documents and 29 of 32
  German questions on English documents (`python -m scripts.evaluate`). On
  the deployed function the same day, every one of the 20 answerable German
  questions on German documents cited the document its answer comes from.
- **Staying with the sources:** 36 of 42 questions built to tempt a model
  (`python -m scripts.control_eval`). No answer added a figure of its own (0
  of 34), every German question was answered in German (29 of 29), all 8
  advice questions were refused. The six wrong ones are right answers the
  grounding check withheld, all of them German answers to English documents.
- **The topic check:** gpt-oss-120b in the region, on the deployed function,
  in 957 checks over three runs: no denied question or answer let through (45
  questions, 32 answers, 11 advice questions), and none of 87 harmless
  questions, 87 harmless answers and 57 questions of the evaluation sets
  refused, for 0.30 USD.
- **The grounding check at 0.8:** of 104 made-up answers it withheld 71, and
  it let all 19 right German answers to German documents through
  (`python -m scripts.grounding_probe`).
- **Cost and time per question:** 0.0021 USD on average for German questions
  on German documents, 0.0023 for English and 0.0013 for German on English
  documents, a median of 2.2 to 2.7 seconds from retrieval to cost line. The
  averages count only the questions with a known cost: an answer the
  grounding check withheld reports no model tokens and carries
  `cost_usd: null` (14 of the 88 on the deployed function).

### Ireland (`eu-west-1`), 28.09.2026

- **Where the data is:** in `eu-west-1` — documents, vectors, questions and
  answers. Models run in the region or behind an EU inference profile, never
  a global one.
- **How often the answer is right:** 30 fixed questions — 22 the documents
  answer, 4 the guardrail must refuse, 4 no document answers. Nova Pro, the
  configured model, got 28 right with six passages per question on
  28.09.2026 and cited the expected document in 20 of the 22 it should
  answer. Since the function hands it ten passages, two runs of the 22
  answerable questions got 22 and 21 right, citing the expected document
  in 22 and 21 (decisions: [docs/decisions.md](docs/decisions.md)). In the
  comparison the same day, at six passages, Nova Pro, Claude Haiku 4.5 and
  Claude Sonnet 4.6 got 29 right, Nova 2 Lite 28, and Nemotron 3 Super
  answered none in eu-west-1. Table and method:
  [scripts/README.md](scripts/README.md#evaluate--which-model-answers-best).
- **Cost per question:** 0.0043 USD on average for an answered question
  with Nova Pro at ten passages (0.0032 to 0.0062, 43 answers), 0.0029 USD
  at six (0.0019 to 0.0045, 39 answers), 0.00015 USD for one the guardrail
  refuses (measured at six) — `cost_usd` of those runs on 28.09.2026, at that
  day's prices. Details: [docs/costs.md](docs/costs.md).
- **Answer time:** 0.7 to 4.4 seconds inside the function for four questions
  on the page with Nova Pro, the first after a deploy included
  (`latency_ms`); a median of 1.8 seconds from retrieval to cost line for
  the 22 answerable questions, measured from a laptop in Germany.

### Both regions

- **Tests:** 1,347 passing and one skipped, without AWS — 70 seconds on a
  laptop on 07.10.2026. The skipped one compares German time with the time
  zone database, which that laptop does not have and CI does.
- **Documents:** 37 Markdown documents of two fictional companies, uploaded
  as 147 sections so that a hit is a passage, not a page.
- **Key budget:** a demo key allows 10 questions within 48 hours of its
  first — at the measured cost, about 0.04 USD per key with Nova Pro. A
  paying customer's key carries a plan instead: `betrieb`, 6,000 questions
  a calendar month and at most 600 a day, or `firma`, 15,000 and 1,500,
  German time, renewed every month (`app/plans.py`).

## Decisions

Everything with a monthly base fee is left out: S3 Vectors behind a Bedrock
knowledge base instead of a search cluster. A key is a budget, not a
password, counted in one conditional DynamoDB write. Three guardrails: one on
the answer, one that checks the question and the answer, each after a frame
that says what it is, for the denied topics, and one that masks the excerpts
of its sources. Every AWS call has
a timeout, and their sum stays under the function's own; a model call AWS
turns away is made once more while the caller still waits. In Ireland the
data stays in the EU; in Frankfurt it stays in Germany, which costs two
things: no CloudFront, so the function's own URL is the address, and no
topic guardrail that reads German without leaving the region, so a model in
the region checks the topics. Why each of these, and every other decision,
87 in all, with dates: [docs/decisions.md](docs/decisions.md).

## Limits

- **Personal details are filtered, not guaranteed.** The masking is
  statistical. In the browser acceptance a first name without a surname
  stayed readable in two excerpts, while the answer next to them was masked.
- **Citations are the model's.** One of the four answered live questions
  carried no `[n]` at all, although the grounding check scored it 1.0. The
  prompt asks for citations; the model does not always give them.
- **The topic filter is not exact.** In Ireland, of 87 harmless questions
  about a business's own documents the topic guardrail refused 1 — "How much
  early payment discount can I deduct, and by when must I pay?" — and all 45
  it must refuse, three runs on 06.10.2026; in Frankfurt the topic model
  refused none of the 87, three runs on 07.10.2026.
  The same question can get a different verdict in another run. Every change
  to the topics is measured first:
  [scripts/README.md](scripts/README.md#topic_probe--what-the-denied-topics-refuse-before-a-deploy).
- **The grounding threshold is a starting point.** In Ireland, 0.7 is the
  value AWS works its example with, not a tuned one; a correct answer scored
  0.73, just above it. In Frankfurt it is 0.8, measured (above), and there it
  withholds right German answers to English documents: six of the 42
  control questions on 07.10.2026. Every answer logs both scores, so the
  threshold can be moved on evidence.
- **Frankfurt has no domain of its own and no WAF.** The page and `/ask` are
  the function's URL, `https://<id>.lambda-url.eu-central-1.on.aws/`; the
  workspace key is the gate there, as it is behind CloudFront.

## How it was built

Built with Claude Code as a coding partner; the decisions, the reviews and
the measurements are the author's.

## Try it

Needs Python 3.11+, Node.js, the AWS CDK (`npm install -g aws-cdk`) and AWS
credentials. Everything goes to `eu-west-1` (Ireland), so documents, vectors
and answers stay in the EU; `-c region=eu-central-1` on every `cdk` command
and `--region eu-central-1` on every script keep them in Frankfurt instead.

```bash
python -m venv .venv && . .venv/Scripts/activate   # Linux/macOS: . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                  # 1,348 tests, no AWS needed

cdk bootstrap aws://<account>/eu-west-1    # once per account and region
cdk deploy                                 # builds everything
python -m scripts.workspace add quillmoor > /dev/null   # stores the workspace's key
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
