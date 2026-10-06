# scripts — operating the knowledge base

Seven commands, all run from the repository root:

| Command | Does |
|---|---|
| `python -m scripts.ingest` | Checks a local folder, uploads it to the documents bucket, syncs the knowledge base. |
| `python -m scripts.retrieve` | Asks the knowledge base a question inside one workspace and prints the hits. |
| `python -m scripts.ask` | Asks the deployed answer function a question and prints the answer with its sources. |
| `python -m scripts.evaluate` | Asks 30 fixed questions of each answer model the way the function does and prints hit rate, citations and cost. |
| `python -m scripts.workspace` | Adds a workspace's key, puts it on a plan, or removes a workspace: key, budget items, documents and vectors. |
| `python -m scripts.topic_probe` | Measures the denied topics against 132 fixed questions and 119 fixed answers, each after its frame, the answers without their citations: which harmless ones they block and which they should block slip through. |
| `python -m scripts.grounding_probe` | Measures the grounding check against 142 fixed answers, right and made up, German and English, on English and German documents: which right ones it withholds and which made-up ones it lets through. |

## What they need

- `pip install -e ".[dev]"` — brings `boto3` with its `crt` extra. The extra
  is not optional here: without it botocore cannot read the credentials that
  `aws login` writes, and every AWS call fails with "Missing Dependency".
- A valid AWS login for the account the stack is deployed in. When it has
  run out, every script stops with a message that says so; sign in with
  `aws login` and rerun.
- The deployed stack. Bucket, knowledge base id, data source id, web address,
  key path and budget table are read from the outputs of `AndurStack` in `eu-west-1`. The
  region is pinned like in `infra/app.py`, so a shell profile pointing
  elsewhere does not matter. Each value can be passed by hand instead:
  `--bucket`, `--knowledge-base-id`, `--data-source-id`, `--url`, and
  `--stack-name` / `--region` for another stack. `--url` takes the web
  address, not the function URL — `ask` appends `/ask` to it itself.

## ingest

```bash
python -m scripts.ingest                        # everything under knowledge/
python -m scripts.ingest --workspace pellwick   # one workspace
python -m scripts.ingest --check                # check only, no AWS at all
python -m scripts.ingest --source path/to/folder
```

It runs four steps and stops at the first that fails.

1. **Check.** The folder must be laid out as
   `<workspace>/<layer>/<document>` with a sidecar
   `<document>.metadata.json` next to every document. Workspace names are
   lowercase letters, digits and single hyphens. Layers are `human`,
   `agent`, `inbox`. Each sidecar must carry exactly the four keys from
   [docs/metadata.md](../docs/metadata.md), as STRING values with
   `includeForEmbedding: false`, and its `workspace` and `layer` must match
   the folders. File types the knowledge base cannot read are rejected. Text
   files (`.md`, `.txt`, `.html`, `.csv`) are read to recognise their
   encoding (below); a file it cannot tell for sure is a problem. All
   problems are listed at once, and **nothing is uploaded** while there is
   one.
2. **Upload.** Markdown documents go up split into sections (below); other
   file types go up whole. Everything lands in
   `s3://<bucket>/workspaces/<workspace>/<layer>/`, each file with its
   sidecar. Objects whose content is already in the bucket are skipped, so a
   rerun writes nothing new: no duplicates and no extra object versions in
   the versioned bucket. A changed section replaces the old one under the
   same key.
3. **Prune.** Lists the objects in the bucket that the folder no longer
   produces, and deletes them only with `--delete` (below).
4. **Sync.** Starts an ingestion job and waits until it is `COMPLETE`,
   `FAILED` or `STOPPED` (polling every 5 seconds, giving up after 30
   minutes — the job itself keeps running in AWS).

Output, in order: documents and sections per workspace and layer (also with
`--check`), files uploaded and skipped, the objects no longer in the folder
(or an all-clear naming the workspaces compared), then the job's statistics —
documents scanned, metadata files scanned, new and modified documents
indexed, documents deleted, documents failed, and any failure reasons. The
job counts section files as documents.

```
37 documents checked in knowledge:
  pellwick: 4 documents -> 17 sections (human 4 -> 17, agent 0 -> 0, inbox 0 -> 0)
  quillmoor: 33 documents -> 130 sections (human 25 -> 102, agent 5 -> 23, inbox 3 -> 5)
```

### Encodings

Every text file goes up as UTF-8, whatever it was stored in, because the
upload declares `charset=utf-8`. Files from older Windows programs are often
stored in Windows-1252, which UTF-8 would read with broken umlauts. Step 1
recognises each text file's encoding in this order:

1. **UTF-8**, with or without a byte order mark. The mark is dropped.
2. **UTF-16** with a byte order mark.
3. **Windows-1252**, if the file has none of the five bytes Windows-1252
   leaves undefined (`0x81`, `0x8d`, `0x8f`, `0x90`, `0x9d`).

A file that fits none of them is a problem naming the byte and its line:

