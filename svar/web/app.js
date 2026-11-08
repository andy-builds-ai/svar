/* Svar — the page.
 *
 * One address for everything: this page and the answer function share one
 * origin, so a question is a POST to /ask on this very origin. Behind
 * CloudFront (eu-west-1) the page comes from a bucket and /ask goes on to the
 * function; where the function is its own address (eu-central-1), it serves
 * this page itself. No other server is contacted, and nothing is loaded from
 * one.
 *
 * Three things the request needs:
 *   Content-Type: application/json
 *   X-Andur-Key:  the workspace key — not Authorization, because CloudFront
 *                 signs every request it forwards to the function and replaces
 *                 that header with its own signature.
 *   x-amz-content-sha256: the SHA-256 of the body, because a signed request to
 *                 a Lambda function URL may not carry an unsigned payload.
 *                 Where no CloudFront signs, nothing reads it, and it goes
 *                 along all the same: one page for both ways in.
 *                 crypto.subtle computes it; it is built into the browser.
 *
 * Nothing the function returns is ever inserted as markup. Every value goes
 * into a text node, so a document that contains angle brackets stays text.
 * The answer's bold and lists are elements this page builds itself
 * (markdown.js); the words inside them are text nodes all the same.
 *
 * Every word the page says itself comes from language.js, in the visitor's
 * language: this file holds no sentence of its own. It sends the language with
 * every question, so the function answers in it too.
 */

"use strict";

const ASK_PATH = "/ask";
const KEY_HEADER = "X-Andur-Key";
const BODY_HASH_HEADER = "x-amz-content-sha256";
const MAX_QUESTION_CHARACTERS = 1000;

/* How long this page waits before it gives up on its own: longer than the
 * function may run (195 seconds), so every answer it gives arrives. Where the
 * function is its own address, nothing between ends a request sooner, and the
 * function times a second model call by its own timeout (app/aws.py,
 * PAGE_TIMEOUT_SECONDS holds this number). Behind CloudFront, CloudFront stops
 * waiting after 120 seconds and sends a 504 first. Either way, this limit is
 * for a connection that neither answers nor fails, where without it the
 * button would show its asking label forever. */
const REQUEST_TIMEOUT_MS = 200000;

/* What the page remembers between visits. The key lives here and nowhere
 * else: not in the address, not in a log line, not in this file. */
const STORED_KEY = "andur.key";
const STORED_WORKSPACE = "andur.workspace";
const STORED_LANGUAGE = "andur.language";

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

// --- The language ----------------------------------------------------------
// The language is the visitor's: the one kept in this browser, else the
// browser's own. The switch changes it at once, and it is kept like the
// workspace.

let language = startLanguage(recall(STORED_LANGUAGE), window.navigator.language);

function t(key, values) {
  return translate(language, key, values);
}

/* What is on screen that was built from a language: the message, the answer's
 * labels. Both are drawn again when the language changes. */
let currentMessage = null;
let currentAnswer = null;

function applyLanguage() {
  document.documentElement.lang = language;
  document.title = t("title");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
  for (const node of document.querySelectorAll("[data-i18n-label]")) {
    node.setAttribute("aria-label", t(node.dataset.i18nLabel));
  }
  for (const button of document.querySelectorAll("[data-language]")) {
    button.setAttribute("aria-pressed", String(button.dataset.language === language));
  }
  working(asking);
  if (currentMessage) {
    say(currentMessage.text, currentMessage.kind);
  }
  if (currentAnswer) {
    show(currentAnswer);
  }
}

function chooseLanguage(chosen) {
  if (chosen === language) {
    return;
  }
  language = chosen;
  remember(STORED_LANGUAGE, language);
  applyLanguage();
}

// --- Saying something ------------------------------------------------------

/* `text` is a sentence, or a function that makes it, so that a message the page
 * wrote itself is written again in the new language when the switch is used. */
function say(text, kind) {
  currentMessage = { text: text, kind: kind };
  message.textContent = typeof text === "function" ? text() : text;
  message.className = kind ? "message message--" + kind : "message";
  message.hidden = false;
}

function clearMessage() {
  currentMessage = null;
  message.hidden = true;
  message.textContent = "";
}

function working(busy) {
  askButton.disabled = busy;
  askButton.textContent = t(busy ? "asking_button" : "ask_button");
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
  const body = JSON.stringify({ question: question, workspace: workspace, language: language });
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
    payload = null; // CloudFront's and Lambda's own error pages are not this JSON.
  }
  return { status: response.status, payload: payload };
}

// --- Showing the answer ----------------------------------------------------

function sourceEntry(source) {
  const item = document.createElement("li");
  item.className = source.cited ? "source source--cited" : "source";
  item.value = source.number;

  const layer = document.createElement("span");
  layer.className = "layer layer--" + String(source.layer).replace(/[^a-z]/g, "");
  layer.textContent = layerName(language, source.layer);
  item.appendChild(layer);

  const file = document.createElement("span");
  file.className = "file";
  file.textContent = source.file;
  item.appendChild(file);

  const about = document.createElement("span");
  about.className = "note";
  about.textContent = sourceNote(language, source);
  item.appendChild(about);

  const excerpt = document.createElement("p");
  excerpt.className = "excerpt";
  excerpt.textContent = source.excerpt;
  item.appendChild(excerpt);

  return item;
}

function show(answer) {
  currentAnswer = answer;
  renderAnswer(answerText, answer.answer);

  const layers = Array.isArray(answer.layers) ? answer.layers : [];
  layersText.textContent = layersLine(language, layers);

  sourceList.replaceChildren();
  const sources = Array.isArray(answer.sources) ? answer.sources : [];
  for (const source of sources) {
    sourceList.appendChild(sourceEntry(source));
  }
  noSources.hidden = sources.length > 0;

  costText.textContent = costLine(language, answer);

  result.hidden = false;
}

// --- One question, start to finish -----------------------------------------

/* Whether a question is on its way. The disabled button is not enough: Enter
 * in the question field submits the form directly, and a form can be submitted
 * while its button is disabled. A second question would be charged against the
 * same key's allowance and could come back out of order, so the answer
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
    say(() => t("need_workspace_and_key"), "warn");
    return;
  }
  if (!question) {
    say(() => t("need_question"), "warn");
    return;
  }
  if (question.length > MAX_QUESTION_CHARACTERS) {
    say(() => t("question_too_long", { max: MAX_QUESTION_CHARACTERS }), "warn");
    return;
  }
  if (!window.crypto || !window.crypto.subtle) {
    say(() => t("need_https"), "warn");
    return;
  }

  remember(STORED_WORKSPACE, workspace);
  remember(STORED_KEY, key);

  clearMessage();
  result.hidden = true;
  currentAnswer = null;
  asking = true;
  working(true);
  try {
    const { status, payload } = await ask(workspace, key, question);
    if (status === 200 && payload && typeof payload.answer === "string") {
      show(payload);
    } else {
      say(() => problemSentence(language, status, payload), "error");
    }
  } catch (problem) {
    // A dropped connection, an offline browser, a request the browser refused
    // to make, or the wait above running out. The exception text is the
    // browser's, never the key's.
    if (problem && problem.name === "AbortError") {
      say(() => t("problem_timed_out", { seconds: Math.round(REQUEST_TIMEOUT_MS / 1000) }),
        "error");
    } else {
      say(() => t("problem_connection"), "error");
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
  say(() => t("key_forgotten"), "warn");
});

for (const button of document.querySelectorAll("[data-language]")) {
  button.addEventListener("click", () => chooseLanguage(button.dataset.language));
}

element("ask-form").addEventListener("submit", submit);

applyLanguage();
