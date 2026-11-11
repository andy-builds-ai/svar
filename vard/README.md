# Vard

Vard is one part of Andur ([the overview](../README.md)). His loop is
[engine.py](engine.py), his first responsibility [receiving.py](receiving.py),
and his measuring scripts are in [scripts/](scripts/README.md).

Vard is an agent. He takes on one responsibility after another, works on
each with tools, and a person decides what happens. His first is the
receiving check: he fetches a delivery note that has come in, fetches the
purchase order it names, compares them and reports what differs. His second
build picked his model on 24 cases instead of twelve, the new ones harder
([decisions.md, 21.10.2026](../docs/decisions.md#2026-10-21-vard-second-build)). It started from the
four candidates below; MiniMax M2.1 dropped out, and MiniMax M2.5, the newer
model of the same maker, took its place. The operator chose M2.5: it passed
the most cases of the models measured with their makers' settings, and his
rule puts the fewest missed discrepancies next, because a missed shortfall
costs money and nobody notices it
([decisions.md, 21.10.2026](../docs/decisions.md#2026-10-21-minimax-m2-5)). Running him in
Frankfurt, with a trigger for new delivery notes, memory of corrections and
pausing, follows as a stage of its own. A support inbox, to be tried on the
made-up software company Quillmoor, is planned after that.

The loop is `vard/engine.py`: a model calls tools turn after turn until a
tool that closes the task has run, it answers without a tool, or the step
limit is reached (8 for the receiving check). Each tool carries a level:
allowed, ask (runs only when a person approves) or denied. A call to a tool
that is not offered, a denied one, one nobody approved and one without its
required fields is not run; the model is told why, and the call counts as
invalid. The loop knows no task of its own: a responsibility brings the task
text, the tools and the step limit (`vard/receiving.py`). The receiving check
has six tools, all allowed, and none of them books.

**Measured on 20.10.2026 in Frankfurt**, twelve made-up cases, two runs of
every model ([decisions.md, 20.10.2026](../docs/decisions.md#2026-10-20-vard-first-build)):

| Model | Passed, of 24 | USD, both runs | Median seconds per case |
|---|---|---|---|
| Nemotron 3 Super | 23; the 24th got no answer, a read timeout | 0.024 | 4.4 and 4.3 |
| Devstral 2 123B | 23 | 0.044 | 2.6 and 2.5 |
| MiniMax M2.1 | 23 | 0.061 | 6.2 and 7.4 |
| gpt-oss-20b | 23 | 0.010 | 2.0 and 2.0 |
| gpt-oss-120b, Qwen3 235B, Qwen3 32B, MiniMax M2.5, GLM 4.7 Flash | 22, 22, 21, 21, 17 | 0.010 to 0.052 each | 1.1 to 14.3 |
| Claude Sonnet 4.6, EU profile | 24 | 0.572 | 4.9 and 4.9 |
| Nova Pro, EU profile | 17 | 0.133 | 3.1 and 3.1 |

The runs took 11 min 56 s and 8 min 34 s and cost 0.488 and 0.490 USD for
all eleven models. A model's result moved by up to two cases between the
runs at the same settings, so twelve cases do not tell the leaders apart. The
four Frankfurt models at 23 of 24 are the candidates. Claude Sonnet 4.6 and
Nova Pro run behind the EU profile, which routes to every EU region, and
stand as a comparison only.

**Measured on 21.10.2026 in Frankfurt**, 24 made-up cases, three rounds, so 72
cases a model ([decisions.md, 21.10.2026](../docs/decisions.md#2026-10-21-minimax-m2-5)). The
four candidates ran first with the settings of the first build: MiniMax M2.1
passed 69, Nemotron 3 Super 64, Devstral 2 123B 58 and gpt-oss-20b 49. 19 of
gpt-oss-20b's cases ended in an error of Vard's loop, which is fixed. MiniMax
M2.1 was left out of the choice because AWS lists it as Legacy, with end of
life on 30.03.2027. Claude Sonnet 4.6, run once to see that the cases can be
solved, passed all 24. Then three models ran again, each as its maker
recommends (temperature, top_p, its reasoning given back or not, a read
timeout of 300 s):

| Model | Passed, of 72 | Discrepancies missed | USD | Median seconds per case |
|---|---|---|---|---|
| MiniMax M2.5 | 67 | 6 | 0.222 | 20.8 |
| Nemotron 3 Super | 57 | 11 | 0.114 | 5.3 |
| gpt-oss-20b | 51 | 7 | 0.046 | 2.8 |

Three of Nemotron's calls did not answer within 300 s. gpt-oss-20b wrote 25
tool names that do not exist, and a case with such a call fails. MiniMax
M2.5 missed discrepancies in 5 cases, in three of them it closed the note as
matching, and it is the slowest of the three. The two runs used different settings, so
their rows do not compare directly. The cases are made up, twelve of them in
this build; the result says nothing about real delivery notes.

**What Vard does not do yet.** So far he is only measured: nothing of him is
deployed, nothing starts him when a delivery note comes in, he remembers no
correction, and he books nothing. The documents come from the case file, not
from a real system.

```bash
python -m vard.scripts.receiving_probe --out receiving.json   # every model of FRANKFURT_MODELS, at most 2.25 USD
```

It calls real models in `eu-central-1`; the two full runs on 20.10.2026, on
the first twelve cases, cost 0.488 and 0.490 USD, and the runs of 21.10.2026,
on all 24 cases, 0.599 (four candidates), 0.708 (Claude Sonnet 4.6) and 0.382
USD (three models with `--vendor-settings`). `--max-usd` stops it before any
call that could take it past the limit. How it scores:
[vard/scripts/README.md](scripts/README.md#receiving_probe--which-model-vard-checks-deliveries-with).
