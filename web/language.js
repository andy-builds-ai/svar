/* Ask Andur — the two languages of the page.
 *
 * Every word the page says itself is in TEXTS, once in English and once in
 * German. Andur speaks the visitor's language, not the company's: the page
 * starts in German when the browser's language begins with "de" and in
 * English otherwise, a switch in the corner changes it, and the choice is
 * kept in the browser like the workspace. app.js sends the language with every
 * question, so the sentences the function says itself come back in it.
 *
 * What the function returns is shown as it comes: the answer, and the sentence
 * of an error, are in the language the question asked in. The labels around
 * them follow the switch at once.
 *
 * Nothing here touches the page. The functions are pure: a language and some
 * values in, a text out. That is what lets the tests run this file in Node, and
 * it is why app.js holds no sentence of its own.
 *
 * Loaded before app.js as a classic script, so its names are globals the page
 * uses. Under Node (the tests) it exports them.
 */

"use strict";

const LANGUAGES = ["en", "de"];
const DEFAULT_LANGUAGE = "en";

/* A text with {name} in it takes the same names in both languages. The key
 * names in index.html (data-i18n) and in app.js (translate) are checked
 * against these two tables by the tests. */
const TEXTS = {
  en: {
    title: "Ask Svar",
    heading: "Ask Svar",
    lead_before: "A demo with ",
    lead_strong: "fictional",
    lead_after:
      " companies. Each keeps its handbooks, notes and drafts in a vault of its own, and "
      + "Svar answers from one vault at a time. Every answer comes with the passages it was "
      + "built from, and every passage says which layer it came from: written by a person, "
      + "produced by a machine, or merely arrived and not yet sorted.",
    language_label: "Language",
    access_heading: "Workspace and key",
    access_note:
      "A key opens one workspace and carries an allowance of questions; a demo key also runs "
      + "out a set time after its first question. It stays in this browser and is never part "
      + "of the address.",
    workspace_label: "Workspace",
    workspace_placeholder: "quillmoor",
    key_label: "Key",
    key_placeholder: "paste the workspace key",
    forget_button: "Forget key",
    question_heading: "Question",
    question_placeholder: "How do I export project data?",
    ask_button: "Ask",
    asking_button: "Asking…",
    enter_hint: "Enter asks. Shift + Enter starts a new line.",
    answer_heading: "Answer",
    sources_heading: "Sources",
    no_sources: "None — no passage in this workspace matched.",
    layer_human: "human",
    layer_agent: "agent",
    layer_inbox: "inbox",
    layers_rest: "Rests on: {layers}",
    layers_none: "Rests on: nothing the answer cites.",
    source_cited: "{words} words · cited",
    source_not_cited: "{words} words · not cited",
    cost_no_model: "no model call",
    cost_tokens: "{input} tokens in, {output} out",
    cost_price: "{cost} USD at the prices of {date}",
    cost_latency: "{ms} ms in the function",
    need_workspace_and_key: "Fill in the workspace and its key first.",
    need_question: "Type a question.",
    question_too_long: "A question is at most {max} characters.",
    need_https:
      "This page needs an https connection: without one the browser withholds the hashing "
      + "a signed request needs.",
    key_forgotten: "The key is gone from this browser. The workspace is kept.",
    ask_for_a_new_key: " Ask whoever sent you this link for a new one.",
    problem_busy:
      "Svar is busy: it answers a few questions at a time, and the others are turned away. "
      + "Try again in a moment.",
    problem_refused_early:
      "The request was refused before it reached Svar (403). That is a problem with the way "
      + "in, not with the key — a wrong key is answered with 401.",
    problem_too_slow:
      "The service stopped waiting for the answer (504). The question may still be running; "
      + "asking again costs another question of the key's allowance.",
    problem_unreadable:
      "The service answered with status {status} and nothing this page can read.",
    problem_timed_out:
      "Nothing came back within {seconds} seconds, so the page stopped waiting. The question "
      + "may still be running; asking again costs another question of the key's allowance.",
    problem_connection:
      "The question did not get through: the connection failed. Check the network and ask "
      + "again.",
  },
  de: {
    title: "Svar fragen",
    heading: "Svar fragen",
    lead_before: "Eine Demo mit ",
    lead_strong: "erfundenen",
    lead_after:
      " Betrieben. Jeder hält seine Handbücher, Notizen und Entwürfe in einer eigenen Ablage, "
      + "und Svar antwortet immer aus einer Ablage. Jede Antwort kommt mit den Textstellen, "
      + "aus denen sie gebaut wurde, und jede Stelle sagt, aus welcher Schicht sie stammt: von "
      + "einem Menschen geschrieben, von einer Maschine erzeugt oder nur eingetroffen und noch "
      + "nicht sortiert.",
    language_label: "Sprache",
    access_heading: "Arbeitsbereich und Schlüssel",
    access_note:
      "Ein Schlüssel öffnet einen Arbeitsbereich und hat ein Kontingent an Fragen; ein "
      + "Demo-Schlüssel läuft außerdem eine feste Zeit nach der ersten Frage ab. Er bleibt in "
      + "diesem Browser und ist nie Teil der Adresse.",
    workspace_label: "Arbeitsbereich",
    workspace_placeholder: "quillmoor",
    key_label: "Schlüssel",
    key_placeholder: "Schlüssel des Arbeitsbereichs einfügen",
    forget_button: "Schlüssel vergessen",
    question_heading: "Frage",
    question_placeholder: "Wie exportiere ich Projektdaten?",
    ask_button: "Fragen",
    asking_button: "Frage läuft…",
    enter_hint: "Enter stellt die Frage. Umschalt + Enter beginnt eine neue Zeile.",
    answer_heading: "Antwort",
    sources_heading: "Quellen",
    no_sources: "Keine — keine Textstelle in diesem Arbeitsbereich passte.",
    layer_human: "Mensch",
    layer_agent: "Maschine",
    layer_inbox: "Eingang",
    layers_rest: "Beruht auf: {layers}",
    layers_none: "Beruht auf: nichts, was die Antwort zitiert.",
    source_cited: "{words} Wörter · zitiert",
    source_not_cited: "{words} Wörter · nicht zitiert",
    cost_no_model: "kein Modellaufruf",
    cost_tokens: "{input} Tokens ein, {output} aus",
    cost_price: "{cost} USD zu den Preisen vom {date}",
    cost_latency: "{ms} ms in der Funktion",
    need_workspace_and_key: "Tragen Sie zuerst den Arbeitsbereich und seinen Schlüssel ein.",
    need_question: "Geben Sie eine Frage ein.",
    question_too_long: "Eine Frage hat höchstens {max} Zeichen.",
    need_https:
      "Diese Seite braucht eine https-Verbindung: Ohne sie stellt der Browser die Prüfsumme "
      + "nicht bereit, die eine signierte Anfrage braucht.",
    key_forgotten: "Der Schlüssel ist aus diesem Browser gelöscht. Der Arbeitsbereich bleibt.",
    ask_for_a_new_key:
      " Bitten Sie den, der Ihnen diesen Link geschickt hat, um einen neuen Schlüssel.",
    problem_busy:
      "Svar ist ausgelastet: Er beantwortet nur ein paar Fragen gleichzeitig, die übrigen "
      + "werden abgewiesen. Versuchen Sie es gleich noch einmal.",
    problem_refused_early:
      "Die Anfrage wurde abgewiesen, bevor sie Svar erreicht hat (403). Das liegt am Weg "
      + "hinein, nicht am Schlüssel — ein falscher Schlüssel wird mit 401 beantwortet.",
    problem_too_slow:
      "Der Dienst hat aufgehört, auf die Antwort zu warten (504). Die Frage läuft "
      + "vielleicht noch; noch einmal zu fragen kostet eine weitere Frage aus dem Kontingent "
      + "des Schlüssels.",
    problem_unreadable:
      "Der Dienst hat mit Status {status} geantwortet, und diese Seite kann nichts davon lesen.",
    problem_timed_out:
      "Innerhalb von {seconds} Sekunden kam nichts zurück, deshalb hat die Seite aufgehört zu "
      + "warten. Die Frage läuft vielleicht noch; noch einmal zu fragen kostet eine weitere "
      + "Frage aus dem Kontingent des Schlüssels.",
    problem_connection:
      "Die Frage ist nicht durchgekommen: Die Verbindung ist ausgefallen. Prüfen Sie das "
      + "Netzwerk und fragen Sie noch einmal.",
  },
};

