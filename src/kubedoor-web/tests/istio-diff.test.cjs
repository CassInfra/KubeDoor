const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");

const { transformSync } = Module.createRequire(require.resolve("vite/package.json"))("esbuild");

function compileTs(file) {
  const sourcePath = path.resolve(__dirname, "../src/views/istio/level1-vs", file);
  const output = transformSync(fs.readFileSync(sourcePath, "utf8"), {
    loader: "ts",
    target: "es2020",
    format: "cjs"
  });
  const loaded = new Module(sourcePath, module);
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath));
  loaded._compile(output.code, sourcePath);
  return loaded.exports;
}
const { sideBySide, foldSame } = compileTs("lineDiff.ts");

// 一行的显示:不同的部分用 [] 括起来;每行 [类型, 左行号, 左边, 右行号, 右边],没有的一边是 0 / null
const show = cell => (cell ? cell.parts.map(p => (p.diff ? `[${p.text}]` : p.text)).join("") : null);
const brief = rows => rows.map(r => [r.type, r.left?.no ?? 0, show(r.left), r.right?.no ?? 0, show(r.right)]);

test("两边一样:每行都是 same,行号对齐", () => {
  const text = "match:\n  - uri:\n      prefix: /a/\n";
  assert.deepEqual(brief(sideBySide(text, text)), [
    ["same", 1, "match:", 1, "match:"],
    ["same", 2, "  - uri:", 2, "  - uri:"],
    ["same", 3, "      prefix: /a/", 3, "      prefix: /a/"]
  ]);
});

test("改了值的行和同一个键对齐,标出具体不同的字;多出来的行另一边是空行", () => {
  const left = "name: a\nroute:\n  - destination: x\ntimeout: 7s\n";
  const right = "route:\n  - destination: x\n  - destination: y\ntimeout: 10s\nretries: 3\n";
  assert.deepEqual(brief(sideBySide(left, right)), [
    ["left", 1, "name: a", 0, null],
    ["same", 2, "route:", 1, "route:"],
    ["same", 3, "  - destination: x", 2, "  - destination: x"],
    ["right", 0, null, 3, "  - destination: y"],
    ["changed", 4, "timeout: [7]s", 4, "timeout: [10]s"],
    ["right", 0, null, 5, "retries: 3"]
  ]);
});

test("整段都换了:左右依次配对", () => {
  assert.deepEqual(brief(sideBySide("a: 1\nb: 2", "c: 3\nd: 4\ne: 5")), [
    ["changed", 1, "[a: 1]", 1, "[c: 3]"],
    ["changed", 2, "[b: 2]", 2, "[d: 4]"],
    ["right", 0, null, 3, "e: 5"]
  ]);
});

test("一边是空的:整段都是只有一边有", () => {
  assert.deepEqual(brief(sideBySide("a: 1\nb: 2\n", "")), [
    ["left", 1, "a: 1", 0, null],
    ["left", 2, "b: 2", 0, null]
  ]);
  assert.deepEqual(sideBySide("", ""), []);
});

test("离不同的行较远的相同行折叠起来,展开后全部显示", () => {
  const lines = n => Array.from({ length: n }, (_, i) => `k${i}: v`);
  const right = lines(10);
  right[8] = "k8: changed";
  const rows = sideBySide(lines(10).join("\n"), right.join("\n"));
  const items = list => list.map(it => (it.kind === "fold" ? `fold ${it.start}+${it.count}` : it.index));
  assert.deepEqual(items(foldSame(rows, 2)), ["fold 0+6", 6, 7, 8, 9]);
  assert.deepEqual(items(foldSame(rows, 2, new Set([0]))), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  // 两边一样:超过 3 行整段折叠,3 行以内不折叠
  assert.deepEqual(items(foldSame(sideBySide("a\nb\nc\nd", "a\nb\nc\nd"), 2)), ["fold 0+4"]);
  assert.deepEqual(items(foldSame(sideBySide("a\nb\nc", "a\nb\nc"), 2)), [0, 1, 2]);
});
