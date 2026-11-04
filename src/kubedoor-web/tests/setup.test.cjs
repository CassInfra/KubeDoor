const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");
const { parse, compileScript } = require("@vue/compiler-sfc");
const { createRenderer, h, nextTick } = require("vue");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

function compile(filename, overrides = {}, vue = false) {
  const target = path.resolve(__dirname, filename);
  let source = fs.readFileSync(target, "utf8");
  if (vue)
    source = compileScript(parse(source, { filename: target }).descriptor, {
      id: "setup-test"
    }).content;
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  const original = instance.require.bind(instance);
  instance.require = id =>
    Object.hasOwn(overrides, id) ? overrides[id] : original(id);
  instance._compile(
    transformSync(source, { loader: "ts", target: "es2020", format: "cjs" })
      .code,
    target
  );
  return instance.exports;
}

const utils = compile("../src/views/setup/setup-utils.ts");
const client = compile("../src/api/setup.ts");
const state = (extra = {}) => ({
  id: "install-1",
  phase: "draft",
  revision: 1,
  step: 2,
  config: {
    postgres: {
      host: "192.0.2.10",
      port: 5432,
      database: "kubedoor",
      user: "kubedoor",
      sslmode: "prefer",
      has_password: true
    },
    metrics: {
      type: "Victoria-Metrics-Single",
      url: "http://192.0.2.11:8428",
      user: "",
      has_password: false,
      write_url: "",
      tag_key: "origin_prometheus"
    },
    admin: { username: "kubedoor", has_password: true },
    public_url: "https://kubedoor.example.com",
    components: { grafana: true, vmalert: true, alertmanager: true }
  },
  environment: {
    namespace: "kubedoor",
    server: "https://kubernetes.default.svc",
    version: "v1.30"
  },
  services: [{ id: "master", name: "控制服务", status: "pending" }],
  checks: [],
  ...extra
});

test("只有独立引导标记可触发安装，已完成或未知状态不重开向导", () => {
  for (const phase of [
    "draft",
    "ready",
    "running",
    "failed",
    "interrupted",
    "finalizing"
  ])
    assert.equal(utils.needsSetup({ phase }), true);
  for (const value of [
    null,
    {},
    { phase: "succeeded" },
    { phase: "database_unavailable" },
    { phase: "error" }
  ])
    assert.equal(utils.needsSetup(value), false);
});

test("单机 VM 保留代理前缀生成写入地址，其他类型必须显式给出", () => {
  assert.equal(
    utils.derivedWriteURL(
      "Victoria-Metrics-Single",
      "https://metrics.example.com/vm/"
    ),
    "https://metrics.example.com/vm/api/v1/write"
  );
  assert.equal(
    utils.derivedWriteURL("Prometheus", "https://metrics.example.com"),
    ""
  );
  assert.equal(
    utils.derivedWriteURL(
      "Victoria-Metrics-Single",
      "https://user:secret@metrics.example.com"
    ),
    ""
  );
  assert.equal(
    utils.metricsPayload({
      ...state().config.metrics,
      type: "Prometheus",
      password: ""
    }).write_url,
    ""
  );
  assert.throws(
    () =>
      utils.metricsPayload({ ...state().config.metrics, tag_key: "1-invalid" }),
    /标签/
  );
});

test("保存过的密码留空不发送，特殊密码原样保留，拒绝回环和无效端口", () => {
  const payload = utils.postgresPayload({
    ...state().config.postgres,
    password: ""
  });
  assert.equal(Object.hasOwn(payload, "password"), false);
  const password = "quote' dollar$ slash\\ 中文";
  assert.equal(
    utils.postgresPayload({ ...payload, password }).password,
    password
  );
  assert.throws(
    () => utils.postgresPayload({ ...payload, has_password: false }),
    /密码/
  );
  for (const host of ["localhost", "127.1.2.3", "::1", "http://db.example.com"])
    assert.throws(
      () => utils.postgresPayload({ ...payload, has_password: true, host }),
      /数据库/
    );
  assert.throws(
    () =>
      utils.postgresPayload({ ...payload, has_password: true, port: 5432.5 }),
    /端口/
  );
  assert.equal(utils.suggestedPublicURL("http://localhost:8080"), "");
  assert.throws(() => utils.publicURL("https://example.com/prefix"), /路径/);
});

