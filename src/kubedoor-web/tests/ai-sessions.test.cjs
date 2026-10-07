const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { performance } = require("node:perf_hooks");
const { createPinia } = require("pinia");
const { nextTick } = require("vue");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

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
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
const flush = async () => {
  for (let i = 0; i < 3; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
};
const scope = env => ({ env, namespace: null, deployment: null, pod: null });
const history = id => ({
  id,
  title: id,
  active_run: null,
  messages: [
    {
      id: `${id}-m`,
      role: "assistant",
      content: `${id} history`,
      scope: scope("cluster-a")
    }
  ]
});

async function harness(t) {
  const oldWindow = global.window,
    oldStorage = global.localStorage;
  global.window = { setTimeout, clearTimeout };
  const storage = new Map();
  global.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key)
  };
  const state = {
    username: "alice",
    details: new Map(),
    delayed: new Map(),
    list: null,
    create: null,
    run: null
  };
  const calls = {
    detail: [],
    create: [],
    run: [],
    summary: [],
    decision: [],
    rename: [],
    delete: []
  };
  const streams = [];
  let count = 0;
  const api = {
    bootstrap: async () => ({
      username: state.username,
      enabled: true,
      permission: "rw",
      skills: [],
      clusters: [
        { env: "cluster-a", online: true },
        { env: "cluster-b", online: true }
      ]
    }),
    sessions: async () =>
      state.list
        ? state.list.promise
        : {
            sessions: [...state.details.values()].map(
              ({ messages, active_run, ...summary }) => summary
            )
          },
    session: async (id, signal) => {
      calls.detail.push({ id, signal });
      const pending = state.delayed.get(id);
      return pending
        ? pending.promise
        : structuredClone(state.details.get(id) || history(id));
    },
    createSession: async (key, title) => {
      calls.create.push({ key, title });
      const created = state.create
        ? await state.create.promise
        : { id: `created-${++count}`, title, messages: [], active_run: null };
      state.details.set(created.id, created);
      return created;
    },
    run: async (id, body) => {
      calls.run.push({ id, body });
      return state.run
        ? state.run.promise
        : { run_id: `run-${calls.run.length}`, status: "running" };
    },
    summarizeMemory: async (id, provider) => {
      calls.summary.push({ id, provider });
      return state.summary
        ? state.summary.promise
        : { title: "经验", content: "已验证步骤" };
    },
    decision: async (...args) => {
      calls.decision.push(args);
      return state.decision ? state.decision.promise : { status: "running" };
    },
    renameSession: async (id, title) => {
      calls.rename.push({ id, title });
      state.details.get(id).title = title;
    },
    deleteSession: async id => {
      calls.delete.push(id);
      state.details.delete(id);
    }
  };
  const pinia = createPinia();
  const ai = compile("../src/store/modules/ai.ts", {
    "@/store": { store: pinia },
    "@/utils/ai": utils,
    "@/api/ai": {
      aiApi: api,
      AIRequestError: class AIRequestError extends Error {},
      streamAIEvents: (id, after, signal, callback) => {
        const task = deferred();
        streams.push({ id, after, signal, callback, ...task });
        signal.addEventListener("abort", task.resolve, { once: true });
        return task.promise;
      }
    }
  }).useAIStore(pinia);
  await ai.open();
  ai.saveProvider({
    base_url: "https://model.example/v1",
    model: "tool-model",
    api_key: "local-test-key"
  });
  t.after(async () => {
    state.username = "cleanup";
    await ai.refreshBootstrap();
    for (const stream of streams) stream.resolve();
    await flush();
    ai.$dispose();
    global.window = oldWindow;
    global.localStorage = oldStorage;
  });
  return { ai, state, calls, streams, storage };
}

