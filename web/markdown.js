/* Ask Andur — the little Markdown an answer may carry.
 *
 * The answering model writes **bold** and lists now and then. Shown raw, the
 * asterisks stand in the text; so this file understands exactly three things
 * and nothing else:
 *
 *   **bold**           a run of text between double asterisks, on one line
 *   - item, * item     a bulleted list (also "• item")
 *   1. item, 1) item   a numbered list, starting at its first number
 *
 * A blank line ends a paragraph or a list. Everything else stays as written:
 * headings, links, tables, single asterisks and an unmatched "**" are text.
 *
 * Nothing from the answer is ever inserted as markup. The parser turns the
 * text into plain data, and the renderer builds every element itself with
 * createElement and puts the answer's words in with textContent. An answer
 * that contains <b> or <script> shows those characters and nothing more.
 *
 * Loaded before app.js as a classic script, so its functions are globals the
 * page calls. Under Node (the tests) it exports the two pure functions.
 */

"use strict";

const BULLET_ITEM = /^\s*[-*•]\s+(.*)$/;
const NUMBERED_ITEM = /^\s*(\d{1,9})[.)]\s+(.*)$/;
/* Bold starts and ends with a character that is not a space. No lookbehind:
 * older Safari versions (before 16.4) refuse the whole file over one. The
 * lazy "??" tries the shortest bold first, so "**2** of **3**" stays two. */
const BOLD = /\*\*(\S(?:.*?\S)??)\*\*/g;

/* One line of text as runs: [{ text, bold }]. Plain text between the bold
 * runs keeps its exact characters. */
function inlineParts(line) {
  const parts = [];
  let last = 0;
  for (const match of line.matchAll(BOLD)) {
    if (match.index > last) {
      parts.push({ text: line.slice(last, match.index), bold: false });
    }
    parts.push({ text: match[1], bold: true });
    last = match.index + match[0].length;
  }
  if (last < line.length) {
    parts.push({ text: line.slice(last), bold: false });
  }
  return parts;
}

/* The answer as blocks, in order:
 *   { type: "paragraph", lines: [string] }
 *   { type: "list", ordered: bool, start: number, items: [string] }
 * A list changes kind, bulleted to numbered, where a new list begins. */
function answerBlocks(text) {
  const blocks = [];
  let current = null;

  for (const line of String(text).split(/\r?\n/)) {
    const bullet = BULLET_ITEM.exec(line);
    const numbered = bullet ? null : NUMBERED_ITEM.exec(line);

    if (!line.trim()) {
      current = null;
    } else if (bullet || numbered) {
      const ordered = Boolean(numbered);
      if (!current || current.type !== "list" || current.ordered !== ordered) {
        current = {
          type: "list",
          ordered: ordered,
          start: ordered ? Number(numbered[1]) : 1,
          items: [],
        };
        blocks.push(current);
      }
      current.items.push(ordered ? numbered[2] : bullet[1]);
    } else {
      if (!current || current.type !== "paragraph") {
        current = { type: "paragraph", lines: [] };
        blocks.push(current);
      }
      current.lines.push(line);
    }
  }
  return blocks;
}

/* Put one line's runs into an element: text nodes, and <strong> for bold. */
function appendInline(element, line) {
  for (const part of inlineParts(line)) {
    if (part.bold) {
      const strong = document.createElement("strong");
      strong.textContent = part.text;
      element.appendChild(strong);
    } else {
      element.appendChild(document.createTextNode(part.text));
    }
  }
}

/* Replace the container's content with the rendered answer. */
function renderAnswer(container, text) {
  container.replaceChildren();
  for (const block of answerBlocks(text)) {
    if (block.type === "paragraph") {
      const paragraph = document.createElement("p");
      block.lines.forEach((line, index) => {
        if (index > 0) {
          paragraph.appendChild(document.createElement("br"));
        }
        appendInline(paragraph, line);
      });
      container.appendChild(paragraph);
    } else {
      const list = document.createElement(block.ordered ? "ol" : "ul");
      if (block.ordered && block.start !== 1) {
        list.start = block.start;
      }
      for (const itemText of block.items) {
        const item = document.createElement("li");
        appendInline(item, itemText);
        list.appendChild(item);
      }
      container.appendChild(list);
    }
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { answerBlocks: answerBlocks, inlineParts: inlineParts };
}
