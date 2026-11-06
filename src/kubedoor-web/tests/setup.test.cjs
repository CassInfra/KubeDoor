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

async function harness(t, options = {}) {
  const originalWindow = global.window;
  global.window = { location: { origin: "https://kubedoor.example.com" } };
  const calls = {
    postgres: [],
    metrics: [],
    configure: [],
    retry: [],
    installAgent: [],
    skipAgent: [],
    state: 0,
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
    state: async () => {
      calls.state++;
      return current;
    },
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
    installAgent: async (revision, data) => {
      calls.installAgent.push({ revision, options: data });
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
  await settle();
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

test("时序库检测没查到集群标签时停在第 3 步，确认后再继续", async t => {
  const sent = [];
  let current = state({ step: 3 });
  const { vm } = await harness(t, {
    initial: current,
    api: {
      state: async () => current,
      metrics: async (revision, data) => {
        sent.push(data);
        current = state({
          step: 4,
          revision: revision + 1,
          checks: [
            {
              id: "metrics",
              name: "metrics",
              status: "warning",
              message: "最近一天没有查到带 origin_prometheus 标签的数据"
            }
          ]
        });
        return current;
      }
    }
  });
  assert.equal(vm.step, 3);
  await vm.advance();
  assert.equal(sent.length, 1);
  assert.equal(vm.step, 3);
  assert.equal(vm.actionText, "继续");
  assert.equal(vm.groupChecks[0].status, "warning");
  await vm.advance();
  assert.equal(vm.step, 4);
  assert.equal(sent.length, 1);
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