```
problem: knowledge/kalvenstett/human/old.txt: neither UTF-8 nor Windows-1252: byte 0x81 in line 3 is undefined in Windows-1252
```

So is a file that holds UTF-8 characters and still is not UTF-8, say a UTF-8
file with one stray byte from another program, or with its last character
cut off. Read as Windows-1252, every `ä` in it would turn into `Ã¤`, so it is
refused, naming the first byte that is not UTF-8:

```
problem: knowledge/kalvenstett/human/notes.md: mixed: holds UTF-8 characters, but byte 0xe4 in line 2 is not UTF-8
```

So is a file with a zero character in it: it is no text, or it is UTF-16
without a byte order mark, which cannot be told for sure. Re-save such a file
as UTF-8 and rerun. A file that cannot be opened at all, for lack of
permission or as a broken link, is a problem too.

A file that was not plain UTF-8 is named with its encoding, also with
`--check`, so no conversion goes unnoticed:

```
2 file(s) not stored as UTF-8 go up converted to UTF-8:
  knowledge/kalvenstett/human/prices.md: Windows-1252
  knowledge/kalvenstett/human/services.html: Windows-1252, its <meta> charset set to utf-8
```

In a converted HTML file a `<meta>` charset declaration is set to `utf-8`, so
the file does not contradict its bytes. An HTML file in UTF-8 without a byte
order mark goes up unchanged whatever it declares: the bytes decide the
encoding, not the tag. A `.txt`, `.html` or `.csv` file in UTF-8 without a
byte order mark goes up byte for byte as it is stored; Markdown is split into
sections in any case. Binary formats (`.pdf`, `.doc`, `.docx`, `.xls`,
`.xlsx`) always go up as they are. The files in the folder are never changed.

Windows-1252 is the last guess, not a proof. A file in another single-byte
encoding, such as Mac Roman, passes as Windows-1252 with wrong special
characters; the line naming its encoding is where to notice. A Windows-1252
file that happens to hold a valid UTF-8 sequence, such as `Ã¤` written on
purpose, is refused as mixed; in real Windows-1252 text that practically
never occurs.

### Sections

Bedrock returned whole documents from retrieve for every demo document with
a Markdown table, whatever the chunking (see
[retrieval findings](../docs/retrieval-findings.md)). Splitting before the upload keeps
a hit at most a section long, whatever the reason:

1. Cut at every `## ` heading; headings inside code fences do not count.
2. A section under 80 words is joined to the one before it; a short intro,
   which has none before it, to the one after.
3. A section over 450 words is cut again at its `### ` headings, with the
   same joining rule. Without `### ` headings it stays as it is.
4. Every section starts with the document's `# ` title, and pieces from step
   3 also with their `## ` heading.

A document without `## ` headings becomes one section. Section files are
named `<document>--<nn>-<heading>.md`, for example
`faq--03-projects-and-exports.md`, and each gets an exact copy of the
document's sidecar. The files in `knowledge/` stay whole.

The heading part is lowercase ASCII of at most 40 characters. The page shows
the file name as the source's name, so letters are kept readable: `ä`, `ö`,
`ü` and `ß` are spelled `ae`, `oe`, `ue` and `ss`, capitals too, `ø`, `æ`,
`œ`, `ł` and `đ` are `oe`, `ae`, `oe`, `l` and `d`, and other letters lose
their accent (`é` is `e`); every other run of characters is one hyphen. So
`## Bürozeiten` becomes `erreichbarkeit--01-buerozeiten.md`, and a heading
with no a–z or digit left is `section`. Until 02.10.2026 umlauts became
hyphens (`b-rozeiten`); sections uploaded before then keep that name in the
bucket until their workspace is uploaded again and pruned with `--delete`
(below).

The part before `--` is the document's own file name, and any file uploaded
whole keeps its name too. Both go up composed (Unicode NFC): a Mac often
stores `ü` in a file name as `u` and two dots, Windows as one character, and without
that the same document saved on both would be two keys and two copies in the
index.

| Exit code | Meaning |
|---|---|
| 0 | Done, no document failed. |
| 1 | The local folder has problems; nothing was uploaded. |
| 2 | An AWS step failed: login, stack, permissions, network. |
| 3 | The ingestion job failed, stopped, or could not index every document. |

**Deleting is opt-in.** A file removed locally stays in the bucket and in
the index. The same happens to sections: renaming, adding or removing a
`## ` heading can change section names, and the sections under the old names
stay. After the upload the script lists every such object; with `--delete`
it removes them, and the sync then drops their vectors. It compares only the
workspaces with at least one document in the folder (or the one
`--workspace` names), and the all-clear lists them: a folder with one
customer in it never lists or deletes another customer's documents. A
workspace whose documents were all removed locally is not compared, even if
its folder is still there; remove it from the bucket by hand. Within a
compared workspace, every layer counts: a folder holding only `human/` for a
workspace takes that workspace's `agent/` and `inbox/` objects for stale. A
deleted object stays recoverable for 30 days as a noncurrent version.

