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

function compileModule(filename, overrides = {}) {
  const target = path.resolve(__dirname, filename);
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  const originalRequire = instance.require.bind(instance);
  instance.require = id =>
    Object.hasOwn(overrides, id) ? overrides[id] : originalRequire(id);
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

const utils = compileModule("../src/utils/ai.ts");

class AIRequestError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = "AIRequestError";
    this.status = status;
    this.code = code;
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

async function flush() {
  // Let Vue watchers, API promises and the next watcher pass all settle.
  for (let i = 0; i < 3; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
}

function action(id) {
  return {
    action_id: id,
    name: "k8s_api",
    source: "agent",
    arguments: { operation: "patch", name: id },
    preview: { diff: "- replicas: 1\n+ replicas: 2" }
  };
}

async function harness(t) {
  const previousWindow = global.window;
  const previousStorage = global.localStorage;
  const saved = new Map();
  const timers = new Map();
  const streams = new Map();
  const connections = [];
  const requests = [];
  const state = {
    username: "alice",
    permission: "rw",
    sessions: [],
    detail: { id: "session-1", messages: [], active_run: null },
    decision: async () => ({ status: "running" }),
    cancel: null
  };
  let timerId = 0;
  let runNumber = 0;
  const sequences = new Map();

  global.window = {
    setTimeout(callback, delay) {
      const id = ++timerId;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    }
  };
  global.localStorage = {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key)
  };

  function emit(type, data, options = {}) {
    const id = options.runId || ai.activeRun?.id;
    const seq = options.seq ?? (sequences.get(id) || 0) + 1;
    sequences.set(id, Math.max(seq, sequences.get(id) || 0));
    const callback = streams.get(id)?.callback;
    assert.ok(callback, `run ${id} must have an SSE subscription`);
    callback({ type, data, seq });
    return seq;
  }

  const api = {
    bootstrap: async () => ({
      enabled: true,
      username: state.username,
      permission: state.permission,
      skills: [],
      clusters: [{ env: "cluster-a", online: true, configured: true }]
    }),
    sessions: async () => ({ sessions: state.sessions }),
    createSession: async () => {
      const session = { id: "session-1", title: "新会话" };
      state.sessions.push(session);
      return session;
    },
    session: async () => state.detail,
    run: async () => ({ run_id: `run-${++runNumber}`, status: "running" }),
    decision: async (runId, actionId, decision, provider) => {
      requests.push({ runId, actionId, decision, provider });
      return state.decision(runId, actionId, decision, provider);
    },
    cancel: async id => {
      if (state.cancel) return state.cancel(id);
      emit("status", { status: "cancelled" }, { runId: id });
      return { status: "cancelled" };
    }
  };

  function loadStore() {
    const pinia = createPinia();
    const exports = compileModule("../src/store/modules/ai.ts", {
      "@/store": { store: pinia },
      "@/utils/ai": utils,
      "@/api/ai": {
        aiApi: api,
        AIRequestError,
        streamAIEvents: (runId, after, signal, callback) => {
          const task = deferred();
          const connection = { runId, after, signal, callback, ...task };
          streams.set(runId, connection);
          connections.push(connection);
          signal.addEventListener("abort", task.resolve, { once: true });
          return task.promise;
        }
      }
    });
    return exports.useAIStore(pinia);
  }

  const ai = loadStore();
  t.after(async () => {
    if (ai.activeRun && !utils.isTerminalRun(ai.activeRun.status)) {
      emit("status", { status: "cancelled" });
    }
    for (const connection of connections) connection.resolve();
    ai.$dispose();
    timers.clear();
    await flush();
    global.window = previousWindow;
    global.localStorage = previousStorage;
  });

  await ai.open();
  ai.saveProvider({
    base_url: "https://example.test/v1",
    api_key: "test-only-key",
    model: "tool-model"
  });

  return {
    ai,
    state,
    requests,
    saved,
    timers,
    streams,
    connections,
    loadStore,
    emit,
    async start(autoApprove = false) {
      ai.autoApprove = autoApprove;
      await ai.send("检查并修复服务");
      await flush();
    },
    async nextTimer() {
      const next = [...timers].sort((a, b) => a[1].delay - b[1].delay)[0];
      assert.ok(next, "a retry or reconnect timer must be pending");
      timers.delete(next[0]);
      next[1].callback();
      await flush();
      return next[1].delay;
    }
  };
}

test("automatic approval starts disabled and is never persisted with model credentials", async t => {
  const h = await harness(t);
  assert.equal(h.ai.autoApprove, false);
  await h.start();
  h.emit("approval", action("manual"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 0);
  assert.equal(h.ai.pendingTools.length, 1);
  h.ai.autoApprove = true;
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].decision, "approve");
  assert.deepEqual(h.requests[0].provider, h.ai.provider);
  assert.equal([...h.saved].length, 1);
  assert.ok(![...h.saved.values()][0].includes("autoApprove"));
  const fresh = h.loadStore();
  assert.equal(fresh.autoApprove, false);
  fresh.$dispose();
});

