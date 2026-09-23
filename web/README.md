# web — the page

Four files and no build step: `index.html`, `app.js`, `markdown.js`,
`style.css`. No framework, no bundler, no npm package, nothing loaded from
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
| Any status with a JSON `error` | That sentence, unchanged. It comes from the function and says it better than the page could. |
| 402 | The sentence, plus a line that a new key is what helps. |
| 429 without a sentence | Andur is busy — five questions run at a time, the rest are turned away. |
| 403 without a sentence | The request was refused on the way in. Not a wrong key — that is a 401 with a sentence. |
| 504 | The answer took longer than CloudFront waits (120 seconds). The function may still be running it, and the question is already counted. |
| No response at all | The connection failed. |
| Nothing within 125 seconds | The page stopped waiting. The question may still be running, and asking again costs another one. |

The three statuses without a sentence never passed through the function, so
there is no sentence to pass on. Their bodies are not the function's either:
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
