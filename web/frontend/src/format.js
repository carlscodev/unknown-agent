/* Minimal, dependency-free markdown rendering: fenced code blocks, headings,
   lists, inline code, bold and links. Everything is escaped first, so model
   output can never inject markup. */

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`,
    );
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;

function textBlocks(raw) {
  const out = [];
  const lines = raw.split("\n");
  let para = [];
  let list = null; // "ul" | "ol"

  const flushPara = () => {
    if (!para.length) return;
    const html = inline(escapeHtml(para.join("\n"))).replace(/\n/g, "<br>");
    out.push({ kind: "p", html });
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    out.push({ kind: "list", tag: list, items: listItems.map((i) => inline(escapeHtml(i))) });
    list = null;
    listItems = [];
  };
  let listItems = [];

  for (const line of lines) {
    const head = line.match(HEADING);
    if (head) {
      flushPara();
      flushList();
      const level = Math.min(head[1].length, 4);
      out.push({ kind: "h", level, html: inline(escapeHtml(head[2])) });
      continue;
    }
    const bullet = line.match(BULLET);
    const ordered = line.match(ORDERED);
    if (bullet || ordered) {
      flushPara();
      const wanted = bullet ? "ul" : "ol";
      if (list && list !== wanted) flushList();
      list = wanted;
      listItems.push((bullet || ordered)[1]);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return out;
}

export function renderMarkdown(raw) {
  const out = [];
  const parts = String(raw || "").split(/```/);
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      // fenced block: first line may carry the language
      const block = parts[i];
      const nl = block.indexOf("\n");
      const lang = nl === -1 ? "" : block.slice(0, nl).trim();
      const code = nl === -1 ? block : block.slice(nl + 1);
      out.push({ kind: "code", lang: lang || "text", code: code.replace(/\n$/, "") });
    } else {
      out.push(...textBlocks(parts[i]));
    }
  }
  return out;
}