test("安装 API 不跟随重定向、不经过登录接口，并保留服务端错误状态", async t => {
  const original = global.fetch;
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url, options });
    return {
      ok: false,
      status: 422,
      json: async () => ({ success: false, message: "没有建表权限" })
    };
  };
  t.after(() => (global.fetch = original));
  await assert.rejects(
    client.setupApi.postgres(7, state().config.postgres),
    error =>
      error instanceof client.SetupRequestError &&
      error.status === 422 &&
      error.message === "没有建表权限"
  );
  assert.equal(calls[0].url, "/setup-api/check/postgres");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(JSON.parse(calls[0].options.body).revision, 7);
});

test("引导标记缺失、非 JSON 或网络不可用时保持正常登录流程", async t => {
  const original = global.fetch;
  t.after(() => (global.fetch = original));
  for (const result of [
    async () => ({ ok: false, status: 404, json: async () => ({}) }),
    async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new Error("HTML");
      }
    }),
    async () => {
      throw new Error("network");
    }
  ]) {
    global.fetch = result;
    assert.equal(await client.getSetupMarker(), null);
  }
});

async function harness(t, options = {}) {
  const originalWindow = global.window;
  global.window = { location: { origin: "https://kubedoor.example.com" } };
  const calls = {
    postgres: [],
    metrics: [],
    configure: [],
    retry: [],
    success: [],
    push: []
  };
  let current = options.initial || state();
  const api = {
    status: async () => ({
      phase: current.phase,
      authenticated: true,
      installation_id: current.id
    }),
    session: async () => ({ authenticated: true }),
    state: async () => current,
    postgres: async (revision, data) => {
      calls.postgres.push({ revision, data });
      current = {
        ...current,
        revision: revision + 1,
        step: 3,
        config: { ...current.config, postgres: { ...data, has_password: true } }
      };
      return current;
    },
    metrics: async (revision, data) => {
      calls.metrics.push({ revision, data });
      return { ...current, step: 4 };
    },
    configure: async (revision, data) => {
      calls.configure.push({ revision, data });
      return { ...current, revision: revision + 1, phase: "ready", step: 5 };
    },
    start: async () => ({ ...current, phase: "running", step: 5 }),
    retry: async revision => {
      calls.retry.push(revision);
      return { ...current, phase: "running", step: 5 };
    },
    ...options.api
  };
  const component = compile(
    "../src/views/setup/index.vue",
    {
      "@/api/setup": {
        ...client,
        setupApi: api,
        getSetupMarker: options.marker || (async () => null)
      },
      "./setup-utils": utils,
      "vue-router": {
        useRouter: () => ({ push: value => calls.push.push(value) })
      },
      "element-plus": {
        ElMessage: { success: value => calls.success.push(value) }
      }
    },
    true
  ).default;
  const renderer = createRenderer({
    createElement: () => ({}),
    createText: () => ({}),
    createComment: () => ({}),
    setText() {},
    setElementText() {},
    parentNode: () => null,
    nextSibling: () => null,
    insert() {},
    remove() {},
    patchProp() {}
  });
  const container = {};
  const vnode = h({ ...component, render: () => null });
  renderer.render(vnode, container);
  t.after(() => {
    renderer.render(null, container);
    global.window = originalWindow;
  });
  for (let i = 0; i < 5; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
  return {
    vm: vnode.component.setupState,
    calls,
    setState: value => (current = value)
  };
}

test("通过实际检测响应才前进，并清空输入密码和采用新版本", async t => {
  const { vm, calls } = await harness(t);
  vm.postgres.password = "new-password";
  await vm.advance();
  assert.equal(vm.step, 3);
  assert.equal(vm.task.revision, 2);
  assert.equal(vm.postgres.password, "");
  assert.equal(calls.postgres[0].data.password, "new-password");
  assert.equal(calls.postgres[0].revision, 1);
});

test("检测失败留在原步骤，获取新版本，保留待修正输入", async t => {
  let current = state();
  const { vm } = await harness(t, {
    api: {
      state: async () => current,
      postgres: async () => {
        current = state({ revision: 2 });
        throw new client.SetupRequestError("数据库密码验证失败", 422);
      }
    }
  });
  vm.postgres.password = "typed-password";
  await vm.advance();
  assert.equal(vm.step, 2);
  assert.equal(vm.task.revision, 2);
  assert.equal(vm.postgres.password, "typed-password");
  assert.match(vm.error, /密码验证失败/);
});

test("恢复后保留已保存管理员密码，未重输不会提交空值覆盖", async t => {
  const { vm, calls } = await harness(t, { initial: state({ step: 4 }) });
  assert.equal(vm.hasAdminPassword, true);
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.deepEqual(calls.configure[0].data.admin, { username: "kubedoor" });
});

test("某个服务失败或全部已就绪但最终检查未完成，都不会提示成功", async t => {
  const failed = state({
    phase: "failed",
    step: 5,
    retryable: true,
    services: [{ id: "grafana", name: "Grafana", status: "failed" }]
  });
  const { vm, calls } = await harness(t, { initial: failed });
  assert.equal(vm.completed, false);
  assert.deepEqual(calls.success, []);
  await vm.advance();
  assert.deepEqual(calls.retry, [1]);
  const ready = state({
    phase: "finalizing",
    step: 5,
    services: [{ id: "grafana", name: "Grafana", status: "ready" }]
  });
  vm.applyState(ready);
  assert.equal(vm.readyServices, 1);
  assert.equal(vm.completed, false);
  assert.deepEqual(calls.success, []);
  vm.applyState({ ...ready, phase: "succeeded" });
  vm.applyState({ ...ready, phase: "succeeded" });
  assert.equal(vm.completed, true);
  assert.equal(calls.success.length, 1);
});

test("完成标记可在安装器关闭后展示结果，重开页面不重复通知", async t => {
  const marker = {
    phase: "succeeded",
    installation_id: "install-1",
    services: [{ id: "master", name: "控制服务", status: "ready" }]
  };
  const { vm, calls } = await harness(t, {
    marker: async () => marker,
    api: {
      status: async () => {
        throw new Error("must not call");
      }
    }
  });
  assert.equal(vm.completed, true);
  assert.equal(vm.services.length, 1);
  assert.deepEqual(calls.success, []);
  vm.enterConsole(true);
  assert.deepEqual(calls.push, [
    { path: "/login", query: { redirect: "/workbench/index" } }
  ]);
});

test("不同安装任务的旧完成标记不能将当前安装判为成功", async t => {
  const { vm, calls } = await harness(t, {
    initial: state({ phase: "running", step: 5 })
  });
  assert.equal(
    vm.applyMarker({ phase: "succeeded", installation_id: "old-install" }),
    false
  );
  assert.equal(vm.completed, false);
  assert.deepEqual(calls.success, []);
});

test("失败后可修改连接并重新检测，修复完成返回逐项结果再重试", async t => {
  const { vm, calls } = await harness(t, {
    initial: state({
      phase: "failed",
      step: 5,
      target_locked: true,
      retryable: true
    })
  });
  vm.editConnections();
  assert.equal(vm.step, 2);
  assert.equal(vm.repairing, true);
  vm.postgres.password = "corrected-password";
  await vm.advance();
  assert.equal(calls.postgres[0].data.password, "corrected-password");
  assert.equal(vm.step, 3);
  assert.deepEqual(calls.retry, []);
  await vm.advance();
  assert.equal(vm.step, 4);
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.equal(vm.repairing, false);
});

test("安装器停止后公开收尾状态仍可恢复，只有最终成功才通知", async t => {
  const marker = {
    phase: "finalizing",
    installation_id: "install-1",
    services: [{ id: "master", name: "控制服务", status: "ready" }],
    error: "权限清理待重试"
  };
  const { vm, calls } = await harness(t, {
    marker: async () => marker,
    api: {
      status: async () => {
        throw new Error("must not need installer");
      }
    }
  });
  assert.equal(vm.authenticated, false);
  assert.equal(vm.publicFinalizing, true);
  assert.equal(vm.step, 5);
  assert.equal(vm.completed, false);
  assert.equal(vm.taskError, "权限清理待重试");
  assert.deepEqual(calls.success, []);
  // Replace the marker object like the JSON fetch result rather than mutating the current ref.
  vm.applyMarker({ ...marker, phase: "succeeded" });
  assert.equal(vm.completed, true);
  assert.equal(calls.success.length, 1);
});

test("收尾任务退回失败时重新获取安装会话和重试入口", async t => {
  let marker = {
    phase: "finalizing",
    installation_id: "install-1",
    services: []
  };
  const failed = state({
    phase: "failed",
    step: 5,
    retryable: true,
    target_locked: true
  });
  const { vm, calls } = await harness(t, {
    initial: failed,
    marker: async () => marker
  });
  assert.equal(vm.publicFinalizing, true);
  marker = { ...marker, phase: "failed" };
  await vm.pollState();
  assert.equal(vm.publicFinalizing, false);
  assert.equal(vm.authenticated, true);
  assert.equal(vm.task.phase, "failed");
  assert.equal(vm.canRetry, true);
  assert.equal(vm.step, 5);
  assert.deepEqual(calls.success, []);
});
