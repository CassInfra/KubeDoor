const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { createPinia } = require("pinia");
const { nextTick } = require("vue");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

// 网页 AI 助手：默认用企微专用模型 / 自定义模型的切换，以及会话分享（utils 纯函数 + store）
function compile(filename, overrides = {}) {
  const target = path.resolve(__dirname, filename);
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  const original = instance.require.bind(instance);
  instance.require = id =>
    Object.hasOwn(overrides, id) ? overrides[id] : original(id);
  instance._compile(
    transformSync(fs.readFileSync(target, "utf8"), {
      loader: "ts",
      format: "cjs",
      target: "es2015"
    }).code,
    target
  );
  return instance.exports;
}
const utils = compile("../src/utils/ai.ts");

const SHARED = {
  available: true,
  base_url: "https://api.deepseek.com",
  model: "deepseek-chat",
  max_input_tokens: null,
  max_output_tokens: null,
  compression_ratio: null,
  reasoning_effort: null,
  api_key_set: true
};
const UNAVAILABLE = { ...SHARED, available: false, base_url: "", model: "" };
const CUSTOM = {
  base_url: "https://model.example/v1",
  api_key: "key-alice",
  model: "tool-model",
  max_input_tokens: null,
  max_output_tokens: null,
  compression_ratio: null,
  reasoning_effort: null
};
const scope = env => ({ env, namespace: null, deployment: null, pod: null });
const flush = async () => {
  for (let i = 0; i < 3; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
};

test("没在模型设置里选过时：企微专用模型可用就用它，不可用才退回浏览器里的自定义配置", () => {
  const custom = { base_url: "https://x/v1", model: "m" };
  assert.equal(utils.effectiveProviderMode(null, true, custom), "wecom");
  assert.equal(utils.effectiveProviderMode(undefined, false, custom), "custom");
  assert.equal(utils.effectiveProviderMode(null, false, null), "wecom");
  assert.equal(
    utils.effectiveProviderMode(null, false, {
      base_url: "https://x",
      model: " "
    }),
    "wecom"
  );
  // 保存过的选择优先，即使企微专用模型不可用 / 没有自定义配置
  assert.equal(utils.effectiveProviderMode("wecom", false, custom), "wecom");
  assert.equal(utils.effectiveProviderMode("custom", true, null), "custom");
  assert.equal(utils.effectiveProviderMode("other", true, custom), "wecom");
  assert.equal(utils.savedProviderMode("custom"), "custom");
  assert.equal(utils.savedProviderMode("WECOM"), null);
  assert.equal(utils.savedProviderMode(undefined), null);
});

test("输入框下方的模型名、角色名和分享页的进行中提示", () => {
  assert.equal(
    utils.providerLabel("wecom", SHARED, CUSTOM),
    "企微专用模型 · deepseek-chat"
  );
  assert.equal(
    utils.providerLabel("wecom", UNAVAILABLE, CUSTOM),
    "企微专用模型（管理员尚未配置）"
  );
  assert.equal(
    utils.providerLabel("wecom", null, null),
    "企微专用模型（管理员尚未配置）"
  );
  assert.equal(utils.providerLabel("custom", SHARED, CUSTOM), "tool-model");
  assert.equal(
    utils.providerLabel("custom", SHARED, { base_url: "", model: "m" }),
    "尚未配置模型"
  );
  assert.deepEqual(utils.aiRoleLabel("user"), { icon: "🧑", name: "你" });
  assert.deepEqual(utils.aiRoleLabel("user", true), {
    icon: "🧑",
    name: "提问人"
  });
  assert.deepEqual(utils.aiRoleLabel("assistant"), {
    icon: "🤖",
    name: "花折 AI"
  });
  assert.deepEqual(utils.aiRoleLabel("assistant", true), {
    icon: "🤖",
    name: "花折 AI"
  });
  assert.equal(utils.aiRoleLabel("system").name, "系统");
  assert.equal(utils.shareRunNotice(null), "");
  assert.equal(utils.shareRunNotice("running"), "回答生成中…");
  assert.equal(utils.shareRunNotice("waiting_approval"), "等待审批中…");
});

test("分享链接走控制台的 hash 路由，分享码做 URL 编码", () => {
  assert.equal(
    utils.shareLink("https://kd.example.com", "/", "AbC_-123"),
    "https://kd.example.com/#/ai-share/AbC_-123"
  );
  assert.equal(
    utils.shareLink("http://10.0.0.1:8080", "/console/", "a/b"),
    "http://10.0.0.1:8080/console/#/ai-share/a%2Fb"
  );
  assert.equal(utils.shareLink("http://h", "", "x"), "http://h/#/ai-share/x");
});

test("历史消息规范化：工具按入库记录转换，记忆只留展示字段，非字符串正文按文本显示", () => {
  const [first, second] = utils.normalizeAIMessages([
    {
      id: "m1",
      role: "assistant",
      content: "完成",
      scope: scope("cluster-a"),
      tools: [{ action_id: "a1", operation: "patch", state: "pending" }],
      memories: [
        { id: "k1", title: "标题", content: "内容", version: 2, extra: "x" }
      ]
    },
    {
      id: "m2",
      role: "user",
      content: { api_key: "secret" },
      tools_hidden: true
    }
  ]);
  assert.equal(first.tools[0].id, "a1");
  assert.equal(first.tools[0].name, "patch");
  assert.equal(first.tools[0].status, "waiting_approval");
  assert.equal(first.tools[0].pending, true);
  assert.equal(first.tools[0].env, "cluster-a");
  assert.deepEqual(first.memories, [
    { id: "k1", title: "标题", content: "内容", version: 2 }
  ]);
  assert.ok(!second.content.includes("secret"));
  assert.deepEqual(second.tools, []);
  assert.equal(second.tools_hidden, true);
  assert.deepEqual(utils.normalizeAIMessages(null), []);
});

function detail(id, code) {
  return {
    id,
    title: "排查",
    share_code: code,
    shared_at: null,
    active_run: null,
    messages: [
      {
        id: `${id}-u`,
        role: "user",
        content: "问题",
        scope: scope("cluster-a")
      },
      {
        id: `${id}-a`,
        role: "assistant",
        content: "回答",
        scope: scope("cluster-a")
      }
    ]
  };
}

async function harness(
  t,
  { shared = SHARED, saved = undefined, legacy = false } = {}
) {
  const oldWindow = global.window,
    oldStorage = global.localStorage;
  global.window = { setTimeout, clearTimeout };
  const storage = new Map();
  global.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key)
  };
  if (saved !== undefined)
    storage.set(utils.providerStorageKey("alice"), JSON.stringify(saved));
  const state = {
    username: "alice",
    shared,
    shares: new Map(),
    detail: null,
    setShare: null,
    sessions: [{ id: "s1", title: "排查" }]
  };
  const calls = { run: [], title: [], summary: [], decision: [], share: [] };
  const streams = new Map();
  const api = {
    bootstrap: async () => ({
      username: state.username,
      enabled: true,
      permission: "rw",
      is_admin: false,
      skills: [],
      clusters: [{ env: "cluster-a", online: true }],
      // legacy：旧版 AI 服务，bootstrap 没有 shared_model
      ...(legacy ? {} : { shared_model: state.shared })
    }),
    // 列表上的分享标记按服务端当前状态返回
    sessions: async () => ({
      sessions: state.sessions.map(item => ({
        ...item,
        shared: state.shares.has(item.id)
      }))
    }),
    session: async id =>
      state.detail
        ? state.detail(id)
        : detail(id, state.shares.get(id) ?? null),
    createSession: async () => ({ id: "s-new", title: "新会话" }),
    run: async (id, body) => {
      calls.run.push({ id, body });
      return { run_id: `run-${calls.run.length}`, status: "running" };
    },
    generateSessionTitle: async (id, message, provider) => {
      calls.title.push({ id, message, provider });
      return { id, title: "新会话", generated: false };
    },
    summarizeMemory: async (id, provider) => {
      calls.summary.push({ id, provider });
      return { title: "t", content: "c" };
    },
    decision: async (runId, actionId, decision, provider) => {
      calls.decision.push({ runId, actionId, decision, provider });
      return { status: "running" };
    },
    setShare: async (id, enabled) => {
      calls.share.push({ id, enabled });
      if (state.setShare) return state.setShare(id, enabled);
      const code = enabled
        ? state.shares.get(id) || `code-${calls.share.length}`
        : null;
      if (code) state.shares.set(id, code);
      else state.shares.delete(id);
      return {
        id,
        shared: !!code,
        share_code: code,
        shared_at: code ? "2026-11-23T10:00:00Z" : null
      };
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
  const pinia = createPinia();
  const exports = compile("../src/store/modules/ai.ts", {
    "@/store": { store: pinia },
    "@/utils/ai": utils,
    "@/api/ai": {
      aiApi: api,
      AIRequestError: class AIRequestError extends Error {},
      streamAIEvents: (id, after, signal, onEvent) =>
        new Promise(resolve => {
          streams.set(id, onEvent);
          signal.addEventListener("abort", resolve, { once: true });
        })
    }
  });
  const ai = exports.useAIStore(pinia);
  t.after(() => {
    global.window = oldWindow;
    global.localStorage = oldStorage;
  });
  await ai.open();
  await flush();
  return { ai, state, calls, storage, streams };
}

test("统一默认企微专用模型：发送、生成标题、审批都只传来源，不带 API Key", async t => {
  // 浏览器里留着旧的自定义配置（没有 mode）：企微专用模型可用时也默认用企微
  const h = await harness(t, { saved: { ...CUSTOM } });
  assert.equal(h.ai.modelMode, "wecom");
  assert.equal(h.ai.configured, true);
  assert.equal(h.ai.modelLabel, "企微专用模型 · deepseek-chat");
  assert.equal(h.ai.provider.api_key, "key-alice", "自定义配置保留在浏览器里");
  await h.ai.createSession();
  await h.ai.send("看看服务状态");
  await flush();
  assert.deepEqual(h.calls.run[0].body.provider, { source: "wecom" });
  assert.deepEqual(h.calls.title[0].provider, { source: "wecom" });
  h.streams.get("run-1")({
    seq: 1,
    type: "approval",
    data: { action_id: "act-1", name: "patch" }
  });
  h.streams.get("run-1")({
    seq: 2,
    type: "status",
    data: { status: "waiting_approval" }
  });
  await h.ai.decide("act-1", "approve");
  assert.deepEqual(h.calls.decision[0].provider, { source: "wecom" });
  assert.ok(!JSON.stringify(h.calls).includes("key-alice"));
  await h.ai.cancel();
});

test("总结记忆也用企微专用模型", async t => {
  const h = await harness(t);
  await h.ai.selectSession("s1");
  await flush();
  await h.ai.summarizeMemory();
  assert.deepEqual(h.calls.summary[0], {
    id: "s1",
    provider: { source: "wecom" }
  });
});

test("管理员没配企微专用模型、账号也没选过：退回浏览器里的自定义配置", async t => {
  const h = await harness(t, { shared: UNAVAILABLE, saved: { ...CUSTOM } });
  assert.equal(h.ai.modelMode, "custom");
  assert.equal(h.ai.configured, true);
  assert.equal(h.ai.modelLabel, "tool-model");
  await h.ai.createSession();
  await h.ai.send("看看服务状态");
  assert.equal(h.calls.run[0].body.provider.api_key, "key-alice");
  assert.equal(h.calls.run[0].body.provider.model, "tool-model");
  await h.ai.cancel();
});

test("明确选了企微专用模型而管理员没配：不能发送，提示改用自定义模型", async t => {
  const h = await harness(t, {
    shared: UNAVAILABLE,
    saved: { ...CUSTOM, mode: "wecom" }
  });
  assert.equal(h.ai.modelMode, "wecom");
  assert.equal(h.ai.configured, false);
  assert.match(h.ai.modelHint, /改用自定义模型/);
  await h.ai.createSession();
  await assert.rejects(h.ai.send("看看"), /管理员还没有配置企微专用模型/);
  assert.equal(h.calls.run.length, 0);
});

test("旧版 AI 服务没有 shared_model：保存过自定义配置的账号照旧用自定义模型", async t => {
  const h = await harness(t, { legacy: true, saved: { ...CUSTOM } });
  assert.equal(h.ai.sharedModel, null);
  assert.equal(h.ai.modelMode, "custom");
  assert.equal(h.ai.configured, true);
});

test("保存模型选择：自定义写 mode=custom；改用企微保留自定义字段；清除配置连选择一起删", async t => {
  const h = await harness(t);
  const key = utils.providerStorageKey("alice");
  h.ai.saveProvider({ ...CUSTOM, base_url: "https://model.example/v1/" });
  let stored = JSON.parse(h.storage.get(key));
  assert.equal(stored.mode, "custom");
  assert.equal(stored.base_url, "https://model.example/v1");
  assert.equal(h.ai.modelMode, "custom");
  h.ai.useSharedModel();
  stored = JSON.parse(h.storage.get(key));
  assert.equal(stored.mode, "wecom");
  assert.equal(stored.api_key, "key-alice");
  assert.equal(stored.model, "tool-model");
  assert.equal(h.ai.modelMode, "wecom");
  h.ai.forgetProvider();
  assert.equal(h.storage.has(key), false);
  assert.equal(h.ai.providerMode, null);
  assert.equal(h.ai.provider.api_key, "");
  // 换账号：选择不串到别的账号
  h.ai.saveProvider(CUSTOM);
  h.state.username = "bob";
  await h.ai.open();
  assert.equal(h.ai.providerMode, null);
  assert.equal(h.ai.modelMode, "wecom");
  assert.equal(h.ai.provider.api_key, "");
});

test("会话分享：开关更新列表标记和当前会话的分享码，会话详情带回分享状态", async t => {
  const h = await harness(t);
  await h.ai.selectSession("s1");
  await flush();
  assert.equal(h.ai.shareCode, null);
  const code = await h.ai.setShare(true);
  assert.equal(code, "code-1");
  assert.equal(h.ai.shareCode, "code-1");
  assert.equal(h.ai.sessions.find(item => item.id === "s1").shared, true);
  assert.deepEqual(h.calls.share[0], { id: "s1", enabled: true });
  await h.ai.refreshSessions();
  assert.equal(h.ai.sessions.find(item => item.id === "s1").shared, true);
  // 重新打开会话：详情里的分享码
  await h.ai.selectSession("s1");
  await flush();
  assert.equal(h.ai.shareCode, "code-1");
  assert.equal(await h.ai.setShare(false), null);
  assert.equal(h.ai.shareCode, null);
  assert.equal(h.ai.sessions.find(item => item.id === "s1").shared, false);
  assert.equal(h.ai.sharing, false);
});

test("会话分享：草稿不能分享，读取会话时不能开关；回合结束的同步晚于开关回来时以开关结果为准", async t => {
  const h = await harness(t);
  await h.ai.createSession();
  assert.equal(h.ai.shareCode, null);
  await assert.rejects(h.ai.setShare(true), /还没有发送过消息/);
  assert.equal(h.calls.share.length, 0);

  let release;
  h.state.detail = id =>
    new Promise(resolve => {
      release = () => resolve(detail(id, null));
    });
  const opening = h.ai.selectSession("s1");
  await flush();
  await assert.rejects(h.ai.setShare(true), /正在核验会话状态/);
  release();
  await opening;
  await flush();
  h.state.detail = null;
  assert.equal(h.ai.shareCode, null);

  await h.ai.send("继续排查");
  let late;
  h.state.detail = id =>
    new Promise(resolve => {
      late = () => resolve(detail(id, null));
    });
  h.streams.get("run-1")({
    seq: 1,
    type: "done",
    data: { status: "completed" }
  });
  await flush();
  assert.equal(typeof late, "function", "回合结束后的会话同步已发出");
  const code = await h.ai.setShare(true);
  late();
  await flush();
  assert.equal(h.ai.shareCode, code);
  assert.equal(h.ai.sessions.find(item => item.id === "s1").shared, true);
});

test("换账号时清掉分享状态，进行中的分享请求结果不串到新账号", async t => {
  const h = await harness(t);
  await h.ai.selectSession("s1");
  await flush();
  let finish;
  h.state.setShare = (id, enabled) =>
    new Promise(resolve => {
      finish = () =>
        resolve({
          id,
          shared: enabled,
          share_code: "late-code",
          shared_at: null
        });
    });
  const pending = h.ai.setShare(true);
  assert.equal(h.ai.sharing, true);
  h.state.username = "bob";
  h.state.sessions = [{ id: "b1", title: "bob 的会话" }];
  await h.ai.open();
  await flush();
  assert.equal(h.ai.sharing, false);
  finish();
  assert.equal(await pending, null);
  assert.notEqual(h.ai.shareCode, "late-code");
  assert.equal(
    h.ai.sessions.some(item => item.shared),
    false
  );
});
