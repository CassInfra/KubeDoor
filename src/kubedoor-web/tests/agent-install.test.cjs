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
  if (vue) {
    const { descriptor } = parse(source, { filename: target });
    source = compileScript(descriptor, { id: "agent-install-test" }).content;
  }
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  const original = instance.require.bind(instance);
  instance.require = id =>
    Object.hasOwn(overrides, id) ? overrides[id] : original(id);
  instance._compile(
    transformSync(source, {
      loader: "ts",
      target: "es2018",
      format: "cjs"
    }).code,
    target
  );
  return instance.exports;
}

const utils = compile("../src/views/workbench/install-utils.ts");
const notifyUtils = compile(
  "../src/views/system/notification/notification-utils.ts"
);
const options = (extra = {}) => ({
  env: "production",
  namespace: "kubedoor",
  master_url: "https://kubedoor.example.com",
  mode: "agent",
  agent_tag: "2.3.0",
  vmagent: "reuse",
  ksm: "auto",
  node_exporter: "auto",
  ...extra
});
// 服务端返回的安装参数还带着对话框里没有的集群别名、镜像拉取 Secret（旧任务里还可能有镜像仓库、地址）
const served = (extra = {}) =>
  options({
    aliases: ["生产"],
    image_repo: "registry.example.com/kubedoor",
    image_pull_secret: "pull-key",
    oss_url: "https://objects.example.com",
    remote_write_url: "https://metrics.example.com/write",
    ...extra
  });
// 正在使用的通用配置（地址已脱敏）
const settings = (extra = {}) => ({
  metrics_write_url: "http://monit:******@vm.example.com:8428/api/v1/write",
  oss_url: "",
  image_repo: "swr.cn-south-1.myhuaweicloud.com/starsl.cn",
  kubedoor_exturl: "https://kubedoor.example.com",
  ...extra
});
const task = (extra = {}) => ({
  id: "installation-1",
  status: "draft",
  revision: 1,
  context: "production",
  contexts: [{ name: "production", server: "https://k8s.example.com" }],
  options: served(),
  ...extra
});
// 消息通知的已保存配置：production 集群已有单独配置
const notificationSettings = (extra = {}) => ({
  default: {
    type: "dingding",
    at: "",
    configured: true,
    token_hint: "****cdef"
  },
  clusters: [
    {
      env: "production",
      type: "wecom",
      at: "zhangsan",
      configured: true,
      token_hint: "****a1b2"
    }
  ],
  envs: ["production"],
  ...extra
});
const WECOM_URL =
  "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2";

