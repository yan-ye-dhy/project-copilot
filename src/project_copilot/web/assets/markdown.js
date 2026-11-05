/* Small Markdown renderer: create DOM nodes, never parse model-provided HTML. */
const MAX_CHARS = 100000;

function node(doc, tag, text) {
  const value = doc.createElement(tag);
  if (text !== undefined) value.textContent = text;
  return value;
}

function safeHref(value) {
  // Links are optional decoration, never executable URLs, images or raw HTML.
  if (/^https?:\/\/[^\s\\<>]+$/i.test(value)) {
    try { const url = new URL(value); return !url.username && !url.password ? url.href : null; } catch {}
  }
  return null;
}

function inline(doc, target, text, depth = 0) {
  if (depth > 5) { target.append(doc.createTextNode(text)); return; }
  const tokens = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_|\[([^\]\n]+)\]\(([^\s)]+)\)/g;
  let at = 0, match;
  while ((match = tokens.exec(text))) {
    target.append(doc.createTextNode(text.slice(at, match.index)));
    if (match[1] !== undefined) target.append(node(doc, "code", match[1]));
    else if (match[2] !== undefined || match[3] !== undefined) {
      const strong = node(doc, "strong"); inline(doc, strong, match[2] ?? match[3], depth + 1); target.append(strong);
    } else if (match[4] !== undefined || match[5] !== undefined) {
      const em = node(doc, "em"); inline(doc, em, match[4] ?? match[5], depth + 1); target.append(em);
    } else {
      const href = safeHref(match[7]);
      if (href) {
        const link = node(doc, "a", match[6]); link.href = href;
        link.rel = "noopener noreferrer"; link.target = "_blank"; target.append(link);
      } else target.append(doc.createTextNode(match[0]));
    }
    at = tokens.lastIndex;
  }
  target.append(doc.createTextNode(text.slice(at)));
}

const tableCells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
const isTableRule = (line) => /^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$/.test(line.trim());
const special = (line) => /^(?: {0,3}#{1,6}\s|\s*```|\s*~~~|\s*>|\s*[-*+]\s|\s*\d+[.)]\s|\s*---+\s*$)/.test(line);

export function renderMarkdown(text, doc = document) {
  const root = node(doc, "div"); root.className = "markdown-answer";
  const lines = String(text ?? "").slice(0, MAX_CHARS).replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  const appendInline = (tag, value, target = root) => { const item = node(doc, tag); inline(doc, item, value); target.append(item); return item; };
  while (i < lines.length) {
    if (!lines[i].trim()) { i += 1; continue; }
    const fence = /^\s*(`{3,}|~{3,})([\w+-]*)\s*$/.exec(lines[i]);
    if (fence) {
      const codeLines = []; i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) codeLines.push(lines[i++]);
      if (i < lines.length) i += 1;
      const pre = node(doc, "pre"), code = node(doc, "code", codeLines.join("\n"));
      if (fence[2]) code.dataset.language = fence[2]; pre.append(code); root.append(pre); continue;
    }
    const heading = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(lines[i]);
    if (heading) { appendInline(`h${Math.min(6, heading[1].length + 1)}`, heading[2]); i += 1; continue; }
    if (i + 1 < lines.length && lines[i].includes("|") && isTableRule(lines[i + 1])) {
      const wrap = node(doc, "div"); wrap.className = "markdown-table";
      const table = node(doc, "table"), head = node(doc, "thead"), row = node(doc, "tr");
      for (const cell of tableCells(lines[i]).slice(0, 24)) appendInline("th", cell, row);
      head.append(row); table.append(head); const body = node(doc, "tbody"); i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const tr = node(doc, "tr"); for (const cell of tableCells(lines[i++]).slice(0, 24)) appendInline("td", cell, tr); body.append(tr);
      }
      table.append(body); wrap.append(table); root.append(wrap); continue;
    }
    if (/^\s*>/.test(lines[i])) {
      const quote = node(doc, "blockquote"); const values = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) values.push(lines[i++].replace(/^\s*>\s?/, ""));
      appendInline("p", values.join("\n"), quote); root.append(quote); continue;
    }
    const list = /^\s*([-*+]|\d+[.)])\s+(.+)$/.exec(lines[i]);
    if (list) {
      const ordered = /^\d/.test(list[1]), group = node(doc, ordered ? "ol" : "ul");
      if (ordered) group.start = Math.min(9999, Number.parseInt(list[1], 10));
      while (i < lines.length) {
        const item = /^\s*([-*+]|\d+[.)])\s+(.+)$/.exec(lines[i]);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        appendInline("li", item[2], group); i += 1;
      }
      root.append(group); continue;
    }
    if (/^\s*(?:---+|\*\*\*+)\s*$/.test(lines[i])) { root.append(node(doc, "hr")); i += 1; continue; }
    const paragraph = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !special(lines[i]) && !(i + 1 < lines.length && isTableRule(lines[i + 1]))) paragraph.push(lines[i++]);
    appendInline("p", paragraph.join("\n"));
  }
  return root;
}
