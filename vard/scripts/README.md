# Vard's scripts — measuring the models he works with

Two commands, both run from the repository root. What the scripts need (install, AWS login, the deployed stack): [taug/scripts/README.md](../../taug/scripts/README.md#what-they-need).

| Command | Does |
|---|---|
| `python -m vard.scripts.tool_probe` | Gives every model of `FRANKFURT_MODELS` six orders to record with one tool: whether it fills the tool cleanly and asks back instead of guessing. |
| `python -m vard.scripts.receiving_probe` | Lets every model of `FRANKFURT_MODELS` check 24 delivery notes against their purchase orders through Vard's tool loop: whether it finds every discrepancy, makes none up and closes with the right tool; ranks the models by cases passed, then by discrepancies missed. |

## tool_probe — whether a model can work a tool

```bash
python -m vard.scripts.tool_probe --model "Nova Pro" --out tools.json
```

It measures whether each model of `FRANKFURT_MODELS` fills in one tool
cleanly and asks back instead of guessing. `tool-cases.json` holds six
conversations for one tool, `record_order` (material, quantity, unit, site,
delivery date, all required), sent through `converse` without guardrail or
knowledge base. Three give everything and expect exactly one call with the
right values ("morgen" is 2026-10-07: the system prompt names the day); three
leave something out or vague ("ein paar Paletten") and expect no call and a
question or a request back for what is missing.

Every request Bedrock refuses as sent (`ValidationException`) fails its case
and counts as a result, whatever it was refused for, and the run ends with 0.
The probe does not tell a model that takes no tools from a wrong model id or
a tool definition Bedrock does not accept: the row stands in the table with
the error's text, under *Failed*, and that text says which it was. A call
that fails any other way — throttled, a service error, a timeout — says
nothing about the model, is named under the table and fails the run with
exit code 2. A missing login or permission stops it at once.

## receiving_probe — which model Vard checks deliveries with

```bash
python -m vard.scripts.receiving_probe --model "Qwen3 235B A22B 2507" --out receiving.json
python -m vard.scripts.receiving_probe --max-usd 1.00     # every model of FRANKFURT_MODELS, at most 1 USD
python -m vard.scripts.receiving_probe --model gpt-oss-20b --repeat 3   # every case three times
```

Vard takes on one responsibility after another, and a person decides; his
first is the receiving check. A model works in his loop (`vard/engine.py`)
with the receiving responsibility (`vard/receiving.py`) and six tools, all
allowed: `get_delivery_note`, `get_purchase_order`, `get_earlier_deliveries`
(the notes that came in before for an order), and the three that close the
check, `report_discrepancies`, `report_match` and `hand_off`. There is no
tool to book with; a call to a tool that is not offered, like one to book, is
refused, told to the model and counted as invalid, and so is a call without
its required fields. A tool that fails while it runs is no refusal: its error
goes back to the model as the tool's result, and the call is not invalid. The
loop ends when a closing tool has run, when the model answers without a tool,
or after 8 model calls.

`receiving-cases.json` holds 24 German cases, twelve for Pellwick Joinery and
twelve for Kalvenstett Haustechnik; the documents are made up, the businesses'
trades are taken from their documents. Each case starts with "Lieferschein …
ist eingegangen." and says which tool should close it and which discrepancies,
by article number and kind, should be reported. The first twelve have notes of
two or three positions: everything agrees (also with German numbers like
"1.250,00" against "1.250", and with boxes of 200 against pieces), a quantity
short or over, a position missing or extra, a similar but wrong article, a
wrong unit, a partial delivery whose open rest is reported, a note without a
purchase order to hand off, and three discrepancies at once. The other twelve
are harder, three of each kind: long notes of 23 to 40 positions with one to
three discrepancies among them; the rest of a partial delivery, compared with
what is still open after the earlier deliveries, which brings exactly the
rest, too little or too much; orders that list the supplier's article number
beside the business's own while the note gives only the supplier's, once with
a successor article in place of the ordered one, which is a wrong article; and
a note addressed to the other business, for an order of that business, which
the business that has the note does not have: it goes to a person.

A case passes when exactly one closing tool ran and it is the right one, every
expected discrepancy is reported with its kind, none is made up, and no call
was invalid. A wrong article counts as reported too when the ordered one is
reported missing and the delivered one extra: a person sees both all the same.
One of the two alone does not, and counts as made up, and so do both with their
kinds swapped. The business's own number and the supplier's both name an
ordered article. With `--repeat N` every case runs N rounds, all cases once
before the next round, and the marks count over all of them. The first table
ranks the models: most cases passed first; at a tie, fewest expected
discrepancies missed (the column Missed), since a missed shortfall costs money
and nobody notices it; then the lower cost. A case that got no answer, or
stopped, misses all of its discrepancies. It gives each model's marks,
invalid calls, mean steps, cost and wall time; under it each model's cases with
their round, steps, cost and seconds. `--out` holds the same, every call with
its input and result, and the tokens of every model call (`usage`).
`--keep-reasoning` gives a model's reasoning (Bedrock's `reasoningContent`
blocks) back in the conversation as it came; without it the reasoning stays
out, as on the runs of 20.10.2026 and the first two of 21.10.2026. `--out` says
for every model which it was.
`--vendor-settings` calls each model of `VENDOR_SETTINGS` in the script as its
maker recommends: temperature and top_p, its reasoning given back or not,
`maxTokens` 8,192 and a read timeout of 300 s instead of 40. Each row names its
source. A model without a row keeps the defaults (temperature 0, no top_p,
`maxTokens` 4,096, 40 s). The output and `--out`
say for every model which settings it ran with. With a row, a request Bedrock
refuses as sent fails the run with exit code 2, since the probe chose its values.

`--max-usd` (2.25 by default) is a hard limit. Before every model call the
probe works out the most the call can cost: the request's size in UTF-8 bytes
plus 500, as input tokens, and the request's `maxTokens` (4,096, or 8,192 with
`--vendor-settings`) as output tokens, at the
model's price in `taug/infra/answer_models.py`. If what the run has spent plus that
would pass the limit, the call is not made: the tables and `--out` are written
and the run ends with exit code 2. A call that fails keeps its most counted,
since AWS may have billed it. Claude Sonnet 4.6 is last in `FRANKFURT_MODELS`,
so the cheaper models are measured first. A missing login or permission stops
the run the same way, with the cases measured so far kept. A request Bedrock
refuses as sent fails its case and is a result; any other failed call fails the
run with exit code 2, as in `tool_probe`, and so does a tool of the case file
that fails, since neither says anything about the model. `--region` takes the
models and prices
`taug/infra/answer_models.py` gives that region: Frankfurt by default, Ireland with
`--region eu-west-1`; a region without a table there is refused.
