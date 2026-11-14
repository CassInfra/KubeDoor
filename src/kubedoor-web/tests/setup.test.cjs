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

/** vue 传 true 只编译脚本；传对象时作为 compileScript 选项（如 inlineTemplate 连模板一起编译） */
function compile(filename, overrides = {}, vue = false) {
  const target = path.resolve(__dirname, filename);
  let source = fs.readFileSync(target, "utf8");
  if (vue)
    source = compileScript(parse(source, { filename: target }).descriptor, {
      id: "setup-test",
      ...(typeof vue === "object" ? vue : {})
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
      scheme: "http",
      host: "192.0.2.11",
      tenant: "0",
      user: "",
      has_password: false,
      read_url: "http://192.0.2.11:8428",
      write_url: "http://192.0.2.11:8428/api/v1/write",
      tag_key: "origin_prometheus"
    },
    admin: { username: "kubedoor", has_password: true },
    public_url: "https://kubedoor.example.com",
    components: { alerting: true }
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

test("按类型生成远程读写地址，没有账号时不带账号密码", () => {
  const form = { ...state().config.metrics, password: "" };
  assert.deepEqual(utils.metricsEndpoints(form), {
    read_url: "http://192.0.2.11:8428",
    write_url: "http://192.0.2.11:8428/api/v1/write"
  });
  assert.deepEqual(
    utils.metricsEndpoints({
      ...form,
      type: "Victoria-Metrics-Cluster",
      scheme: "https",
      host: "vm.example.com",
      tenant: "5:7"
    }),
    {
      read_url: "https://vm.example.com:8481/select/5:7/prometheus",
      write_url: "https://vm.example.com:8480/insert/5:7/prometheus"
    }
  );
  assert.equal(
    utils.metricsEndpoints({
      ...form,
      type: "Victoria-Metrics-Cluster",
      tenant: " "
    }).write_url,
    "http://192.0.2.11:8480/insert/0/prometheus"
  );
  assert.equal(
    utils.metricsEndpoints({ ...form, host: "fd00::11" }).read_url,
    "http://[fd00::11]:8428"
  );
  assert.deepEqual(utils.metricsEndpoints({ ...form, host: " " }), {
    read_url: "",
    write_url: ""
  });
});

test("有账号时地址带 URL 编码的账号密码，已保存的密码用 ****** 占位", () => {
  const form = { ...state().config.metrics, user: "mon it" };
  assert.equal(
    utils.metricsEndpoints({ ...form, password: "p@ss:w/rd" }).read_url,
    "http://mon%20it:p%40ss%3Aw%2Frd@192.0.2.11:8428"
  );
  assert.equal(
    utils.metricsEndpoints({ ...form, password: "", has_password: true })
      .write_url,
    "http://mon%20it:******@192.0.2.11:8428/api/v1/write"
  );
});

test("时序库提交前校验连接地址、租户、账号和标签", () => {
  const form = { ...state().config.metrics, password: "" };
  const payload = utils.metricsPayload({ ...form, tenant: "9" });
  assert.equal(payload.host, "192.0.2.11");
  assert.equal(payload.tenant, "0");
  assert.equal(Object.hasOwn(payload, "password"), false);
  assert.equal(
    utils.metricsPayload({ ...form, host: "[fd00::11]" }).host,
    "fd00::11"
  );
  for (const host of [
    "192.0.2.11:8428",
    "http://192.0.2.11",
    "vm.example.com/prefix",
    "user@vm.example.com",
    "localhost",
    ""
  ])
    assert.throws(() => utils.metricsPayload({ ...form, host }), /连接地址/);
  assert.throws(
    () =>
      utils.metricsPayload({
        ...form,
        type: "Victoria-Metrics-Cluster",
        tenant: "abc"
      }),
    /租户/
  );
  assert.equal(
    utils.metricsPayload({
      ...form,
      type: "Victoria-Metrics-Cluster",
      tenant: "3:4"
    }).tenant,
    "3:4"
  );
  assert.throws(
    () => utils.metricsPayload({ ...form, tag_key: "1-invalid" }),
    /标签/
  );
  assert.throws(
    () => utils.metricsPayload({ ...form, user: "monit" }),
    /请填写密码/
  );
  assert.throws(
    () => utils.metricsPayload({ ...form, password: "secret" }),
    /请填写账号/
  );
  const saved = utils.metricsPayload({
    ...form,
    user: "monit",
    has_password: true,
    read_url: "http://monit:******@192.0.2.11:8428/",
    write_url: "http://monit:******@192.0.2.11:8428/api/v1/write"
  });
  assert.equal(Object.hasOwn(saved, "password"), false);
  assert.equal(saved.read_url, "http://monit:******@192.0.2.11:8428");
  assert.throws(
    () =>
      utils.metricsPayload({
        ...form,
        read_url: "http://192.0.2.11:8428?tenant=1"
      }),
    /远程读地址/
  );
  assert.throws(
    () => utils.metricsPayload({ ...form, write_url: "http://127.0.0.1:8428" }),
    /远程写地址/
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

async function settle() {
  for (let i = 0; i < 5; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
}

/** 安装服务检测通过后会把检查项写进状态 */
function withCheck(value, name, status = "success") {
  return {
    ...value,
    checks: [
      ...(value.checks || []).filter(check => check.name !== name),
      { id: name, name, status, message: `${name} 检测结果` }
    ]
  };
}

async function harness(t, options = {}) {
  const originalWindow = global.window;
  global.window = {
    location: { origin: options.origin || "https://kubedoor.example.com" }
  };
  const calls = {
    postgres: [],
    metrics: [],
    configure: [],
    retry: [],
    installAgent: [],
    skipAgent: [],
    state: 0,
    success: [],
    errors: [],
    copied: [],
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
    state: async () => {
      calls.state++;
      return current;
    },
    postgres: async (revision, data) => {
      calls.postgres.push({ revision, data });
      current = withCheck(
        {
          ...current,
          revision: revision + 1,
          step: 3,
          config: {
            ...current.config,
            postgres: { ...data, has_password: true }
          }
        },
        "postgres"
      );
      return current;
    },
    metrics: async (revision, data) => {
      calls.metrics.push({ revision, data });
      current = withCheck(
        { ...current, revision: revision + 1, step: 4 },
        "metrics"
      );
      return current;
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
    installAgent: async (revision, data, force) => {
      calls.installAgent.push({
        revision,
        options: data,
        ...(force ? { force } : {})
      });
      current = {
        ...current,
        revision: revision + 1,
        agent: {
          ...current.agent,
          status: "running",
          options: data,
          steps: [{ name: "验证安装计划", status: "running" }]
        }
      };
      return current;
    },
    skipAgent: async revision => {
      calls.skipAgent.push(revision);
      current = {
        ...current,
        revision: revision + 1,
        phase: "finalizing",
        agent: { status: "skipped" }
      };
      return current;
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
        ElMessage: {
          success: value => calls.success.push(value),
          error: value => calls.errors.push(value)
        }
      },
      "@/utils/clipboard": {
        copyText: async value => {
          calls.copied.push(value);
          return options.copyResult ?? true;
        }
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
  await settle();
  return {
    vm: vnode.component.setupState,
    calls,
    setState: value => (current = value)
  };
}

test("测试连接通过后停在本步显示结果，点继续才进入下一步", async t => {
  const { vm, calls } = await harness(t);
  vm.postgres.password = "new-password";
  assert.equal(vm.actionText, "测试连接");
  await vm.advance();
  assert.equal(vm.step, 2);
  assert.equal(vm.task.revision, 2);
  assert.equal(vm.postgres.password, "");
  assert.equal(calls.postgres[0].data.password, "new-password");
  assert.equal(calls.postgres[0].revision, 1);
  assert.equal(vm.connectionReady, true);
  assert.equal(vm.actionText, "继续");
  assert.equal(vm.groupChecks[0].status, "success");
  assert.equal(utils.serviceStatus[vm.groupChecks[0].status], "检查通过");
  await vm.advance();
  assert.equal(vm.step, 3);
  assert.equal(calls.postgres.length, 1);
});

test("测试通过后改动表单要重新测试，也可以点重新测试再测一次", async t => {
  const { vm, calls } = await harness(t);
  await vm.advance();
  assert.equal(vm.connectionReady, true);
  vm.edited();
  assert.equal(vm.connectionReady, false);
  assert.equal(vm.actionText, "测试连接");
  assert.deepEqual(vm.groupChecks, []);
  await vm.advance();
  assert.equal(calls.postgres.length, 2);
  assert.equal(vm.step, 2);
  assert.equal(vm.actionText, "继续");
  await vm.testConnection();
  assert.equal(calls.postgres.length, 3);
  assert.equal(vm.step, 2);
});

test("检测失败留在原步骤显示失败结果，获取新版本，保留待修正输入", async t => {
  let current = state();
  const { vm } = await harness(t, {
    api: {
      state: async () => current,
      postgres: async () => {
        current = withCheck(state({ revision: 2 }), "postgres", "error");
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
  assert.equal(vm.groupChecks[0].status, "error");
  assert.equal(vm.connectionReady, false);
  assert.equal(vm.actionText, "测试连接");
});

for (const status of ["success", "warning"])
  test(`时序库测试结果为 ${status} 时停在第 3 步显示结果，点继续进入下一步`, async t => {
    const sent = [];
    let current = state({ step: 3 });
    const { vm } = await harness(t, {
      initial: current,
      api: {
        state: async () => current,
        metrics: async (revision, data) => {
          sent.push(data);
          current = withCheck(
            state({ step: 4, revision: revision + 1 }),
            "metrics",
            status
          );
          return current;
        }
      }
    });
    assert.equal(vm.step, 3);
    assert.equal(vm.actionText, "测试连接");
    await vm.advance();
    assert.equal(sent.length, 1);
    assert.equal(vm.step, 3);
    assert.equal(vm.actionText, "继续");
    assert.equal(vm.groupChecks[0].status, status);
    await vm.advance();
    assert.equal(vm.step, 4);
    assert.equal(sent.length, 1);
  });

test("连接测试只有检查通过和请留意才算通过", () => {
  const check = status => [{ id: "metrics", name: "metrics", status }];
  for (const status of ["success", "warning"])
    assert.equal(utils.connectionPassed(check(status), "metrics"), true);
  for (const status of ["error", "running", "pending"])
    assert.equal(utils.connectionPassed(check(status), "metrics"), false);
  assert.equal(utils.connectionPassed(check("success"), "postgres"), false);
  // 安装执行器写的检查项只有 name
  assert.equal(
    utils.connectionPassed(
      [{ name: "postgres", status: "success" }],
      "postgres"
    ),
    true
  );
  assert.equal(utils.connectionPassed(undefined, "postgres"), false);
  assert.equal(utils.serviceStatus.warning, "请留意");
});

test("载入时保留已保存的地址，改类型、租户或账号才重新生成", async t => {
  const initial = state({ step: 3 });
  initial.config.metrics.read_url = "http://192.0.2.11:9428";
  const { vm } = await harness(t, { initial });
  assert.equal(vm.metrics.read_url, "http://192.0.2.11:9428");
  vm.metrics.type = "Victoria-Metrics-Cluster";
  vm.metrics.tenant = "2";
  vm.metrics.user = "monit";
  vm.metrics.password = "pw";
  vm.regenerateEndpoints();
  assert.equal(
    vm.metrics.read_url,
    "http://monit:pw@192.0.2.11:8481/select/2/prometheus"
  );
  assert.equal(
    vm.metrics.write_url,
    "http://monit:pw@192.0.2.11:8480/insert/2/prometheus"
  );
});

test("恢复后保留已保存管理员密码，未重输不会提交空值覆盖", async t => {
  const { vm, calls } = await harness(t, { initial: state({ step: 4 }) });
  assert.equal(vm.hasAdminPassword, true);
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.deepEqual(calls.configure[0].data.admin, { username: "kubedoor" });
  assert.deepEqual(calls.configure[0].data.components, { alerting: true });
});

test("告警组件默认安装，关闭开关后三个组件都不安装", async t => {
  const { vm, calls } = await harness(t, { initial: state({ step: 4 }) });
  assert.equal(vm.basics.alerting, true);
  vm.basics.alerting = false;
  await vm.advance();
  assert.deepEqual(calls.configure[0].data.components, { alerting: false });
  const saved = state({ step: 4 });
  saved.config.components = { alerting: false };
  const restored = await harness(t, { initial: saved });
  assert.equal(restored.vm.basics.alerting, false);
});

test("访问地址自动获取：优先用当前页面地址，端口转发打开时用 Web 的 NodePort 地址", async t => {
  assert.equal(
    utils.autoPublicURL("http://192.0.2.10:30080", "http://192.0.2.20:30080"),
    "http://192.0.2.10:30080"
  );
  assert.equal(
    utils.autoPublicURL("http://127.0.0.1:8080", "http://192.0.2.20:30080/"),
    "http://192.0.2.20:30080"
  );
  assert.equal(
    utils.autoPublicURL("http://localhost:8080", "http://[fd00::20]:30080"),
    "http://[fd00::20]:30080"
  );
  assert.equal(utils.autoPublicURL("http://localhost:8080", ""), "");
  const initial = state({
    step: 4,
    environment: {
      namespace: "kubedoor",
      web_url: "http://192.0.2.20:30080"
    }
  });
  initial.config.public_url = "";
  const { vm, calls } = await harness(t, {
    initial,
    origin: "http://127.0.0.1:8080"
  });
  assert.equal(vm.basics.public_url, "http://192.0.2.20:30080");
  assert.match(vm.publicURLNote, /NodePort/);
  assert.equal(vm.editingURL, false);
  vm.editURL();
  assert.equal(vm.editingURL, true);
  vm.basics.public_url = "https://kubedoor.example.com";
  assert.match(vm.publicURLNote, /不能填写本机地址/);
  vm.resetURL();
  assert.equal(vm.basics.public_url, "http://192.0.2.20:30080");
  assert.equal(vm.editingURL, false);
  await vm.advance();
  assert.equal(calls.configure[0].data.public_url, "http://192.0.2.20:30080");
});

test("已保存的访问地址优先，取不到自动地址时需要填写", async t => {
  const { vm } = await harness(t, { initial: state({ step: 4 }) });
  assert.equal(vm.basics.public_url, "https://kubedoor.example.com");
  assert.match(vm.publicURLNote, /当前打开安装页面/);
  const empty = state({ step: 4 });
  empty.config.public_url = "";
  const missing = await harness(t, {
    initial: empty,
    origin: "http://localhost:8080"
  });
  assert.equal(missing.vm.basics.public_url, "");
  assert.match(missing.vm.publicURLNote, /未能自动获取/);
  await missing.vm.advance();
  assert.equal(missing.vm.step, 4);
  assert.match(missing.vm.error, /访问地址/);
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
  assert.equal(vm.step, 2);
  await vm.advance();
  assert.equal(vm.step, 3);
  assert.deepEqual(calls.retry, []);
  await vm.advance();
  assert.equal(calls.metrics.length, 1);
  assert.equal(vm.step, 3);
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
  // 收尾和完成结果显示在最后一步（第 6 步）
  assert.equal(vm.step, 6);
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

const agentOptions = {
  env: "prod-k8s",
  aliases: ["生产"],
  namespace: "kubedoor-agent",
  mode: "full",
  ksm: "auto",
  node_exporter: "auto"
};

// 控制端全部就绪，等待选择是否在本集群安装 Agent
const agentState = (agent = {}, extra = {}) =>
  state({
    phase: "agent",
    step: 6,
    revision: 5,
    services: [{ id: "master", name: "控制服务", status: "ready" }],
    agent: {
      status: "choice",
      deadline: "2026-11-06T09:00:00Z",
      defaults: {
        env: "",
        namespace: "kubedoor-agent",
        mode: "full",
        ksm: "auto",
        node_exporter: "auto"
      },
      ...agent
    },
    ...extra
  });

test("Agent 安装选项按安装服务的规则校验，别名去重去空", () => {
  const form = {
    install: true,
    env: " prod-k8s ",
    aliases: "生产, prod\n生产，，  \n",
    namespace: "kubedoor-agent",
    mode: "full",
    ksm: "install",
    node_exporter: "reuse"
  };
  assert.deepEqual(utils.agentPayload(form), {
    env: "prod-k8s",
    aliases: ["生产", "prod"],
    namespace: "kubedoor-agent",
    mode: "full",
    ksm: "install",
    node_exporter: "reuse"
  });
  assert.throws(
    () => utils.agentPayload({ ...form, env: " " }),
    /请填写集群名称/
  );
  for (const env of ["prod k8s", "prod.k8s", "生产", "a".repeat(101)])
    assert.throws(() => utils.agentPayload({ ...form, env }), /集群名称只能/);
  assert.equal(
    utils.agentPayload({ ...form, env: "A_b-1".padEnd(100, "x") }).env.length,
    100
  );
  for (const namespace of [
    "",
    "Kubedoor",
    "-kubedoor",
    "kubedoor-",
    "kube_door",
    "a".repeat(64)
  ])
    assert.throws(() => utils.agentPayload({ ...form, namespace }), /命名空间/);
  assert.equal(
    utils.agentPayload({ ...form, namespace: "a".repeat(63) }).namespace,
    "a".repeat(63)
  );
  const aliases = Array.from({ length: 21 }, (_, index) => `alias-${index}`);
  assert.throws(
    () => utils.agentPayload({ ...form, aliases: aliases.join(",") }),
    /最多 20 个/
  );
  assert.equal(
    utils.agentPayload({ ...form, aliases: aliases.slice(0, 20).join("\n") })
      .aliases.length,
    20
  );
  assert.throws(
    () => utils.agentPayload({ ...form, aliases: "x".repeat(101) }),
    /最长 100/
  );
  assert.deepEqual(
    utils.agentPayload({ ...form, aliases: "中".repeat(100) }).aliases,
    ["中".repeat(100)]
  );
  assert.deepEqual(
    utils.agentPayload({ ...form, aliases: " , \n" }).aliases,
    []
  );
  assert.throws(
    () => utils.agentPayload({ ...form, mode: "vmagent" }),
    /指标采集/
  );
  assert.throws(
    () => utils.agentPayload({ ...form, ksm: "yes" }),
    /kube-state-metrics/
  );
  assert.throws(
    () => utils.agentPayload({ ...form, node_exporter: "" }),
    /node-exporter/
  );
});

test("Agent 阶段、收尾和完成结果在第 6 步，控制端安装中和失败仍在第 5 步", () => {
  assert.equal(utils.SETUP_STEPS, 6);
  for (const phase of ["agent", "finalizing", "succeeded"])
    assert.equal(utils.stateStep({ phase, step: 5 }), 6);
  for (const phase of ["ready", "running", "failed", "interrupted"])
    assert.equal(utils.stateStep({ phase, step: 6 }), 5);
  assert.equal(utils.stateStep({ phase: "draft", step: 3 }), 3);
  assert.equal(utils.stateStep({ phase: "draft", step: 6 }), 5);
  assert.equal(utils.needsSetup({ phase: "agent" }), true);
  assert.equal(utils.hasInstallation("agent"), true);
  for (const status of ["choice", "failed", undefined])
    assert.equal(utils.isInstalling("agent", status), false);
  for (const status of ["running", "succeeded", "partial", "skipped"])
    assert.equal(utils.isInstalling("agent", status), true);
  assert.equal(utils.isInstalling("running"), true);
  assert.equal(utils.isInstalling("finalizing"), true);
  assert.equal(utils.isInstalling("failed"), false);
});

test("Agent 表单优先按上次提交的选项预填，完成页按结果给出说明", () => {
  assert.deepEqual(
    utils.agentFormFrom({
      status: "choice",
      defaults: {
        env: "prod-k8s",
        namespace: "kubedoor-agent",
        mode: "agent",
        ksm: "bad"
      }
    }),
    {
      install: false,
      env: "prod-k8s",
      aliases: "",
      namespace: "kubedoor-agent",
      mode: "agent",
      ksm: "auto",
      node_exporter: "auto"
    }
  );
  assert.deepEqual(
    utils.agentFormFrom({
      status: "failed",
      defaults: { env: "other" },
      options: {
        ...agentOptions,
        aliases: ["生产", "prod"],
        ksm: "install",
        node_exporter: "reuse"
      }
    }),
    {
      install: true,
      env: "prod-k8s",
      aliases: "生产, prod",
      namespace: "kubedoor-agent",
      mode: "full",
      ksm: "install",
      node_exporter: "reuse"
    }
  );
  assert.equal(utils.agentFormFrom().namespace, "kubedoor");

  assert.equal(utils.agentOutcome(undefined), undefined);
  const ok = utils.agentOutcome({ status: "succeeded", env: "prod-k8s" });
  assert.equal(ok.installed, true);
  assert.equal(ok.tone, "success");
  assert.equal(ok.title, "已在本集群安装 Agent：prod-k8s");
  const partial = utils.agentOutcome({
    status: "partial",
    env: "prod-k8s",
    message: "最近 5 分钟没有查到 prod-k8s 的指标"
  });
  assert.equal(partial.installed, true);
  assert.equal(partial.tone, "warning");
  assert.equal(partial.note, "最近 5 分钟没有查到 prod-k8s 的指标");
  assert.match(
    utils.agentOutcome({ status: "unavailable", message: "x" }).note,
    /未授予临时集群权限.*上传 kubeconfig/
  );
  const skipped = utils.agentOutcome({ status: "skipped" });
  assert.equal(skipped.installed, false);
  assert.equal(skipped.title, "未在本集群安装 Agent");
  // 安装状态里只有 options.env 时也能显示集群名称
  assert.equal(
    utils.agentOutcome({ status: "succeeded", options: { env: "ops" } }).title,
    "已在本集群安装 Agent：ops"
  );

  const now = new Date(2026, 10, 6, 13, 0);
  assert.equal(
    utils.deadlineText(new Date(2026, 10, 6, 15, 5).toISOString(), now),
    "15:05"
  );
  assert.equal(
    utils.deadlineText(new Date(2026, 10, 7, 1, 30).toISOString(), now),
    "11-07 01:30"
  );
  assert.equal(utils.deadlineText("", now), "");
  assert.equal(utils.deadlineText("not-a-date", now), "");
  assert.equal(utils.toneMark("success"), "✓");
  assert.equal(utils.toneMark("error"), "!");
  assert.equal(utils.toneMark("muted"), "·");
});

test("第 6 步勾选安装时校验选项后提交，之后锁定页面、不能回到前面的步骤", async t => {
  const { vm, calls } = await harness(t, { initial: agentState() });
  assert.equal(vm.step, 6);
  assert.equal(vm.highestStep, 6);
  assert.equal(vm.executing, true);
  assert.equal(vm.locked, false);
  assert.equal(vm.agentChoosing, true);
  assert.ok(vm.agentDeadline);
  assert.equal(vm.agentForm.install, false);
  assert.equal(vm.agentForm.namespace, "kubedoor-agent");
  vm.goStep(4);
  assert.equal(vm.step, 6);
  vm.agentForm.install = true;
  assert.equal(vm.actionText, "安装 Agent");
  await vm.advance();
  assert.match(vm.error, /请填写集群名称/);
  assert.deepEqual(calls.installAgent, []);
  vm.agentForm.env = "prod-k8s";
  vm.agentForm.aliases = "生产，prod\n生产";
  vm.agentForm.ksm = "install";
  await vm.advance();
  assert.equal(vm.error, "");
  assert.deepEqual(calls.installAgent, [
    {
      revision: 5,
      options: {
        env: "prod-k8s",
        aliases: ["生产", "prod"],
        namespace: "kubedoor-agent",
        mode: "full",
        ksm: "install",
        node_exporter: "auto"
      }
    }
  ]);
  assert.deepEqual(calls.skipAgent, []);
  assert.equal(vm.task.revision, 6);
  assert.equal(vm.agentStatus, "running");
  assert.equal(vm.running, true);
  assert.equal(vm.locked, true);
  assert.equal(vm.agentChoosing, false);
  assert.equal(vm.actionText, "正在安装 Agent…");
  await vm.advance();
  assert.equal(calls.installAgent.length, 1);
  assert.equal(vm.step, 6);
});

test("第 6 步不勾选安装时直接完成，收尾和结果都在最后一步", async t => {
  const { vm, calls } = await harness(t, { initial: agentState() });
  assert.equal(vm.actionText, "完成安装");
  await vm.advance();
  assert.deepEqual(calls.skipAgent, [5]);
  assert.deepEqual(calls.installAgent, []);
  assert.equal(vm.task.phase, "finalizing");
  assert.equal(vm.step, 6);
  assert.equal(vm.running, true);
  assert.equal(vm.actionText, "正在完成安装…");
  assert.equal(vm.agentResult.title, "未在本集群安装 Agent");
  assert.deepEqual(calls.success, []);
  vm.applyState({ ...vm.task, phase: "succeeded" });
  assert.equal(vm.completed, true);
  assert.equal(calls.success.length, 1);
});

test("Agent 安装失败后按上次选项预填，可改选项重试，也可跳过直接完成", async t => {
  const { vm, calls, setState } = await harness(t, {
    initial: agentState({
      status: "failed",
      options: agentOptions,
      error: "等待 Agent 上线超时",
      steps: [
        { name: "验证安装计划", status: "success" },
        { name: "等待 Agent 上线", status: "error", message: "超时" }
      ]
    })
  });
  assert.equal(vm.agentChoosing, true);
  assert.equal(vm.locked, false);
  assert.deepEqual(
    { ...vm.agentForm },
    { ...agentOptions, install: true, aliases: "生产" }
  );
  assert.equal(vm.agentDone, 1);
  assert.equal(vm.actionText, "重新安装 Agent");
  vm.agentForm.namespace = "kubedoor-ops";
  vm.agentForm.mode = "agent";
  await vm.advance();
  const submitted = {
    ...agentOptions,
    namespace: "kubedoor-ops",
    mode: "agent"
  };
  assert.deepEqual(calls.installAgent, [{ revision: 5, options: submitted }]);
  assert.equal(vm.agentStatus, "running");
  // 安装中退回失败时按实际提交的选项回填（安装中表单已锁定）
  vm.agentForm.namespace = "stale";
  setState(
    agentState(
      { status: "failed", options: submitted, error: "镜像拉取失败" },
      { revision: 7 }
    )
  );
  await vm.pollState();
  assert.equal(vm.agentStatus, "failed");
  assert.equal(vm.agentForm.namespace, "kubedoor-ops");
  assert.equal(vm.agentForm.mode, "agent");
  await vm.submitAgent(true);
  assert.deepEqual(calls.skipAgent, [7]);
  assert.equal(calls.installAgent.length, 1);
  assert.equal(vm.task.phase, "finalizing");
  assert.equal(vm.step, 6);
});

test("提交 Agent 安装被安装服务拒绝时留在第 6 步，保留输入并采用新版本", async t => {
  let current = agentState();
  const { vm } = await harness(t, {
    initial: current,
    api: {
      state: async () => current,
      installAgent: async () => {
        current = agentState(
          {
            status: "failed",
            checks: [
              {
                name: "命名空间",
                status: "error",
                message: "kubedoor-agent 已有其他应用"
              }
            ]
          },
          { revision: 6 }
        );
        throw new client.SetupRequestError("安装前检查未通过", 422);
      }
    }
  });
  vm.agentForm.install = true;
  vm.agentForm.env = "prod-k8s";
  vm.agentForm.aliases = "生产";
  await vm.advance();
  assert.match(vm.error, /安装前检查未通过/);
  assert.equal(vm.task.revision, 6);
  assert.equal(vm.step, 6);
  assert.equal(vm.agentForm.install, true);
  assert.equal(vm.agentForm.env, "prod-k8s");
  assert.equal(vm.agentForm.aliases, "生产");
  assert.equal(vm.agentChecks.length, 1);
  assert.equal(vm.actionText, "重新安装 Agent");
});

test("安装 Agent 时每 2.5 秒刷新进度，Agent 上线后进入收尾", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const steps = [
    { name: "验证安装计划", status: "success" },
    { name: "应用安装资源", status: "running" },
    { name: "等待 Agent 上线", status: "pending" }
  ];
  const { vm, calls, setState } = await harness(t, {
    initial: agentState({ status: "running", options: agentOptions, steps })
  });
  assert.equal(vm.step, 6);
  assert.equal(vm.running, true);
  assert.equal(vm.locked, true);
  assert.equal(vm.agentDone, 1);
  assert.equal(vm.actionText, "正在安装 Agent…");
  await vm.advance();
  assert.deepEqual(calls.installAgent, []);
  assert.deepEqual(calls.skipAgent, []);
  const polled = calls.state;
  const message = "Agent 已上线，最近 5 分钟没有查到 prod-k8s 的指标";
  setState(
    agentState({
      status: "partial",
      message,
      options: agentOptions,
      steps: steps.map(item => ({ ...item, status: "success" }))
    })
  );
  t.mock.timers.tick(2499);
  await settle();
  assert.equal(calls.state, polled);
  t.mock.timers.tick(1);
  await settle();
  assert.equal(calls.state, polled + 1);
  assert.equal(vm.agentStatus, "partial");
  assert.equal(vm.agentDone, 3);
  assert.equal(vm.locked, true);
  assert.equal(vm.actionText, "正在完成安装…");
  // partial / succeeded 后安装服务自动收尾，结果显示在最后一步
  setState(
    state({
      phase: "finalizing",
      step: 6,
      agent: { status: "partial", env: "prod-k8s", message }
    })
  );
  t.mock.timers.tick(2500);
  await settle();
  assert.equal(vm.task.phase, "finalizing");
  assert.equal(vm.step, 6);
  assert.equal(vm.agentResult.tone, "warning");
  assert.equal(vm.agentResult.title, "已在本集群安装 Agent：prod-k8s");
  assert.equal(vm.agentResult.note, message);
  assert.deepEqual(calls.success, []);
});

test("等待选择时慢速刷新状态，不覆盖正在填写的内容，到期自动完成后显示结果", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { vm, calls, setState } = await harness(t, { initial: agentState() });
  const polled = calls.state;
  t.mock.timers.tick(2500);
  await settle();
  assert.equal(calls.state, polled);
  vm.agentForm.install = true;
  vm.agentForm.env = "typed-name";
  t.mock.timers.tick(12500);
  await settle();
  assert.equal(calls.state, polled + 1);
  assert.equal(vm.agentForm.install, true);
  assert.equal(vm.agentForm.env, "typed-name");
  setState(
    state({
      phase: "finalizing",
      step: 6,
      agent: {
        status: "skipped",
        message: "超过 2 小时没有选择，已按不安装完成"
      }
    })
  );
  t.mock.timers.tick(15000);
  await settle();
  assert.equal(vm.task.phase, "finalizing");
  assert.equal(vm.agentResult.installed, false);
  assert.equal(vm.agentResult.note, "超过 2 小时没有选择，已按不安装完成");
});

test("提交前已发出的轮询结果不会覆盖提交结果", async t => {
  let release;
  let hold = false;
  const { vm, calls } = await harness(t, {
    initial: agentState(),
    api: {
      state: () =>
        hold
          ? new Promise(resolve => (release = () => resolve(agentState())))
          : Promise.resolve(agentState())
    }
  });
  hold = true;
  const polling = vm.pollState();
  vm.agentForm.install = true;
  vm.agentForm.env = "prod-k8s";
  await vm.advance();
  assert.equal(calls.installAgent.length, 1);
  assert.equal(vm.agentStatus, "running");
  release();
  await polling;
  assert.equal(vm.agentStatus, "running");
  assert.equal(vm.task.revision, 6);
});

test("完成标记带回本集群 Agent 结果，环境检查显示临时集群权限", async t => {
  const marker = {
    phase: "succeeded",
    installation_id: "install-1",
    services: [{ id: "master", name: "控制服务", status: "ready" }],
    agent: { status: "succeeded", env: "prod-k8s" }
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
  assert.equal(vm.step, 6);
  assert.equal(vm.agentResult.installed, true);
  assert.equal(vm.agentResult.title, "已在本集群安装 Agent：prod-k8s");
  vm.applyMarker({ ...marker, agent: { status: "unavailable" } });
  assert.equal(vm.agentResult.installed, false);
  assert.match(vm.agentResult.note, /未授予临时集群权限/);
  vm.applyMarker({ ...marker, agent: undefined });
  assert.equal(vm.agentResult, undefined);
  assert.deepEqual(calls.success, []);
  assert.equal(
    vm.checkName({ id: "agent_installer", name: "agent_installer" }),
    "本集群 Agent 安装权限"
  );
});

test("Agent 阶段不会重试控制端安装，也不能回去修改连接设置", async t => {
  const { vm, calls } = await harness(t, {
    initial: agentState({}, { retryable: true, target_locked: true })
  });
  assert.equal(vm.executing, true);
  assert.equal(vm.canRetry, false);
  vm.editConnections();
  vm.goStep(4);
  assert.equal(vm.step, 6);
  assert.equal(vm.repairing, false);
  await vm.advance();
  assert.deepEqual(calls.retry, []);
  assert.deepEqual(calls.skipAgent, [5]);
});

test("环境检查显示本集群 Agent 安装权限，最终检查列表不重复显示", async t => {
  // 安装服务把环境检查和安装权限都记在顶层 checks 里，environment 不带 checks
  const { vm } = await harness(t, {
    initial: state({
      step: 1,
      checks: [
        { id: "environment", name: "environment", status: "success" },
        {
          id: "agent_installer",
          name: "agent_installer",
          status: "warning",
          message: "未授予在本集群安装 Agent 的临时集群权限"
        },
        { id: "postgres", name: "postgres", status: "success" }
      ]
    })
  });
  assert.equal(vm.step, 1);
  assert.deepEqual(
    vm.environmentChecks.map(check => check.id),
    ["environment", "agent_installer"]
  );
  assert.equal(vm.agentInstallerCheck.status, "warning");
  assert.equal(vm.checkName(vm.agentInstallerCheck), "本集群 Agent 安装权限");
  assert.equal(vm.checkName({ name: "cleanup" }), "收尾与临时权限清理");
  assert.deepEqual(
    vm.finalChecks.map(check => check.id),
    ["environment", "postgres"]
  );
  // 只是提示，未授予权限也能继续安装控制端
  await vm.advance();
  assert.equal(vm.error, "");
  assert.equal(vm.step, 2);
});

/**
 * 用真正的模板渲染安装页（自定义渲染器记录节点树，不需要浏览器），
 * 返回根节点和查找元素、取文字的辅助函数。不传 api 时只能渲染完成页（访问安装服务直接报错）。
 */
async function renderPage(t, marker, api) {
  const originalWindow = global.window;
  // 复制按钮交给的内容（真实的 copyText 在 HTTP 下先用 execCommand 兜底）
  const copied = [];
  global.window = { location: { origin: "https://kubedoor.example.com" } };
  const component = compile(
    "../src/views/setup/index.vue",
    {
      "@/api/setup": {
        ...client,
        setupApi: api || {
          status: async () => {
            throw new Error("完成页不应访问安装服务");
          }
        },
        getSetupMarker: async () => marker
      },
      "./setup-utils": utils,
      "vue-router": { useRouter: () => ({ push() {} }) },
      "element-plus": { ElMessage: { success() {}, error() {} } },
      "@/utils/clipboard": {
        copyText: async value => {
          copied.push(value);
          return true;
        }
      }
    },
    { inlineTemplate: true }
  ).default;
  // 表单的 v-model 指令会监听事件、读下拉框的 options
  const node = fields => ({
    children: [],
    parent: null,
    props: {},
    options: [],
    addEventListener() {},
    removeEventListener() {},
    ...fields
  });
  const detach = child => {
    const parent = child.parent;
    if (parent) parent.children.splice(parent.children.indexOf(child), 1);
    child.parent = null;
  };
  const attach = (child, parent, anchor) => {
    detach(child);
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index >= 0) parent.children.splice(index, 0, child);
    else parent.children.push(child);
    child.parent = parent;
  };
  const renderer = createRenderer({
    createElement: tag => node({ tag }),
    createText: text => node({ text }),
    createComment: () => node({ text: "" }),
    setText: (target, text) => (target.text = text),
    setElementText: (element, text) => {
      element.children.forEach(child => (child.parent = null));
      element.children = [];
      if (text) attach(node({ text }), element);
    },
    parentNode: target => target.parent,
    nextSibling: target =>
      target.parent
        ? target.parent.children[target.parent.children.indexOf(target) + 1] ||
          null
        : null,
    insert: attach,
    remove: detach,
    patchProp: (element, key, _previous, next) => (element.props[key] = next),
    insertStaticContent: (content, parent, anchor) => {
      const text = node({ text: content.replace(/<[^>]+>/g, " ") });
      attach(text, parent, anchor);
      return [text, text];
    }
  });
  const root = node({ tag: "root" });
  renderer.render(h(component), root);
  t.after(() => {
    renderer.render(null, root);
    global.window = originalWindow;
  });
  await settle();
  const textOf = target =>
    (target.text || "") + target.children.map(textOf).join("");
  const findAll = (predicate, from = root) =>
    from.children.flatMap(child => [
      ...(predicate(child) ? [child] : []),
      ...findAll(predicate, child)
    ]);
  return { root, textOf, findAll, copied };
}

test("安装结果列出每个服务的 Deployment 名，旧的完成标记没有名称时不显示", async t => {
  const page = await renderPage(t, {
    phase: "succeeded",
    installation_id: "install-1",
    services: [
      {
        id: "alarm",
        name: "告警服务",
        resource: "kubedoor-alarm",
        status: "ready"
      },
      { id: "grafana", name: "Grafana 看板", status: "ready" }
    ]
  });
  const rows = page.findAll(item => item.props["data-service"]);
  assert.deepEqual(
    rows.map(row => row.props["data-service"]),
    ["alarm", "grafana"]
  );
  const resourceOf = row =>
    page
      .findAll(item => item.props.class === "service-resource", row)
      .map(page.textOf);
  assert.deepEqual(resourceOf(rows[0]), ["kubedoor-alarm"]);
  assert.match(page.textOf(rows[0]), /告警服务kubedoor-alarm已就绪/);
  assert.deepEqual(resourceOf(rows[1]), []);
});

/* ---------- 第 4 步：告警通知（可选） ---------- */

const WECOM_URL =
  "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-test-key";
const DING_URL =
  "https://oapi.dingtalk.com/robot/send?access_token=ding-test-token";

test("告警通知：按 IM 类型给提示，已保存时留空保留，第 5 步摘要显示 IM 类型", () => {
  const saved = { type: "wecom", at: "zhangsan", configured: true };
  const form = utils.notificationFormFrom(saved);
  // 机器人地址不回传，预填时一律留空；@ 人沿用已保存的
  assert.deepEqual(form, {
    type: "wecom",
    token: "",
    at: "zhangsan",
    clear: false
  });
  assert.deepEqual(utils.notificationFormFrom(undefined), {
    type: "wecom",
    token: "",
    at: "",
    clear: false
  });
  assert.deepEqual(utils.NOTIFY_TYPES, [
    "wecom",
    "dingding",
    "feishu",
    "slack"
  ]);
  assert.match(utils.notifyHint("wecom").at, /userid/);
  assert.match(utils.notifyHint("dingding").at, /手机号/);
  assert.match(utils.notifyHint("feishu").at, /open_id/);
  assert.match(utils.notifyHint("slack").at, /成员 ID/);
  assert.match(utils.notifyHint("dingding").token, /access_token= 后面那一段/);
  assert.match(utils.notifyHint("unknown").token, /key= 后面那一段/);
  assert.equal(
    utils.notificationTokenPlaceholder(form, saved),
    "已保存，留空保留"
  );
  assert.equal(
    utils.notificationTokenPlaceholder({ ...form, type: "feishu" }, saved),
    "换了 IM 类型，请重新填写飞书机器人地址"
  );
  assert.equal(
    utils.notificationTokenPlaceholder({ ...form, clear: true }, saved),
    "已清除，留空表示不发通知"
  );
  assert.equal(
    utils.notificationTokenPlaceholder(
      { ...utils.notificationFormFrom(), type: "slack" },
      undefined
    ),
    utils.NOTIFY_HINTS.slack.token
  );
  assert.deepEqual(utils.notificationBadge(form, saved), {
    on: true,
    text: "企业微信"
  });
  assert.deepEqual(utils.notificationBadge({ ...form, clear: true }, saved), {
    on: false,
    text: "未配置"
  });
  assert.deepEqual(
    utils.notificationBadge(
      { ...utils.notificationFormFrom(), type: "feishu", token: "hook" },
      undefined
    ),
    { on: true, text: "飞书" }
  );
  assert.equal(utils.notificationSummary(saved), "企业微信");
  assert.equal(
    utils.notificationSummary({ type: "slack", at: "", configured: false }),
    "未配置"
  );
  assert.equal(utils.notificationSummary(undefined), "未配置");
});

test("告警通知的请求体：去掉空白，留空沿用已保存的，换类型要重填，可以清除", () => {
  const saved = { type: "wecom", at: "zhangsan", configured: true };
  const form = utils.notificationFormFrom(saved);
  assert.deepEqual(
    utils.notificationPayload(
      { ...form, token: `  ${WECOM_URL}  `, at: " lisi " },
      saved
    ),
    { type: "wecom", token: WECOM_URL, at: "lisi" }
  );
  assert.deepEqual(utils.notificationPayload(form, saved), {
    type: "wecom",
    token: "",
    at: "zhangsan"
  });
  // 都没填：不发通知，不是错误
  assert.deepEqual(
    utils.notificationPayload(utils.notificationFormFrom(), undefined),
    { type: "wecom", token: "", at: "" }
  );
  assert.deepEqual(
    utils.notificationPayload({ ...form, clear: true, at: "" }, saved),
    { type: "wecom", token: "", at: "", clear: true }
  );
  // 清除后又填了新地址：按新地址保存
  assert.deepEqual(
    utils.notificationPayload(
      { ...form, clear: true, type: "dingding", token: DING_URL },
      saved
    ),
    { type: "dingding", token: DING_URL, at: "zhangsan" }
  );
  assert.throws(
    () => utils.notificationPayload({ ...form, type: "dingding" }, saved),
    /IM 类型从企业微信改成了钉钉，请重新填写机器人地址；不发通知时点「清除」/
  );
  assert.throws(
    () => utils.notificationPayload({ ...form, token: DING_URL }, saved),
    /机器人地址是钉钉的，IM 类型却选了企业微信/
  );
  assert.throws(
    () =>
      utils.notificationPayload(
        { ...form, type: "feishu", token: "hooks.slack.com/services/T/B/x" },
        saved
      ),
    /是Slack的/
  );
  assert.equal(utils.webhookType("T000/B000/secret"), "");
  assert.equal(
    utils.webhookType("open.feishu.cn/open-apis/bot/v2/hook/x"),
    "feishu"
  );
  for (const [patch, reason] of [
    [{ token: "a b" }, /空格/],
    [{ token: "a\u0001b" }, /控制字符/],
    [{ token: "x".repeat(513) }, /512/],
    [{ at: "a\u0001" }, /控制字符/],
    [{ at: "x".repeat(129) }, /128/],
    [{ type: "qq" }, /IM 类型/]
  ])
    assert.throws(
      () => utils.notificationPayload({ ...form, ...patch }, saved),
      reason
    );
});

test("测试消息的请求与结果：没填地址用已保存的，结果按检查通过 / 失败显示", () => {
  const saved = { type: "dingding", at: "", configured: true };
  const form = utils.notificationFormFrom(saved);
  assert.deepEqual(utils.notificationTestRequest(form, saved), {
    type: "dingding",
    token: "",
    at: ""
  });
  assert.throws(
    () =>
      utils.notificationTestRequest(utils.notificationFormFrom(), undefined),
    /请先填写企业微信机器人地址/
  );
  assert.throws(
    () => utils.notificationTestRequest({ ...form, type: "slack" }, saved),
    /换了 IM 类型，请先填写新的机器人地址/
  );
  assert.throws(
    () => utils.notificationTestRequest({ ...form, clear: true }, saved),
    /请先填写钉钉机器人地址/
  );
  assert.deepEqual(
    utils.notifyResultView({ ok: true, message: "发送成功" }, "dingding"),
    {
      tone: "success",
      label: "检查通过",
      message: "测试消息已发送，请到钉钉群里确认收到。"
    }
  );
  assert.deepEqual(
    utils.notifyResultView(
      { ok: false, message: "错误码 310000:keywords not in content" },
      "dingding"
    ),
    {
      tone: "error",
      label: "失败",
      message: "发送失败：错误码 310000:keywords not in content"
    }
  );
  assert.equal(
    utils.notifyResultView({ ok: false, message: " " }, "wecom").message,
    "发送失败，IM 接口没有返回原因。"
  );
  assert.deepEqual(
    utils.notifyErrorView(
      new client.SetupRequestError("测试太频繁，请 3 秒后再试", 429)
    ),
    { tone: "warning", label: "请稍后", message: "测试太频繁，请 3 秒后再试" }
  );
  assert.deepEqual(
    utils.notifyErrorView(new Error("请先填写钉钉机器人地址。")),
    { tone: "error", label: "未发送", message: "请先填写钉钉机器人地址。" }
  );
  assert.equal(
    utils.notifyErrorView("x").message,
    "发送测试消息失败，请稍后重试。"
  );
});

test("第 4 步填写告警通知后随基础设置一起提交，保存后不再保留输入的地址", async t => {
  const sent = [];
  const { vm } = await harness(t, {
    initial: state({ step: 4 }),
    api: {
      configure: async (revision, data) => {
        sent.push(data);
        const next = state({ revision: revision + 1, phase: "ready", step: 5 });
        next.config.notification = {
          type: data.notification.type,
          at: data.notification.at,
          configured: !!data.notification.token
        };
        return next;
      }
    }
  });
  assert.deepEqual(vm.notifyBadge, { on: false, text: "未配置" });
  assert.equal(vm.notifyPlaceholder, utils.NOTIFY_HINTS.wecom.token);
  vm.notification.type = "dingding";
  assert.match(vm.notifyHints.at, /手机号/);
  assert.match(vm.notifyHints.tokenHelp, /oapi\.dingtalk\.com/);
  vm.notification.token = ` ${DING_URL} `;
  vm.notifyTokenEdited();
  vm.notification.at = "13800000000";
  assert.deepEqual(vm.notifyBadge, { on: true, text: "钉钉" });
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.deepEqual(sent[0].notification, {
    type: "dingding",
    token: DING_URL,
    at: "13800000000"
  });
  assert.deepEqual(sent[0].components, { alerting: true });
  assert.equal(vm.notification.token, "");
  assert.equal(vm.notifyPlaceholder, "已保存，留空保留");
  assert.equal(utils.notificationSummary(vm.savedNotification), "钉钉");
});

test("对象存储上传地址可选：去掉空白和末尾 /，可带账号密码，拒绝查询参数、锚点和本机地址", () => {
  assert.equal(utils.ossURLInput(""), "");
  assert.equal(utils.ossURLInput("   "), "");
  assert.equal(
    utils.ossURLInput(" https://ak:s%40k@oss.example.com/bucket/ "),
    "https://ak:s%40k@oss.example.com/bucket"
  );
  // 已保存的密码显示为 ******，原样提交表示沿用
  assert.equal(
    utils.ossURLInput("https://ak:******@oss.example.com/bucket"),
    "https://ak:******@oss.example.com/bucket"
  );
  assert.equal(
    utils.ossURLInput("http://192.0.2.30:9000"),
    "http://192.0.2.30:9000"
  );
  for (const [value, reason] of [
    ["oss.example.com/bucket", /有效的对象存储上传地址/],
    ["ftp://oss.example.com", /HTTP \/ HTTPS/],
    ["https://oss.example.com/upload?token=x", /查询参数或锚点/],
    ["https://oss.example.com/upload#part", /查询参数或锚点/],
    ["https://oss.example.com/upload?", /查询参数或锚点/],
    ["https://ak:pa ss@oss.example.com", /不含空格/],
    ["https://:secret@oss.example.com", /缺少账号/],
    ["http://127.0.0.1:9000", /本机地址/],
    ["http://localhost:9000/bucket", /本机地址/]
  ])
    assert.throws(() => utils.ossURLInput(value), reason, value);
  assert.equal(utils.ossSummary(""), "未配置");
  assert.equal(utils.ossSummary(undefined), "未配置");
  assert.equal(
    utils.ossSummary("https://ak:******@oss.example.com/b"),
    "https://ak:******@oss.example.com/b"
  );
});

test("第 4 步的对象存储上传地址随基础设置一起提交，填错时留在本步", async t => {
  const sent = [];
  const initial = state({ step: 4 });
  initial.config.oss_url = "https://ak:******@oss.example.com/old";
  const { vm } = await harness(t, {
    initial,
    api: {
      configure: async (revision, data) => {
        sent.push(data);
        const next = state({ revision: revision + 1, phase: "ready", step: 5 });
        next.config.oss_url = data.oss_url.replace(/:[^:@/]+@/, ":******@");
        return next;
      }
    }
  });
  // 载入已保存的打码地址：原样提交表示沿用密码
  assert.equal(vm.basics.oss_url, "https://ak:******@oss.example.com/old");
  vm.basics.oss_url = "https://oss.example.com/upload?token=x";
  await vm.advance();
  assert.equal(sent.length, 0);
  assert.equal(vm.step, 4);
  assert.match(vm.error, /查询参数或锚点/);
  vm.basics.oss_url = " https://ak:new-secret@oss.example.com/kubedoor/ ";
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.equal(
    sent[0].oss_url,
    "https://ak:new-secret@oss.example.com/kubedoor"
  );
  // 保存后表单换成安装服务返回的打码地址，新密码不再留在页面上
  assert.equal(vm.basics.oss_url, "https://ak:******@oss.example.com/kubedoor");
});

test("对象存储上传地址留空表示不配置，提交空串", async t => {
  const sent = [];
  const { vm } = await harness(t, {
    initial: state({ step: 4 }),
    api: {
      configure: async (revision, data) => {
        sent.push(data);
        return state({ revision: revision + 1, phase: "ready", step: 5 });
      }
    }
  });
  assert.equal(vm.basics.oss_url, "");
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.equal(sent[0].oss_url, "");
});

test("已保存的告警通知：留空保留，换类型不填地址不能继续，点清除后提交 clear", async t => {
  const initial = state({ step: 4 });
  initial.config.notification = {
    type: "wecom",
    at: "zhangsan",
    configured: true
  };
  const { vm, calls } = await harness(t, { initial });
  assert.deepEqual(
    { ...vm.notification },
    { type: "wecom", token: "", at: "zhangsan", clear: false }
  );
  assert.equal(vm.notifyPlaceholder, "已保存，留空保留");
  vm.notification.type = "feishu";
  await vm.advance();
  assert.equal(vm.step, 4);
  assert.match(vm.error, /请重新填写机器人地址/);
  assert.equal(calls.configure.length, 0);
  vm.notification.type = "wecom";
  vm.clearNotification();
  assert.equal(vm.notifyPlaceholder, "已清除，留空表示不发通知");
  assert.deepEqual(vm.notifyBadge, { on: false, text: "未配置" });
  // 清除后又填了新地址：不再是清除
  vm.notification.token = WECOM_URL;
  vm.notifyTokenEdited();
  assert.equal(vm.notification.clear, false);
  vm.clearNotification();
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.deepEqual(calls.configure[0].data.notification, {
    type: "wecom",
    token: "",
    at: "",
    clear: true
  });
});

test("发送测试消息：只测试不保存，结果显示为检查通过 / 失败，改动设置后清掉", async t => {
  const requests = [];
  let reply = { ok: true, message: "发送成功" };
  const { vm, calls } = await harness(t, {
    initial: state({ step: 4 }),
    api: {
      testNotification: async data => {
        requests.push(data);
        if (reply instanceof Error) throw reply;
        return reply;
      }
    }
  });
  // 没填地址：不请求安装服务，直接提示
  await vm.testNotification();
  assert.equal(requests.length, 0);
  assert.deepEqual(vm.notifyTest, {
    tone: "error",
    label: "未发送",
    message: "请先填写企业微信机器人地址。"
  });
  vm.notification.token = WECOM_URL;
  vm.notifyTokenEdited();
  assert.equal(vm.notifyTest, undefined);
  await vm.testNotification();
  assert.deepEqual(requests, [{ type: "wecom", token: WECOM_URL, at: "" }]);
  assert.deepEqual(vm.notifyTest, {
    tone: "success",
    label: "检查通过",
    message: "测试消息已发送，请到企业微信群里确认收到。"
  });
  assert.equal(vm.notifyTesting, false);
  assert.equal(calls.configure.length, 0);
  assert.equal(vm.step, 4);
  reply = { ok: false, message: "错误码 93000:invalid webhook url" };
  await vm.testNotification();
  assert.deepEqual(vm.notifyTest, {
    tone: "error",
    label: "失败",
    message: "发送失败：错误码 93000:invalid webhook url"
  });
  reply = new client.SetupRequestError("测试太频繁，请 3 秒后再试", 429);
  await vm.testNotification();
  assert.equal(vm.notifyTest.tone, "warning");
  assert.equal(vm.notifyTest.label, "请稍后");
  vm.notification.at = "zhangsan";
  vm.notifyEdited();
  assert.equal(vm.notifyTest, undefined);
  // 安装会话过期：回到输入安装密钥
  reply = new client.SetupRequestError("请先输入安装密钥验证身份", 401);
  await vm.testNotification();
  assert.equal(vm.authenticated, false);
  assert.match(vm.error, /安装密钥/);
  assert.equal(requests.length, 4);
  assert.deepEqual(requests[3], {
    type: "wecom",
    token: WECOM_URL,
    at: "zhangsan"
  });
});

test("页面上的告警通知：IM 类型下拉、说明表、测试结果的样式，第 5 步摘要", async t => {
  const initial = state({ step: 4 });
  initial.config.notification = {
    type: "feishu",
    at: "ou_123",
    configured: true
  };
  const requests = [];
  const page = await renderPage(t, null, {
    status: async () => ({
      phase: "draft",
      authenticated: true,
      installation_id: "install-1"
    }),
    state: async () => initial,
    testNotification: async data => {
      requests.push(data);
      return { ok: false, message: "错误码 19021:sign match fail" };
    }
  });
  const byId = id => page.findAll(item => item.props.id === id)[0];
  const select = byId("notify-type");
  assert.deepEqual(
    page
      .findAll(item => item.tag === "option", select)
      .map(item => page.textOf(item).trim()),
    ["企业微信", "钉钉", "飞书", "Slack"]
  );
  const token = byId("notify-token");
  assert.equal(token.props.type, "password");
  assert.equal(token.props.placeholder, "已保存，留空保留");
  assert.match(byId("notify-at").props.placeholder, /open_id/);
  const text = page.textOf(page.root);
  assert.match(text, /告警通知可选飞书/);
  assert.match(text, /留空表示不发通知，装完可以在「系统管理 → 消息通知」配置/);
  assert.match(text, /open\.feishu\.cn\/open-apis\/bot\/v2\/hook/);
  assert.ok(text.includes(utils.NOTICE_NOTE));
  // 说明表：四类通知，文字和控制台消息通知页一致
  const rows = page.findAll(item => "data-notice-row" in item.props);
  assert.deepEqual(
    rows.map(page.textOf),
    utils.NOTICE_ROWS.map(row => row.name + row.sender + row.when)
  );
  const button = page.findAll(
    item => "data-notify-test-button" in item.props
  )[0];
  assert.equal(page.textOf(button).trim(), "发送测试消息");
  await button.props.onClick();
  await settle();
  assert.deepEqual(requests, [{ type: "feishu", token: "", at: "ou_123" }]);
  const result = page.findAll(item => item.props["data-notify-test"])[0];
  assert.equal(result.props.class, "error");
  assert.match(
    page.textOf(result),
    /测试消息发送失败：错误码 19021:sign match fail/
  );
  assert.deepEqual(
    page.findAll(item => item.tag === "b", result).map(page.textOf),
    ["失败"]
  );

  for (const [notification, expected] of [
    [{ type: "wecom", at: "", configured: true }, "企业微信"],
    [{ type: "wecom", at: "", configured: false }, "未配置"],
    [undefined, "未配置"]
  ]) {
    const ready = state({ phase: "ready", step: 5 });
    ready.config.notification = notification;
    const summary = await renderPage(t, null, {
      status: async () => ({
        phase: "ready",
        authenticated: true,
        installation_id: "install-1"
      }),
      state: async () => ready
    });
    const row = summary.findAll(
      item => item.props["data-summary"] === "notification"
    )[0];
    assert.equal(summary.textOf(row), `告警通知${expected}`);
  }
});

test("页面上的对象存储上传地址：第 4 步的可选输入框，第 5 步摘要显示打码后的地址", async t => {
  const initial = state({ step: 4 });
  initial.config.oss_url = "https://ak:******@oss.example.com/bucket";
  const page = await renderPage(t, null, {
    status: async () => ({
      phase: "draft",
      authenticated: true,
      installation_id: "install-1"
    }),
    state: async () => initial
  });
  const input = page.findAll(item => item.props.id === "oss-url")[0];
  assert.ok(input, "第 4 步应有对象存储上传地址输入框");
  assert.equal(input.props.type, "url");
  assert.match(input.props.placeholder, /oss\.example\.com/);
  const text = page.textOf(page.root);
  assert.match(text, /对象存储上传地址\s*可选/);
  assert.match(text, /系统管理 →\s*通用配置/);
  // 说明表：K8S 事件告警写明规则在哪里配置（和控制台消息通知页的说明一致）
  assert.equal(
    utils.NOTICE_ROWS[0].when,
    "K8S 事件命中事件告警规则（在系统管理 → 事件告警规则配置）时，例如重启、健康检查失败、挂载失败、调度失败、资源压力；不受告警屏蔽影响"
  );

  for (const [oss, expected] of [
    [
      "https://ak:******@oss.example.com/bucket",
      "https://ak:******@oss.example.com/bucket"
    ],
    ["", "未配置"],
    [undefined, "未配置"]
  ]) {
    const ready = state({ phase: "ready", step: 5 });
    if (oss !== undefined) ready.config.oss_url = oss;
    const summary = await renderPage(t, null, {
      status: async () => ({
        phase: "ready",
        authenticated: true,
        installation_id: "install-1"
      }),
      state: async () => ready
    });
    const row = summary.findAll(
      item => item.props["data-summary"] === "oss"
    )[0];
    assert.equal(summary.textOf(row), `对象存储上传地址${expected}`);
  }
});

/* ---------- 完成页的登录信息、第 6 步提示 ---------- */

/** 浏览器 Storage 的最小实现：length / key / getItem / setItem / removeItem */
function mockStorage(map = new Map()) {
  return {
    get length() {
      return map.size;
    },
    key: index => [...map.keys()][index] ?? null,
    getItem: name => (map.has(name) ? map.get(name) : null),
    setItem: (name, value) => map.set(name, String(value)),
    removeItem: name => map.delete(name)
  };
}

function useStorage(t, storage) {
  const original = global.sessionStorage;
  global.sessionStorage = storage;
  t.after(() => (global.sessionStorage = original));
  return storage;
}

test("第 4 步保存成功后在当前标签页记下管理员密码，进入控制台时清除", async t => {
  const storage = useStorage(t, mockStorage());
  const { vm } = await harness(t, { initial: state({ step: 4 }) });
  vm.basics.admin.password = "Admin@2026!";
  vm.passwordConfirmation = "Admin@2026!";
  await vm.advance();
  assert.equal(vm.step, 5);
  assert.equal(
    storage.getItem(utils.adminPasswordKey("install-1")),
    "Admin@2026!"
  );
  vm.applyMarker({
    phase: "succeeded",
    installation_id: "install-1",
    public_url: "http://192.0.2.20:30080"
  });
  assert.equal(vm.accessURL, "http://192.0.2.20:30080");
  assert.equal(vm.adminPassword, "Admin@2026!");
  vm.enterConsole();
  assert.equal(storage.length, 0);
});

test("完成页显示访问地址、管理员账号和默认隐藏的密码，不装 Agent 也显示", async t => {
  useStorage(
    t,
    mockStorage(new Map([[utils.adminPasswordKey("install-1"), "Admin@2026!"]]))
  );
  const page = await renderPage(t, {
    phase: "succeeded",
    installation_id: "install-1",
    public_url: "http://192.0.2.20:30080",
    services: [],
    agent: { status: "skipped" }
  });
  const info = page.findAll(item => item.props["aria-label"] === "登录信息")[0];
  assert.match(page.textOf(info), /访问地址http:\/\/192\.0\.2\.20:30080/);
  assert.match(page.textOf(info), /管理员账号kubedoor/);
  assert.match(page.textOf(info), /管理员密码••••••••/);
  assert.doesNotMatch(page.textOf(info), /Admin@2026!/);
  const eye = page.findAll(item => item.props.class === "eye-button")[0];
  assert.equal(eye.props["aria-label"], "查看密码");
  eye.props.onClick();
  await settle();
  assert.match(page.textOf(info), /管理员密码Admin@2026!/);
});

test("没记下密码时（例如换了标签页）提示是第 4 步设置的密码", async t => {
  useStorage(t, mockStorage());
  const page = await renderPage(t, {
    phase: "succeeded",
    installation_id: "install-1",
    public_url: "http://192.0.2.20:30080",
    services: []
  });
  const info = page.findAll(item => item.props["aria-label"] === "登录信息")[0];
  assert.match(page.textOf(info), /管理员密码第 4 步设置的管理员密码/);
  assert.deepEqual(
    page.findAll(item => item.props.class === "eye-button"),
    []
  );
});

test("第 6 步先提示控制端组件已安装完成，不再显示本集群信息卡片", async t => {
  const agentPhase = state({
    phase: "agent",
    step: 6,
    agent: { status: "choice", defaults: { namespace: "kubedoor" } }
  });
  const page = await renderPage(t, null, {
    status: async () => ({
      phase: "agent",
      authenticated: true,
      installation_id: "install-1"
    }),
    state: async () => agentPhase
  });
  const text = page.textOf(page.root);
  assert.match(text, /KubeDoor 控制端组件安装完成/);
  assert.match(text, /在 KubeDoor 控制端的 K8S 同时安装 KubeDoor-agent/);
  assert.doesNotMatch(text, /本集群（控制端所在集群）/);
  assert.doesNotMatch(text, /⬡/);
  // 安装 Agent 的勾选卡片：没勾选淡红色，勾选后淡绿色（checked 样式）
  const card = page.findAll(item =>
    /agent-card/.test(item.props.class || "")
  )[0];
  assert.ok(card);
});

test("完成页可以复制管理员密码", async t => {
  useStorage(
    t,
    mockStorage(new Map([[utils.adminPasswordKey("install-1"), "Admin@2026!"]]))
  );
  const page = await renderPage(t, {
    phase: "succeeded",
    installation_id: "install-1",
    public_url: "http://192.0.2.20:30080",
    services: []
  });
  const copy = page.findAll(item => item.props.class === "copy-button")[0];
  assert.equal(copy.props["aria-label"], "复制密码");
  // 密码默认隐藏时也能直接复制
  await copy.props.onClick();
  assert.deepEqual(page.copied, ["Admin@2026!"]);
});

test("预检报本集群已有 Agent 资源时，确认后强制覆盖安装", async t => {
  const takeover = [
    {
      kind: "Deployment",
      name: "kubedoor-agent",
      namespace: "kubedoor",
      reason: "属于另一项安装：安装记录不在当前控制端，或集群名称不同"
    }
  ];
  const installs = [];
  const failed = agentState(
    {
      status: "failed",
      options: agentOptions,
      error: "安装预检未通过：目标集群已有 Agent 资源，不会直接覆盖",
      checks: [
        {
          name: "安装预检",
          status: "error",
          message: "目标集群已有 Agent 资源",
          forceable: true
        }
      ],
      takeover
    },
    { revision: 7 }
  );
  const page = await renderPage(t, null, {
    status: async () => ({
      phase: "agent",
      authenticated: true,
      installation_id: "install-1"
    }),
    state: async () => failed,
    installAgent: async (revision, options, force) => {
      installs.push({ revision, options, force });
      return failed;
    }
  });
  const buttons = () =>
    page.findAll(item => item.props.class === "takeover-button");
  assert.equal(page.textOf(buttons()[0]).trim(), "强制覆盖安装");
  buttons()[0].props.onClick();
  await settle();
  // 先列出要接管的资源，确认后才带 force 提交
  const list = page.findAll(
    item => item.props["aria-label"] === "要接管的已有资源"
  )[0];
  assert.match(page.textOf(list), /Deployment\/kubedoor-agent/);
  assert.match(page.textOf(list), /属于另一项安装/);
  assert.deepEqual(installs, []);
  await buttons()[0].props.onClick();
  await settle();
  assert.deepEqual(
    installs.map(item => [item.revision, item.force]),
    [[7, true]]
  );
});