test("new chat becomes an editable memory draft immediately, without any HTTP request", async t => {
  const h = await harness(t);
  h.ai.messages = history("old").messages;
  h.state.create = deferred();
  const started = performance.now();
  const pending = h.ai.createSession();
  assert.match(h.ai.sessionId, /^draft:/);
  assert.equal(h.ai.messages.length, 0);
  assert.equal(h.ai.sessionReady, true);
  assert.equal(h.calls.create.length, 0);
  const draft = await pending;
  t.diagnostic(
    `Editable draft appeared in ${(performance.now() - started).toFixed(2)} ms with HTTP creation still blocked.`
  );
  await h.ai.createSession();
  assert.equal(h.ai.sessionId, draft.id);
  assert.equal(h.ai.sessions.filter(item => item.draft).length, 1);
  assert.equal(h.storage.size, 1); // Only username-specific model settings.
});

test("draft rename, list refresh, scope recovery and deletion stay in memory", async t => {
  const h = await harness(t);
  const draft = await h.ai.createSession();
  await h.ai.renameSession(draft.id, "  排查新服务  ");
  h.ai.changeScope(scope("cluster-b"));
  await h.ai.refreshSessions();
  assert.equal(h.ai.sessions[0].title, "排查新服务");
  await h.ai.selectSession("other");
  await h.ai.selectSession(draft.id);
  assert.equal(h.ai.scope.env, "cluster-b");
  await h.ai.deleteSession(draft.id);
  assert.equal(h.ai.sessionId, "");
  assert.equal(h.ai.sessions.length, 0);
  assert.equal(h.calls.rename.length, 0);
  assert.equal(h.calls.delete.length, 0);
});

test("first send persists the renamed draft once and simultaneous sends cannot double-create or run", async t => {
  const h = await harness(t);
  const draft = await h.ai.createSession();
  await h.ai.renameSession(draft.id, "诊断日志");
  h.state.create = deferred();
  const sending = h.ai.send("检查日志");
  await h.ai.send("重复点击");
  assert.equal(h.calls.create.length, 1);
  assert.equal(h.calls.run.length, 0);
  assert.equal(h.calls.create[0].title, "诊断日志");
  h.state.create.resolve({
    id: "saved",
    title: "诊断日志",
    messages: [],
    active_run: null
  });
  await sending;
  assert.equal(h.calls.run.length, 1);
  assert.equal(h.calls.run[0].id, "saved");
  assert.equal(h.ai.sessionId, "saved");
  assert.equal(
    h.ai.sessions.some(item => item.draft),
    false
  );
  assert.equal(h.ai.messages[0].content, "检查日志");
});

test("simultaneous first sends from an empty store also share one draft and run", async t => {
  const h = await harness(t);
  h.state.create = deferred();
  const first = h.ai.send("第一次");
  const second = h.ai.send("第二次");
  assert.equal(h.calls.create.length, 1);
  h.state.create.resolve({
    id: "saved",
    title: "新会话",
    messages: [],
    active_run: null
  });
  await Promise.all([first, second]);
  assert.equal(h.calls.run.length, 1);
  assert.equal(h.ai.messages[0].content, "第一次");
});

test("failed draft persistence preserves its title and retries with the same idempotency key", async t => {
  const h = await harness(t);
  const draft = await h.ai.createSession();
  await h.ai.renameSession(draft.id, "重试会话");
  h.state.create = deferred();
  const attempt = h.ai.send("原消息");
  h.state.create.reject(new Error("offline"));
  await assert.rejects(attempt, /offline/);
  assert.equal(h.ai.sessionId, draft.id);
  assert.equal(h.ai.sessions[0].title, "重试会话");
  assert.equal(h.ai.busy, false);
  h.state.create = null;
  await h.ai.send("原消息");
  assert.equal(h.calls.create.length, 2);
  assert.equal(h.calls.create[0].key, h.calls.create[1].key);
  assert.equal(h.calls.run.length, 1);
});

