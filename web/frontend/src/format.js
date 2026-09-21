/* Minimal, dependency-free markdown rendering: fenced code blocks, inline code,
   bold. Everything else is escaped, so model output can never inject markup. */

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
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
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
      out.push(
        { kind: "code", lang: lang || "text", code: code.replace(/\n$/, "") },
      );
    } else {
      const escaped = escapeHtml(parts[i]);
      const paras = escaped
        .split(/\n{2,}/)
        .map((p) => p.trim())
        .filter(Boolean)
        .map((p) => ({ kind: "p", html: inline(p).replace(/\n/g, "<br>") }));
      out.push(...paras);
    }
  }
  return out;
}
