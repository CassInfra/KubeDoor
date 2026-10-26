const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

function compileModule(filename) {
  const target = path.resolve(__dirname, filename);
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  instance._compile(
    transformSync(fs.readFileSync(target, "utf8"), {
      loader: "ts",
      target: "es2015",
      format: "cjs"
    }).code,
    target
  );
  return instance.exports;
}

const mcp = compileModule("../src/utils/aiMcp.ts");

function form(overrides = {}) {
  return {
    ...mcp.emptyMcpForm(),
    name: "jira",
    url: "https://mcp.example.com/mcp?key=abc",
    ...overrides
  };
}

const header = (name, value = "v", saved = null) => ({ name, value, saved });

test("a valid form becomes a trimmed request without empty header rows", () => {
  const value = form({
    name: " jira ",
    url: " https://x/mcp ",
    headers: [header(" Authorization ", " Bearer t "), header("", "")]
  });
  assert.equal(mcp.validateMcpForm(value), "");
  assert.deepEqual(mcp.mcpPayload(value), {
    name: "jira",
    transport: "streamable_http",
    url: "https://x/mcp",
    headers: [{ name: "Authorization", value: "Bearer t" }],
    approval: "readonly",
    enabled: true
  });
});

test("names, URLs and headers follow the AI service rules", () => {
  const cases = [
    [{ name: "a b" }, "名称只能用"],
    [{ name: "" }, "名称只能用"],
    [{ name: "x".repeat(41) }, "名称只能用"],
    [{ name: "a/b" }, "名称只能用"],
    [{ url: "ftp://x/mcp" }, "URL"],
    [{ url: "https://u:p@x/mcp" }, "URL"],
    [{ url: "https://x/mcp#part" }, "URL"],
    [{ url: "x/mcp" }, "URL"],
    [{ url: "https://x/ mcp" }, "URL"],
    [{ headers: [header("Host")] }, "MCP 客户端自己设置"],
    [{ headers: [header("Mcp-Session-Id")] }, "MCP 客户端自己设置"],
    [{ headers: [header("X-A", "1"), header("x-a", "2")] }, "重复"],
    [{ headers: [header("X A")] }, "请求头名称"],
    [{ headers: [header("X-A", "")] }, "请填写请求头 X-A"],
    [{ headers: [header("X-A", "a\nInjected: 1")] }, "换行"],
    [
      {
        headers: Array.from({ length: 21 }, (_, index) => header(`X-${index}`))
      },
      "最多"
    ]
  ];
  for (const [overrides, message] of cases)
    assert.match(
      mcp.validateMcpForm(form(overrides)),
      new RegExp(message),
      JSON.stringify(overrides)
    );
  assert.equal(mcp.validateMcpForm(form({ name: "高德地图" })), "");
  assert.equal(mcp.validateMcpForm(form({ name: "jira-prod.v2" })), "");
  // 只测试连接时不看名称
  assert.equal(mcp.validateMcpForm(form({ name: "" }), false), "");
});

test("editing keeps saved header values unless the header is renamed", () => {
  const value = mcp.mcpFormFrom({
    id: "1",
    name: "jira",
    transport: "sse",
    url: "https://x/sse",
    headers: [{ name: "Authorization" }],
    enabled: false,
    approval: "always",
    tools: []
  });
  assert.deepEqual(value.headers, [
    { name: "Authorization", value: "", saved: "Authorization" }
  ]);
  assert.deepEqual(
    [value.transport, value.enabled, value.approval],
    ["sse", false, "always"]
  );
  assert.equal(mcp.validateMcpForm(value), "");
  // 值留空发给服务端，表示沿用已保存的
  assert.deepEqual(mcp.mcpPayload(value).headers, [
    { name: "Authorization", value: "" }
  ]);
  value.headers[0].name = "authorization";
  assert.equal(mcp.validateMcpForm(value), "");
  value.headers[0].name = "X-Token";
  assert.match(mcp.validateMcpForm(value), /请填写请求头 X-Token/);
});

test("connection status and approval modes explain what will happen", () => {
  const server = {
    tools: [{ name: "a", read_only: true }],
    tools_at: "2026-10-26T00:00:00Z",
    last_error: null
  };
  assert.deepEqual(mcp.mcpStatus(server), {
    type: "success",
    label: "1 个工具",
    detail: ""
  });
  assert.equal(
    mcp.mcpStatus({ ...server, last_error: "HTTP 401" }).type,
    "warning"
  );
  assert.deepEqual(
    mcp.mcpStatus({ ...server, tools: [], last_error: "HTTP 401" }),
    {
      type: "danger",
      label: "连接失败",
      detail: "HTTP 401"
    }
  );
  assert.equal(
    mcp.mcpStatus({ ...server, tools: [], tools_at: null }).label,
    "未连接"
  );
  assert.deepEqual(
    [
      ["readonly", true],
      ["readonly", false],
      ["never", false],
      ["always", true]
    ].map(([mode, readOnly]) => mcp.mcpNeedsApproval(mode, readOnly)),
    [false, true, false, true]
  );
  assert.equal(mcp.transportLabel("sse"), "SSE");
  assert.equal(mcp.transportLabel("streamable_http"), "Streamable HTTP");
  assert.equal(mcp.approvalLabel("never"), "全部自动执行");
});

test("the list masks query values such as keys in the URL", () => {
  assert.equal(
    mcp.maskedUrl("https://mcp.amap.com/sse?key=secret&v=2"),
    "https://mcp.amap.com/sse?key=***&v=***"
  );
  assert.equal(mcp.maskedUrl("https://x/mcp"), "https://x/mcp");
  assert.equal(mcp.maskedUrl("not a url"), "not a url");
});