test("first visit selects immediately but gates sending and approvals until the latest run is verified", async t => {
  const h = await harness(t);
  h.state.delayed.set("cold", deferred());
  const pending = h.ai.selectSession("cold");
  assert.equal(h.ai.sessionId, "cold");
  assert.equal(h.ai.sessionLoading, true);
  assert.equal(h.ai.sessionReady, false);
  await assert.rejects(h.ai.send("不能发送"), /核验/);
  await assert.rejects(h.ai.decide("unknown-action", "approve"), /核验/);
  assert.equal(h.calls.run.length, 0);
  assert.equal(h.calls.decision.length, 0);
  h.state.delayed.get("cold").resolve(history("cold"));
  await pending;
  assert.equal(h.ai.sessionReady, true);
  assert.equal(h.ai.messages[0].content, "cold history");
});

test("visited history displays from cache while HTTP is blocked, with no approval of cached pending actions", async t => {
  const h = await harness(t);
  const old = history("a");
  old.messages[0].tools = [
    {
      id: "pending",
      action_id: "pending",
      name: "patch",
      state: "pending",
      pending: true
    }
  ];
  h.state.details.set("a", old);
  await h.ai.selectSession("a");
  await h.ai.selectSession("b");
  h.ai.autoApprove = true;
  const pending = deferred();
  h.state.delayed.set("a", pending);
  const started = performance.now();
  const selection = h.ai.selectSession("a");
  assert.equal(h.ai.sessionId, "a");
  assert.equal(h.ai.messages[0].content, "a history");
  assert.equal(h.ai.sessionReady, false);
  await flush();
  assert.equal(h.calls.decision.length, 0);
  await assert.rejects(h.ai.decide("pending", "approve"), /核验/);
  t.diagnostic(
    `Cached history returned in ${(performance.now() - started).toFixed(2)} ms without waiting for the delayed response.`
  );
  pending.resolve({
    ...history("a"),
    active_run: {
      id: "live-run",
      status: "waiting_approval",
      event_seq: 5,
      scope: scope("cluster-a"),
      pending_actions: [
        { action_id: "verified", name: "patch", arguments: { name: "app" } }
      ]
    }
  });
  await selection;
  await flush();
  assert.equal(h.calls.decision.length, 1);
  assert.equal(h.calls.decision[0][1], "verified");
  assert.equal(h.streams.at(-1).id, "live-run");
});

test("rapid A/B/C switching aborts old requests and ignores out-of-order successes and failures", async t => {
  const h = await harness(t);
  for (const id of ["a", "b", "c"]) h.state.delayed.set(id, deferred());
  const a = h.ai.selectSession("a");
  const b = h.ai.selectSession("b");
  const c = h.ai.selectSession("c");
  assert.equal(h.ai.sessionId, "c");
  assert.equal(h.calls.detail[0].signal.aborted, true);
  assert.equal(h.calls.detail[1].signal.aborted, true);
  assert.equal(h.calls.detail[2].signal.aborted, false);
  h.state.delayed.get("c").resolve(history("c"));
  await c;
  h.state.delayed.get("a").resolve({
    ...history("a"),
    active_run: { id: "wrong", status: "waiting_approval" }
  });
  h.state.delayed.get("b").reject(new Error("obsolete request failed"));
  await Promise.all([a, b]);
  assert.equal(h.ai.sessionId, "c");
  assert.equal(h.ai.messages[0].content, "c history");
  assert.equal(h.ai.activeRun, null);
  assert.equal(h.ai.error, "");
  assert.equal(h.ai.sessionLoading, false);
});

test("new draft wins over an outstanding history load", async t => {
  const h = await harness(t);
  h.state.delayed.set("old", deferred());
  const old = h.ai.selectSession("old");
  const draft = await h.ai.createSession();
  h.state.delayed.get("old").resolve(history("old"));
  await old;
  assert.equal(h.ai.sessionId, draft.id);
  assert.equal(h.ai.messages.length, 0);
  assert.equal(h.ai.sessionReady, true);
});