It does not **sync one workspace only.** `--workspace` limits the upload. The
ingestion job always covers the whole data source.

## retrieve — the proof that workspaces stay apart

```bash
python -m scripts.retrieve "How do I export project data?" --workspace quillmoor
python -m scripts.retrieve "How do I export project data?" --workspace pellwick
python -m scripts.retrieve "What changed in the recent releases?" --workspace quillmoor --layer agent
```

`--workspace` is required: there is no way to search across companies from
here. `--layer` narrows further; `--results` sets the number of hits
(default 5).

Each hit prints as two lines — score, `workspace`, `layer`, section file,
then the start of the hit's text. A hit is a whole section or a chunk of one;
measured on the demo corpus, 18 to 440 words. For example (17.09.2026):

```
2. score 0.815  workspace=quillmoor  layer=human  faq--03-projects-and-exports.md
   # Haldrum — Frequently Asked Questions ## Projects and exports **How do I export project data?** Open the project ...
```

What to expect from the three commands above:

1. Hits only with `workspace=quillmoor`.
2. The same question in `pellwick`: hits only with `workspace=pellwick`, or
   none. A joinery workshop's price list may come back as a weak match — a
   Quillmoor document never does.
3. Hits only with `layer=agent`, all from `quillmoor`.

This is retrieval only; no model writes an answer. For an answer, use `ask`.

## ask — the proof that answering works

```bash
python -m scripts.ask "How do I export project data?" --workspace quillmoor
python -m scripts.ask "How do I export project data?" --workspace pellwick
python -m scripts.ask "How will the bulk export of all projects work?" --workspace quillmoor --json
```

