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

test("context window prefill follows model-name rules and keeps hand-edited values", () => {
  const K = 1024;
  assert.deepEqual(utils.inferModelLimits("deepseek-flash"), {
    max_input_tokens: 1024 * K,
    max_output_tokens: 384 * K,
    compression_ratio: 0.5,
    verify: false
  });
  assert.equal(
    utils.inferModelLimits(" DeepSeek/DeepSeek-V4-Pro ").max_output_tokens,
    384 * K
  );
  assert.deepEqual(
    [
      utils.inferModelLimits("gpt-5.5").max_output_tokens,
      utils.inferModelLimits("gpt-5.5").verify
    ],
    [128 * K, false]
  );
  // gpt-4o 等旧型号和其他模型同样按 1024K / 128K 预填，但提示按实际填写
  for (const model of ["gpt-4o-mini", "qwen3", "openrouter/claude"])
    assert.equal(utils.inferModelLimits(model).verify, true);

  // 已保存的值优先；旧数据没有这些字段时按模型名预填
  assert.deepEqual(utils.modelLimitsFor("deepseek-flash", null), {
    max_input_tokens: 1024 * K,
    max_output_tokens: 384 * K,
    compression_ratio: 0.5
  });
  assert.deepEqual(
    utils.modelLimitsFor("qwen3", {
      max_input_tokens: 128 * K,
      max_output_tokens: null,
      compression_ratio: 0.6
    }),
    {
      max_input_tokens: 128 * K,
      max_output_tokens: 128 * K,
      compression_ratio: 0.6
    }
  );

  // 改模型名：仍是旧模型默认值的项跟着变，手动改过的保持不变
  const prefilled = utils.modelLimitsFor("gpt-5.5", null);
  assert.equal(
    utils.followModelLimits(prefilled, "gpt-5.5", "deepseek-flash")
      .max_output_tokens,
    384 * K
  );
  const edited = {
    ...prefilled,
    max_output_tokens: 64 * K,
    compression_ratio: 0.7
  };
  assert.deepEqual(
    utils.followModelLimits(edited, "gpt-5.5", "deepseek-flash"),
    edited
  );
});

test("context share is the latest prompt over the model window", () => {
  assert.equal(
    utils.contextShare({
      last_input_tokens: 262144,
      max_input_tokens: 1048576
    }),
    0.25
  );
  assert.equal(utils.CONTEXT_HINT_RATIO, 0.25);
  // 旧会话、MCP 或还没跑完第一轮：没有用量就不显示
  for (const usage of [
    null,
    undefined,
    {},
    { last_input_tokens: 0, max_input_tokens: 1048576 },
    { last_input_tokens: 1000 }
  ])
    assert.equal(utils.contextShare(usage), null);
});

test("control plans and read-back verification render from the approval preview and result", () => {
  const preview = {
    operation: "scale_deployment",
    plan: {
      control: {
        state: "controlled",
        controlled: true,
        blocked: false,
        message: "受花折资源管控"
      },
      steps: [
        {
          kind: "master",
          name: "写入管控副本数",
          method: "POST",
          path: "/api/db/res/pod_count",
          query: { env: "prod" },
          body: { pod_count_manual: 5 }
        },
        {
          kind: "k8s",
          name: "修改 Deployment",
          source: "agent",
          arguments: { method: "PATCH" },
          diff: "--- current\n+++ proposed"
        },
        "broken"
      ],
      changes: [
        {
          scope: "在线",
          field: "replicas",
          label: "副本数",
          current: "2",
          target: "5"
        },
        null
      ],
      effects: ["立即扩缩到 5 个副本", 1],
      warnings: []
    }
  };
  const plan = utils.controlPlan(preview);
  assert.deepEqual(
    plan.steps.map(step => step.target),
    ["POST /api/db/res/pod_count", "直接修改 Deployment（经 Agent）"]
  );
  assert.deepEqual(plan.steps[0].request, {
    query: { env: "prod" },
    body: { pod_count_manual: 5 }
  });
  assert.equal(plan.steps[1].diff, "--- current\n+++ proposed");
  assert.deepEqual(plan.changes, [
    { scope: "在线", label: "副本数", current: "2", target: "5" }
  ]);
  assert.deepEqual(plan.effects, ["立即扩缩到 5 个副本"]);
  assert.deepEqual(utils.controlBadge(plan), {
    label: "受花折资源管控",
    type: "warning"
  });
  assert.equal(
    utils.controlBadge({ controlled: false, blocked: true }).type,
    "danger"
  );
  assert.equal(utils.controlPlan({ command: ["kubectl"] }), null);
  assert.equal(utils.controlPlan("text"), null);
  const verification = utils.verificationOf({
    verification: {
      checked: true,
      matched: false,
      message: "副本数 期望 5，实际 2"
    }
  });
  assert.deepEqual(verification, {
    checked: true,
    matched: false,
    message: "副本数 期望 5，实际 2"
  });
  assert.equal(utils.verificationOf({ success: true }), null);
  assert.equal(
    utils.verificationOf({
      verification: { checked: false, message: "读取不到" }
    }).matched,
    null
  );
});

test("ask-AI entries preselect the resource and prefill a question with the source facts", () => {
  const alert = utils.alertQuestion({
    env: "prod",
    namespace: "demo",
    pod: "order-7d9-x",
    alert_name: "PodOOM",
    severity: "critical",
    alert_status: "firing",
    description: "内存超限",
    count_firing: 3
  });
  // 告警只知道 Pod、不知道所属 Deployment：逐级的资源选择选不上 Pod，只选到命名空间，Pod 写进问题
  assert.deepEqual(alert.scope, { env: "prod", namespace: "demo" });
  assert.match(
    alert.prompt,
    /告警：PodOOM\n级别：critical\n状态：firing\nPod：order-7d9-x\n触发次数：3\n描述：内存超限/
  );
  const event = utils.eventQuestion("prod", [
    "ADDED",
    "Warning",
    5,
    "Deployment",
    "",
    "demo",
    "order",
    "FailedCreate",
    "quota exceeded",
    "t1",
    "t2"
  ]);
  assert.deepEqual(event.scope, {
    env: "prod",
    namespace: "demo",
    deployment: { namespace: "demo", name: "order" }
  });
  assert.match(
    event.prompt,
    /资源：Deployment demo\/order\n级别：Warning\n原因：FailedCreate\n次数：5/
  );
  const clusterEvent = utils.eventQuestion("prod", [
    "ADDED",
    "Normal",
    1,
    "Node",
    "",
    "",
    "node-1",
    "Ready",
    "ok",
    "",
    ""
  ]);
  assert.deepEqual(clusterEvent.scope, {
    env: "prod",
    namespace: null,
    deployment: null
  });
  assert.match(clusterEvent.prompt, /资源：Node node-1/);
  assert.deepEqual(
    utils.podQuestion("prod", "demo", "order-1", "order").scope.deployment,
    { namespace: "demo", name: "order" }
  );
  assert.deepEqual(
    utils.podQuestion("prod", "demo", "order-1", "order").scope.pod,
    { namespace: "demo", name: "order-1" }
  );
  // 不属于 Deployment 的 Pod（StatefulSet 等）同样只选到命名空间
  const orphan = utils.podQuestion("prod", "demo", "order-1");
  assert.equal(orphan.scope.deployment, null);
  assert.equal(orphan.scope.pod, null);
  assert.match(
    utils.deploymentQuestion("prod", "demo", "order").prompt,
    /^检查 Deployment demo\/order/
  );
});
