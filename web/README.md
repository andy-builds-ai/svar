# web — the page

Five files and no build step: `index.html`, `app.js`, `language.js`,
`markdown.js`, `style.css`. No framework, no bundler, no npm package, nothing loaded from
another server — not a font, not a script, not a pixel. What is in this
folder is what the browser gets. `cdk deploy` copies it into the site bucket and clears the
CloudFront caches, so a change to a file here is live with the next deploy.
This README stays behind: the deploy leaves it out, like every other
`README.md` in a packaged folder.

## One address for everything

The page and the answer function sit behind the same CloudFront distribution.
The page is served from a private S3 bucket; the single path `/ask` goes to
the answer function's URL instead. Both are reached through an origin access
control, so neither is reachable from outside on its own.

Every response the distribution sends carries the security headers set on it:
a content security policy that allows nothing but this page's own script,
stylesheet and `/ask` call, `frame-ancestors 'none'` so the page cannot be
put in somebody else's frame, HSTS, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: no-referrer`. They are set once on the distribution, so no
file here has to carry a `<meta>` of its own.

Because it is one address, the page posts to `/ask` — a relative path. It
never has to know its own address, which is why no file here carries one and
why there is no configuration file to keep in step. It also means there is no
cross-origin request and so nothing to allow with CORS.

Two consequences for the request, both from the way in rather than from the
function:

- **The key travels in `X-Andur-Key`, not in `Authorization`.** CloudFront
  signs every request it forwards to a function URL and puts its own
  signature into `Authorization`, so a key sent there would never arrive.
- **The request carries the SHA-256 of its body in `x-amz-content-sha256`.**
  A signed request to a Lambda function URL may not have an unsigned payload.
  `crypto.subtle.digest` computes it; it is built into the browser and needs
  an https connection, which the page always has.

## Two languages

Andur speaks the visitor's language: German and English. The page starts in
German when the browser's language (`navigator.language`) begins with `de`
and in English otherwise. A **DE | EN** switch in the corner changes it at
once, and the choice is kept in the browser under `andur.language`, like the
workspace, so it wins over the browser's language from then on. Anything else
in storage counts as no choice.

Everything the page says itself changes with it: `<html lang>`, the title,
labels, placeholders, the button (*Ask* / *Asking…*, *Fragen* / *Frage
läuft…*), every message, the sentence for each status without an answer from
the function (`problemSentence`), the layers by name (human, agent, inbox
become Mensch, Maschine, Eingang), the note of each source and the cost line,
which writes its decimal with a comma in German. A message the page wrote
itself, and the labels of an answer already on screen, are drawn again when
the switch is used.

What stays as it came: the answer and the `error` sentence of the function
are in the language the question was asked in. The page sends its language
in the request body (`"language": "de"` or `"en"`) with every question, and
the function says its own sentences in it ([app/README.md](../app/README.md),
*Language*). So after a switch the answer on screen keeps its language and
the next one comes in the new one. The model's answer follows the language of
the question, not the page.

Every sentence is in `language.js`, once in each language, under the same
key; `index.html` is empty where text goes (`data-i18n`, `data-i18n-placeholder`,
`data-i18n-label`) and `app.js` holds no sentence. The file has no DOM code, so
the tests run it in Node: both tables have the same keys and placeholders,
every key asked for exists and no key is left unused. A new text is a key in
both tables. German addresses the reader as *Sie*.

## What it does

The workspace and the key are typed once and kept in `localStorage`. The key
is never part of the address, never written to the console, and never sent
anywhere but the `X-Andur-Key` header of a request to `/ask`. **Forget key**
removes it from this browser and leaves the workspace. A browser with storage
switched off works the same; it just asks again next time.

A question is 1 to 1,000 characters. Enter asks, Shift + Enter starts a new
line, and the button is disabled while an answer is on its way. Enter reaches
the form directly, past the disabled button, so the page also keeps a flag:
a second question while the first is in flight is ignored. Two would be two
questions off the same ten, and an answer that may not belong to the question
in the field.

The page stops waiting after 125 seconds. CloudFront gives up at 120 and
sends a 504, so in every ordinary case that answer arrives first; this is the
floor under it, for a connection that neither answers nor fails. Without it
the button stayed on *Asking…* until the browser was reloaded.

An answer is shown as the model wrote it, with its `[n]` citations, then the
layers it rests on, then every retrieved passage with its layer, length,
whether the answer cites it, and the opening of its text. The last line is
what the answer cost: model, tokens in and out, the cost in USD with the day
its prices were read, and the milliseconds the function spent. When no
passage matched, the function's own sentence appears and the source list
says so.

Nothing from a response is ever inserted as markup. Every value goes into a
text node, so a document containing angle brackets stays text and a document
cannot bring markup of its own into the page.

The model writes a little Markdown now and then, and raw asterisks read
badly, so `markdown.js` understands three things: `**bold**` on one line,
bulleted lists (`-`, `*`, `•`) and numbered lists (`1.`, `1)`). Everything
else — headings, links, tables, a single asterisk — stays as written. The
file turns the answer into plain data first and then builds each `<p>`,
`<ul>`, `<ol>`, `<li>` and `<strong>` itself, with the words put in as text;
there is no `innerHTML` in it, and a test keeps it that way. An answer
containing `<script>` shows those eight characters.

## What an error says

| What arrives | What the page says |
|---|---|
| Any status with a JSON `error` | That sentence, unchanged. It comes from the function, in the language the question was asked in, and says it better than the page could. |
| 402 | The sentence, plus a line (in the page's language) that a new key is what helps. |
| 429 with a sentence | A key on a plan has reached its quota for the month or the day. The sentence alone: the quota comes back, and a new key is not what helps. |
| 429 without a sentence | Andur is busy — five questions run at a time, the rest are turned away. |
| 403 without a sentence | The request was refused on the way in. Not a wrong key — that is a 401 with a sentence. |
| 504 | The answer took longer than CloudFront waits (120 seconds). The function may still be running it, and the question is already counted. |
| No response at all | The connection failed. |
| Nothing within 125 seconds | The page stopped waiting. The question may still be running, and asking again costs another one. |

The sentences in this table are the English ones; `language.js` has them in
German too. The three statuses without a sentence never passed through the
function, so there is no sentence to pass on. Their bodies are not the function's either:
a 403 comes back as Lambda's `{"message": "The request signature we
calculated does not match ..."}` when a request reaches the function URL
unsigned or wrongly signed (measured 17.09.2026), and CloudFront's own error
pages are HTML. Neither carries an `error` field, which is how the page tells
them apart from a refusal it should quote. Status codes and their meanings
are in [app/README.md](../app/README.md).

## Editing it

Open `index.html` from disk and the layout is there, but asking will not
work: `crypto.subtle` needs a secure connection, and `/ask` needs the
distribution in front of it. The page says so instead of failing silently.
To try a change end to end, deploy it.
