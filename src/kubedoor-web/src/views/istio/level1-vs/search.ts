/** 全局VS管理矩阵的全文搜索(纯函数,不 import 别的模块;单测见 tests/istio-search.test.cjs) */

/** 关键字按空白拆开、转小写:每个都要命中(VS 名称、hosts、规则内容任一处都算) */
export function searchTerms(keyword: string): string[] {
  return keyword.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

export interface SearchEntry {
  /** 名称、hosts、规则内容转小写后一项一行拼起来,关键字不含空白,不会跨项命中 */
  text: string;
  /** 规则内容的原文和小写,找命中的规则行用 */
  lines: string[];
  lower: string[];
}

/** 一行 VS 的搜索内容:head 是名称、hosts 这些不列命中片段的,lines 是规则内容(search_lines) */
export function searchEntry(head: string[], lines: string[]): SearchEntry {
  const lower = lines.map(line => line.toLowerCase());
  return {
    text: [...head.map(item => item.toLowerCase()), ...lower].join("\n"),
    lines,
    lower
  };
}

export function matchesAll(entry: SearchEntry, terms: string[]) {
  return terms.every(term => entry.text.includes(term));
}

/** 命中了关键字的规则行:命中的关键字多的在前,一样多的保持原来的顺序 */
export function ruleHits(entry: SearchEntry, terms: string[]): string[] {
  const scored: { index: number; score: number }[] = [];
  entry.lower.forEach((line, index) => {
    const score = terms.filter(term => line.includes(term)).length;
    if (score) scored.push({ index, score });
  });
  return scored
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map(item => entry.lines[item.index]);
}

export interface TextPart {
  text: string;
  hit: boolean;
}

/** 按命中关键字的位置把文本切开,用来高亮;不区分大小写,相邻、重叠的命中合成一段 */
export function highlightParts(text: string, terms: string[]): TextPart[] {
  const lower = text.toLowerCase();
  // 个别字符转小写后长度会变,位置对不上就不高亮
  if (!terms.length || lower.length !== text.length) {
    return text ? [{ text, hit: false }] : [];
  }
  const marks = new Uint8Array(text.length);
  for (const term of terms) {
    if (!term) continue;
    let i = lower.indexOf(term);
    while (i !== -1) {
      marks.fill(1, i, i + term.length);
      i = lower.indexOf(term, i + 1);
    }
  }
  const parts: TextPart[] = [];
  let start = 0;
  for (let i = 1; i <= text.length; i++) {
    if (i === text.length || marks[i] !== marks[start]) {
      parts.push({ text: text.slice(start, i), hit: marks[start] === 1 });
      start = i;
    }
  }
  return parts;
}