test("failed cache refresh keeps visible history but blocks cached execution", async t => {
  const h = await harness(t);
  await h.ai.selectSession("a");
  await h.ai.selectSession("b");
  const pending = deferred();
  h.state.delayed.set("a", pending);
  const loading = h.ai.selectSession("a");
  pending.reject(new Error("unavailable"));
  await assert.rejects(loading, /unavailable/);
  assert.equal(h.ai.messages[0].content, "a history");
  assert.equal(h.ai.sessionReady, false);
  await assert.rejects(h.ai.send("cannot send"), /核验/);
});

test("bounded LRU evicts old history and does not retain an oversized conversation", async t => {
  const h = await harness(t);
  for (let i = 0; i < 17; i++) await h.ai.selectSession(`s${i}`);
  const recent = deferred();
  h.state.delayed.set("s16", recent);
  const hit = h.ai.selectSession("s16");
  assert.equal(h.ai.messages[0].content, "s16 history");
  recent.resolve(history("s16"));
  await hit;
  const old = deferred();
  h.state.delayed.set("s0", old);
  const miss = h.ai.selectSession("s0");
  assert.equal(h.ai.messages.length, 0);
  old.resolve(history("s0"));
  await miss;
  const huge = history("huge");
  huge.messages[0].content = "x".repeat(2000001);
  h.state.details.set("huge", huge);
  await h.ai.selectSession("huge");
  await h.ai.selectSession("other");
  h.state.delayed.set("huge", deferred());
  const oversized = h.ai.selectSession("huge");
  assert.equal(h.ai.messages.length, 0);
  h.state.delayed.get("huge").resolve(huge);
  await oversized;
});

test("owner changes clear drafts and cached histories and suppress the previous owner's response", async t => {
  const h = await harness(t);
  await h.ai.selectSession("a");
  await h.ai.createSession();
  h.state.delayed.set("a", deferred());
  const previous = h.ai.selectSession("a");
  h.state.username = "bob";
  await h.ai.refreshBootstrap();
  assert.equal(h.ai.sessionId, "");
  assert.equal(h.ai.sessions.length, 0);
  assert.equal(h.ai.messages.length, 0);
  assert.equal(h.ai.autoApprove, false);
  assert.equal(h.ai.provider.api_key, "");
  h.state.delayed.get("a").resolve(history("a"));
  await previous;
  const bobLoad = h.ai.selectSession("a");
  assert.equal(h.ai.messages.length, 0); // Alice's cached history cannot display.
  await bobLoad;
});

test("an old owner's send completion cannot unlock the new owner's pending submission", async t => {
  const h = await harness(t);
  const aliceCreate = deferred();
  h.state.create = aliceCreate;
  const alice = h.ai.send("alice request");
  h.state.username = "bob";
  await h.ai.refreshBootstrap();
  h.ai.saveProvider({
    base_url: "https://bob.example/v1",
    model: "tool-model",
    api_key: "bob-key"
  });
  const bobCreate = deferred();
  h.state.create = bobCreate;
  const bob = h.ai.send("bob request");
  aliceCreate.resolve({ id: "alice-saved", title: "alice", messages: [] });
  await alice;
  assert.equal(h.ai.submitting, true);
  assert.match(h.ai.sessionId, /^draft:/);
  assert.equal(h.calls.run.length, 0);
  bobCreate.resolve({ id: "bob-saved", title: "bob", messages: [] });
  await bob;
  assert.equal(h.calls.run.length, 1);
  assert.equal(h.calls.run[0].id, "bob-saved");
});

test("known active runs keep session switching and new drafts locked", async t => {
  const h = await harness(t);
  await h.ai.send("run");
  const id = h.ai.sessionId;
  await h.ai.selectSession("other");
  await h.ai.createSession();
  assert.equal(h.ai.sessionId, id);
  assert.equal(h.calls.detail.length, 0);
});

