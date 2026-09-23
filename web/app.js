/* Ask Andur — the page.
 *
 * One address for everything: this page and the answer function sit behind the
 * same CloudFront distribution, so a question is a POST to /ask on this very
 * origin. No other server is contacted, and nothing is loaded from one.
 *
 * Three things the request needs:
 *   Content-Type: application/json
 *   X-Andur-Key:  the workspace key — not Authorization, because CloudFront
 *                 signs every request it forwards to the function and replaces
 *                 that header with its own signature.
 *   x-amz-content-sha256: the SHA-256 of the body, because a signed request to
 *                 a Lambda function URL may not carry an unsigned payload.
 *                 crypto.subtle computes it; it is built into the browser.
 *
 * Nothing the function returns is ever inserted as markup. Every value goes
 * into a text node, so a document that contains angle brackets stays text.
 * The answer's bold and lists are elements this page builds itself
 * (markdown.js); the words inside them are text nodes all the same.
 */

"use strict";

const ASK_PATH = "/ask";
const KEY_HEADER = "X-Andur-Key";
const BODY_HASH_HEADER = "x-amz-content-sha256";
const MAX_QUESTION_CHARACTERS = 1000;

/* How long this page waits before it gives up on its own. CloudFront stops
 * waiting for the function after 120 seconds and sends a 504, so in every
 * ordinary case that answer arrives first and says more than this page could.
 * This is for the case underneath it: a connection that neither answers nor
 * fails, where without a limit the button would say "Asking…" forever. */
const REQUEST_TIMEOUT_MS = 125000;

/* What the page remembers between visits. The key lives here and nowhere
 * else: not in the address, not in a log line, not in this file. */
const STORED_KEY = "andur.key";
const STORED_WORKSPACE = "andur.workspace";

const element = (id) => document.getElementById(id);

const workspaceField = element("workspace");
const keyField = element("key");
const questionField = element("question");
const askButton = element("ask");
const counter = element("counter");
const message = element("message");
const result = element("result");
const answerText = element("answer");
const layersText = element("layers");
const sourceList = element("sources");
const noSources = element("no-sources");
const costText = element("cost");

// --- Remembering -----------------------------------------------------------
// Storage can be switched off or full. A browser that refuses it still works;
// it just asks for the key again next time.

function remember(name, value) {
  try {
    window.localStorage.setItem(name, value);
  } catch (problem) {
    /* nothing to do: the page works without remembering */
  }
}

function recall(name) {
  try {
    return window.localStorage.getItem(name) || "";
  } catch (problem) {
    return "";
  }
}

function forget(name) {
  try {
    window.localStorage.removeItem(name);
  } catch (problem) {
    /* see remember() */
  }
}

// --- Saying something ------------------------------------------------------

function say(text, kind) {
  message.textContent = text;
  message.className = kind ? "message message--" + kind : "message";
  message.hidden = false;
}

function clearMessage() {
  message.hidden = true;
  message.textContent = "";
}

function working(busy) {
  askButton.disabled = busy;
  askButton.textContent = busy ? "Asking…" : "Ask";
}

// --- The request -----------------------------------------------------------