test("approval display events cannot authorize execution before the server checkpoint is ready", async t => {
  const h = await harness(t);
  await h.start(true);
  h.emit("approval", action("first"));
  await flush();
  assert.equal(h.ai.activeRun.status, "waiting_approval");
  assert.equal(h.requests.length, 0);
  h.emit("token", { content: "正在准备操作。" });
  await flush();
  assert.equal(h.requests.length, 0);
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first"]
  );
});

test("parallel calls of the same tool are approved in order, once per real waiting checkpoint", async t => {
  const h = await harness(t);
  await h.start(true);
  const first = h.emit("approval", {
    actions: [action("first"), action("second")]
  });
  const ready = h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first"]
  );
  assert.equal(h.ai.pendingTools.at(-1).action_id, "second");
  h.emit(
    "approval",
    { actions: [action("first"), action("second")] },
    { seq: first }
  );
  h.emit("status", { status: "waiting_approval" }, { seq: ready });
  await flush();
  assert.equal(h.requests.length, 1);
  h.emit("status", { status: "running" });
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first", "second"]
  );
  h.emit("approval", action("third"));
  await flush();
  assert.equal(h.requests.length, 2);
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first", "second", "third"]
  );
});

test("replayed approved actions do not submit a second decision after reconnect", async t => {
  const h = await harness(t);
  await h.start(true);
  h.emit("approval", action("once"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  const connection = h.streams.get(h.ai.activeRun.id);
  connection.resolve();
  await flush();
  await h.nextTimer();
  assert.equal(h.connections.length, 2);
  assert.ok(h.connections[1].after >= 2);
  h.emit("approval", action("once"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["once"]
  );
});

test("closing the dialog leaves the selected automatic mode running", async t => {
  const h = await harness(t);
  await h.start(true);
  h.ai.visible = false;
  h.emit("approval", action("background"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.ai.visible, false);
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["background"]
  );
});

test("disabling automatic mode prevents the next queued operation while a decision is in flight", async t => {
  const h = await harness(t);
  const first = deferred();
  h.state.decision = () => first.promise;
  await h.start(true);
  h.emit("approval", { actions: [action("first"), action("second")] });
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 1);
  h.ai.autoApprove = false;
  h.emit("status", { status: "waiting_approval" });
  first.resolve({ status: "running" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first"]
  );
  assert.ok(h.ai.pendingTools.some(item => item.action_id === "second"));
});

test("cancelling a run stops further approvals even when an earlier decision resolves later", async t => {
  const h = await harness(t);
  const decision = deferred();
  const cancelled = deferred();
  h.state.decision = () => decision.promise;
  h.state.cancel = () => cancelled.promise;
  await h.start(true);
  h.emit("approval", { actions: [action("first"), action("second")] });
  h.emit("status", { status: "waiting_approval" });
  await flush();
  const stopping = h.ai.cancel();
  h.emit("status", { status: "waiting_approval" });
  decision.resolve({ status: "running" });
  await flush();
  assert.equal(h.requests.length, 1);
  cancelled.resolve({ status: "cancelled" });
  await stopping;
  await flush();
  assert.equal(h.ai.activeRun.status, "cancelled");
  assert.equal(h.requests.length, 1);
});

test("restoring a waiting run approves only its current pending action, never old messages", async t => {
  const h = await harness(t);
  h.state.detail = {
    id: "session-1",
    messages: [
      {
        id: "old-assistant",
        role: "assistant",
        content: "之前的会话结果",
        tools: [{ ...action("historic"), state: "pending" }]
      },
      { id: "current-user", role: "user", content: "继续修复" },
      { id: "current-assistant", role: "assistant", content: "", tools: [] }
    ],
    active_run: {
      id: "restored-run",
      status: "waiting_approval",
      scope: { env: "cluster-a", namespace: null, deployment: null, pod: null },
      pending_actions: [action("current")]
    }
  };
  h.ai.autoApprove = true;
  await h.ai.selectSession("session-1");
  await flush();
  assert.deepEqual(
    h.requests.map(item => [item.runId, item.actionId]),
    [["restored-run", "current"]]
  );
  assert.equal(h.ai.messages[0].tools[0].pending, true);
});

test("permission loss and a change of signed-in account disable automatic approval", async t => {
  const h = await harness(t);
  await h.start(true);
  h.state.permission = "ro";
  await h.ai.refreshBootstrap();
  await flush();
  assert.equal(h.ai.writable, false);
  assert.equal(h.ai.autoApprove, false);
  h.emit("approval", action("read-only"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 0);
  h.state.permission = "rw";
  await h.ai.refreshBootstrap();
  await h.ai.cancel();
  h.ai.autoApprove = true;
  await flush();
  h.state.username = "bob";
  await h.ai.refreshBootstrap();
  await flush();
  assert.equal(h.ai.autoApprove, false);
  assert.equal(h.ai.activeRun, null);
  assert.equal(h.ai.provider.api_key, "");
  assert.equal(h.requests.length, 0);
});

test("a restored checkpoint ignores earlier stream statuses before approving the next action", async t => {
  const h = await harness(t);
  h.state.detail = {
    id: "session-1",
    messages: [{ id: "assistant", role: "assistant", content: "", tools: [] }],
    active_run: {
      id: "restored-run",
      status: "waiting_approval",
      event_seq: 10,
      scope: { env: "cluster-a", namespace: null, deployment: null, pod: null },
      pending_actions: [action("first"), action("second")]
    }
  };
  h.ai.autoApprove = true;
  await h.ai.selectSession("session-1");
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first"]
  );
  for (let seq = 1; seq <= 10; seq++) {
    h.emit("status", { status: "waiting_approval" }, { seq });
    await flush();
  }
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first"]
  );
  h.emit("status", { status: "waiting_approval" }, { seq: 11 });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first", "second"]
  );
});

test("permanent approval failure disables automatic mode and leaves the pending operation inspectable", async t => {
  const h = await harness(t);
  h.state.decision = async () => {
    throw new AIRequestError("该操作没有执行权限", 403, "forbidden");
  };
  await h.start(true);
  h.emit("approval", action("denied"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.ai.autoApprove, false);
  assert.match(h.ai.error, /没有执行权限/);
  assert.equal(h.ai.pendingTools[0].action_id, "denied");
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.timers.size, 0);
});

test("a briefly locked server checkpoint is retried and resumes without a new confirmation", async t => {
  const h = await harness(t);
  let attempts = 0;
  h.state.decision = async () => {
    if (++attempts === 1)
      throw new AIRequestError("执行线程仍在结束", 409, "run_active");
    return { status: "running" };
  };
  await h.start(true);
  h.emit("approval", action("retry"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.ai.autoApprove, true);
  assert.equal(await h.nextTimer(), 150);
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[0].actionId, h.requests[1].actionId);
  assert.equal(h.ai.pendingTools.length, 0);
  assert.equal(h.ai.autoApprove, true);
});

test("checkpoint retries are bounded and other HTTP 409 errors are not retried", async t => {
  const h = await harness(t);
  h.state.decision = async () => {
    throw new AIRequestError("等待检查点落盘", 409, "checkpoint_pending");
  };
  await h.start(true);
  h.emit("approval", action("exhausted"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  const delays = [];
  while (h.timers.size && delays.length < 8) delays.push(await h.nextTimer());
  assert.deepEqual(delays, [150, 300, 600, 1200]);
  assert.equal(h.requests.length, 5);
  assert.equal(h.ai.autoApprove, false);
  assert.equal(h.ai.pendingTools[0].action_id, "exhausted");
  assert.match(h.ai.error, /检查点/);
  assert.equal(h.timers.size, 0);
  h.state.decision = async () => {
    throw new AIRequestError("资源版本冲突", 409, "resource_conflict");
  };
  h.ai.autoApprove = true;
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 6);
  assert.equal(h.ai.autoApprove, false);
  assert.equal(h.timers.size, 0);
  assert.match(h.ai.error, /资源版本冲突/);
});

test("turning off automatic mode during the retry delay prevents resubmission", async t => {
  const h = await harness(t);
  h.state.decision = async () => {
    throw new AIRequestError("执行线程仍在结束", 409, "run_active");
  };
  await h.start(true);
  h.emit("approval", action("stop-retry"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 1);
  h.ai.autoApprove = false;
  await flush();
  if (h.timers.size) await h.nextTimer();
  assert.equal(h.requests.length, 1);
  assert.equal(h.ai.pendingTools[0].action_id, "stop-retry");
});

test("a failed stream cannot be resurrected by a late decision response", async t => {
  const h = await harness(t);
  const decision = deferred();
  h.state.decision = () => decision.promise;
  await h.start(true);
  h.emit("approval", { actions: [action("first"), action("second")] });
  h.emit("status", { status: "waiting_approval" });
  await flush();
  h.emit("error", { message: "执行连接失败" });
  await flush();
  decision.resolve({ status: "running" });
  await flush();
  assert.equal(h.ai.activeRun.status, "failed");
  assert.equal(h.requests.length, 1);
  assert.match(h.ai.error, /执行连接失败/);
});

test("re-enabling automatic mode after a paused retry uses the existing confirmed checkpoint", async t => {
  const h = await harness(t);
  h.state.decision = async () => {
    throw new AIRequestError("执行线程仍在结束", 409, "run_active");
  };
  await h.start(true);
  h.emit("approval", action("resume"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.equal(h.requests.length, 1);
  h.ai.autoApprove = false;
  await flush();
  await h.nextTimer();
  assert.equal(h.requests.length, 1);
  assert.equal(h.ai.pendingTools[0].action_id, "resume");
  h.state.decision = async () => ({ status: "running" });
  h.ai.autoApprove = true;
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["resume", "resume"]
  );
  assert.equal(h.ai.pendingTools.length, 0);
  assert.equal(h.timers.size, 0);
});

test("a manual decision in flight pauses automatic retries until the next server waiting status", async t => {
  const h = await harness(t);
  const manualDecision = deferred();
  let firstAttempts = 0;
  h.state.decision = async (_runId, actionId) => {
    if (actionId === "second") return manualDecision.promise;
    if (++firstAttempts === 1)
      throw new AIRequestError("执行线程仍在结束", 409, "run_active");
    return { status: "running" };
  };
  await h.start(true);
  h.emit("approval", { actions: [action("first"), action("second")] });
  h.emit("status", { status: "waiting_approval" });
  await flush();
  const manual = h.ai.decide("second", "approve");
  await flush();
  assert.equal(h.ai.deciding, "second");
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first", "second"]
  );
  await h.nextTimer();
  assert.equal(h.requests.length, 2);
  assert.equal(h.ai.deciding, "second");
  assert.ok(h.ai.pendingTools.some(item => item.action_id === "first"));
  manualDecision.resolve({ status: "running" });
  await manual;
  await flush();
  assert.equal(h.requests.length, 2);
  assert.equal(h.ai.activeRun.status, "running");
  assert.ok(h.ai.pendingTools.some(item => item.action_id === "first"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  assert.deepEqual(
    h.requests.map(item => item.actionId),
    ["first", "second", "first"]
  );
  assert.equal(h.ai.pendingTools.length, 0);
});

test("the real API wrapper preserves backend error codes required for checkpoint retries", async t => {
  const previousWindow = global.window;
  const previousFetch = global.fetch;
  global.window = { setTimeout, clearTimeout };
  t.after(() => {
    global.window = previousWindow;
    global.fetch = previousFetch;
  });
  const real = compileModule("../src/api/ai.ts", { "@/utils/ai": utils });
  global.fetch = async (url, options) => {
    assert.equal(url, "/api/ai/runs/run-1/decisions");
    assert.equal(options.method, "POST");
    assert.equal(JSON.parse(options.body).action_id, "action-1");
    return new Response(
      JSON.stringify({
        error: { code: "run_active", message: "执行线程仍在结束" }
      }),
      { status: 409, headers: { "Content-Type": "application/json" } }
    );
  };
  await assert.rejects(
    real.aiApi.decision("run-1", "action-1", "approve", {
      base_url: "https://example.test/v1",
      model: "tool-model",
      api_key: "test-only-key"
    }),
    error => {
      assert.ok(error instanceof real.AIRequestError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "run_active");
      assert.equal(error.message, "执行线程仍在结束");
      return true;
    }
  );
});

test("a completed stream cannot be resurrected by a late successful decision response", async t => {
  const h = await harness(t);
  const decision = deferred();
  h.state.decision = () => decision.promise;
  await h.start(true);
  h.emit("approval", action("first"));
  h.emit("status", { status: "waiting_approval" });
  await flush();
  h.emit("status", { status: "completed" });
  await flush();
  assert.equal(h.ai.busy, false);
  decision.resolve({ status: "running" });
  await flush();
  assert.equal(h.ai.activeRun.status, "completed");
  assert.equal(h.ai.busy, false);
  assert.equal(h.requests.length, 1);
});