test("a new run resets old approval locks and late replies cannot unlock its current decision", async t => {
  const h = await harness(t);
  h.ai.autoApprove = true;
  await h.ai.send("first run");
  const firstDecision = deferred();
  h.state.decision = firstDecision;
  const firstStream = h.streams.at(-1);
  firstStream.callback({
    seq: 1,
    type: "approval",
    data: { action_id: "first", name: "patch" }
  });
  firstStream.callback({
    seq: 2,
    type: "status",
    data: { status: "waiting_approval" }
  });
  await flush();
  assert.equal(h.ai.deciding, "first");
  firstStream.callback({
    seq: 3,
    type: "status",
    data: { status: "completed" }
  });
  await flush();
  await h.ai.send("next run");
  const nextDecision = deferred();
  h.state.decision = nextDecision;
  const nextStream = h.streams.at(-1);
  nextStream.callback({
    seq: 1,
    type: "approval",
    data: { action_id: "next", name: "patch" }
  });
  nextStream.callback({
    seq: 2,
    type: "status",
    data: { status: "waiting_approval" }
  });
  await flush();
  assert.equal(h.calls.decision.length, 2);
  assert.equal(h.ai.deciding, "next");
  firstDecision.resolve({ status: "running" });
  await flush();
  assert.equal(h.ai.deciding, "next");
  assert.equal(h.ai.activeRun.id, "run-2");
  nextDecision.resolve({ status: "running" });
  await flush();
  assert.equal(h.ai.deciding, "");
});

test("the actual fetch API forwards cancellation and distinguishes it from a timeout", async t => {
  const previousFetch = global.fetch,
    previousWindow = global.window;
  const timers = new Set();
  global.window = {
    setTimeout(callback, delay) {
      const timer = setTimeout(callback, delay);
      timers.add(timer);
      return timer;
    },
    clearTimeout(timer) {
      clearTimeout(timer);
      timers.delete(timer);
    }
  };
  let signal;
  global.fetch = async (_, options) =>
    new Promise((resolve, reject) => {
      signal = options.signal;
      signal.addEventListener(
        "abort",
        () => reject(new DOMException("cancelled", "AbortError")),
        { once: true }
      );
    });
  t.after(() => {
    for (const timer of timers) clearTimeout(timer);
    global.fetch = previousFetch;
    global.window = previousWindow;
  });
  const api = compile("../src/api/ai.ts", { "@/utils/ai": utils }).aiApi;
  const controller = new AbortController();
  const pending = api.session("a", controller.signal);
  controller.abort();
  await assert.rejects(pending, error => error.name === "AbortError");
  assert.equal(signal.aborted, true);
  assert.equal(timers.size, 0);

  const resourcesController = new AbortController();
  const resources = api.resources(
    "namespaces",
    scope("cluster-a"),
    resourcesController.signal
  );
  resourcesController.abort();
  await assert.rejects(resources, error => error.name === "AbortError");
  assert.equal(signal.aborted, true);
  assert.equal(timers.size, 0);
});

test("the actual draft persistence request preserves the title and idempotency key", async t => {
  const previousFetch = global.fetch,
    previousWindow = global.window;
  global.window = { setTimeout, clearTimeout };
  const requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return new Response(
      JSON.stringify({ id: "saved", title: JSON.parse(options.body).title }),
      { status: 200 }
    );
  };
  t.after(() => {
    global.fetch = previousFetch;
    global.window = previousWindow;
  });
  const api = compile("../src/api/ai.ts", { "@/utils/ai": utils }).aiApi;
  const created = await api.createSession("draft-key", "我的会话");
  assert.equal(created.title, "我的会话");
  assert.equal(requests[0].url, "/api/ai/sessions");
  assert.equal(requests[0].options.headers["Idempotency-Key"], "draft-key");
  assert.deepEqual(JSON.parse(requests[0].options.body), { title: "我的会话" });
});