async function bodyHash(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function ask(workspace, key, question) {
  const body = JSON.stringify({ question: question, workspace: workspace });
  const headers = {
    "Content-Type": "application/json",
    [KEY_HEADER]: key,
    // The hash is of the exact bytes fetch will send: JSON.stringify gives the
    // text, TextEncoder gives its UTF-8 bytes, and fetch encodes it the same.
    [BODY_HASH_HEADER]: await bodyHash(body),
  };

  // A request that hangs is ended by this page rather than left running: the
  // controller aborts it, fetch rejects with an AbortError, and submit() turns
  // that into a sentence. The timer is cleared whatever happens, so a fast
  // answer leaves nothing behind.
  const controller = new AbortController();
  const giveUp = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  let raw;
  try {
    response = await fetch(ASK_PATH, {
      method: "POST",
      headers: headers,
      body: body,
      signal: controller.signal,
    });
    raw = await response.text();
  } finally {
    window.clearTimeout(giveUp);
  }
  let payload = null;
  try {
    payload = JSON.parse(raw);
  } catch (problem) {
    payload = null; // CloudFront's own error pages are HTML, not JSON.
  }
  return { status: response.status, payload: payload };
}

// --- What an unhappy answer means ------------------------------------------

function problemSentence(status, payload) {
  // The function's own refusals carry one sentence and say it better than
  // this page could.
  if (payload && typeof payload.error === "string" && payload.error) {
    if (status === 402) {
      return payload.error + " Ask whoever sent you this link for a new one.";
    }
    return payload.error;
  }
  // Everything below never passed through the function, so there is no
  // sentence to pass on.
  if (status === 429) {
    return "Andur is busy: it answers a few questions at a time, and the others "
      + "are turned away. Try again in a moment.";
  }
  if (status === 403) {
    return "The request was refused before it reached Andur (403). That is a problem "
      + "with the way in, not with the key — a wrong key is answered with 401.";
  }
  if (status === 504) {
    return "The answer took longer than this page waits (504). The question may still "
      + "be running; asking again costs another question of the key's allowance.";
  }
  return "The service answered with status " + status + " and nothing this page can read.";
}

// --- Showing the answer ----------------------------------------------------

function sourceEntry(source) {
  const item = document.createElement("li");
  item.className = source.cited ? "source source--cited" : "source";
  item.value = source.number;

  const layer = document.createElement("span");
  layer.className = "layer layer--" + String(source.layer).replace(/[^a-z]/g, "");
  layer.textContent = source.layer;
  item.appendChild(layer);

  const file = document.createElement("span");
  file.className = "file";
  file.textContent = source.file;
  item.appendChild(file);

  const about = document.createElement("span");
  about.className = "note";
  about.textContent = source.words + " words · " + (source.cited ? "cited" : "not cited");
  item.appendChild(about);

  const excerpt = document.createElement("p");
  excerpt.className = "excerpt";
  excerpt.textContent = source.excerpt;
  item.appendChild(excerpt);

  return item;
}

function show(answer) {
  renderAnswer(answerText, answer.answer);

  const layers = Array.isArray(answer.layers) ? answer.layers : [];
  layersText.textContent = layers.length
    ? "Rests on: " + layers.join(", ")
    : "Rests on: nothing the answer cites.";

  sourceList.replaceChildren();
  const sources = Array.isArray(answer.sources) ? answer.sources : [];
  for (const source of sources) {
    sourceList.appendChild(sourceEntry(source));
  }
  noSources.hidden = sources.length > 0;

  const usage = answer.usage || { input_tokens: 0, output_tokens: 0 };
  costText.textContent =
    (answer.model || "no model call")
    + " · " + usage.input_tokens + " tokens in, " + usage.output_tokens + " out"
    + (typeof answer.cost_usd === "number"
      ? " · " + answer.cost_usd.toFixed(6) + " USD at the prices of " + answer.price_date
      : "")
    + " · " + answer.latency_ms + " ms in the function";

  result.hidden = false;
}

// --- One question, start to finish -----------------------------------------

/* Whether a question is on its way. The disabled button is not enough: Enter
 * in the question field submits the form directly, and a form can be submitted
 * while its button is disabled. A second question would be charged against the
 * same ten-question allowance and could come back out of order, so the answer
 * on screen would not belong to the question in the field. */
let asking = false;

async function submit(event) {
  event.preventDefault();
  if (asking) {
    return;
  }

  const workspace = workspaceField.value.trim();
  const key = keyField.value.trim();
  const question = questionField.value.trim();

  if (!workspace || !key) {
    say("Fill in the workspace and its key first.", "warn");
    return;
  }
  if (!question) {
    say("Type a question.", "warn");
    return;
  }
  if (question.length > MAX_QUESTION_CHARACTERS) {
    say("A question is at most " + MAX_QUESTION_CHARACTERS + " characters.", "warn");
    return;
  }
  if (!window.crypto || !window.crypto.subtle) {
    say("This page needs an https connection: without one the browser withholds the "
      + "hashing a signed request needs.", "warn");
    return;
  }

  remember(STORED_WORKSPACE, workspace);
  remember(STORED_KEY, key);

  clearMessage();
  result.hidden = true;
  asking = true;
  working(true);
  try {
    const { status, payload } = await ask(workspace, key, question);
    if (status === 200 && payload && typeof payload.answer === "string") {
      show(payload);
    } else {
      say(problemSentence(status, payload), "error");
    }
  } catch (problem) {
    // A dropped connection, an offline browser, a request the browser refused
    // to make, or the wait above running out. The exception text is the
    // browser's, never the key's.
    if (problem && problem.name === "AbortError") {
      say("Nothing came back within " + Math.round(REQUEST_TIMEOUT_MS / 1000) + " seconds, so "
        + "the page stopped waiting. The question may still be running; asking again costs "
        + "another question of the key's allowance.", "error");
    } else {
      say("The question did not get through: the connection failed. Check the network "
        + "and ask again.", "error");
    }
  } finally {
    asking = false;
    working(false);
  }
}

// --- Wiring ----------------------------------------------------------------

function countCharacters() {
  counter.textContent = questionField.value.length + " / " + MAX_QUESTION_CHARACTERS;
}

workspaceField.value = recall(STORED_WORKSPACE);
keyField.value = recall(STORED_KEY);
countCharacters();

questionField.addEventListener("input", countCharacters);

questionField.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    element("ask-form").requestSubmit();
  }
});

workspaceField.addEventListener("change", () => remember(STORED_WORKSPACE, workspaceField.value.trim()));
keyField.addEventListener("change", () => remember(STORED_KEY, keyField.value.trim()));

element("forget").addEventListener("click", () => {
  forget(STORED_KEY);
  keyField.value = "";
  keyField.focus();
  say("The key is gone from this browser. The workspace is kept.", "warn");
});

element("ask-form").addEventListener("submit", submit);
