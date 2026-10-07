const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");
const { createPinia } = require("pinia");

const utilsPath = path.resolve(__dirname, "../src/utils/ai.ts");
const utilityModule = new Module(utilsPath, module);
utilityModule._compile(
  transformSync(fs.readFileSync(utilsPath, "utf8"), {
    loader: "ts",
    target: "es2015",
    format: "cjs"
  }).code,
  utilsPath
);
const utils = utilityModule.exports;

test("SSE survives byte boundaries, CRLF, heartbeat and multiline JSON data", () => {
  const frames = [];
  const parser = utils.createSseParser(frame => frames.push(frame));
  const source =
    '\uFEFF: heartbeat\r\nid: 12\r\nevent: token\r\ndata: {"seq":12,\r\ndata: "content":"集群🛠"}\r\n\r\n';
  const bytes = new TextEncoder().encode(source);
  const decoder = new TextDecoder();
  for (const byte of bytes)
    parser.push(decoder.decode(Uint8Array.of(byte), { stream: true }));
  parser.push(decoder.decode());
  parser.finish();
  assert.equal(frames.length, 1);
  assert.equal(frames[0].id, "12");
  assert.equal(frames[0].event, "token");
  assert.deepEqual(JSON.parse(frames[0].data), { seq: 12, content: "集群🛠" });
});

test("SSE ignores empty frames and invalid NUL ids, and retains the last id", () => {
  const frames = [];
  const parser = utils.createSseParser(frame => frames.push(frame));
  parser.push(
    "id: 3\ndata: first\n\n\n: ping\n\nid: bad\0id\ndata: second\n\n"
  );
  parser.finish();
  assert.deepEqual(
    frames.map(frame => [frame.id, frame.data]),
    [
      ["3", "first"],
      ["3", "second"]
    ]
  );
});

test("cross-namespace resources round-trip without a name collision", () => {
  const a = { namespace: "production", name: "web" };
  const b = { namespace: "staging", name: "web" };
  assert.notEqual(utils.resourceKey(a), utils.resourceKey(b));
  assert.deepEqual(utils.parseResourceKey(utils.resourceKey(a)), a);
  assert.equal(utils.parseResourceKey(""), null);
  assert.equal(utils.parseResourceKey('["ns",null]'), null);
  assert.equal(utils.parseResourceKey("not-json"), null);
});

test("persisted uncertain actions retain source/freshness and display their real error", () => {
  const tool = utils.normalizeAITool(
    {
      action_id: "action-1",
      name: "patch",
      state: "unknown",
      source: "direct",
      arguments: { namespace: "app", name: "web" },
      preview: { diff: "- old\n+ new" },
      result: {
        success: false,
        observed_at: "2026-10-05T10:00:00Z",
        freshness: "live",
        truncated: true,
        error: {
          code: "outcome_unknown",
          message: "请求已提交，需要查询实际状态"
        }
      }
    },
    "cluster-a"
  );
  assert.equal(tool.id, "action-1");
  assert.equal(tool.status, "unknown");
  assert.equal(tool.env, "cluster-a");
  assert.equal(tool.freshness, "live");
  assert.equal(tool.truncated, true);
  assert.equal(tool.diff, "- old\n+ new");
  assert.match(tool.error, /查询实际状态/);
  assert.equal(
    utils.normalizeAITool({ state: "succeeded" }).status,
    "completed"
  );
});

test("credential field rendering is redacted and browser profiles are username-specific", () => {
  const text = utils.safeAIText({
    api_key: "secret-key",
    nested: { authorization: "Bearer secret", password: "pass" },
    normal: "public"
  });
  assert.ok(!text.includes("secret-key"));
  assert.ok(!text.includes("Bearer secret"));
  assert.match(text, /public/);
  assert.notEqual(
    utils.providerStorageKey("alice"),
    utils.providerStorageKey("bob")
  );
});

function loadStore(api, streams) {
  const storePath = path.resolve(__dirname, "../src/store/modules/ai.ts");
  const compiled = transformSync(fs.readFileSync(storePath, "utf8"), {
    loader: "ts",
    target: "es2015",
    format: "cjs"
  }).code;
  const storeModule = new Module(storePath, module);
  storeModule.paths = Module._nodeModulePaths(path.dirname(storePath));
  const originalRequire = storeModule.require.bind(storeModule);
  const pinia = createPinia();
  storeModule.require = id => {
    if (id === "@/store") return { store: pinia };
    if (id === "@/utils/ai") return utils;
    if (id === "@/api/ai")
      return {
        aiApi: api,
        AIRequestError: class AIRequestError extends Error {},
        streamAIEvents: (id, after, signal, onEvent) =>
          new Promise(resolve => {
            streams.set(id, onEvent);
            signal.addEventListener("abort", resolve, { once: true });
          })
      };
    return originalRequire(id);
  };
  storeModule._compile(compiled, storePath);
  return storeModule.exports.useAIStore(pinia);
}