test("resource fetches include only the selected parents needed by each level", async t => {
  const previousFetch = global.fetch,
    previousWindow = global.window;
  global.window = { setTimeout, clearTimeout };
  const requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return new Response(JSON.stringify({ items: [] }), { status: 200 });
  };
  t.after(() => {
    global.fetch = previousFetch;
    global.window = previousWindow;
  });
  const api = compile("../src/api/ai.ts", { "@/utils/ai": utils }).aiApi;
  const selected = {
    ...scope("cluster-a"),
    namespace: "ops",
    deployment: { namespace: "ops", name: "app" },
    pod: { namespace: "ops", name: "app-1" }
  };
  for (const kind of ["namespaces", "deployments", "pods"])
    await api.resources(kind, selected);
  const queries = requests.map(({ url }) =>
    Object.fromEntries(new URL(url, "http://local").searchParams)
  );
  assert.deepEqual(queries, [
    { env: "cluster-a", kind: "namespaces" },
    { env: "cluster-a", kind: "deployments", namespace: "ops" },
    {
      env: "cluster-a",
      kind: "pods",
      namespace: "ops",
      deployment: "app",
      deployment_namespace: "ops"
    }
  ]);
});

const memory = (id = "memory-1") => ({
  id,
  title: "重启经验",
  version: 1,
  created_by: "alice",
  updated_by: "alice",
  created_at: "2026-10-05",
  updated_at: "2026-10-05"
});

test("memories default off, survive automatic first-send drafts, then clear only on acceptance", async t => {
  const h = await harness(t);
  assert.deepEqual(h.ai.selectedMemories, []);
  h.ai.setSelectedMemories([
    { ...memory(), content: "do not cache library content" }
  ]);
  assert.equal(h.ai.selectedMemories[0].content, undefined);
  h.state.run = deferred();
  const pending = h.ai.send("排查服务");
  await flush();
  assert.deepEqual(h.calls.run[0].body.memory_ids, ["memory-1"]);
  assert.equal(h.ai.selectedMemories.length, 1);
  const attachment = {
    id: "memory-1",
    title: "更新后的标题",
    content: "## 已验证\n\n| 项 | 值 |\n| -- | -- |\n| 版本 | 2 |",
    version: 2
  };
  h.state.run.resolve({
    run_id: "run-1",
    status: "running",
    message: {
      id: "saved-user",
      role: "user",
      content: "排查服务",
      scope: scope("cluster-a"),
      memories: [attachment]
    }
  });
  await pending;
  assert.deepEqual(h.ai.selectedMemories, []);
  assert.equal(h.ai.messages[0].id, "saved-user");
  assert.deepEqual(h.ai.messages[0].memories, [attachment]);
  h.ai.activeRun = null;
  h.state.run = null;
  await h.ai.send("继续核验");
  assert.deepEqual(h.calls.run[1].body.memory_ids, []);
  assert.deepEqual(h.ai.messages[0].memories, [attachment]);
  assert.equal(h.storage.size, 1); // References never go to localStorage.
});

test("failed sends preserve pending memories for retry, including draft persistence failure", async t => {
  const h = await harness(t);
  h.ai.setSelectedMemories([memory()]);
  h.state.create = deferred();
  const creation = h.ai.send("排查");
  h.state.create.reject(new Error("create failed"));
  await assert.rejects(creation, /create failed/);
  assert.equal(h.ai.selectedMemories.length, 1);
  h.state.create = null;
  h.state.run = deferred();
  const failed = h.ai.send("重试");
  await flush();
  h.state.run.reject(new Error("memory not found"));
  await assert.rejects(failed, /memory not found/);
  assert.equal(h.ai.selectedMemories.length, 1);
  assert.equal(h.ai.messages.length, 0);
  h.state.run = null;
  await h.ai.send("再试");
  assert.deepEqual(h.calls.run[1].body.memory_ids, ["memory-1"]);
  assert.deepEqual(h.ai.selectedMemories, []);
});

