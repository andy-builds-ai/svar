# Operating Andur

Deploy, fill, ask, test and tear down.

## Deploy

Needs Python 3.11+ (tested on 3.14), Node.js 22, the AWS CDK CLI
(`npm install -g aws-cdk@2.1142.0`) and AWS credentials for the target
account. Everything goes to `eu-west-1` (Ireland), the comparison stack,
unless `-c region=` names another region of `REGIONS` in
`infra/answer_models.py`; the main stack is the one in `eu-central-1`
([Another region](#another-region)).

```bash
python -m venv .venv && . .venv/Scripts/activate   # Linux/macOS: . .venv/bin/activate
pip install -e ".[dev]" -c constraints.txt

cdk bootstrap aws://<account>/eu-west-1   # once per account and region
cdk deploy                                # builds or updates everything
cdk destroy                               # removes it again
```

`cdk deploy` prints the knowledge base id, the data source id, the bucket
names, the web address (`WebUrl`) and the Parameter Store path of the
workspace keys. The scripts read them from there. It also uploads `web/` into
the site bucket and clears the CloudFront caches, so the page is whatever is
in `web/` at the moment of the deploy.

### Another region

`REGIONS` in `infra/answer_models.py` says what a stack answers with in each
region. In eu-central-1 (Frankfurt) that is Qwen3 235B in the region itself,
and gpt-oss-120b in the region checks the denied topics instead of a topic
guardrail: the Standard topic tier, which reads German, goes only with the EU
guardrail profile, which carries its checks to six EU regions (*Guardrails
and cost* in [decisions.md](decisions.md)). Masking and the grounding check
run there without a profile, so no guardrail of the Frankfurt stack uses one.
A topic the model names on its first call stands unless two more calls at
once both name none; each topic lifted that way is a `topic-model-unconfirmed`
line in the function's log, with the topic and the side it was read on.
Frankfurt has no CloudFront, which answers at edge locations worldwide and
cannot be kept to Germany: the answer function's URL in eu-central-1 is the
one address. It is open (`AuthType NONE`); the workspace key in `X-Andur-Key`
is the gate, as it is behind CloudFront. The function serves the page itself,
from a layer with the files of `web/`, and sets the security headers
CloudFront's policy sets in Ireland (`app/page.py`). `WebUrl` among the
outputs is that URL, `https://<id>.lambda-url.eu-central-1.on.aws/`; there is
no own domain and no WAF in front of it. The region and the account come
from outside: the region from `-c region=`, the account from the AWS profile.

```bash
cdk bootstrap aws://<account>/eu-central-1 --profile <profile>
cdk deploy -c region=eu-central-1 --profile <profile>
```

A new account may allow only 10 concurrent Lambda executions, all of which
Lambda keeps unreserved, so the reservation of `ANSWER_MAX_CONCURRENCY` fails
the deploy there. `-c answer_max_concurrency=none` leaves it out, and the
account's limit caps the function; raising that limit is a Service Quotas
request (`L-B99A9384`). Without CloudFront every file of the page is a call
of the function too, so loading the page takes up to five executions for a
moment; Frankfurt therefore reserves `ANSWER_MAX_CONCURRENCY` plus
`PAGE_LOAD_CONCURRENCY`, 5 + 5 = 10, and an account needs room for that
beside the 10 Lambda keeps unreserved. Without the reservation, the page and
the questions share whatever the account allows. Every script then takes `--region eu-central-1`, with
`AWS_PROFILE=<profile>` for the account.

The first deploy of the distribution takes a few minutes longer than the
others: CloudFront rolls a new configuration out to its edge locations before
the stack reports success.

A deploy that changes a guardrail setting makes a new guardrail version, and
no question fails for it, at any hour. The answer role allows every numbered
version of the answer guardrail, so the new version leaves the role as it
is; the function moves to the new version, and the answers already running
finish on the old one. CloudFormation deletes the old version only in its
cleanup phase, and before that the deploy waits 225 seconds (210 until
06.10.2026; `DEPLOY_DRAIN_SECONDS`, longer than an answer may run) in
`Custom::AndurDeployDrain`: such a deploy takes about four minutes longer, a
deploy without a new version does not wait, unless the wait itself changes
(a stack built from nothing waits once, for no one). A new guardrail
the function calls, such as the topic guardrail of 05.10.2026, adds nothing:
CloudFormation creates it, and widens the role for it, before it updates the
function, and the old code never calls it. After a deploy that touches the
topics, `python -m scripts.topic_probe --deployed` measures the deployed
versions.

Two other changes still open a window, and need two deploys. A right taken
from the answer role or moved to another resource: CloudFormation updates the
role 23 to 24 seconds before the function (CloudTrail, 02.10.2026), and the
old code runs without the right meanwhile. A new answer model and a replaced
knowledge base are such changes; `cdk diff` shows them as a `-` line for
`AnswerFunctionRole` in its IAM statement changes. And an environment
variable the old code needs, removed or renamed: CloudFormation changes the
function's configuration 5 to 7 seconds before its code (CloudTrail,
28.09., 01.10. and 02.10.2026), and the old code reads the new environment
meanwhile. Deploy first the code that works with old and new, then the
change that takes the old away, as on 02./03.10.2026 for the key reading.

The topic guardrail of 05.10.2026 comes in two deploys for the same reason.
In the seconds the old code reads the new environment, it would call an
answer guardrail that no longer reads the answer's topics, without the topic
check it does not know yet: answers unchecked for their topics, though no
error. So the first deploy keeps the answer guardrail reading the answer's
topics as before (`ANSWER_GUARDRAIL_READS_ANSWER_TOPICS = True` in
`infra/stacks/andur_stack.py`, a flag since gone) and brings the topic
guardrail and the code that calls it; the answer is checked twice. The
second sets it `False`: the answer guardrail gets a new version, and old and
new code both check the answer without its citations.

The check of the question by the topic guardrail, after its frame
(06.10.2026), comes in two deploys the same way. The first deploy (06.10.2026)
brought the code that checks the question there, and kept
`ANSWER_GUARDRAIL_READS_QUESTION_TOPICS = True`: the answer guardrail read
the question's topics as before, and no guardrail got a new version. It
waited once in `Custom::AndurDeployDrain`, since the function's timeout grew
from 180 to 195 seconds and the wait with it (225 seconds, longer than an
answer may run). The question was checked twice then, so a harmless question
the answer guardrail refused stayed refused; nothing was freed yet. The
second sets it `False`: the answer guardrail and the topic guardrail get new
versions (the topic guardrail's description changes with the flag), the
deploy waits in `Custom::AndurDeployDrain`, and old and new code both check
the question with the topic guardrail. After the second, measure with
`python -m scripts.topic_probe --deployed`.

The deploy creates no workspace key, and no workspace is named in the stack's
code. A workspace exists only in AWS: its key under
`/andur/api-keys/<workspace>` and its documents under `workspaces/<workspace>/`
in the documents bucket. Keys are added and removed with one command each,
without a deploy (see *Workspaces* below). After a first deploy, give the two
demo workspaces their keys:

```bash
python -m scripts.workspace add quillmoor > /dev/null
python -m scripts.workspace add pellwick > /dev/null
```

`> /dev/null` keeps the key off the screen; `scripts.ask` reads it from
Parameter Store itself. Until a workspace has its key, every question to it
is refused with 401.

Resource names are mostly not generated by CloudFormation. The documents
bucket, the site bucket, the vector bucket, the knowledge base role, the
answer function (`andur-answer`) and its log group have fixed names; the
bucket names and the account-wide names — the IAM role, the CloudFront
response headers policy and the two origin access controls — carry the
region. The index, the knowledge
base and the data source carry a short digest of the settings that force a
replacement, so a changed setting gives the replacement a new name instead
of an "already exists" failure. Either way the stack is one instance per
account and region: a second copy under another stack name collides with
the first. A copy in another region does not, because every name that holds
for the whole account carries the region.

`cdk destroy` deliberately leaves the documents bucket and its bucket policy
behind. Vectors can be rebuilt from documents; documents cannot be rebuilt
from anything. Because the bucket name is fixed, the bucket left behind also
blocks the next `cdk deploy`, which stops with "already exists". Two ways
out:

- **Delete it.** Empty it in the S3 console (*Empty* removes old versions
  too; `aws s3 rm` does not on a versioned bucket), then:

  ```bash
  aws s3 rb s3://andur-documents-<account>-eu-west-1
  ```

- **Keep it and adopt it.** Import the bucket into the new stack, then
  deploy the rest:

  ```bash
  cdk import AndurStack   # enter the bucket name for the bucket and its policy, leave the others empty
  cdk deploy
  ```

`cdk destroy` only removes the index this stack created. An index added to
the vector bucket by hand keeps the bucket from being deleted, and the stack
ends in `DELETE_FAILED`.

`cdk destroy` deletes the function logs and the budget table with the stack.
The workspace keys are not part of the stack and stay; after a rebuild they
work again, each with a fresh budget. To take a workspace away for good, use
`python -m scripts.workspace remove` before the destroy.

The site bucket and the distribution go with the stack too. The bucket is
emptied first — the file copier removes what it uploaded — so the deletion
does not stop at a bucket that is not empty. A new distribution has a new
address, so `WebUrl` changes with every rebuild.

## Filling it

One command checks the local corpus, uploads it and syncs the knowledge
base. It reads bucket and ids from the stack outputs and needs a valid AWS
login (`aws login`):

```bash
python -m scripts.ingest
```

The corpus in `knowledge/` belongs to two **fictional** companies invented
for this demo: *Quillmoor Systems*, a software company with 33 documents
across all three layers, and *Pellwick Joinery*, a joinery workshop with 4.
Neither company, nor anything in their documents, is real. Details in
[knowledge/README.md](../knowledge/README.md).

Every document carries a sidecar metadata file
([metadata.md](metadata.md)); the script refuses to upload anything
while one is missing or wrong. Markdown documents are uploaded split into
sections, for the reason in [retrieval findings](retrieval-findings.md). A
rerun uploads only what changed. Options, output and exit codes:
[scripts/README.md](../scripts/README.md).

The proof that workspaces stay apart:

```bash
python -m scripts.retrieve "How do I export project data?" --workspace quillmoor
python -m scripts.retrieve "How do I export project data?" --workspace pellwick
```

The first returns Quillmoor sections only — every hit prints
`workspace=quillmoor`. The second returns Pellwick sections or nothing, never
a Quillmoor document, although Quillmoor's documents answer the question
and Pellwick's do not. Add `--layer agent` to see only machine-written
documents.

## Workspaces

```bash
python -m scripts.workspace add <name>             # create the key, print it once
python -m scripts.workspace add <name> --plan firma   # the same, on a plan
python -m scripts.workspace plan <name> betrieb    # change the plan; the key stays
python -m scripts.workspace remove <name>          # show what belongs to it, delete nothing
python -m scripts.workspace remove <name> --yes    # delete key, budget items and documents
```

`add` refuses to overwrite an existing key. `remove` without `--yes` only
lists the key, the budget items and every document under
`workspaces/<name>/`; with `--yes` it deletes exactly those and then runs an
ingestion job, which removes their vectors. Nothing outside that one name is
touched: the documents prefix ends in a slash, so `kalv` never reaches
`kalvenstett`. The documents of a new workspace go in with
`python -m scripts.ingest --source <folder> --workspace <name>`; text files
stored in Windows-1252 or UTF-16 go up converted to UTF-8, and `--check`
names each one ([Encodings](../scripts/README.md#encodings)). Details,
output and exit codes: [scripts/README.md](../scripts/README.md#workspace--adding-and-removing-a-workspace).

## Asking Svar

```bash
python -m scripts.ask "How do I export project data?" --workspace quillmoor
python -m scripts.ask "How do I export project data?" --workspace pellwick
```

The first prints Quillmoor's answer, the layers it rests on, every source
with its layer and whether the answer cites it, then tokens and latency. The
second answers *The sources do not answer this question.* from Pellwick's
documents; with `--language de` the sentence is *Die Quellen beantworten diese
Frage nicht.* The script fetches the workspace key from Parameter Store itself;
the key never appears on screen. Request format, answer fields, status codes
and how to get a key for curl or the page: [app/README.md](../app/README.md).

### In the browser

Open the stack output `WebUrl`. The page asks once for a workspace and its
key, keeps both in the browser, and shows the answer with its sources, the
layers it rests on and what it cost. **Forget key** removes the key again.
The key is never part of the address. The page is in German or English: the
browser's language decides at first, the DE | EN switch in the corner after
that. What the page does with each status,
and why it has no configuration file: [web/README.md](../web/README.md).

### With curl

```bash
WEB_URL=$(aws cloudformation describe-stacks --region eu-west-1 --stack-name AndurStack \
  --query "Stacks[0].Outputs[?OutputKey=='WebUrl'].OutputValue" --output text)
BODY='{"question": "How do I export project data?", "workspace": "quillmoor"}'

curl -s -X POST "$WEB_URL/ask" \
  -H "X-Andur-Key: $KEY" \
  -H "Content-Type: application/json" \
  -H "x-amz-content-sha256: $(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)" \
  -d "$BODY"
```

`$KEY` is the workspace key ([app/README.md](../app/README.md#getting-a-key)).
`printf '%s'` and not `echo`: a trailing newline would go into the hash but
not into the body, and the request would be refused. The stack output
`FunctionUrl` is not an address to send this to — from outside the account
it answers 403 to everyone but CloudFront.

## Tests

```bash
pytest -q
```

The stack tests render the stack to CloudFormation JSON and assert on it,
including the way in: the function URL on `AWS_IAM`, both invoke permissions
bound to this distribution alone, the private site bucket, and the `/ask`
behaviour with its methods, its disabled cache and its response timeout. Some
of them read `web/` itself, to keep the page from growing an address of its
own, a key, a `console.log` or a file from another server. The handler tests
drive the answer function through every status code against fake Bedrock,
Parameter Store and DynamoDB clients; the budget fake runs the handler's own
update and condition strings, so a wrong attribute name fails there rather
than in AWS. The script tests check the corpus layout, the sidecars, the
object keys and the counters, and drive upload, job wait, retrieval and
asking against small fake clients. None of them call AWS or need credentials,
and they finish in under a minute — a broken stack or a broken sidecar shows up
before a deploy or a sync spends minutes finding out.