/* The language a visit starts in: the one kept in the browser if it is one of
 * ours, else German for a browser whose language begins with "de", else English. */
function startLanguage(stored, browserLanguage) {
  if (LANGUAGES.includes(stored)) {
    return stored;
  }
  return String(browserLanguage || "").toLowerCase().startsWith("de") ? "de" : "en";
}

/* One text in one language, with its {name} placeholders filled. An unknown
 * language reads English; an unknown key is an error, so a typo shows at once. */
function translate(language, key, values) {
  const table = TEXTS[LANGUAGES.includes(language) ? language : DEFAULT_LANGUAGE];
  if (!Object.prototype.hasOwnProperty.call(table, key)) {
    throw new Error("No text for " + key);
  }
  const given = values || {};
  return table[key].replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(given, name) ? String(given[name]) : whole
  );
}

/* A layer's name as the reader sees it. A layer the page does not know shows as it is. */
function layerName(language, layer) {
  const key = "layer_" + String(layer).replace(/[^a-z]/g, "");
  return Object.prototype.hasOwnProperty.call(TEXTS[DEFAULT_LANGUAGE], key)
    ? translate(language, key)
    : String(layer);
}

/* What an unhappy answer means, in a sentence. The function's own refusals
 * carry one sentence, in the language the question asked in, and say it better
 * than this page could. What never passed through the function has none. */