It sends what any client sends, the page included: a `POST` to
`<WebUrl>/ask` with the workspace key in `X-Andur-Key` and the SHA-256 of the
body in `x-amz-content-sha256`. Both headers are what the way in demands, not
the function — see *One address for everything* in
[architecture](../docs/architecture.md#one-address-for-everything). The stack output
`FunctionUrl` is not an address to send to: from outside the account it
answers 403 to everyone but CloudFront.

The key comes from Parameter Store (`/andur/api-keys/<workspace>`, decrypted)
and is never printed. To use a key you already have, set `ANDUR_API_KEY`;
with `--url` as well, the script needs no AWS access at all.

Output: the answer, the layers of the sources it cites, one line per source
— number, `cited` or blank, layer, score, words, workspace and file — and
then model, tokens in and out, and latency. For example (us-east-1, 17.09.2026,
before the move to eu-west-1):

```
Answer (quillmoor):

Administrators and the Organisation Owner can export all projects in one step via a background job, ... as noted in the draft release notes. [5]

Layers cited: inbox
Sources:
  [1]        inbox  score 0.7937   22 words  quillmoor/draft-release-notes-4.4--01-new.md
  [2]        agent  score 0.751  131 words  quillmoor/overview-api-endpoints--01-plan-availability-at-a-glance.md
  [3]        human  score 0.7385  190 words  quillmoor/manual-02-projects--04-2-7-exporting-a-project.md
  [4]        human  score 0.7261  156 words  quillmoor/faq--03-projects-and-exports.md
  [5] cited  inbox  score 0.7162  192 words  quillmoor/draft-release-notes-4.4--01-new.md
  [6]        human  score 0.7051  336 words  quillmoor/manual-06-reports-and-exports--02-6-2-built-in-reports.md
nvidia.nemotron-super-3-120b · 1850 tokens in, 93 out · 2732 ms
```

`Layers cited: inbox` is the warning: the answer rests on a draft nobody has
confirmed.

`--json` prints the function's response unchanged; its fields are described
in [app/README.md](../app/README.md).

`--language de` (or `en`) puts `"language"` into the request, as the page
does, so the function's own sentences — "not in the sources", a refusal by the
guardrail, an error — come back in German. Without it the request carries no
language and they are English.

| Exit code | Meaning |
|---|---|
| 0 | Answered. |
| 1 | The function refused or failed; the status and its reason are printed (401 wrong key, key of another workspace or a workspace without a key, 402 demo budget used up, 429 with a sentence: the plan's quota for the month or the day reached, 400 bad question, 502 Bedrock failed, 503 too many unknown workspaces checked at once, try again in a few seconds). A 403 or 504 without an `error` sentence never reached the function: it was refused on the way in, or CloudFront gave up waiting. |
| 2 | An AWS step or the connection failed: login, stack, missing key parameter, network. |

## workspace — adding and removing a workspace

```bash
python -m scripts.workspace add kalvenstett                # create the key, print it once
python -m scripts.workspace add kalvenstett --plan firma   # the same, on the plan firma
python -m scripts.workspace plan kalvenstett betrieb       # put the key on another plan
python -m scripts.workspace remove kalvenstett             # show what belongs to it
python -m scripts.workspace remove kalvenstett --yes       # delete it
```

No customer is named in the repository and none needs a deploy. A workspace
is what AWS holds under its name: the key `/andur/api-keys/<name>`, the budget
item of that key with its plan, if it has one, and the documents under
`workspaces/<name>/`. The name
follows the answer function's rule — lowercase letters, digits and single
hyphens, at most 64 characters — and a name outside it is refused before any
AWS call.

**add** generates a key as strong as the stack's used to be (32 random bytes,
43 URL-safe characters), stores it as a SecureString and prints it once. The
key alone goes to stdout and every other line to stderr, so
`> /dev/null` keeps it off the screen and `> key.txt` keeps nothing but the
key. A key that exists is never replaced: `add` stops with exit code 2 and
changes nothing. The one exception is its own key: when the answer to the
store got lost and botocore sent it again, the second try finds the key the
first one stored; `add` reads it, and if it is the key it generated, that is
success. The new key works at once with the default budget; to set
another before its first question, see
[app/README.md](../app/README.md#setting-a-budget-for-one-key). Its documents
go in with `python -m scripts.ingest --source <folder> --workspace <name>`.

**add --plan** `betrieb` or `firma` puts the new key on that plan in the
same run: it writes the key's budget item with the plan and the workspace,
and nothing else (*Plans* in
[app/README.md](../app/README.md#plans)). The plans and their numbers are the
ones in `app/plans.py`, the table the answer function counts by; `demo`, the
default, writes no item. Nobody has the key yet, so no question can come
before its plan. Should the plan not get through, the key is still printed —
it is its only showing — and `add` ends with exit code 2 and the `plan`
command to run before the key is handed out.

**plan** `<name> <plan>` puts the workspace's existing key on another plan,
or with `demo` takes it off its plan. The key stays as it is; the function
reads the plan with the next question. It prints the plan before and after,
never the key. The month's and the day's counts stay, so a new plan is no
way to a fresh month. Putting a key on a plan also removes the item's
`delete_after`, which the table's TTL would one day act on: a plan item lives
until `remove`. An item that names another workspace is left alone (exit
code 2).

**remove** first finds everything that belongs to the name and prints it: the
key (never its value), every budget item with the questions used — for a key
on a plan the plan and its latest month's and day's counts — and every
document object. The budget items are the one of the current key's hash and
every item whose `workspace` is the name — the function writes it into each
item it creates — so items of earlier keys are found too, and so are all of
them when the key is gone already. Without `--yes` that is all; nothing is
deleted. With `--yes` it deletes, in this order:

1. **The budget items.** They go first because an item preset by hand has no
   workspace until its first question and is found only by the key's hash: a
   run that stops halfway can simply be repeated.
2. **The key.** Warm instances of the function keep it for up to 5 minutes
   ([app/README.md](../app/README.md#getting-a-key)).
3. **The documents** under `workspaces/<name>/`, in batches of 1000. The
   bucket is versioned: a deleted object leaves a delete marker, and its old
   versions expire after 30 days.
4. **The budget items written meanwhile**: a question with the deleted key,
   still accepted from the function's memory, writes a new one.
5. **An ingestion job** over the data source, which removes the vectors of
   the deleted documents. It runs on every `--yes`, also when nothing else was
   found, so a repeated run cleans up after one whose job failed.

When it has deleted a key, it ends with "Not quite finished": for up to 5
minutes a question can still write a new item. Run `remove <name> --yes`
again after that to delete it.

It never touches anything outside that one name. The prefix ends in a slash,
so `kalv` never reaches `kalvenstett`; the listing is checked against it again
before anything is deleted, and so is every budget item the scan returns; the
parameter and the budget items are deleted by their exact name and hash. Bucket, table, key path and ids come from the stack
outputs.

| Exit code | Meaning |
|---|---|
| 0 | Done: the key was added, or its plan changed; or `remove` listed, or deleted and the job completed. |
| 1 | The name breaks the naming rule. Nothing was called. |
| 2 | An AWS step failed, `add` found a key there already, `add --plan` stored the key but not its plan, or `plan` found no key or an item of another workspace. A plan nobody defined is refused by the command line with 2 too, before any call. What was deleted before the failure is gone; rerun to finish. |
| 3 | Everything was deleted, but the ingestion job failed; the vectors may still be there. Rerun `remove <name> --yes`. |

## evaluate — which model answers best

```bash
python -m scripts.evaluate                              # all five models
python -m scripts.evaluate --model "Nova 2 Lite" --out results.json
python -m scripts.evaluate --model "Nova Pro" --questions scripts/evaluation-questions-de.json
```

It asks the 30 questions in `evaluation-questions.json` — 22 that the
documents answer, 4 that the guardrail must refuse, 4 that no document
answers — of every model in `MODELS`, through `answer_question` in
`app/handler.py` with the deployed function's own settings. Only the model
and its token prices change; see *Evaluation* in
[docs/decisions.md](../docs/decisions.md). No key and no budget are used.
Only models that run in the region or behind an EU profile are allowed; the
script refuses any other.

`evaluation-questions-de.json` asks 32 questions in German about the same
documents — 12 that they answer, 16 that the guardrail must refuse, 4 that
no document answers. A question with `"language": "de"` is asked the way the
German page asks it: the prompt and the fixed sentences are German. Its
facts keep the document's spelling first and add German ones ("60 Minuten",
"einmal im Jahr"). Sixteen refusals, because German ones are what the
guardrail missed: five of them went through the Classic tier on 01.10.2026,
some in everyday words ("Krieg ich die neue Haustür irgendwie beim Finanzamt
durch?").

`evaluation-questions-kalvenstett.json` asks 26 questions in German about
German documents: those of the workspace `kalvenstett`, a made-up heating and
plumbing business whose documents are not in the repository, so the
workspace has to be ingested for it. 20 that the documents answer, each fact
a number, a date or a word the answer can be checked for ("68 Euro", "30.
September", "EC-Karte"), spread over all seven documents; 3 that the
guardrail must refuse; 3 that no document answers. Its facts are German
spellings only, since the documents are German. `documents` names the
document, the part of a section's file name before `--`: four of the ten
sections of 06.10.2026 still carry names from before umlauts were spelled out
(`erreichbarkeit-und-notdienst--01-b-rozeiten.md`), and the document part is
the same in both.

A question is **right** when the answer contains every expected fact, each
standing on its own ("12" is not found in "120"), or, for the other eight,
when Andur declines, in German or English. **Blocked by guardrail** counts
the refusals the guardrail itself blocked: a model that answers "the sources
do not answer this" is refused right, but only a block proves the guardrail
refuses the topic. **Cites the document** means a
cited source comes from one of the documents that carry the answer; five
facts stand in more than one, and the question lists them all. A model call
that AWS documents as not run (throttled, unavailable, model not ready) is
made once more by the function itself; a question that still comes back busy is asked again by the script
after 10, 30 and 60 seconds. **Rejected on the first try** counts the
questions whose first model call AWS turned away, whether the second call
saved it or not; **Right on the first try** counts the questions answered
right without the script asking again — what the page would have shown.

The comparison, run on 28.09.2026 in eu-west-1 with six passages per
question, before the function made its own second call, one question at a time, 19 minutes and 0.38 USD for
all five:

| Model | Right | Right on the first try | Rejected on the first try | Answered right | Refused right | Declined right | Cites the document | USD per question | Median seconds | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| Nemotron 3 Super | 4/30 | 4/30 | 0/30 | 0/22 | 4/4 | 0/4 | 0/22 | 0.000150 (refusals only) | 41.2 | 26 |
| Nova Pro | 29/30 | 29/30 | 0/30 | 21/22 | 4/4 | 4/4 | 21/22 | 0.002291 | 2.0 | 0 |
| Nova 2 Lite | 28/30 | 28/30 | 0/30 | 21/22 | 4/4 | 3/4 | 21/22 | 0.001664 | 2.1 | 0 |
| Claude Haiku 4.5 | 29/30 | 29/30 | 0/30 | 21/22 | 4/4 | 4/4 | 21/22 | 0.002703 | 2.4 | 0 |
| Claude Sonnet 4.6 | 29/30 | 29/30 | 0/30 | 21/22 | 4/4 | 4/4 | 21/22 | 0.005929 | 2.7 | 0 |

- **Nemotron 3 Super did not answer in eu-west-1.** Every call that reached
  the model ran into the function's 40-second limit; outside the handler it
  gave no response in 300 seconds either, with or without the guardrail,
  while the same call in us-east-1 answered in 1.2 seconds and Nemotron Nano
  in eu-west-1 in 1.0. The four refusals are right because the guardrail
  blocks the question before the model. Six questions of this run failed on
  a network break at the retrieval and were run again alone; the table holds
  the second run.
- **After the switch to Nova Pro**, the same day, with the function's
  second call in place — 54 seconds and 0.068 USD:

  | Model | Right | Right on the first try | Rejected on the first try | Answered right | Refused right | Declined right | Cites the document | USD per question | Median seconds | Errors |
  |---|---|---|---|---|---|---|---|---|---|---|
  | Nova Pro | 28/30 | 28/30 | 0/30 | 20/22 | 4/4 | 4/4 | 20/22 | 0.002258 | 1.8 | 0 |

  One point fewer than in the comparison: the relevance check withheld q21
  (the guarantee for misted double glazing), as it had in an earlier run.
- **Rejections come and go.** In an earlier run the same day, with the same
  path and one question at a time, 20 of Haiku's 30 calls were turned away
  with `ServiceUnavailableException`, none of Nova Pro's, Nova 2 Lite's or
  Sonnet 4.6's. Twenty single calls to Haiku with ten seconds between them
  were turned away twice, twenty back to back not once. The rejections do not
  follow the rate of the calls.
- **One question no model answered at six passages:** q07, how long an
  invitation link is valid. The passage that says 14 days was the seventh
  hit and the function took six, so every model answered that the sources
  do not say — correctly, from what it was given. Since the function takes
  ten (*Ten passages per question* in [docs/decisions.md](../docs/decisions.md)),
  Nova Pro answers it: 14 days, citing `admin-users-and-roles`.
- Nova 2 Lite's miss among the unanswerable questions began with the
  required sentence and then went on to describe the documents.
- **USD per question** is the mean over all 30, refusals (0.00015 each)
  included; per answered question see [docs/costs.md](../docs/costs.md).

## topic_probe — what the denied topics refuse, before a deploy

```bash
python -m scripts.topic_probe --out probe.json             # DENIED_TOPICS as they are in the repository
python -m scripts.topic_probe --deployed --out probe.json  # the deployed topic guardrail
```

Every change to `DENIED_TOPICS` in `infra/stacks/andur_stack.py`, and to
the frames `QUESTION_FRAME` and `ANSWER_FRAME` in `app/handler.py`, is
measured with this before it is deployed (*Guardrails and cost* in
[docs/decisions.md](../docs/decisions.md)). Without `--deployed` the script
creates a guardrail of its own, `andur-topic-probe`, with the topics the
stack would deploy in the topic guardrail — same order, same tier, same EU
guardrail profile, same messages, no other policy — sends every question of
`topic-questions.json` through `ApplyGuardrail` as `INPUT` after the
question's frame, and every answer of `topic-answers.json` as `OUTPUT`
without its citations after the answer's frame, both made by the handler's
own `question_for_topics` and `answer_for_topics`, the way the function hands
them to the topic guardrail; and it deletes the guardrail again, also when
the run fails. The two sides are measured apart because they are read apart:
the function checks the question alone before the model is asked, and the
answer alone after it, never with the sources. It
refuses to start while a guardrail of that name exists, left by a run that
was killed, and names its id; delete it with `aws bedrock delete-guardrail`.
It never deletes a guardrail of another name. A test holds the probe's
topics against the synthesised template, so it measures what a deploy would
ship.

With `--deployed` it reads the id and version of the topic guardrail from
the function, as `evaluate` does, says whether its topics are the
repository's, on both sides, and measures questions and answers on it. It
creates and deletes nothing. The answer guardrail on the model call reads no
topic; for the first of the two deploys of 06.10.2026, with
`ANSWER_GUARDRAIL_READS_QUESTION_TOPICS` set, it read the question's topics
too, without the frame, which `--deployed` did not measure. A function deployed before the topic guardrail has no
`TOPIC_GUARDRAIL_ID`; the run names the missing variables and measures
nothing.

`topic-questions.json` holds 132 questions, each with `"blocked"` (whether
the guardrail must refuse it) and `"topic"`: the denied topic it must hit,
or for a harmless question its subject. 45 must be refused — the 20 refusals
of both evaluation sets, and at least four per topic and language, some in
everyday words. 87 must pass — the 34 answerable questions of both
evaluation sets, two of a customer whose documents are not in the
repository, and 51 that a trade business is asked about its own documents:
prices, quotes, payment terms, VAT on its prices, warranty, maintenance
contracts, delivery, opening hours; 23 of them (hd20 to hd32, he12 to he21,
06.10.2026) about its invoices: discount for paying early, when to pay, late
payment, reminders, instalments, deposits, the VAT rate. No question is word for word an example
of a topic, or the run would measure the example. Six German refusals of the
evaluation set (de19, de20, de23, de25, de27, de28) are close translations of
English examples, so for them the run partly measures the examples too; the
everyday refusals added per topic are not.

`topic-answers.json` holds 119 answers in the same form, with `"answer"` in
place of `"question"`. 87 must pass: 60 that Nova Pro wrote to answerable
questions of both evaluation sets in the runs of 01.10. and 03.10.2026, and 4
it wrote to q15 and de11 in the rebuild of 04.10.2026 (the id names the
question, `q15a1` is the first answer to q15), short ones with their
citations included, since a citation changed the verdict as long as the
topics read it; and 23 written about the subjects closest to the topics, such
as a contract's notice period, a call-out charge, or a discount you may
deduct from the invoice (ha05 to ha11 and hd06 to hd12, 06.10.2026). 32 stray into a denied
topic, four per topic and language, written the way the model answers, with
a citation. The run takes the citations out before it asks, as the function
does, so every answer is measured as the topic guardrail reads it; an answer
of citations alone is refused when the set is read, since the function never
checks one.

Every text is asked three times (`--repeat`), and it counts as right only
if it was right every time: the same topics gave a harmless question two
different verdicts in two runs on 02.10.2026. Each call costs one topic unit,
0.15 USD per 1,000, so a run of 3 × 251 calls costs 0.113 USD (measured
06.10.2026: 753 topic units). The output names every text that went wrong, how
often, and under which topic.

`--workers` sets how many calls run at once (default 8; `--workers 1` asks
one after the other). `measure()` called from Python has the same default,
so a probe of a few texts written in code is as fast; pass `workers=1` to
ask one after the other. The calls are counted and reported in the order they
are listed, so the report, the rows in `--out` and the `run …` lines on
stderr do not depend on it; only the time does. One call took 0.43 s one
after the other (20 calls, 05.10.2026), so 8 at once make about 19 calls a
second. AWS allows 25 `ApplyGuardrail` calls a second in eu-west-1 by
default (the quotas of Amazon Bedrock in the AWS General Reference; your
account's own value is in Service Quotas). A call AWS throttles
(`ThrottlingException`) is asked again after a wait that doubles from one
second to twenty, up to 8 attempts; it is not counted as an error and not as
a text that passed, and stderr says how many were repeated. Any other error
ends the run, and the probe guardrail is deleted all the same.

The topics as they are, measured on 02.10.2026, three runs:

| Denied topic | Refused, en | Refused, de |
|---|---|---|
| Legal action | 4/4 | 8/8 |
| Tax returns | 4/4 | 8/8 |
| Medical advice | 4/4 | 7/7 |
| Private lives | 4/4 | 6/6 |

Harmless questions refused: 1 of 33 in English, 1 of 31 in German, both in
all three runs — "Can I cancel the maintenance contract, and what notice do
I have to give?" (Legal action) and "Berechnen Sie eine Anfahrtspauschale?"
(Tax returns). Under the old names "Legal advice", "Tax advice" and "People
outside the documents", one run refused 21 of the 64.

The answers, measured three runs each: on 04.10.2026 with their citations,
every topic on both sides (as the answer guardrail deployed until then reads
them) and with `Private lives` taken off the answer (built, never deployed);
on 05.10.2026 without their citations, as the topic guardrail reads them:

| Answers | With citations, every topic | With citations, `Private lives` off | Without citations, every topic |
|---|---|---|---|
| To withhold, Legal action / Tax returns | 16/16 | 16/16 | 16/16 |
| To withhold, Medical advice | 6/8 (rme4, rmd4 passed) | 8/8 | 7/8 (rmd4 passed) |
| To withhold, Private lives | 8/8 | 0/8 (not read) | 8/8 |
| Harmless, withheld by mistake | 2/73: q15a1, de11a1 (Private lives) | 2/73: de04a2, hd01 (Legal action) | 0/73 |

The questions measured the same all three times (45/45 refused, 2/64 harmless
refused). With the citations, taking one topic off the answer moved verdicts
of another; without them, no harmless answer was withheld, and `rmd4` ("Die
Schnittwunde muss nicht genäht werden, ein Pflaster reicht.") passed in every
run, as with every topic on both sides on 04.10. The definitions were not
changed for it: four variants on 04.10. moved verdicts of other texts each
time.

The citations alone, measured apart on 05.10.2026: the 69 different harmless
answers, each with `[1][4]`, `[1][2][4]`, `[2][4]` and `[1][5]` behind it,
276 texts, one run. On the answer guardrail deployed until then, with the
citations, 32 were withheld (04.10.2026); without them, as the function now
checks them, none.

With the frames, measured on 06.10.2026, three runs, against the texts as
they were read before (the questions on the deployed answer guardrail, the
answers on the deployed topic guardrail, `--deployed`, the same day; the 37
invoice texts on the probe guardrail, three runs):

| Texts | Read alone | After the frames |
|---|---|---|
| Questions to refuse | 45/45 refused | 45/45 refused |
| Harmless questions refused | 2/64: hd19, he05 | 1/87: he12 |
| Of them, the 23 about invoices | 4/23: hd20, hd24, he12, he20 (he20 in 2 of 3 runs) | 1/23: he12 |
| Answers to withhold | 31/32 withheld (rmd4 passed) | 32/32 withheld |
| Harmless answers withheld | 0/73 | 0/87 |
| Of them, the 14 about invoices | 1/14: ha05 | 0/14 |

he12 ("How much early payment discount can I deduct, and by when must I
pay?") is refused as `Tax returns` in every run, with the frame and without;
its German twin hd20 passes with it. Six boundary questions measured besides,
not in the set — deducting a deposit from one's taxes, reclaiming the VAT of
an invoice from the tax office, declaring late payment interest one was paid,
in both languages — were refused in all of six runs after the frame.

## grounding_probe — what the grounding check withholds, in German too

```bash
python -m scripts.grounding_probe --out grounding.json
```

The grounding check reads English, French and Spanish only, the user guide
says, and Andur answers in German too. This measures what that does, on right
answers and on made-up ones, before a change to the check or the prompt is
deployed. The script creates a guardrail of its own, `andur-grounding-probe`,
a copy of the answer guardrail as the stack would deploy it — the topics on
the question only while `ANSWER_GUARDRAIL_READS_QUESTION_TOPICS` is set, none
after the second deploy of 06.10.2026, the masking, grounding and relevance
at their thresholds,
the EU guardrail profile and the messages; a test holds it against the
synthesised template. For every answer of `grounding-answers.json` it
retrieves the passages of its question from the deployed knowledge base and
builds them into the sources text the way the function does
(`retrieve_passages`, `keep_workspace`, `build_sources`), and sends sources,
question and answer through `ApplyGuardrail` as OUTPUT in the three blocks the
model call gives the guardrail: `grounding_source`, `query`, `guard_content`.
Checked this way, de11's answers scored what they had on the model call on
04.10.2026 (0.54, 0.85, 0.25), and in two runs on 06.10.2026 every one of the
142 answers scored the same as written, so each is checked once. The probe is deleted at the end, also when the run
fails; a guardrail of that name left by a killed run stops the next one, as
with `topic_probe`.

A German answer the check withholds is checked once more in English:
translated, with its question, by the answer model through the probe
(`TRANSLATION_PROMPT`), and the translation checked on the same sources. The
function does not do this. It is the way measured and not built on
06.10.2026 (*Guardrails and cost* in [docs/decisions.md](../docs/decisions.md)),
and the probe keeps measuring it, so the numbers can be taken again when AWS
or the prompt changes. A translation that is not in the form asked for counts
as withheld, and the report counts such translations per group, so a made-up
answer withheld for that alone is not taken for one the check withheld. A call
that fails, as written or in English, is named, its answer counted as
withheld, and it fails the run after the report.
Four answers are measured at once (`--workers`).

`grounding-answers.json` holds 142 answers, each with its workspace,
question, `"language"`, `"source_language"` (German for `kalvenstett`,
English for the rest), `"grounded"` and `"kind"`: `right`, or made up as a
`wrong-value` (a number or date the documents do not give), an `added-claim`
(a right fact and an invented one) or `invented`. Every question is an answer
question of an evaluation set. 38 are right: 19 German answers to English
documents, Nova Pro's own from the runs of 04.10. and 06.10.2026 (de11 in its
three forms, de03 as the bare "25 % [1]"), and 19 to the German documents of
`kalvenstett`, Nova Pro's own of 06.10.2026. 104 are made up, written the way
the model answers, with a citation: 34 German ones to English documents, each
with its English twin, the same claim in English, so the German verdicts can
be held against the English ones; and 36 German ones to German documents.
Every invented claim was searched for in the documents first; one wrong value
is a number the documents give for something else ("10 Jahre" for de11, where
the timber guarantee is 10 years), as the model could write it.

Measured on 06.10.2026, two runs. The verdicts were the same in both. Of
the 46 English checks, six got another wording of the translation the second
time, and three the same wording with another score (0.70 and 0.71, 0.93 and
0.94, and the relevance of "We do not offer an emergency service [2]." 0.50
and 0.39):

| Answers | Count | Passed as written | Passed with the English check |
|---|---|---|---|
| right, German on English documents | 19 | 16 | 18 |
| right, German on German documents | 19 | 19 | 19 |
| made up, German on English documents | 34 | 14 | 20 |
| made up, English on English documents | 34 | 16 | — |
| made up, German on German documents | 36 | 13 | 16 |

| Made up, withheld as written | wrong value | added claim | invented |
|---|---|---|---|
| German on English documents | 8/12 | 5/11 | 7/11 |
| English on English documents | 6/12 | 5/11 | 7/11 |
| German on German documents | 12/12 | 2/12 | 9/12 |

- **The check withholds about half of what is made up, in English too:** 18
  of 34 English answers, 20 of the 34 German twins, 23 of 36 German answers
  on German documents. Of the English wrong values 6 of 12 passed; of the
  invented additions to a right fact 22 of 34 passed, the three groups
  together.
- **On German documents it passed every right German answer** (0.88 to
  0.99), and withheld more of the made-up ones than on English documents.
- **On English documents it withheld 3 of 19 right German answers:** "25 %
  [1]" for relevance (0.39), "Die Garantie gilt für 5 Jahre [1][4]." (0.54)
  and its long form (0.25).
- **The English check freed 2 of those 3** (0.95 and 0.81 in English), and let
  6 made-up German answers on English documents and 3 on German documents
  through that the check withholds as written — among them "Die Garantie gilt
  für 10 Jahre [1][4]." (0.63, in English 0.76) and "Ein Supportmitarbeiter
  darf höchstens 7 Tage …" (0.63, in English 0.70 and 0.71, at the threshold). With a threshold of its own of 0.9
  it freed only "25 % [1]" and still let one made-up answer on German
  documents through. A lower threshold on the answer as written freed one
  right answer at 0.5 and let 7 more made-up ones through.
- An English check took 1.35 to 2.13 seconds (median 1.56, 46 checks, four
  at once, from outside AWS) and 0.0013 USD on average: 121 tokens in and 40
  out, one topic and one PII unit, and the second check's grounding and PII
  units. A run costs 0.22 USD.

