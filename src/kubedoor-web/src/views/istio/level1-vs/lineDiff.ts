/** 左右对照的逐行对比(BCompare 风格)用的纯函数,单测见 tests/istio-diff.test.cjs */

export interface DiffPart {
  text: string;
  /** 和另一边对应行不同的部分 */
  diff: boolean;
}

export interface DiffCell {
  /** 行号,从 1 开始 */
  no: number;
  parts: DiffPart[];
}

export interface DiffRow {
  /** same:两边一样;changed:两边都有但不同;left / right:只有这一边有,另一边是空行占位 */
  type: "same" | "changed" | "left" | "right";
  left: DiffCell | null;
  right: DiffCell | null;
}

// 不同的部分太大时不做 LCS(内存 O(n*m)),整段左右依次配对
const MAX_CELLS = 4_000_000;

function lines(text: string) {
  return text ? text.replace(/\n$/, "").split("\n") : [];
}

/** YAML 行的键(连同缩进和列表的 -),如 `  - destination: x` → `  - destination`;不是 键: 值 的返回 null */
function yamlKey(line: string) {
  return /^(\s*(?:-\s+)?[^\s:#][^:]*?):(\s|$)/.exec(line)?.[1] ?? null;
}

/** 相同的开头和结尾占较长一行的比例 */
function ratio(x: string, y: string) {
  const max = Math.max(x.length, y.length);
  let p = 0;
  while (p < x.length && p < y.length && x[p] === y[p]) p++;
  let s = 0;
  while (
    s < x.length - p &&
    s < y.length - p &&
    x[x.length - 1 - s] === y[y.length - 1 - s]
  )
    s++;
  return max ? (p + s) / max : 1;
}

/** 改过的同一行:键一样(值改了),或者大半一样 */
function similar(x: string, y: string) {
  const key = yamlKey(x);
  return (key !== null && key === yamlKey(y)) || ratio(x, y) >= 0.5;
}

/** 两行里不同的部分:去掉相同的开头和结尾,剩下的标出来 */
function inline(x: string, y: string): [DiffPart[], DiffPart[]] {
  let start = 0;
  while (start < x.length && start < y.length && x[start] === y[start]) start++;
  let endX = x.length;
  let endY = y.length;
  while (endX > start && endY > start && x[endX - 1] === y[endY - 1]) {
    endX--;
    endY--;
  }
  const parts = (s: string, end: number) =>
    [
      { text: s.slice(0, start), diff: false },
      { text: s.slice(start, end), diff: true },
      { text: s.slice(end), diff: false }
    ].filter(part => part.text);
  return [parts(x, endX), parts(y, endY)];
}

/** 两个序列的最长公共子序列,返回配上的下标对(按顺序);太大时不配 */
function lcsPairs(
  n: number,
  m: number,
  equal: (i: number, j: number) => boolean
): Array<[number, number]> {
  if (!n || !m || n * m > MAX_CELLS) return [];
  const width = m + 1;
  const lcs = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * width + j] = equal(i, j)
        ? lcs[(i + 1) * width + j + 1] + 1
        : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }
  const pairs: Array<[number, number]> = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (equal(i, j)) pairs.push([i++, j++]);
    else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) i++;
    else j++;
  }
  return pairs;
}

/** 两段文本按行对齐。一段连续的不同行里,相似的行(同一个键改了值)配在同一行并标出不同的字,
 *  其余的左右依次配对,多出来的是只有一边有的行 */
export function sideBySide(left: string, right: string): DiffRow[] {
  const a = lines(left);
  const b = lines(right);
  const rows: DiffRow[] = [];
  const cell = (no: number, text: string): DiffCell => ({
    no: no + 1,
    parts: [{ text, diff: false }]
  });
  const pair = (x: number, y: number) => {
    const [l, r] = inline(a[x], b[y]);
    rows.push({
      type: "changed",
      left: { no: x + 1, parts: l },
      right: { no: y + 1, parts: r }
    });
  };
  const block = (dels: number[], adds: number[]) => {
    const anchors = lcsPairs(dels.length, adds.length, (x, y) =>
      similar(a[dels[x]], b[adds[y]])
    );
    let x = 0;
    let y = 0;
    for (const [px, py] of [...anchors, [dels.length, adds.length]]) {
      for (; x < px && y < py; x++, y++) pair(dels[x], adds[y]);
      for (; x < px; x++)
        rows.push({
          type: "left",
          left: cell(dels[x], a[dels[x]]),
          right: null
        });
      for (; y < py; y++)
        rows.push({
          type: "right",
          left: null,
          right: cell(adds[y], b[adds[y]])
        });
      if (px < dels.length) pair(dels[x++], adds[y++]);
    }
  };

  let i = 0;
  let j = 0;
  for (const [x, y] of [
    ...lcsPairs(a.length, b.length, (p, q) => a[p] === b[q]),
    [a.length, b.length]
  ]) {
    const dels: number[] = [];
    const adds: number[] = [];
    for (; i < x; i++) dels.push(i);
    for (; j < y; j++) adds.push(j);
    block(dels, adds);
    if (x < a.length) {
      rows.push({ type: "same", left: cell(x, a[x]), right: cell(y, b[y]) });
      i++;
      j++;
    }
  }
  return rows;
}

export type DiffItem =
  | { kind: "row"; row: DiffRow; index: number }
  | { kind: "fold"; start: number; count: number };

/** 离不同的行超过 context 行的相同行折叠起来(连续 4 行以上才折叠);opened 是已经展开的折叠(按开始的行) */
export function foldSame(
  rows: DiffRow[],
  context: number,
  opened: Set<number> = new Set()
): DiffItem[] {
  const near = rows.map(() => false);
  rows.forEach((row, i) => {
    if (row.type === "same") return;
    for (let k = Math.max(0, i - context); k <= i + context; k++)
      near[k] = true;
  });
  const items: DiffItem[] = [];
  for (let i = 0; i < rows.length; ) {
    let j = i;
    while (j < rows.length && !near[j]) j++;
    if (j - i > 3 && !opened.has(i)) {
      items.push({ kind: "fold", start: i, count: j - i });
      i = j;
    }
    for (; i < rows.length && (i < j || near[i]); i++)
      items.push({ kind: "row", row: rows[i], index: i });
  }
  return items;
}
