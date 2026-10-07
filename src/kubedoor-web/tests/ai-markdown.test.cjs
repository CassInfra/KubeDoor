const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { createSSRApp, h } = require("vue");
const { renderToString } = require("vue/server-renderer");
const { parse, compileScript } = require("vue/compiler-sfc");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

function loadTypeScript(source, filename, overrides = {}) {
  const loaded = new Module(filename, module);
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const originalRequire = loaded.require.bind(loaded);
  loaded.require = name => overrides[name] || originalRequire(name);
  loaded._compile(
    transformSync(source, { loader: "ts", target: "es2020", format: "cjs" })
      .code,
    filename
  );
  return loaded.exports;
}

const utilityPath = path.resolve(__dirname, "../src/utils/aiMarkdown.ts");
const markdown = loadTypeScript(
  fs.readFileSync(utilityPath, "utf8"),
  utilityPath
);
const componentPath = path.resolve(
  __dirname,
  "../src/components/AI/AIMarkdown.vue"
);
const { descriptor } = parse(fs.readFileSync(componentPath, "utf8"));
const script = compileScript(descriptor, {
  id: "ai-markdown-test",
  inlineTemplate: true
});
const AIMarkdown = loadTypeScript(script.content, componentPath, {
  "@/utils/aiMarkdown": markdown
}).default;

async function render(content) {
  return renderToString(
    createSSRApp({ render: () => h(AIMarkdown, { content }) })
  );
}

test("the actual Vue component renders a Markdown table, headings, lists and fenced code", async () => {
  const html = await render(
    [
      "## 集群检查",
      "",
      "| 命名空间 | Deployment | CPU |",
      "| --- | --- | ---: |",
      "| production | **web** | 250m |",
      "",
      "1. 查看 `requests`",
      "2. 核对日志",
      "",
      "```yaml",
      "replicas: 3",
      "```"
    ].join("\n")
  );
  assert.match(html, /<h2>集群检查<\/h2>/);
  assert.match(html, /class="ai-markdown-table"[^>]*tabindex="0"[^>]*><table>/);
  assert.match(html, /<thead>[\s\S]*<th>命名空间<\/th>/);
  assert.match(html, /<td><strong>web<\/strong><\/td>/);
  assert.match(html, /<td style="text-align:right">250m<\/td>/);
  assert.match(html, /<ol>[\s\S]*<li>查看 <code>requests<\/code><\/li>/);
  assert.match(
    html,
    /<pre><code class="language-yaml">replicas: 3\n<\/code><\/pre>/
  );
});

test("raw HTML, SVG handlers and executable links cannot become active markup", async () => {
  const html = await render(
    [
      '<script>globalThis.compromised = true</script><img src=x onerror="alert(1)">',
      '<svg onload="alert(1)"><foreignObject>test</foreignObject></svg>',
      "[JS](javascript:alert%281%29) [mixed](JaVaScRiPt:alert%281%29)",
      "[entity](javascript&#58;alert%281%29) [control](java&#x0a;script:alert%281%29)",
      "[data](data:text/html,hello) [file](file:///etc/passwd)",
      '![remote](https://example.com/tracking.svg "image")'
    ].join("\n\n")
  );
  assert.doesNotMatch(html, /<(?:script|img|svg|iframe|object|embed)\b/i);
  assert.doesNotMatch(html, /\bhref=/i);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /remote/);
});

test("safe links have escaped attributes and cannot control the opener", async () => {
  const html = await render(
    '[Kubernetes](https://kubernetes.io/docs/?a=1&b=2 "docs") ' +
      "[邮箱](mailto:ops@example.com) [位置](#pods) " +
      "https://kubernetes.io"
  );
  assert.match(html, /href="https:\/\/kubernetes.io\/docs\/\?a=1&amp;b=2"/);
  assert.match(html, /href="mailto:ops@example.com"/);
  assert.match(html, /href="#pods"/);
  const links = html.match(/<a\b[^>]*>/g);
  assert.equal(links.length, 4);
  for (const link of links) {
    assert.match(link, /target="_blank"/);
    assert.match(link, /rel="noopener noreferrer"/);
  }
  assert.equal(markdown.isSafeMarkdownLink("https://x\u0000.test"), false);
  assert.equal(markdown.isSafeMarkdownLink("https:\\evil.test"), false);
  assert.equal(markdown.isSafeMarkdownLink("//evil.test"), false);
});

test("streaming partial fences, tables and attack fragments remain safe on every update", async () => {
  const source =
    "| 资源 | 状态 |\n| --- | --- |\n| web | 就绪 |\n\n```yaml\n<script>x</script>\n```\n\n[危险](javascript:alert%281%29)";
  for (let end = 1; end <= source.length; end++) {
    const html = await render(source.slice(0, end));
    assert.doesNotMatch(html, /<(?:script|img|svg)\b/i);
    assert.doesNotMatch(html, /href="javascript:/i);
  }
  const complete = await render(source);
  assert.match(complete, /<table>/);
  assert.match(complete, /&lt;script&gt;x&lt;\/script&gt;/);
  assert.equal(await render(""), '<div class="ai-markdown"></div>');
});