function problemSentence(language, status, payload) {
  if (payload && typeof payload.error === "string" && payload.error) {
    if (status === 402) {
      return payload.error + translate(language, "ask_for_a_new_key");
    }
    return payload.error;
  }
  if (status === 429) {
    return translate(language, "problem_busy");
  }
  if (status === 403) {
    return translate(language, "problem_refused_early");
  }
  if (status === 504) {
    return translate(language, "problem_too_slow");
  }
  return translate(language, "problem_unreadable", { status: status });
}

/* The line under the answer's layers: the layers it rests on, by name. */
function layersLine(language, layers) {
  return layers.length
    ? translate(language, "layers_rest", {
        layers: layers.map((layer) => layerName(language, layer)).join(", "),
      })
    : translate(language, "layers_none");
}

/* A source's note: how long it is, and whether the answer cites it. */
function sourceNote(language, source) {
  return translate(language, source.cited ? "source_cited" : "source_not_cited", {
    words: source.words,
  });
}

/* The last line: model, tokens, cost, time. German writes the cost with a comma. */
function costLine(language, answer) {
  const usage = answer.usage || { input_tokens: 0, output_tokens: 0 };
  const parts = [
    answer.model || translate(language, "cost_no_model"),
    translate(language, "cost_tokens", {
      input: usage.input_tokens,
      output: usage.output_tokens,
    }),
  ];
  if (typeof answer.cost_usd === "number") {
    const cost = answer.cost_usd.toFixed(6);
    parts.push(
      translate(language, "cost_price", {
        cost: language === "de" ? cost.replace(".", ",") : cost,
        date: answer.price_date,
      })
    );
  }
  parts.push(translate(language, "cost_latency", { ms: answer.latency_ms }));
  return parts.join(" · ");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    LANGUAGES: LANGUAGES,
    DEFAULT_LANGUAGE: DEFAULT_LANGUAGE,
    TEXTS: TEXTS,
    startLanguage: startLanguage,
    translate: translate,
    layerName: layerName,
    problemSentence: problemSentence,
    layersLine: layersLine,
    sourceNote: sourceNote,
    costLine: costLine,
  };
}
