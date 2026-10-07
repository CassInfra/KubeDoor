import MarkdownIt from "markdown-it";

/** Markdown URLs never inherit executable or local-file protocols. */
export function isSafeMarkdownLink(value: string): boolean {
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value)) return false;
  return /^(?:https?:\/\/|mailto:)/i.test(value) || value.startsWith("#");
}

// Keep HTML disabled and avoid plugins that introduce arbitrary HTML. The only
// custom markup below is static; user-controlled text is escaped by markdown-it.
const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  typographer: false
});

markdown.validateLink = isSafeMarkdownLink;

markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return self.renderToken(tokens, index, options);
};

// An AI reply cannot load an external image and silently disclose the reader's
// address or conversation metadata. Preserve its alt text as ordinary text.
markdown.renderer.rules.image = (tokens, index) =>
  markdown.utils.escapeHtml(tokens[index].content);

markdown.renderer.rules.table_open = (tokens, index, options, env, self) =>
  '<div class="ai-markdown-table" role="region" aria-label="表格，可横向滚动" tabindex="0">' +
  self.renderToken(tokens, index, options);
markdown.renderer.rules.table_close = (tokens, index, options, env, self) =>
  self.renderToken(tokens, index, options) + "</div>";

/** Re-rendering incomplete Markdown is safe while the assistant is streaming. */
export function renderAIMarkdown(content: string): string {
  return markdown.render(content || "");
}
