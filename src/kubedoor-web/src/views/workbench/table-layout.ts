/**
 * Agent 管理表格的列宽：表头文字不换行，K8S 列按最长的集群名完整显示。
 * 纯函数，不 import 别的模块（单测见 tests/agent-install.test.cjs）。
 */

/** 表格字号（Element Plus 默认尺寸） */
const FONT_SIZE = 14;
/** 单元格左右内边距 12px × 2 */
const CELL_PADDING = 24;
/** 可排序列表头后面的排序图标 */
const SORT_ICON = 24;
/** 表头里问号提示图标的宽度（含间距） */
const HINT_ICON = 18;
/** 边框和取整误差 */
const SLACK = 2;
/** K8S 列最宽多少：再长的集群名截断，悬停看全名 */
export const NAME_COLUMN_MAX = 360;

/**
 * 估算一行文字的宽度（像素）：中文等全角字符按字号算，其他按常见无衬线字体的大致宽度，宁大勿小。
 * 不依赖浏览器：单测和没有 canvas 时用它
 */
export function estimateTextWidth(text: string, fontSize = FONT_SIZE): number {
  let width = 0;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (code >= 0x2e80) width += fontSize;
    else if (/[A-Z]/.test(char) || char === "m" || char === "w")
      width += fontSize * 0.75;
    else if (/[a-z0-9]/.test(char)) width += fontSize * 0.6;
    else if (char === " ") width += fontSize * 0.3;
    else width += fontSize * 0.45;
  }
  return Math.ceil(width);
}

/** 表头文字放在一行时的最小列宽：可排序的列另加排序图标，带问号提示的另加图标 */
export function headerWidth(
  label: string,
  options: { sortable?: boolean; hint?: boolean } = {}
): number {
  return (
    estimateTextWidth(label) +
    CELL_PADDING +
    (options.sortable ? SORT_ICON : 0) +
    (options.hint ? HINT_ICON : 0) +
    SLACK
  );
}

/**
 * 按内容算列的最小宽度：最长的一项完整显示，且不比表头窄（header 是 headerWidth 的结果）；超过 max 的截断。
 * measure 量文字宽度，浏览器里传按实际字体测量的函数，默认用估算
 */
export function fitColumnWidth(
  texts: string[],
  header: number,
  measure: (text: string) => number = estimateTextWidth,
  max = NAME_COLUMN_MAX
): number {
  const longest = texts.reduce(
    (width, text) => Math.max(width, text ? measure(text) : 0),
    0
  );
  return Math.min(max, Math.max(header, longest + CELL_PADDING + SLACK));
}
