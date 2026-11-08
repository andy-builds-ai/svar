# Taug's scripts — the documents and the workspaces

Three commands, all run from the repository root:

| Command | Does |
|---|---|
| `python -m taug.scripts.ingest` | Checks a local folder, uploads it to the documents bucket, syncs the knowledge base. |
| `python -m taug.scripts.retrieve` | Asks the knowledge base a question inside one workspace and prints the hits. |
| `python -m taug.scripts.workspace` | Adds a workspace's key, puts it on a plan, or removes a workspace: key, budget items, documents and vectors. |

## What they need

- `pip install -e ".[dev]" -c constraints.txt` — brings `boto3` with its `crt` extra. The extra
  is not optional here: without it botocore cannot read the credentials that
  `aws login` writes, and every AWS call fails with "Missing Dependency".
- A valid AWS login for the account the stack is deployed in. When it has
  run out, every script stops with a message that says so; sign in with
  `aws login` and rerun.
- The deployed stack. Bucket, knowledge base id, data source id, web address,
  key path and budget table are read from the outputs of `AndurStack` in `eu-west-1`,
  `DEFAULT_REGION` of `taug/infra/answer_models.py`, whatever region a shell profile names;
  `--region eu-central-1` reads the stack in Frankfurt. Each value can be passed by hand
  instead:
  `--bucket`, `--knowledge-base-id`, `--data-source-id`, `--url`, and
  `--stack-name` / `--region` for another stack. `--url` takes the web
  address, not the function URL — `ask` appends `/ask` to it itself.

## ingest

```bash
python -m taug.scripts.ingest                        # everything under knowledge/
python -m taug.scripts.ingest --workspace pellwick   # one workspace
python -m taug.scripts.ingest --check                # check only, no AWS at all
python -m taug.scripts.ingest --source path/to/folder
```

It runs four steps and stops at the first that fails.

1. **Check.** The folder must be laid out as
   `<workspace>/<layer>/<document>` with a sidecar
   `<document>.metadata.json` next to every document. Workspace names are
   lowercase letters, digits and single hyphens. Layers are `human`,
   `agent`, `inbox`. Each sidecar must carry exactly the four keys from
   [taug/docs/metadata.md](../docs/metadata.md), as STRING values with
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
permission or as a broken link, is a problem too, whatever its format. So is
a text file with nothing in it but space: no answer can come from it, and an
emptied Markdown file would otherwise leave its earlier sections to `--delete`.

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
company in it never lists or deletes another company's documents. A
workspace whose documents were all removed locally is not compared, even if
its folder is still there; remove it from the bucket by hand. Within a
compared workspace, every layer counts: a folder holding only `human/` for a
workspace takes that workspace's `agent/` and `inbox/` objects for stale. A
deleted object stays recoverable for 30 days as a noncurrent version.

It does not **sync one workspace only.** `--workspace` limits the upload. The
ingestion job always covers the whole data source.

## retrieve — the proof that workspaces stay apart

```bash
python -m taug.scripts.retrieve "How do I export project data?" --workspace quillmoor
python -m taug.scripts.retrieve "How do I export project data?" --workspace pellwick
python -m taug.scripts.retrieve "What changed in the recent releases?" --workspace quillmoor --layer agent
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

## workspace — adding and removing a workspace

```bash
python -m taug.scripts.workspace add kalvenstett                # create the key, print it once
python -m taug.scripts.workspace add kalvenstett --plan firma   # the same, on the plan firma
python -m taug.scripts.workspace plan kalvenstett betrieb       # put the key on another plan
python -m taug.scripts.workspace remove kalvenstett             # show what belongs to it
python -m taug.scripts.workspace remove kalvenstett --yes       # delete it
```

No workspace is named in the stack's code, and a new one needs no deploy. A
workspace is what AWS holds under its name: the key `/andur/api-keys/<name>`, the budget
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
[svar/app/README.md](../../svar/app/README.md#setting-a-budget-for-one-key). Its documents
go in with `python -m taug.scripts.ingest --source <folder> --workspace <name>`.

**add --plan** `betrieb` or `firma` puts the new key on that plan in the
same run: it writes the key's budget item with the plan and the workspace,
and nothing else (*Plans* in
[svar/app/README.md](../../svar/app/README.md#plans)). The plans and their numbers are the
ones in `svar/app/plans.py`, the table the answer function counts by; `demo`, the
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
   ([svar/app/README.md](../../svar/app/README.md#getting-a-key)).
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
again after that to delete it. The command it prints for that carries
`--region` and `--stack-name` when the run had them, so it reaches the same
stack.

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
