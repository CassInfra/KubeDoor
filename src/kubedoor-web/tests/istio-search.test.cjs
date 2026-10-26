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
const { searchTerms, searchEntry, matchesAll, ruleHits, highlightParts } = compileTs("search.ts");

// 后端 search_lines 的样子:规则内容一个字段一行「字段路径: 值」
const LINES = [
  "gateways: istio-system/internal",
  "name: /order",
  "match.uri.prefix: /order/",
  "rewrite.uri: /",
  "route.destination.host: order.prod.svc.cluster.local",
  "route.destination.port.number: 8080",
  "timeout: 30s",
  "match.uri.prefix: /api/",
  "route.destination.port.number: 80"
];
const entry = searchEntry(["istio-system/order-vs", "Order.Example.com"], LINES);
const match = keyword => matchesAll(entry, searchTerms(keyword));
// 高亮结果:命中的部分用 [] 括起来
const marked = parts => parts.map(p => (p.hit ? `[${p.text}]` : p.text)).join("");

test("关键字按空白拆开、转小写", () => {
  assert.deepEqual(searchTerms("  Prefix:   /API \t8080 "), ["prefix:", "/api", "8080"]);
  assert.deepEqual(searchTerms("   "), []);
});

test("名称、hosts、规则内容任一处命中都算,不区分大小写", () => {
  assert.ok(match("order-vs")); // 名称
  assert.ok(match("order.example")); // hosts
  assert.ok(match("8080")); // 端口
  assert.ok(match("/api")); // 路径
  assert.ok(match("TIMEOUT: 30s")); // 字段名 + 值
  assert.ok(match("order.prod.svc")); // 目标服务
  assert.ok(match(""));
  assert.ok(!match("9090"));
});

test("多个关键字要全部命中,可以分在不同的地方,但不会跨两项拼起来命中", () => {
  assert.ok(match("order-vs 8080 /api/"));
  assert.ok(!match("8080 9090"));
  assert.ok(!matchesAll(searchEntry(["ns/vs"], ["timeout: 30s"]), ["vstimeout"]));
});

test("规则里命中的行:命中的关键字多的在前,只在名称、hosts 里命中的不列", () => {
  assert.deepEqual(ruleHits(entry, searchTerms("prefix /api")), [
    "match.uri.prefix: /api/",
    "match.uri.prefix: /order/"
  ]);
  assert.deepEqual(ruleHits(entry, searchTerms("80")), [
    "route.destination.port.number: 8080",
    "route.destination.port.number: 80"
  ]);
  assert.deepEqual(ruleHits(entry, searchTerms("order-vs")), []);
  assert.deepEqual(ruleHits(entry, []), []);
});

test("高亮:不区分大小写,重叠、相邻的命中合成一段", () => {
  assert.equal(
    marked(highlightParts("match.uri.prefix: /API/v1", ["prefix", "/api"])),
    "match.uri.[prefix]: [/API]/v1"
  );
  assert.equal(marked(highlightParts("8080", ["80"])), "[8080]");
  assert.equal(marked(highlightParts("abcd", ["abc", "bcd"])), "[abcd]");
  assert.equal(marked(highlightParts("abc", [])), "abc");
  assert.deepEqual(highlightParts("", ["a"]), []);
});

test("高亮:空关键字跳过;转小写后长度变了的不高亮", () => {
  assert.equal(marked(highlightParts("aaa", ["", "a"])), "[aaa]");
  assert.deepEqual(highlightParts("İstanbul", ["stan"]), [{ text: "İstanbul", hit: false }]);
});
