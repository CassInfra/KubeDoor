const HTML_ESCAPE_MAP: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
};

const NAMED_HTML_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: "\u00a0"
};

/** Escape plain text before interpolating into HTML strings or attributes. */
export function escapeHtml(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).replace(/[&<>"']/g, ch => HTML_ESCAPE_MAP[ch]);
}

function decodeHtmlEntities(text: string): string {
  return text.replace(
    /&(#[xX][0-9a-fA-F]+|#\d+|amp|lt|gt|quot|apos|nbsp);/g,
    (entity, body: string) => {
      if (body.startsWith("#x") || body.startsWith("#X")) {
        const codePoint = Number.parseInt(body.slice(2), 16);
        return Number.isFinite(codePoint) &&
          codePoint >= 0 &&
          codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : entity;
      }
      if (body.startsWith("#")) {
        const codePoint = Number.parseInt(body.slice(1), 10);
        return Number.isFinite(codePoint) &&
          codePoint >= 0 &&
          codePoint <= 0x10ffff
          ? String.fromCodePoint(codePoint)
          : entity;
      }
      return NAMED_HTML_ENTITIES[body] ?? entity;
    }
  );
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Highlight keyword matches only in plain-text segments of ANSI-converted HTML
 * without corrupting HTML tags or entity sequences.
 */
export function highlightAnsiHtml(
  ansiHtml: string,
  keyword: string,
  highlightClass: string
): string {
  if (!ansiHtml) {
    return "";
  }
  if (!keyword || !keyword.trim()) {
    return ansiHtml;
  }

  const matcher = new RegExp(`(${escapeRegExp(keyword)})`, "gi");
  const safeClass = escapeHtml(highlightClass);

  return ansiHtml
    .split(/(<[^<>]+>)/g)
    .map((segment, index) => {
      if (index % 2 === 1 || !segment) {
        return segment;
      }
      const plainText = decodeHtmlEntities(segment);
      return plainText
        .split(matcher)
        .map((part, partIndex) => {
          if (!part) {
            return "";
          }
          const escapedPart = escapeHtml(part);
          return partIndex % 2 === 1
            ? `<span class="${safeClass}">${escapedPart}</span>`
            : escapedPart;
        })
        .join("");
    })
    .join("");
}

function normalizeColorValue(color: string): string {
  const trimmed = color.trim();
  if (!trimmed) {
    return "";
  }
  const match = trimmed.match(/^color\s*:\s*([^;]+);?$/i);
  return match ? match[1].trim() : trimmed;
}

/**
 * Build a node selector option label using DOM text APIs so untrusted node
 * names or counts can never inject HTML.
 */
export function buildNodeOptionLabel(
  nodeName: unknown,
  percentText: unknown,
  cpodNum: unknown,
  zeroColor = ""
): HTMLSpanElement {
  const label = document.createElement("span");
  label.appendChild(document.createTextNode(`${String(nodeName ?? "")} (`));

  const percentSpan = document.createElement("span");
  percentSpan.style.color = "blue";
  percentSpan.textContent = String(percentText ?? "");
  label.appendChild(percentSpan);

  label.appendChild(document.createTextNode("，"));

  const cpodSpan = document.createElement("span");
  const hasCpod =
    cpodNum !== null &&
    cpodNum !== undefined &&
    cpodNum !== "" &&
    Number(cpodNum) !== 0;
  const resolvedColor = hasCpod ? "red" : normalizeColorValue(zeroColor);
  if (resolvedColor) {
    cpodSpan.style.color = resolvedColor;
  }
  cpodSpan.textContent = `${String(cpodNum ?? "")}Pod`;
  label.appendChild(cpodSpan);

  label.appendChild(document.createTextNode(")"));
  return label;
}