const deferred = () => {
  let resolve;
  const promise = new Promise(yes => (resolve = yes));
  return { promise, resolve };
};
async function flush() {
  for (let i = 0; i < 3; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
}

async function harness(
  t,
  overrides = {},
  elementPlus = {},
  notification = {},
  props = {}
) {
  const oldStorage = global.localStorage;
  const oldWindow = global.window;
  const saved = new Map();
  const opened = [];
  const storageReads = [];
  global.localStorage = {
    getItem: key => {
      storageReads.push(key);
      return saved.get(key) || null;
    },
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key)
  };
  global.window = { open: (...args) => opened.push(args) };
  const calls = { start: 0, inspect: 0, inspected: [], test: [] };
  const api = {
    defaults: async () => ({
      ...served(),
      metrics_available: true,
      settings: settings()
    }),
    list: async () => [],
    inspect: async () => task(),
    repair: async () => ({
      task: null,
      message: "这个集群还没有 AI Kubeconfig"
    }),
    preflight: async (id, context, selected) =>
      task({ id, context, options: selected, status: "ready", revision: 2 }),
    start: async id => {
      calls.start++;
      return task({ id, status: "succeeded" });
    },
    get: async id => task({ id }),
    ...overrides
  };
  const inspect = api.inspect;
  api.inspect = (...args) => {
    calls.inspect++;
    calls.inspected.push(args);
    return inspect(...args);
  };
  const notifyApi = {
    getNotificationSettings: async () => ({
      success: true,
      data: notificationSettings()
    }),
    testNotification: async request => {
      calls.test.push(request);
      return { success: true, data: { ok: true, message: "发送成功" } };
    },
    ...notification
  };
  const component = compile(
    "../src/views/workbench/AgentInstallDialog.vue",
    {
      "@/api/agent-install": { agentInstallApi: api },
      "@/api/notification": notifyApi,
      "@/store/modules/user": {
        useUserStoreHook: () => ({ username: "alice" })
      },
      "@/views/system/notification/NotifyChannelFields.vue": { default: {} },
      "@/views/system/notification/notification-utils": notifyUtils,
      "./install-utils": utils,
      "element-plus": { ...elementPlus }
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
  const vnode = h(
    { ...component, render: () => null },
    { modelValue: true, ...props }
  );
  renderer.render(vnode, container);
  const vm = vnode.component.setupState;
  t.after(() => {
    renderer.render(null, container);
    global.localStorage = oldStorage;
    global.window = oldWindow;
  });
  await flush();
  return { vm, api, calls, saved, opened, storageReads };
}

test("每个安装参数和 context 的变更都会使已检测的计划失效", () => {
  const before = utils.installFingerprint("production", options());
  for (const [key, value] of Object.entries({
    env: "other",
    namespace: "other",
    master_url: "https://other.example.com",
    mode: "full",
    agent_tag: "new",
    vmagent: "install",
    ksm: "reuse",
    node_exporter: "install"
  }))
    assert.notEqual(
      before,
      utils.installFingerprint("production", options({ [key]: value })),
      key
    );
  assert.notEqual(before, utils.installFingerprint("staging", options()));
  // 参数以外的内容（通知配置、通用配置）变了也要重新检测；不再是对话框参数的地址不算
  assert.notEqual(
    before,
    utils.installFingerprint("production", options(), ["wecom"])
  );
  assert.equal(
    before,
    utils.installFingerprint(
      "production",
      options({ oss_url: "https://objects.example.com" })
    )
  );
});

test("对话框不提交别名、镜像仓库、镜像拉取 Secret 和地址，服务端返回的这几项也不带回去", async t => {
  const sent = [];
  const { vm } = await harness(t, {
    list: async () => [task()],
    preflight: async (id, context, selected) => {
      sent.push(selected);
      return task({ id, context, status: "ready", revision: 2 });
    }
  });
  assert.deepEqual({ ...vm.options }, options());
  await vm.preflight();
  assert.deepEqual(sent, [{ ...options(), notification: null }]);
  // 任务原样带回这几项也不影响「参数没改过」的判断
  assert.equal(vm.canInstall, true);
  vm.newInstall();
  assert.deepEqual({ ...vm.options }, options());
});

test("安装方式只决定三个采集组件的默认值，以前没有 vmagent 的任务按安装方式推", () => {
  assert.deepEqual(utils.collectorPreset("full"), {
    vmagent: "install",
    ksm: "auto",
    node_exporter: "auto"
  });
  assert.deepEqual(utils.collectorPreset("agent"), {
    vmagent: "reuse",
    ksm: "reuse",
    node_exporter: "reuse"
  });
  // 新建安装默认是完整安装
  assert.deepEqual(
    { ...utils.emptyInstallOptions(), env: "x" },
    {
      env: "x",
      namespace: "kubedoor",
      master_url: "",
      mode: "full",
      agent_tag: "",
      vmagent: "install",
      ksm: "auto",
      node_exporter: "auto"
    }
  );
  const pick = source => {
    const { vmagent, ksm, node_exporter } = utils.pickInstallOptions(source);
    return [vmagent, ksm, node_exporter];
  };
  // 以前的仅安装 Agent 什么采集组件都不装；以前的完整接入装 vmagent，另外两个保留检测出的结果
  assert.deepEqual(
    pick({ mode: "agent", ksm: "auto", node_exporter: "auto" }),
    ["reuse", "reuse", "reuse"]
  );
  assert.deepEqual(
    pick({ mode: "full", ksm: "install", node_exporter: "reuse" }),
    ["install", "install", "reuse"]
  );
  // 新任务按保存的原样恢复：仅安装 kubedoor-agent 也可以单独装某个组件
  assert.deepEqual(
    pick({
      mode: "agent",
      vmagent: "reuse",
      ksm: "reuse",
      node_exporter: "install"
    }),
    ["reuse", "reuse", "install"]
  );
  assert.deepEqual(
    utils.COLLECTOR_CHOICES.component.map(item => item.label),
    ["自动检测", "安装", "不安装"]
  );
  assert.deepEqual(
    utils.COLLECTOR_CHOICES.vmagent.map(item => item.value),
    ["install", "reuse"]
  );
});

test("集群唯一标识的说明里显示 external_labels 要有的实际键值对", async t => {
  assert.equal(
    utils.clusterLabelPair("cluster", " tx-flink-prod "),
    "cluster: tx-flink-prod"
  );
  assert.equal(
    utils.clusterLabelPair(undefined, ""),
    "origin_prometheus: <集群唯一标识>"
  );
  const { vm } = await harness(t, {
    defaults: async () => ({
      ...served(),
      metrics_available: true,
      settings: settings({ prom_k8s_tag_key: "origin_prometheus" })
    })
  });
  vm.options.env = "tx-flink-prod";
  assert.equal(vm.labelPair, "origin_prometheus: tx-flink-prod");
});

test("切换安装方式时三个采集组件换成默认值，之后每个还能单独改并随检测提交", async t => {
  const sent = [];
  const { vm } = await harness(t, {
    list: async () => [task()],
    preflight: async (id, context, selected) => {
      sent.push(selected);
      return task({
        id,
        context,
        options: selected,
        status: "ready",
        revision: 2
      });
    }
  });
  vm.changeMode("full");
  assert.deepEqual(
    [
      vm.options.mode,
      vm.options.vmagent,
      vm.options.ksm,
      vm.options.node_exporter
    ],
    ["full", "install", "auto", "auto"]
  );
  // 选了自动检测就说明怎么判断：kube-state-metrics 看 Service，node-exporter 看每个节点的 9100
  assert.equal(vm.autoDetecting, true);
  assert.match(
    utils.AUTO_DETECT_HINT,
    /app\.kubernetes\.io\/name=kube-state-metrics 标签、端口 8080 的 Service/
  );
  assert.match(
    utils.AUTO_DETECT_HINT,
    /每个 Linux 节点上都有使用主机网络、声明 9100 端口的运行中 Pod/
  );
  assert.match(vm.collectorHint, /^vmagent 把指标写入通用配置里的远程写地址/);
  vm.changeMode("agent");
  assert.deepEqual(
    [
      vm.options.mode,
      vm.options.vmagent,
      vm.options.ksm,
      vm.options.node_exporter
    ],
    ["agent", "reuse", "reuse", "reuse"]
  );
  assert.equal(vm.autoDetecting, false);
  assert.match(
    vm.collectorHint,
    /^不安装 vmagent 时用已有的 Prometheus \/ vmagent 采集/
  );
  vm.options.node_exporter = "install";
  await vm.preflight();
  assert.deepEqual(
    [sent[0].mode, sent[0].vmagent, sent[0].ksm, sent[0].node_exporter],
    ["agent", "reuse", "reuse", "install"]
  );
  assert.equal(vm.canInstall, true);
  // 改了任何一个组件都要重新检测
  vm.options.vmagent = "install";
  assert.equal(vm.canInstall, false);
});

test("检查地址和命名空间，不能将带口令的 master URL 当作普通连接地址", () => {
  assert.equal(utils.installValidation(options()), "");
  for (const address of [
    "",
    "file:///etc/passwd",
    "https://user:secret@example.com",
    "http://"
  ])
    assert.notEqual(
      utils.installValidation(options({ master_url: address })),
      ""
    );
  for (const namespace of ["Bad_Name", "a".repeat(64), "-invalid"])
    assert.notEqual(utils.installValidation(options({ namespace })), "");
});

test("恢复未完成任务优先选择记住的任务，不恢复已过期或成功任务", () => {
  const tasks = [
    task({ id: "success", status: "succeeded", updated_at: "2026-11-03" }),
    task({ id: "expired", status: "expired", updated_at: "2026-11-03" }),
    task({ id: "old", status: "running", updated_at: "2026-11-01" }),
    task({ id: "new", status: "failed", updated_at: "2026-11-02" })
  ];
  assert.equal(utils.recoverableInstall(tasks).id, "new");
  assert.equal(utils.recoverableInstall(tasks, "old").id, "old");
  assert.equal(utils.recoverableInstall(tasks.slice(0, 2)), undefined);
});

test("向导必须先检测，修改参数后再次禁止安装", async t => {
  const { vm, calls } = await harness(t, { list: async () => [task()] });
  await vm.start(false);
  assert.equal(calls.start, 0);
  await vm.preflight();
  assert.equal(vm.canInstall, true);
  vm.options.namespace = "other";
  await vm.start(false);
  assert.equal(vm.canInstall, false);
  assert.equal(calls.start, 0);
  await vm.preflight();
  await vm.start(false);
  assert.equal(calls.start, 1);
  assert.equal(vm.task.status, "succeeded");
});

const TAKEOVER = [
  {
    kind: "Deployment",
    name: "kubedoor-agent",
    namespace: "kubedoor",
    reason: "属于另一项安装：安装记录不在当前控制端，或集群名称不同"
  }
];

function takeoverPreflight(preflights) {
  return async (id, context, selected, force) => {
    preflights.push(force);
    return force
      ? task({ id, context, options: selected, status: "ready", revision: 3 })
      : task({
          id,
          context,
          options: selected,
          status: "draft",
          revision: 2,
          takeover: TAKEOVER,
          checks: [
            {
              name: "安装预检",
              status: "error",
              message: "目标集群已有 Agent 资源，不会直接覆盖",
              forceable: true
            }
          ]
        });
  };
}

test("预检报目标集群已有 Agent 资源时，确认后带 force 重新检测再安装", async t => {
  const preflights = [];
  const confirms = [];
  const { vm, calls } = await harness(
    t,
    { list: async () => [task()], preflight: takeoverPreflight(preflights) },
    {
      ElMessageBox: {
        confirm: async (...args) => {
          confirms.push(args);
          return "confirm";
        }
      }
    }
  );
  await vm.preflight();
  assert.equal(vm.forceable, true);
  assert.equal(vm.canInstall, false);
  await vm.forcePreflight();
  assert.equal(confirms.length, 1);
  assert.equal(confirms[0][1], "强制覆盖安装");
  assert.deepEqual(preflights, [false, true]);
  assert.equal(vm.forceable, false);
  assert.equal(vm.canInstall, true);
  await vm.start(false);
  assert.equal(calls.start, 1);
});

test("强制覆盖的确认框点取消时不重新检测，普通检测不会带 force", async t => {
  const preflights = [];
  const { vm } = await harness(
    t,
    { list: async () => [task()], preflight: takeoverPreflight(preflights) },
    {
      ElMessageBox: {
        confirm: async () => {
          throw new Error("cancel");
        }
      }
    }
  );
  await vm.preflight();
  await vm.forcePreflight();
  // 检测按钮的点击事件不会被当成 force 传进去
  await vm.preflight({ type: "click" });
  assert.deepEqual(preflights, [false, false]);
  assert.equal(vm.canInstall, false);
});

test("向导只保存 task id，不将上传的 kubeconfig 写入浏览器存储", async t => {
  const { vm, saved } = await harness(t);
  await vm.upload({
    target: {
      files: [
        { name: "config", size: 10, text: async () => "secret-kubeconfig" }
      ],
      value: ""
    }
  });
  assert.deepEqual(
    [...saved],
    [["kubedoor-agent-install:alice", "installation-1"]]
  );
  assert.equal(vm.stage, 0);
  assert.equal(vm.selectedContext, "production");
});

test("超大上传不会调用后端，切换任务后旧请求不能覆盖新状态", async t => {
  const pending = deferred();
  const { vm, calls } = await harness(t, { inspect: () => pending.promise });
  await vm.upload({
    target: { files: [{ size: utils.MAX_KUBECONFIG_BYTES + 1 }], value: "" }
  });
  assert.equal(calls.inspect, 0);
  assert.match(vm.error, /1 MiB/);
  const uploading = vm.upload({
    target: {
      files: [{ name: "config", size: 10, text: async () => "secret" }],
      value: ""
    }
  });
  await vm.selectTask("another-task");
  pending.resolve(task({ id: "stale-upload" }));
  await uploading;
  assert.equal(vm.task.id, "another-task");
});

test("API 将后端业务错误显示为失败，并将预检 revision 用于安装", async () => {
  const requests = [];
  let fail = false;
  const api = compile("../src/api/agent-install.ts", {
    "@/utils/http": {
      http: {
        request: async (...args) => {
          requests.push(args);
          return fail
            ? { success: false, message: "权限不足" }
            : { success: true, data: task() };
        }
      }
    }
  }).agentInstallApi;
  await api.start("id/with/slashes", 7);
  assert.equal(
    requests[0][1],
    "/api/admin/agent-installations/id%2Fwith%2Fslashes/start"
  );
  assert.deepEqual(requests[0][2].data, { revision: 7 });
  fail = true;
  await assert.rejects(api.inspect("secret"), /权限不足/);
});

test("通用配置缺的项给出提醒，没有远程写地址时只能仅安装 kubedoor-agent", () => {
  assert.deepEqual(utils.missingSettings(settings()), [
    { key: "oss_url", label: "对象存储上传地址", required: false }
  ]);
  assert.deepEqual(
    utils
      .missingSettings(settings({ metrics_write_url: " " }))
      .map(item => [item.key, item.required]),
    [
      ["metrics_write_url", true],
      ["oss_url", false]
    ]
  );
  assert.equal(utils.missingSettings(undefined).length, 2);
  assert.equal(
    utils.settingsReminder("远程写地址"),
    "通用配置里还没有远程写地址。在系统管理 → 通用配置填写一次，以后安装 Agent 自动使用"
  );
  assert.equal(utils.GENERAL_SETTINGS_URL, "#/system/general-settings");
});

test("没有远程写地址：完整安装不可选；去通用配置填写后「重新读取」，计划要重新检测", async t => {
  let current = {
    ...served({ mode: "full" }),
    metrics_available: false,
    settings: settings({ metrics_write_url: "" })
  };
  const { vm, opened } = await harness(t, {
    defaults: async () => current,
    list: async () => [task()],
    preflight: async (id, context, selected) =>
      task({ id, context, options: selected, status: "ready", revision: 2 })
  });
  assert.equal(vm.metricsAvailable, false);
  assert.deepEqual(
    vm.missing.map(item => item.key),
    ["metrics_write_url", "oss_url"]
  );
  vm.newInstall();
  assert.equal(vm.options.mode, "agent");
  vm.openGeneralSettings();
  assert.deepEqual(opened, [
    ["#/system/general-settings", "_blank", "noopener"]
  ]);

  vm.options.env = "production";
  vm.options.master_url = "https://kubedoor.example.com";
  vm.task = task();
  await vm.preflight();
  assert.equal(vm.canInstall, true);
  // 在通用配置填好远程写地址后重新读取：完整安装可选，已检测的计划失效
  current = { ...current, metrics_available: true, settings: settings() };
  await vm.reloadDefaults();
  assert.equal(vm.metricsAvailable, true);
  assert.deepEqual(
    vm.missing.map(item => item.key),
    ["oss_url"]
  );
  assert.equal(vm.canInstall, false);
  // 又被删掉了：正在选的完整安装退回仅安装 kubedoor-agent，vmagent 跟着不装
  vm.changeMode("full");
  assert.equal(vm.options.vmagent, "install");
  current = {
    ...current,
    metrics_available: false,
    settings: settings({ metrics_write_url: "" })
  };
  await vm.reloadDefaults();
  assert.equal(vm.options.mode, "agent");
  assert.deepEqual(
    [vm.options.vmagent, vm.options.ksm, vm.options.node_exporter],
    ["reuse", "reuse", "reuse"]
  );
});

test("通知配置：填了就随检测提交，机器人地址只在服务端，重新检测留空沿用", async t => {
  const sent = [];
  const { vm } = await harness(t, {
    list: async () => [task()],
    preflight: async (id, context, selected) => {
      sent.push(selected);
      const { notification, ...rest } = selected;
      return task({
        id,
        context,
        status: "ready",
        revision: 2,
        options: {
          ...rest,
          notification: notification && {
            type: notification.type,
            at: notification.at,
            configured: true
          }
        }
      });
    }
  });
  // 这个集群已有单独配置：说明留空保持不变
  assert.equal(
    vm.existingText,
    "该集群已有单独配置：企业微信（****a1b2），留空保持不变，填写则覆盖"
  );
  // 只填了 @ 人没填地址：提示补地址，不发请求
  vm.notify.at = "lisi";
  await vm.preflight();
  assert.equal(sent.length, 0);
  assert.equal(vm.error, "通知配置请填写机器人地址");
  assert.ok(vm.expanded.includes("notify"));

  vm.notify.token = ` ${WECOM_URL} `;
  await vm.preflight();
  assert.deepEqual(sent.at(-1).notification, {
    type: "wecom",
    token: WECOM_URL,
    at: "lisi"
  });
  // 检测之后表单里不留机器人地址，显示「已填写」，留空重新检测沿用
  assert.equal(vm.notify.token, "");
  assert.deepEqual(vm.notifyKept, {
    type: "wecom",
    at: "lisi",
    configured: true
  });
  assert.equal(vm.notifyPlaceholder, "这次安装已填写，留空沿用");
  assert.equal(vm.canInstall, true);
  await vm.preflight();
  assert.deepEqual(sent.at(-1).notification, {
    type: "wecom",
    token: "",
    at: "lisi"
  });

  // 换了 IM 类型：计划失效，没填新地址不能检测
  vm.notify.type = "dingding";
  assert.equal(vm.canInstall, false);
  assert.equal(vm.notifyPlaceholder, "换了 IM 类型，请重新填写钉钉机器人地址");
  const count = sent.length;
  await vm.preflight();
  assert.equal(sent.length, count);
  assert.equal(vm.error, "通知配置换了 IM 类型，请重新填写机器人地址");

  // 不配置通知
  vm.clearNotification();
  assert.equal(vm.notifyKept, null);
  await vm.preflight();
  assert.equal(sent.at(-1).notification, null);
});

test("发送测试消息：填了地址用填的，没填测这个集群已有的单独配置，上次检测填的地址测不了", async t => {
  const { vm, calls } = await harness(t, { list: async () => [task()] });
  await vm.testNotify();
  // 没填地址、IM 类型和已有的单独配置一样：用已保存的
  assert.deepEqual(calls.test.at(-1), {
    env: "production",
    type: "wecom",
    token: "",
    at: ""
  });
  assert.deepEqual(vm.notifyTest.result, { type: "success", text: "发送成功" });

  vm.notify.type = "feishu";
  vm.notify.token = "hook-token";
  vm.notify.at = "ou_1";
  await vm.testNotify();
  assert.deepEqual(calls.test.at(-1), {
    env: "production",
    type: "feishu",
    token: "hook-token",
    at: "ou_1"
  });

  vm.notify.token = "";
  await vm.testNotify();
  assert.equal(calls.test.length, 2);
  assert.deepEqual(vm.notifyTest.result, {
    type: "warning",
    text: "集群 production 换了 IM 类型，请重新填写机器人地址"
  });

  vm.notifyKept = { type: "feishu", at: "", configured: true };
  await vm.testNotify();
  assert.equal(calls.test.length, 2);
  assert.match(vm.notifyTest.result.text, /发送测试消息请重新填写/);

  vm.options.env = "";
  vm.notify.token = "hook-token";
  await vm.testNotify();
  assert.equal(calls.test.length, 2);
  assert.equal(vm.notifyTest.result.text, "请先填写集群唯一标识");
});

test("测试接口出错时显示原因；读不到消息通知配置不影响安装", async t => {
  const { vm, calls } = await harness(
    t,
    { list: async () => [task()] },
    {},
    {
      getNotificationSettings: async () => {
        throw new Error("Network Error");
      },
      testNotification: async () => {
        throw { response: { status: 429, data: { success: false } } };
      }
    }
  );
  assert.equal(vm.existingText, "");
  assert.equal(vm.error, "");
  vm.notify.token = "abc123";
  await vm.testNotify();
  assert.deepEqual(vm.notifyTest.result, {
    type: "warning",
    text: "操作太频繁，请稍后再试"
  });
  assert.equal(vm.notifyTest.testing, false);
  assert.equal(calls.test.length, 0);
});

test("恢复带通知配置的任务：表单显示已填写，未改动时可以直接安装", async t => {
  const ready = task({
    status: "ready",
    options: {
      ...served(),
      notification: { type: "slack", at: "U012AB3CD", configured: true }
    }
  });
  const { vm } = await harness(t, { list: async () => [ready] });
  assert.equal(vm.task.id, "installation-1");
  assert.deepEqual(
    { ...vm.notify },
    { type: "slack", token: "", at: "U012AB3CD" }
  );
  assert.ok(vm.expanded.includes("notify"));
  assert.equal(vm.canInstall, true);
  vm.notify.at = "U999";
  assert.equal(vm.canInstall, false);
});

const layout = compile("../src/views/workbench/table-layout.ts");

test("Agent 管理表格列宽：表头一行放得下，可排序和带问号的另加图标宽度", () => {
  // 中文按字号 14px 算：6 个字 84，加左右内边距 24、边框 2
  assert.equal(layout.headerWidth("采集历史数据"), 110);
  // 可排序的再加排序图标 24（以前 110 宽，排序图标被挤到第二行）
  assert.equal(layout.headerWidth("新服务免确认", { sortable: true }), 134);
  assert.equal(
    layout.headerWidth("别名", { hint: true }),
    layout.headerWidth("别名") + 18
  );
  // 英文按大致宽度估算，宁大勿小
  assert.ok(layout.headerWidth("K8S", { sortable: true }) >= 3 * 9 + 48);
});

test("Agent 管理表格列宽：K8S 列按最长的集群名完整显示，不比表头窄，太长的截断", () => {
  const header = layout.headerWidth("K8S", { sortable: true });
  // 没有数据时用表头宽度
  assert.equal(layout.fitColumnWidth([], header), header);
  // 用传入的测量函数：最长的一项 + 内边距 24 + 边框 2
  const measure = text => text.length * 8;
  assert.equal(
    layout.fitColumnWidth(
      ["a", "production-apollo-ops", "beta"],
      header,
      measure
    ),
    21 * 8 + 26
  );
  // 空值不参与计算
  assert.equal(layout.fitColumnWidth(["", "ab"], 10, measure), 16 + 26);
  // 超过上限的截断（悬停看全名）
  assert.equal(
    layout.fitColumnWidth(["x".repeat(200)], header, measure),
    layout.NAME_COLUMN_MAX
  );
  assert.equal(layout.fitColumnWidth(["x".repeat(30)], 50, measure, 100), 100);
  // 默认用估算：中文按字号，比英文宽
  assert.ok(
    layout.estimateTextWidth("生产集群") > layout.estimateTextWidth("prod")
  );
});

// ---------------------------------------------------------------- 修复 Agent
const healthUtils = compile("../src/views/workbench/health-utils.ts");
// 服务端用保存的 AI Kubeconfig 建的修复任务：参数已预填（集群标识固定、命名空间沿用现有 Agent 的、Agent 版本为推荐版本）
const repairTask = (extra = {}) =>
  task({
    id: "repair-1",
    context: "kubedoor-agent",
    contexts: [{ name: "kubedoor-agent", server: "https://k8s.example.com" }],
    repair: { env: "production", source: "ai-connection" },
    options: served({ namespace: "kubedoor-system", agent_tag: "2.6.0" }),
    ...extra
  });
const upload = text => ({
  target: {
    files: [{ name: "config", size: 10, text: async () => text }],
    value: ""
  }
});

test("修复：有 AI Kubeconfig 时直接到确认信息，集群标识和命名空间不能改，检测提交的就是这个集群", async t => {
  const repairs = [];
  const sent = [];
  const { vm, calls, saved, storageReads } = await harness(
    t,
    {
      list: async () => {
        throw new Error("修复时不列出别的安装任务");
      },
      repair: async env => {
        repairs.push(env);
        return { task: repairTask(), message: "" };
      },
      preflight: async (id, context, selected) => {
        sent.push({ id, context, selected });
        // 服务端把命名空间定为现有 Agent 所在的，检测结果多一行「现有 Agent」
        return repairTask({
          status: "ready",
          revision: 2,
          options: { ...selected, namespace: "kubedoor-system" },
          checks: [
            {
              name: "现有 Agent",
              status: "success",
              message: "kubedoor-system/kubedoor-agent"
            }
          ]
        });
      }
    },
    {},
    {},
    {
      repairEnv: "production",
      repairConnection: { origin: "agent-install", context: "kubedoor-agent" }
    }
  );
  assert.deepEqual(repairs, ["production"]);
  assert.equal(vm.error, "");
  assert.equal(vm.task.id, "repair-1");
  assert.equal(vm.stage, 1);
  assert.equal(vm.isRepair, true);
  assert.equal(vm.identityLocked, true);
  assert.equal(vm.dialogTitle, "修复 Agent：production");
  assert.equal(vm.usingSavedConnection, true);
  assert.match(
    vm.repairText,
    /^用保存的 AI Kubeconfig（自动配置）连接集群，覆盖安装集群 production 的 Agent；集群唯一标识和命名空间沿用现有的/
  );
  // 服务端预填的参数原样显示
  assert.deepEqual(
    [vm.options.env, vm.options.namespace, vm.options.agent_tag],
    ["production", "kubedoor-system", "2.6.0"]
  );
  await vm.preflight();
  assert.equal(sent.length, 1);
  assert.deepEqual(
    [sent[0].id, sent[0].context, sent[0].selected.env],
    ["repair-1", "kubedoor-agent", "production"]
  );
  assert.equal(vm.canInstall, true);
  await vm.start(false);
  assert.equal(calls.start, 1);
  assert.equal(calls.inspect, 0);
  // 修复不读也不写浏览器里记住的安装任务
  assert.deepEqual(storageReads, []);
  assert.equal(saved.size, 0);
});

test("修复：没有可用的 AI Kubeconfig 时停在上传，上传的 kubeconfig 建这个集群的修复任务", async t => {
  const { vm, calls } = await harness(
    t,
    {
      repair: async () => ({
        task: null,
        message: "这个集群还没有 AI Kubeconfig。"
      }),
      inspect: async (raw, repairEnv) =>
        repairTask({
          id: "repair-2",
          context: "admin@prod",
          contexts: [{ name: "admin@prod" }],
          repair: { env: repairEnv, source: "upload" }
        })
    },
    {},
    {},
    { repairEnv: "production" }
  );
  assert.equal(vm.stage, 0);
  assert.equal(vm.task, undefined);
  assert.equal(vm.repairMessage, "这个集群还没有 AI Kubeconfig。");
  // 服务端的原因后面接「请上传」，句号不重复
  assert.equal(
    vm.uploadForRepair,
    "这个集群还没有 AI Kubeconfig。请上传集群 production 的 kubeconfig（要能在集群里创建、修改 Agent 的资源），用它覆盖安装。"
  );
  assert.equal(vm.dialogTitle, "修复 Agent：production");
  await vm.upload(upload("secret-kubeconfig"));
  assert.deepEqual(calls.inspected, [["secret-kubeconfig", "production"]]);
  assert.equal(vm.task.repair.source, "upload");
  assert.equal(vm.repairMessage, "");
  assert.equal(vm.usingSavedConnection, false);
  assert.equal(vm.stage, 0);
  vm.continueToOptions();
  assert.equal(vm.stage, 1);
  assert.equal(vm.options.env, "production");
  assert.equal(vm.identityLocked, true);
  assert.match(
    vm.repairText,
    /^用上传的 kubeconfig 连接集群，覆盖安装集群 production 的 Agent/
  );
});

test("普通安装不恢复修复任务；在任务列表里选中修复任务按修复显示，也不记进浏览器存储", async t => {
  const repairing = repairTask({ status: "failed", updated_at: "2026-11-15" });
  const normal = task({
    id: "normal",
    status: "draft",
    updated_at: "2026-11-01"
  });
  assert.equal(
    utils.recoverableInstall([repairing, normal], "repair-1").id,
    "normal"
  );
  assert.equal(utils.recoverableInstall([repairing]), undefined);
  const { vm, saved, calls } = await harness(t, {
    list: async () => [repairing],
    get: async () => repairing
  });
  assert.equal(vm.task, undefined);
  assert.equal(vm.isRepair, false);
  assert.equal(vm.dialogTitle, "安装 Agent");
  assert.equal(vm.identityLocked, false);
  await vm.selectTask("repair-1");
  assert.equal(vm.isRepair, true);
  assert.equal(vm.dialogTitle, "修复 Agent：production");
  assert.equal(vm.identityLocked, true);
  assert.equal(saved.get("kubedoor-agent-install:alice"), undefined);
  // 「安装新集群」回到普通安装，普通上传不带要修复的集群
  vm.newInstall();
  assert.equal(vm.isRepair, false);
  await vm.upload(upload("kc"));
  assert.deepEqual(calls.inspected, [["kc", undefined]]);
});

test("修复时目标集群里没有找到这个集群的 Agent：确认 kubeconfig 连对了集群再强制覆盖", async t => {
  const preflights = [];
  const confirms = [];
  // master 给的没找到 Agent 的那一项（不是要接管的资源，命名空间为空）
  const missing = {
    kind: "Agent",
    name: "production",
    namespace: "",
    reason:
      "目标集群里没有找到集群 production 的 Agent，请确认 kubeconfig 连的是集群 production"
  };
  const { vm } = await harness(
    t,
    {
      repair: async () => ({ task: repairTask(), message: "" }),
      preflight: async (id, context, selected, force) => {
        preflights.push(force);
        return force
          ? repairTask({ status: "ready", revision: 3, options: selected })
          : repairTask({
              status: "draft",
              revision: 2,
              options: selected,
              takeover: [missing],
              checks: [
                {
                  name: "安装预检",
                  status: "error",
                  message: missing.reason,
                  forceable: true
                }
              ]
            });
      }
    },
    {
      ElMessageBox: {
        confirm: async (...args) => {
          confirms.push(args);
          return "confirm";
        }
      }
    },
    {},
    { repairEnv: "production" }
  );
  await vm.preflight();
  assert.equal(vm.forceable, true);
  assert.match(vm.takeoverText, /^目标集群里没有找到集群 production 的 Agent/);
  await vm.forcePreflight();
  assert.equal(confirms[0][1], "强制覆盖安装");
  // 确认框只说明没找到 Agent（服务端的原因）和确认后的结果，不显示成「Agent/production（）：…」那种接管资源的清单
  const texts = confirms[0][0].children.map(child => child.children);
  assert.equal(texts.length, 2);
  assert.equal(texts[0], missing.reason);
  assert.equal(
    texts[1],
    "确认后会在这个集群里重新安装 Agent，以 production 的名义接入控制端。"
  );
  assert.ok(texts.every(text => typeof text === "string"));
  assert.deepEqual(preflights, [false, true]);
  assert.equal(vm.canInstall, true);
  // 拆分：kind 为 Agent 的是没找到 Agent，其余是要接管的资源
  assert.deepEqual(utils.takeoverParts(undefined), {
    missing: [],
    resources: []
  });
  assert.deepEqual(
    utils.takeoverParts([missing, ...TAKEOVER]).resources,
    TAKEOVER
  );
  // AI Kubeconfig 的称呼和 Agent 列表那一列一样
  assert.equal(utils.connectionLabel({ origin: "agent-install" }), "自动配置");
  assert.equal(
    utils.connectionLabel({ origin: "upload", context: "admin@prod" }),
    "admin@prod"
  );
  assert.equal(utils.connectionLabel(null, "kubedoor-agent"), "kubedoor-agent");
});

test("修复相关接口：健康检查、开始修复，上传时带上要修复的集群", async () => {
  const requests = [];
  const api = compile("../src/api/agent-install.ts", {
    "@/utils/http": {
      http: {
        request: async (...args) => {
          requests.push(args);
          return { success: true, data: { task: null, message: "" } };
        }
      }
    }
  }).agentInstallApi;
  await api.health("prod");
  assert.deepEqual(requests[0].slice(0, 3), [
    "get",
    "/api/admin/agent-health",
    { params: { env: "prod" } }
  ]);
  assert.equal(requests[0][3].silent, true);
  // 检查最坏要 20~30 秒：不能用 http 默认的 10 秒超时
  assert.equal(requests[0][3].timeout, 60000);
  await api.repair("prod");
  assert.deepEqual(requests[1].slice(0, 3), [
    "post",
    "/api/admin/agent-installations/repair",
    { data: { env: "prod" } }
  ]);
  await api.inspect("kc", "prod");
  assert.deepEqual(requests[2][2].data, {
    kubeconfig: "kc",
    repair_env: "prod"
  });
  await api.inspect("kc");
  assert.deepEqual(requests[3][2].data, { kubeconfig: "kc" });
});

const check = (key, group, status, name = key) => ({
  key,
  group,
  name,
  status,
  message: ""
});
const CHECKS = [
  check("connection", "agent", "success", "连接控制端"),
  check("events", "data", "warning", "K8S 事件上报"),
  check("component:vmagent", "component", "error", "vmagent"),
  check("kubeconfig", "repair", "success", "AI Kubeconfig"),
  check("future", "new-group", "skipped", "以后加的检查"),
  check("version", "agent", "warning", "Agent 版本")
];

test("修复弹框：检查项按分组显示，汇总说明离线 / 异常 / 需要注意 / 全部正常", () => {
  assert.deepEqual(
    healthUtils
      .groupChecks(CHECKS)
      .map(group => [group.label, group.checks.map(item => item.key)]),
    [
      ["Agent 与连接", ["connection", "version"]],
      ["组件", ["component:vmagent"]],
      ["数据上报", ["events"]],
      ["修复", ["kubeconfig"]],
      ["其他", ["future"]]
    ]
  );
  assert.deepEqual(healthUtils.groupChecks([]), []);
  assert.deepEqual(
    ["success", "warning", "error", "skipped"].map(healthUtils.healthTagType),
    ["success", "warning", "danger", "info"]
  );
  // Agent 离线时先说离线
  const offline = healthUtils.healthSummary({ online: false, checks: CHECKS });
  assert.equal(offline.type, "error");
  assert.match(offline.title, /^Agent 离线/);
  assert.deepEqual(
    healthUtils.healthSummary({ online: true, checks: CHECKS }),
    {
      type: "error",
      title: "发现 1 项异常：vmagent"
    }
  );
  const noErrors = CHECKS.filter(item => item.status !== "error");
  assert.deepEqual(
    healthUtils.healthSummary({ online: true, checks: noErrors }),
    { type: "warning", title: "有 2 项需要注意：K8S 事件上报、Agent 版本" }
  );
  const fine = CHECKS.filter(item =>
    ["success", "skipped"].includes(item.status)
  );
  assert.deepEqual(healthUtils.healthSummary({ online: true, checks: fine }), {
    type: "success",
    title: "全部检查通过（1 项跳过）"
  });
  assert.equal(
    healthUtils.healthSummary({ online: true, checks: [] }).type,
    "info"
  );
  // 修复说明：有 AI Kubeconfig 直接用，没有要上传，还没查到时两种都说
  const hints = [
    healthUtils.repairHint({
      configured: true,
      origin: "agent-install",
      context: "kubedoor-agent"
    }),
    healthUtils.repairHint({
      configured: true,
      origin: "upload",
      context: "admin@prod"
    }),
    healthUtils.repairHint({ configured: false }),
    healthUtils.repairHint(undefined)
  ];
  for (const hint of hints) assert.match(hint, /集群唯一标识和命名空间不变/);
  assert.match(hints[0], /保存的 AI Kubeconfig（自动配置）进入「确认信息」/);
  assert.match(hints[1], /保存的 AI Kubeconfig（admin@prod）/);
  assert.match(hints[2], /还没有 AI Kubeconfig，需要上传/);
  assert.match(
    hints[3],
    /有 AI Kubeconfig 时直接用它进入「确认信息」，没有时要上传/
  );
  assert.equal(healthUtils.formatCheckedAt("not a time"), "not a time");
  assert.equal(healthUtils.formatCheckedAt(""), "");
  assert.match(
    healthUtils.formatCheckedAt("2026-11-15T08:00:00Z"),
    /^2026-11-1[45] \d{2}:00:00$/
  );
});

test("修复弹框：打开就检查，检查失败也能点「修复（覆盖安装）」", async t => {
  const asked = [];
  let fail = true;
  const health = async env => {
    asked.push(env);
    if (fail)
      throw { response: { data: { message: "集群 production 没有接入" } } };
    return {
      env,
      online: true,
      version: "2.5.2",
      recommended_version: "2.6.0",
      namespace: "kubedoor",
      checked_at: "2026-11-15T08:00:00Z",
      checks: CHECKS.filter(item => item.status !== "error"),
      kubeconfig: { configured: true, origin: "agent-install" }
    };
  };
  const component = compile(
    "../src/views/workbench/AgentHealthDialog.vue",
    {
      "@/api/agent-install": { agentInstallApi: { health } },
      "./install-utils": utils,
      "./health-utils": healthUtils
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
  const events = { repair: [], visible: [] };
  const container = {};
  const vnode = h(
    { ...component, render: () => null },
    {
      modelValue: true,
      env: "production",
      onRepair: env => events.repair.push(env),
      "onUpdate:modelValue": value => events.visible.push(value)
    }
  );
  renderer.render(vnode, container);
  t.after(() => renderer.render(null, container));
  const vm = vnode.component.setupState;
  await flush();
  assert.deepEqual(asked, ["production"]);
  assert.equal(vm.error, "集群 production 没有接入");
  assert.equal(vm.health, undefined);
  assert.equal(vm.loading, false);
  assert.match(vm.hint, /有 AI Kubeconfig 时直接用它/);
  // 重新检查
  fail = false;
  await vm.load();
  assert.equal(vm.error, "");
  assert.equal(vm.summary.type, "warning");
  assert.deepEqual(
    vm.groups.map(group => group.key),
    ["agent", "data", "repair", "other"]
  );
  assert.match(vm.hint, /保存的 AI Kubeconfig（自动配置）/);
  vm.repair();
  assert.deepEqual(events.repair, ["production"]);
  assert.deepEqual(events.visible, [false]);
});
