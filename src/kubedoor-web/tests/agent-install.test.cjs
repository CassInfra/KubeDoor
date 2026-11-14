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

async function harness(t, overrides = {}, elementPlus = {}, notification = {}) {
  const oldStorage = global.localStorage;
  const oldWindow = global.window;
  const saved = new Map();
  const opened = [];
  global.localStorage = {
    getItem: key => saved.get(key) || null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key)
  };
  global.window = { open: (...args) => opened.push(args) };
  const calls = { start: 0, inspect: 0, test: [] };
  const api = {
    defaults: async () => ({
      ...served(),
      metrics_available: true,
      settings: settings()
    }),
    list: async () => [],
    inspect: async () => task(),
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
  const vnode = h({ ...component, render: () => null }, { modelValue: true });
  renderer.render(vnode, container);
  const vm = vnode.component.setupState;
  t.after(() => {
    renderer.render(null, container);
    global.localStorage = oldStorage;
    global.window = oldWindow;
  });
  await flush();
  return { vm, api, calls, saved, opened };
}

test("每个安装参数和 context 的变更都会使已检测的计划失效", () => {
  const before = utils.installFingerprint("production", options());
  for (const [key, value] of Object.entries({
    env: "other",
    namespace: "other",
    master_url: "https://other.example.com",
    mode: "full",
    agent_tag: "new",
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

test("通用配置缺的项给出提醒，没有远程写地址时只能仅安装 Agent", () => {
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

test("没有远程写地址：完整接入不可选；去通用配置填写后「重新读取」，计划要重新检测", async t => {
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
  // 在通用配置填好远程写地址后重新读取：完整接入可选，已检测的计划失效
  current = { ...current, metrics_available: true, settings: settings() };
  await vm.reloadDefaults();
  assert.equal(vm.metricsAvailable, true);
  assert.deepEqual(
    vm.missing.map(item => item.key),
    ["oss_url"]
  );
  assert.equal(vm.canInstall, false);
  // 又被删掉了：正在选的完整接入退回仅安装 Agent
  vm.options.mode = "full";
  current = {
    ...current,
    metrics_available: false,
    settings: settings({ metrics_write_url: "" })
  };
  await vm.reloadDefaults();
  assert.equal(vm.options.mode, "agent");
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
  assert.ok(vm.advanced.includes("notify"));

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
  assert.ok(vm.advanced.includes("notify"));
  assert.equal(vm.canInstall, true);
  vm.notify.at = "U999";
  assert.equal(vm.canInstall, false);
});