test("explicit new chats, switches and owner changes clear pending memories", async t => {
  const h = await harness(t);
  h.ai.setSelectedMemories([memory()]);
  await h.ai.createSession();
  assert.deepEqual(h.ai.selectedMemories, []);
  h.ai.setSelectedMemories([memory()]);
  await h.ai.selectSession("existing");
  assert.deepEqual(h.ai.selectedMemories, []);
  h.ai.setSelectedMemories([memory()]);
  h.state.username = "bob";
  await h.ai.refreshBootstrap();
  assert.deepEqual(h.ai.selectedMemories, []);
});

test("memory selection is bounded and cannot change while sending or revalidating", async t => {
  const h = await harness(t);
  h.ai.setSelectedMemories([memory(), memory()]);
  assert.equal(h.ai.selectedMemories.length, 1);
  assert.throws(
    () =>
      h.ai.setSelectedMemories(
        Array.from({ length: 11 }, (_, i) => memory(String(i)))
      ),
    /10/
  );
  h.ai.sessionReady = false;
  assert.throws(() => h.ai.setSelectedMemories([]), /核验/);
  h.ai.removeSelectedMemory("memory-1");
  assert.equal(h.ai.selectedMemories.length, 1);
  h.ai.sessionReady = true;
  h.state.run = deferred();
  const pending = h.ai.send("排查");
  await flush();
  assert.throws(() => h.ai.setSelectedMemories([]), /执行/);
  h.state.run.resolve({ run_id: "run-1", status: "running" });
  await pending;
});

test("summary uses the current private conversation and rejects late results after switching", async t => {
  const h = await harness(t);
  await assert.rejects(h.ai.summarizeMemory(), /没有可总结/);
  await h.ai.createSession();
  await assert.rejects(h.ai.summarizeMemory(), /没有可总结/);
  await h.ai.selectSession("completed");
  h.ai.bootstrap.permission = "read";
  assert.deepEqual(await h.ai.summarizeMemory(), {
    title: "经验",
    content: "已验证步骤"
  });
  assert.equal(h.calls.summary[0].id, "completed");
  h.state.summary = deferred();
  const stale = h.ai.summarizeMemory();
  assert.equal(h.ai.summarizingMemory, true);
  await assert.rejects(h.ai.summarizeMemory(), /结束当前轮次/);
  await h.ai.selectSession("other");
  assert.equal(h.ai.summarizingMemory, false);
  const oldSummary = h.state.summary;
  h.state.summary = deferred();
  const latest = h.ai.summarizeMemory();
  assert.equal(h.ai.summarizingMemory, true);
  oldSummary.resolve({ title: "old", content: "old" });
  await assert.rejects(stale, /会话已切换/);
  assert.equal(h.ai.summarizingMemory, true);
  h.state.summary.resolve({ title: "latest", content: "latest" });
  assert.equal((await latest).title, "latest");
  assert.equal(h.ai.summarizingMemory, false);
  assert.equal(h.ai.sessionId, "other");
  assert.equal(h.ai.messages[0].content, "other history");
});

test("historical memory attachments survive caching and count toward its size bound", async t => {
  const h = await harness(t);
  h.state.details.set("small", {
    ...history("small"),
    messages: [
      {
        id: "m",
        role: "user",
        content: "query",
        memories: [{ ...memory(), content: "frozen text" }]
      }
    ]
  });
  await h.ai.selectSession("small");
  await h.ai.selectSession("other");
  h.state.delayed.set("small", deferred());
  const small = h.ai.selectSession("small");
  assert.equal(h.ai.messages[0].memories[0].content, "frozen text");
  h.state.delayed.get("small").resolve(h.state.details.get("small"));
  await small;
  h.state.details.set("huge", {
    ...history("huge"),
    messages: [
      {
        id: "m",
        role: "user",
        content: "query",
        memories: [{ ...memory(), content: "x".repeat(2100000) }]
      }
    ]
  });
  await h.ai.selectSession("huge");
  await h.ai.selectSession("other");
  h.state.delayed.set("huge", deferred());
  const huge = h.ai.selectSession("huge");
  assert.deepEqual(h.ai.messages, []);
  h.state.delayed.get("huge").resolve(h.state.details.get("huge"));
  await huge;
});
