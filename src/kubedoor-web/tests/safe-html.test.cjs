const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const { test } = require("node:test");
const { pathToFileURL } = require("node:url");

const webRoot = path.resolve(__dirname, "..");
const safeHtmlPath = path.join(webRoot, "src/utils/safeHtml.ts");

let cachedSafeHtmlPromise = null;

async function loadSafeHtml() {
  if (!cachedSafeHtmlPromise) {
    cachedSafeHtmlPromise = (async () => {
      try {
        return await import(pathToFileURL(safeHtmlPath).href);
      } catch (nativeErr) {
        try {
          const esbuild = Module.createRequire(
            require.resolve("vite/package.json")
          )("esbuild");
          const source = fs.readFileSync(safeHtmlPath, "utf8");
          const { code } = esbuild.transformSync(source, {
            loader: "ts",
            target: "es2020",
            format: "cjs"
          });
          const loaded = new Module(safeHtmlPath, module);
          loaded.paths = Module._nodeModulePaths(path.dirname(safeHtmlPath));
          loaded._compile(code, safeHtmlPath);
          return loaded.exports;
        } catch {
          throw nativeErr;
        }
      }
    })();
  }
  return cachedSafeHtmlPromise;
}

function readWebFile(relPath) {
  return fs.readFileSync(path.join(webRoot, relPath), "utf8");
}

function createMockDocument() {
  let innerHtmlAssignments = 0;

  class MockTextNode {
    constructor(data) {
      this.nodeType = 3;
      this.data = String(data);
    }

    get textContent() {
      return this.data;
    }

    set textContent(value) {
      this.data = String(value);
    }
  }

  class MockElement {
    constructor(tagName) {
      this.nodeType = 1;
      this.tagName = tagName.toUpperCase();
      this.children = [];
      this.style = {};
      this._text = null;
    }

    appendChild(child) {
      this.children.push(child);
      return child;
    }

    get textContent() {
      if (this.children.length > 0) {
        return this.children.map(c => c.textContent).join("");
      }
      return this._text ?? "";
    }

    set textContent(value) {
      this.children = [];
      this._text = String(value);
    }

    get innerHTML() {
      throw new Error("innerHTML getter should not be accessed");
    }

    set innerHTML(_value) {
      innerHtmlAssignments += 1;
      throw new Error("innerHTML setter must never be called");
    }
  }

  return {
    getInnerHtmlAssignments: () => innerHtmlAssignments,
    document: {
      createElement: tag => new MockElement(tag),
      createTextNode: text => new MockTextNode(text)
    }
  };
}

