/** 按行对比两段文本,给保存前的确认框展示改了哪些行(纯函数,单测见 tests/coredns.test.cjs) */

export interface DiffLine {
  type: "same" | "add" | "del";
  text: string;
  oldNo: number; // 从 1 开始;新增的行为 0
  newNo: number; // 从 1 开始;删除的行为 0
}

export interface DiffResult {
  hunks: DiffLine[][];
  added: number;
  removed: number;
}

// 中间不同的部分太大时不做 LCS(内存 O(n*m)),整段按删除 + 新增展示
const MAX_CELLS = 4_000_000;

export function diffLines(
  before: string,
  after: string,
  context = 2
): DiffResult {
  const a = before.split("\n");
  const b = after.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const x = a.slice(start, endA);
  const y = b.slice(start, endB);
  const ops: Array<{ type: DiffLine["type"]; text: string }> = [];
  if (x.length * y.length > MAX_CELLS) {
    x.forEach(text => ops.push({ type: "del", text }));
    y.forEach(text => ops.push({ type: "add", text }));
  } else {
    const width = y.length + 1;
    const lcs = new Uint32Array((x.length + 1) * width);
    for (let i = x.length - 1; i >= 0; i--) {
      for (let j = y.length - 1; j >= 0; j--) {
        lcs[i * width + j] =
          x[i] === y[j]
            ? lcs[(i + 1) * width + j + 1] + 1
            : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < x.length && j < y.length) {
      if (x[i] === y[j]) {
        ops.push({ type: "same", text: x[i] });
        i++;
        j++;
      } else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) {
        ops.push({ type: "del", text: x[i++] });
      } else {
        ops.push({ type: "add", text: y[j++] });
      }
    }
    while (i < x.length) ops.push({ type: "del", text: x[i++] });
    while (j < y.length) ops.push({ type: "add", text: y[j++] });
  }

  const lines: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  const same = (text: string) =>
    lines.push({ type: "same", text, oldNo: ++oldNo, newNo: ++newNo });
  a.slice(0, start).forEach(same);
  for (const op of ops) {
    if (op.type === "same") same(op.text);
    else if (op.type === "del") lines.push({ ...op, oldNo: ++oldNo, newNo: 0 });
    else lines.push({ ...op, oldNo: 0, newNo: ++newNo });
  }
  a.slice(endA).forEach(same);

  // 改动的行前后各留 context 行,挨得近的合成一段
  const changed = lines.flatMap((line, index) =>
    line.type === "same" ? [] : [index]
  );
  const hunks: DiffLine[][] = [];
  for (let k = 0; k < changed.length; k++) {
    const from = Math.max(0, changed[k] - context);
    let to = changed[k];
    while (k + 1 < changed.length && changed[k + 1] - to <= 2 * context + 1)
      to = changed[++k];
    hunks.push(lines.slice(from, to + context + 1));
  }
  return {
    hunks,
    added: lines.filter(l => l.type === "add").length,
    removed: lines.filter(l => l.type === "del").length
  };
}