test("runs lock the cluster snapshot, drain cancellation, and permit switching within one session", async () => {
  const previousWindow = global.window;
  const previousStorage = global.localStorage;
  global.window = { setTimeout, clearTimeout };
  const saved = new Map();
  global.localStorage = {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key)
  };
  const streams = new Map();
  const requests = [];
  const sessions = [];
  let username = "alice";
  let externalRun = null;
  const api = {
    bootstrap: async () => ({
      enabled: true,
      username,
      permission: "rw",
      skills: [],
      clusters: [
        { env: "a", online: true },
        { env: "b", online: true },
        { env: "archived", online: false, configured: false }
      ]
    }),
    sessions: async () => ({ sessions }),
    createSession: async () => {
      const item = { id: "session-1", title: "新会话" };
      sessions.push(item);
      return item;
    },
    session: async () => ({
      id: "session-1",
      messages: [],
      active_run: externalRun
    }),
    run: async (id, body) => {
      requests.push({ id, body });
      return { run_id: `run-${requests.length}`, status: "running" };
    },
    cancel: async id => {
      streams.get(id)?.({
        seq: 99,
        type: "status",
        data: { status: "cancelled" }
      });
      return { status: "cancelled" };
    }
  };
  try {
    const ai = loadStore(api, streams);
    await ai.open();
    ai.saveProvider({
      base_url: "https://example.test/v1",
      api_key: "key-alice",
      model: "tool-model"
    });
    await Promise.all([ai.send("查询服务"), ai.send("重复点击发送")]);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].body.scope.env, "a");
    assert.equal(ai.busy, true);
    streams.get("run-1")({
      seq: 1,
      type: "token",
      data: { content: "已查询" }
    });
    streams.get("run-1")({
      seq: 1,
      type: "token",
      data: { content: "已查询" }
    });
    assert.equal(ai.messages.at(-1).content, "已查询");
    streams.get("run-1")({
      seq: 2,
      type: "approval",
      data: { action_id: "action-1", name: "patch", arguments: { name: "web" } }
    });
    assert.equal(ai.pendingTools.length, 1);
    streams.get("run-1")({
      seq: 3,
      type: "tool_start",
      data: { action_id: "action-1", name: "patch", source: "agent" }
    });
    assert.equal(ai.pendingTools.length, 0);
    streams.get("run-1")({
      seq: 4,
      type: "tool_result",
      data: {
        action_id: "action-1",
        name: "patch",
        result: {
          success: false,
          error: { code: "outcome_unknown", message: "需要核验实际状态" }
        }
      }
    });
    assert.equal(ai.messages.at(-1).tools.at(-1).status, "unknown");
    ai.changeScope({ env: "b", namespace: null, deployment: null, pod: null });
    assert.equal(ai.scope.env, "a");
    await ai.cancel();
    assert.equal(ai.busy, false);
    ai.changeScope({ env: "b", namespace: null, deployment: null, pod: null });
    await ai.send("继续查询另一个集群");
    assert.equal(requests[1].id, requests[0].id);
    assert.equal(requests[1].body.scope.env, "b");
    assert.equal(requests[0].body.scope.env, "a");
    await ai.cancel();
    ai.changeScope({
      env: "archived",
      namespace: null,
      deployment: null,
      pod: null
    });
    assert.equal(ai.clusterAvailable, true);
    assert.equal(ai.liveAvailable, false);
    await ai.send("查看离线集群的资源历史");
    assert.equal(requests[2].body.scope.env, "archived");
    await ai.cancel();
    externalRun = {
      id: "external-run",
      status: "waiting_approval",
      scope: { env: "a", namespace: null, deployment: null, pod: null },
      pending_actions: [
        {
          action_id: "external-action",
          name: "patch",
          source: "agent",
          arguments: { _preconditions: { resourceVersion: "101" } }
        }
      ]
    };
    await ai.open("session-1");
    assert.equal(ai.activeRun.id, "external-run");
    assert.equal(ai.pendingTools[0].action_id, "external-action");
    assert.equal(
      ai.pendingTools[0].arguments._preconditions.resourceVersion,
      "101"
    );
    await ai.cancel();
    externalRun = null;
    username = "bob";
    await ai.open();
    assert.equal(ai.provider.api_key, "");
    assert.equal(ai.messages.length, 0);
    assert.equal(
      saved.get(utils.providerStorageKey("alice")) !== undefined,
      true
    );
  } finally {
    global.window = previousWindow;
    global.localStorage = previousStorage;
  }
});