test("escapeHtml escapes HTML meta-characters and handles edge primitives", async () => {
  const { escapeHtml } = await loadSafeHtml();

  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(0), "0");
  assert.equal(escapeHtml(false), "false");
  assert.equal(escapeHtml(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
  assert.equal(
    escapeHtml(`<img src=x onerror="alert('xss')">&test`),
    "&lt;img src=x onerror=&quot;alert(&#39;xss&#39;)&quot;&gt;&amp;test"
  );
});

test("highlightAnsiHtml returns original string when keyword is empty or whitespace", async () => {
  const { highlightAnsiHtml } = await loadSafeHtml();
  const sample = '<span style="color:rgb(255,85,85)">Error</span>';

  assert.equal(highlightAnsiHtml(sample, "", "search-highlight"), sample);
  assert.equal(highlightAnsiHtml(sample, "   \t\n", "search-highlight"), sample);
  assert.equal(highlightAnsiHtml("", "err", "search-highlight"), "");
});

test("highlightAnsiHtml never corrupts ANSI tags, CSS attributes, or HTML entities", async () => {
  const { highlightAnsiHtml } = await loadSafeHtml();
  const ansiHtml =
    '<span style="color:rgb(255,85,85)">span style color rgb &lt;script&gt; &quot;amp&quot; &amp; lt</span>';

  for (const kw of ["span", "style", "color", "rgb"]) {
    const out = highlightAnsiHtml(ansiHtml, kw, "search-highlight");
    assert.ok(
      out.startsWith('<span style="color:rgb(255,85,85)">'),
      `expected ANSI span tag preserved for keyword "${kw}", got: ${out}`
    );
    assert.ok(out.endsWith("</span>"));
    assert.match(out, new RegExp(`<span class="search-highlight">${kw}</span>`));
  }

  const ltOut = highlightAnsiHtml(
    '<span style="color:rgb(0,255,0)">&lt;tag&gt; and lt literal</span>',
    "lt",
    "search-highlight"
  );
  assert.equal(
    ltOut,
    '<span style="color:rgb(0,255,0)">&lt;tag&gt; and <span class="search-highlight">lt</span> literal</span>'
  );

  const ampOut = highlightAnsiHtml(
    '<span style="color:rgb(0,255,0)">a &amp; b amp</span>',
    "amp",
    "search-highlight"
  );
  assert.equal(
    ampOut,
    '<span style="color:rgb(0,255,0)">a &amp; b <span class="search-highlight">amp</span></span>'
  );

  const rawAmpOut = highlightAnsiHtml(
    '<span style="color:rgb(0,255,0)">a &amp; b</span>',
    "&",
    "search-highlight"
  );
  assert.equal(
    rawAmpOut,
    '<span style="color:rgb(0,255,0)">a <span class="search-highlight">&amp;</span> b</span>'
  );

  const literalEntityOut = highlightAnsiHtml("&amp;lt;", "lt", "search-highlight");
  assert.equal(literalEntityOut, '&amp;<span class="search-highlight">lt</span>;');
});

test("highlightAnsiHtml prevents XSS and preserves case-insensitive matches", async () => {
  const { highlightAnsiHtml } = await loadSafeHtml();

  const xssLog =
    '<span style="color:rgb(255,0,0)">&lt;img src=x onerror=alert(1)&gt; &lt;script&gt;alert(2)&lt;/script&gt;</span>';
  const imgMatch = highlightAnsiHtml(
    xssLog,
    "<img src=x onerror=alert(1)>",
    "search-highlight-current"
  );
  assert.doesNotMatch(imgMatch, /<img\b/i);
  assert.match(
    imgMatch,
    /<span class="search-highlight-current">&lt;img src=x onerror=alert\(1\)&gt;<\/span>/
  );

  const scriptMatch = highlightAnsiHtml(xssLog, "<script>", "search-highlight");
  assert.doesNotMatch(scriptMatch, /<script\b/i);
  assert.match(
    scriptMatch,
    /<span class="search-highlight">&lt;script&gt;<\/span>/
  );

  const caseOut = highlightAnsiHtml(
    "Fatal ERROR and error and Error",
    "ErRoR",
    'cls" onclick="alert(1)'
  );
  assert.doesNotMatch(caseOut, /"\s+onclick=/i);
  assert.equal(
    caseOut,
    'Fatal <span class="cls&quot; onclick=&quot;alert(1)">ERROR</span> and ' +
      '<span class="cls&quot; onclick=&quot;alert(1)">error</span> and ' +
      '<span class="cls&quot; onclick=&quot;alert(1)">Error</span>'
  );

  const regexCharsOut = highlightAnsiHtml(
    "pod [web-1].* ready",
    "[web-1].*",
    "search-highlight"
  );
  assert.equal(
    regexCharsOut,
    'pod <span class="search-highlight">[web-1].*</span> ready'
  );
});

test("buildNodeOptionLabel uses DOM text APIs with zero innerHTML", async t => {
  const { buildNodeOptionLabel } = await loadSafeHtml();
  const prevDocument = global.document;
  const mock = createMockDocument();
  global.document = mock.document;
  t.after(() => {
    global.document = prevDocument;
  });

  const label = buildNodeOptionLabel(
    '<img src=x onerror="alert(1)">',
    "<script>85%</script>",
    2
  );
  assert.equal(mock.getInnerHtmlAssignments(), 0);
  assert.equal(label.tagName, "SPAN");
  assert.equal(
    label.textContent,
    '<img src=x onerror="alert(1)"> (<script>85%</script>，2Pod)'
  );
  assert.equal(label.children[1].style.color, "blue");
  assert.equal(label.children[3].style.color, "red");

  const zeroDefault = buildNodeOptionLabel("node-a", "40%", 0);
  assert.equal(zeroDefault.children[3].style.color, undefined);
  assert.equal(zeroDefault.textContent, "node-a (40%，0Pod)");

  const zeroInherit = buildNodeOptionLabel("node-b", "5", 0, "inherit");
  assert.equal(zeroInherit.children[3].style.color, "inherit");

  const zeroCssDecl = buildNodeOptionLabel("node-c", "5", 0, "color: inherit;");
  assert.equal(zeroCssDecl.children[3].style.color, "inherit");
  assert.equal(mock.getInnerHtmlAssignments(), 0);
});

test("static regression check confirms unsafe HTML sinks were removed across all target files", () => {
  const resultDialogFiles = [
    "src/views/alarm/detail.vue",
    "src/views/monit/pod/index.vue",
    "src/views/monit/index.vue"
  ];
  for (const file of resultDialogFiles) {
    const content = readWebFile(file);
    assert.doesNotMatch(content, /v-html="resultMessage"/, file);
    assert.match(content, /\{\{\s*resultMessage\s*\}\}/, file);
    assert.match(content, /resultMessage\.value\s*=\s*message\s*\?\?\s*""/, file);
  }

  const nodeSelectorFiles = [
    "src/views/monit/pod/index.vue",
    "src/views/monit/index.vue",
    "src/views/monit/scale/index.vue"
  ];
  for (const file of nodeSelectorFiles) {
    const content = readWebFile(file);
    assert.doesNotMatch(content, /label\.innerHTML/, file);
    assert.doesNotMatch(content, /nodeListContainer\.innerHTML/, file);
    assert.match(content, /nodeListContainer\.replaceChildren\(\)/, file);
    assert.match(content, /from\s+"@\/utils\/safeHtml"/, file);
    assert.match(content, /\bbuildNodeOptionLabel\(/, file);
  }

  const scaleViewFiles = [
    "src/views/monit/scale/index.vue",
    "src/views/resource/scale/index.vue"
  ];
  for (const file of scaleViewFiles) {
    const content = readWebFile(file);
    assert.doesNotMatch(content, /v-html="props\.content"/, file);
    assert.doesNotMatch(content, /\bv-html=/, file);
    assert.match(content, /props\.items/, file);
  }

  const monitHook = readWebFile("src/views/monit/utils/hook.tsx");
  assert.doesNotMatch(monitHook, /dangerouslyUseHTMLString\s*:\s*true/);
  assert.match(monitHook, /h\(\s*"div"/);
  assert.doesNotMatch(monitHook, /<strong\s+style=/);

  const resourceHook = readWebFile("src/views/resource/utils/hook.tsx");
  assert.doesNotMatch(resourceHook, /<strong\s+style=/);

  const overview = readWebFile("src/views/monit/overview/index.vue");
  assert.match(overview, /import\s+\{\s*escapeHtml\s*\}\s+from\s+"@\/utils\/safeHtml"/);
  assert.match(overview, /escapeHtml\(params\.name\)/);
  assert.match(overview, /escapeHtml\(d\.env \|\| "--"\)/);
  assert.match(overview, /escapeHtml\(d\.namespace \|\| "--"\)/);
  assert.match(overview, /escapeHtml\(d\.kind \|\| "--"\)/);
  assert.match(overview, /escapeHtml\(d\.resourceName \|\| "--"\)/);
  assert.match(overview, /escapeHtml\(d\.ratio \|\| params\.percent\)/);
  assert.match(overview, /escapeHtml\(d\.message \|\| "--"\)/);

  const logHighlighterFiles = [
    "src/views/monit/components/PodLogViewer.vue",
    "src/views/monit/daemon/index.vue",
    "src/views/monit/stateful/index.vue"
  ];
  for (const file of logHighlighterFiles) {
    const content = readWebFile(file);
    assert.match(content, /import\s+\{\s*highlightAnsiHtml\s*\}\s+from\s+"@\/utils\/safeHtml"/, file);
    assert.match(content, /\bhighlightAnsiHtml\(/, file);
    assert.doesNotMatch(content, /processedMessage\.replace\(/, file);
  }
});
