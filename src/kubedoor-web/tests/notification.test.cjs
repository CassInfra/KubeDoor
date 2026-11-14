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

/** 单文件编译加载;vue 为 true 时只编译 SFC 的脚本(模板不渲染) */
function compile(filename, overrides = {}, vue = false) {
  const target = path.resolve(__dirname, filename);
  let source = fs.readFileSync(target, "utf8");
  if (vue) {
    const { descriptor } = parse(source, { filename: target });
    source = compileScript(descriptor, { id: "notification-test" }).content;
  }
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

const utils = compile("../src/views/system/notification/notification-utils.ts");

const savedWecom = (extra = {}) => ({
  type: "wecom",
  at: "zhangsan",
  configured: true,
  token_hint: "****a1b2",
  ...extra
});
const savedProd = (extra = {}) => ({
  env: "prod-k8s",
  type: "dingding",
  at: "13800000000",
  configured: true,
  token_hint: "****9f3c",
  ...extra
});
const settings = (extra = {}) => ({
  default: savedWecom(),
  clusters: [savedProd()],
  envs: ["prod-k8s", "test-k8s"],
  updated_by: "kubedoor",
  updated_at: "2026-11-07T12:00:00+08:00",
  ...extra
});
const form = (extra = {}) => ({ type: "wecom", token: "", at: "", ...extra });

test("说明表和补充说明的文字与接口约定第 8 节一致(安装向导共用)", () => {
  assert.deepEqual(
    utils.NOTICE_ROWS.map(row => [row.name, row.sender]),
    [
      ["K8S 事件告警", "控制服务 kubedoor-master"],
      ["运维操作通知", "各集群的 kubedoor-agent，经控制服务发送"],
      ["指标告警", "告警服务 kubedoor-alarm（安装了告警组件才有）"],
      ["管控表通知", "控制服务 kubedoor-master"]
    ]
  );
  assert.equal(
    utils.NOTICE_ROWS[0].when,
    "K8S 事件命中事件告警规则（在系统管理 → 事件告警规则配置）时，例如重启、健康检查失败、挂载失败、调度失败、资源压力；不受告警屏蔽影响"
  );
  assert.equal(
    utils.NOTICE_ROWS[1].when,
    "扩缩容、重启、更新镜像（开始、成功、超时）、准入控制改写副本数或资源、定时任务创建或删除失败、取消禁止调度失败、jstack / jmap 诊断结果"
  );
  assert.equal(
    utils.NOTICE_ROWS[2].when,
    "vmalert 告警规则触发和恢复时；可以按规则屏蔽，消息里带【屏蔽】链接"
  );
  assert.equal(
    utils.NOTICE_ROWS[3].when,
    "每天采集高峰期数据时，发现新服务并自动加入管控表"
  );
  assert.equal(
    utils.NOTICE_NOTE,
    "给某个集群单独配置后，这个集群的上述通知都发到它自己的群，其他集群用全局配置。"
  );
});

test("IM 类型下拉四项，输入提示按类型变化", () => {
  assert.deepEqual(utils.TYPE_OPTIONS, [
    { value: "wecom", label: "企业微信" },
    { value: "dingding", label: "钉钉" },
    { value: "feishu", label: "飞书" },
    { value: "slack", label: "Slack" }
  ]);
  assert.equal(
    utils.TYPE_HINTS.wecom.token,
    "粘贴群机器人 Webhook 地址，或 key= 后面那一段"
  );
  assert.match(utils.TYPE_HINTS.dingding.token, /access_token=/);
  assert.match(utils.TYPE_HINTS.feishu.token, /hook\//);
  assert.match(utils.TYPE_HINTS.slack.token, /services\//);
  assert.match(utils.TYPE_HINTS.wecom.at, /userid/);
  assert.match(utils.TYPE_HINTS.dingding.at, /手机号/);
  assert.match(utils.TYPE_HINTS.feishu.at, /open_id/);
  assert.match(utils.TYPE_HINTS.slack.at, /Slack 成员 ID/);
  for (const type of utils.NOTIFY_TYPES) {
    assert.ok(utils.TYPE_HINTS[type].tokenHelp.includes("https://"));
    assert.equal(utils.typeHint(type), utils.TYPE_HINTS[type]);
  }
  // 不认识的类型给空提示,模板不报错
  assert.deepEqual(utils.typeHint("email"), {
    token: "",
    tokenHelp: "",
    at: ""
  });
  assert.equal(utils.tokenPlaceholder("email", null), "");
});

test("机器人地址的 placeholder：已配置显示最后 4 位并提示留空保留，换了类型要重新填", () => {
  assert.equal(
    utils.tokenPlaceholder("wecom", savedWecom()),
    "已配置（****a1b2），留空保留"
  );
  // 只给了 4 位时补上星号;没给提示时只说已配置
  assert.equal(
    utils.tokenPlaceholder("wecom", savedWecom({ token_hint: "a1b2" })),
    "已配置（****a1b2），留空保留"
  );
  assert.equal(
    utils.tokenPlaceholder("wecom", savedWecom({ token_hint: "" })),
    "已配置，留空保留"
  );
  assert.equal(
    utils.tokenPlaceholder("dingding", savedWecom()),
    "换了 IM 类型，请重新填写钉钉机器人地址"
  );
  assert.equal(
    utils.tokenPlaceholder("feishu", null),
    utils.TYPE_HINTS.feishu.token
  );
  assert.equal(utils.tokenHintText(null), "未配置");
  assert.equal(
    utils.tokenHintText(savedWecom({ configured: false })),
    "未配置"
  );
  assert.equal(utils.tokenHintText(savedWecom()), "已配置（****a1b2）");
  assert.equal(utils.keepsSavedToken("wecom", savedWecom()), true);
  assert.equal(utils.keepsSavedToken("slack", savedWecom()), false);
  assert.equal(utils.keepsSavedToken("wecom", undefined), false);
});

test("安装 Agent 对话框：集群已有单独配置时说明留空保持不变", () => {
  assert.equal(
    utils.existingClusterText(savedWecom()),
    "该集群已有单独配置：企业微信（****a1b2），留空保持不变，填写则覆盖"
  );
  assert.equal(
    utils.existingClusterText(savedProd({ token_hint: "" })),
    "该集群已有单独配置：钉钉，留空保持不变，填写则覆盖"
  );
  assert.equal(utils.existingClusterText(null), "");
  assert.equal(
    utils.existingClusterText(savedWecom({ configured: false })),
    ""
  );
});

test("读取配置可以静默：出错不弹提示，交给调用方处理", async () => {
  const requests = [];
  const api = compile("../src/api/notification.ts", {
    "@/utils/http": {
      http: { request: async (...args) => (requests.push(args), {}) }
    }
  });
  await api.getNotificationSettings();
  await api.getNotificationSettings(true);
  assert.deepEqual(requests, [
    ["get", "/api/admin/notification", undefined, undefined],
    ["get", "/api/admin/notification", undefined, { silent: true }]
  ]);
});

test("按域名认出整条 Webhook 地址是哪家的，只填 token 或认不出时为空", () => {
  assert.equal(
    utils.webhookType(
      " https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc "
    ),
    "wecom"
  );
  assert.equal(
    utils.webhookType("https://OAPI.DINGTALK.COM/robot/send?access_token=x"),
    "dingding"
  );
  assert.equal(
    utils.webhookType("https://open.feishu.cn/open-apis/bot/v2/hook/x"),
    "feishu"
  );
  // 国际版 Lark 的机器人发不了(发送只走 open.feishu.cn),不认作飞书地址,交给后端提示
  assert.equal(
    utils.webhookType("https://open.larksuite.com/open-apis/bot/v2/hook/x"),
    ""
  );
  assert.equal(
    utils.webhookType("https://hooks.slack.com/services/T1/B2/c3"),
    "slack"
  );
  assert.equal(utils.webhookType("abc123"), "");
  assert.equal(utils.webhookType("T1/B2/c3"), "");
  assert.equal(utils.webhookType("https://example.com/hook"), "");
  assert.equal(utils.webhookType("https://qyapi.weixin.qq.com.evil.cn/x"), "");
  assert.equal(utils.webhookType("https://u@qyapi.weixin.qq.com/x"), "");
});

test("通道校验：去掉首尾空白，缺地址、换类型没填、地址和类型不符、@ 人过长都给中文说明", () => {
  assert.deepEqual(
    utils.channelInput(
      "全局配置",
      form({ token: "  abc123 ", at: " zhangsan " }),
      null
    ),
    { type: "wecom", token: "abc123", at: "zhangsan" }
  );
  // 已保存且类型没变:留空沿用
  assert.deepEqual(
    utils.channelInput("全局配置", form({ at: "lisi" }), savedWecom()),
    { type: "wecom", token: "", at: "lisi" }
  );
  assert.throws(
    () => utils.channelInput("全局配置", form(), null),
    /^Error: 全局配置请填写机器人地址$/
  );
  assert.throws(
    () =>
      utils.channelInput(
        "集群 prod-k8s ",
        form({ type: "feishu" }),
        savedProd()
      ),
    /^Error: 集群 prod-k8s 换了 IM 类型，请重新填写机器人地址$/
  );
  assert.throws(
    () => utils.channelInput("全局配置", form({ token: "ab c" }), null),
    /不能包含空格、换行或控制字符/
  );
  assert.throws(
    () =>
      utils.channelInput(
        "全局配置",
        form({ token: "ab\u0007c" }),
        savedWecom()
      ),
    /控制字符/
  );
  assert.throws(
    () =>
      utils.channelInput(
        "集群 prod-k8s ",
        form({
          type: "dingding",
          token: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
        }),
        null
      ),
    /^Error: 集群 prod-k8s 的机器人地址是企业微信的，IM 类型却选了钉钉$/
  );
  assert.throws(
    () => utils.channelInput("全局配置", form({ type: "email", token: "x" })),
    /请选择 IM 类型/
  );
  // @ 人按字符数算,最多 128 个
  const at128 = "张".repeat(128);
  assert.equal(
    utils.channelInput("全局配置", form({ token: "x", at: at128 }), null).at,
    at128
  );
  assert.throws(
    () =>
      utils.channelInput(
        "全局配置",
        form({ token: "x", at: at128 + "三" }),
        null
      ),
    /最多 128 个字符/
  );
  assert.throws(
    () => utils.channelInput("全局配置", form({ token: "x", at: "a\tb" })),
    /默认 @ 人不能包含控制字符/
  );
});

test("组装 PUT 请求体：格式和接口约定一致，全局留空沿用，按集群整体替换", () => {
  const rows = [
    {
      env: "prod-k8s",
      type: "dingding",
      token: "",
      at: "13800000000",
      saved: savedProd()
    },
    {
      env: "test-k8s",
      type: "slack",
      token: " https://hooks.slack.com/services/T1/B2/c3 ",
      at: "",
      saved: null
    }
  ];
  const payload = utils.settingsPayload(
    form({ at: "zhangsan" }),
    savedWecom(),
    rows
  );
  assert.deepEqual(payload, {
    default: { type: "wecom", token: "", at: "zhangsan" },
    clusters: [
      { env: "prod-k8s", type: "dingding", token: "", at: "13800000000" },
      {
        env: "test-k8s",
        type: "slack",
        token: "https://hooks.slack.com/services/T1/B2/c3",
        at: ""
      }
    ]
  });
  // 没保存过全局配置、也没填:不配置全局
  assert.deepEqual(utils.settingsPayload(form({ type: "slack" }), null, []), {
    default: null,
    clusters: []
  });
  // 填了 @ 人却没填地址:提示补地址,不悄悄丢掉
  assert.throws(
    () => utils.settingsPayload(form({ at: "zhangsan" }), null, []),
    /全局配置请填写机器人地址/
  );
  assert.throws(
    () =>
      utils.settingsPayload(form(), savedWecom(), [
        { env: "test-k8s", type: "wecom", token: "", at: "", saved: null }
      ]),
    /^Error: 集群 test-k8s 请填写机器人地址$/
  );
  assert.throws(
    () => utils.settingsPayload(form(), savedWecom(), [rows[0], rows[0]]),
    /集群 prod-k8s 重复了/
  );
  assert.throws(
    () =>
      utils.settingsPayload(form(), savedWecom(), [
        { ...rows[1], env: "prod k8s" }
      ]),
    /不合规/
  );
});

test("删除全局配置的请求体：default 为 null，按集群的配置原样保留", () => {
  assert.deepEqual(utils.withoutDefault(settings()), {
    default: null,
    clusters: [
      { env: "prod-k8s", type: "dingding", token: "", at: "13800000000" }
    ]
  });
  assert.deepEqual(utils.withoutDefault(settings({ clusters: [] })), {
    default: null,
    clusters: []
  });
});

test("测试请求：env 为空测全局；填了地址用填的，没填用已保存的", () => {
  assert.deepEqual(
    utils.testRequest("", form({ at: "zhangsan" }), savedWecom()),
    {
      env: "",
      type: "wecom",
      token: "",
      at: "zhangsan"
    }
  );
  assert.deepEqual(
    utils.testRequest(
      "prod-k8s",
      form({ type: "dingding", token: "new-token" }),
      savedProd()
    ),
    { env: "prod-k8s", type: "dingding", token: "new-token", at: "" }
  );
  assert.throws(
    () => utils.testRequest("test-k8s", form(), null),
    /^Error: 集群 test-k8s 请填写机器人地址$/
  );
});

test("测试结果：成功显示发送成功，失败显示 IM 返回的错误，429 提示操作太频繁", () => {
  assert.deepEqual(utils.testResultView({ ok: true, message: "发送成功" }), {
    type: "success",
    text: "发送成功"
  });
  assert.deepEqual(
    utils.testResultView({
      ok: false,
      message: "errcode 93000: invalid webhook url"
    }),
    { type: "error", text: "发送失败：errcode 93000: invalid webhook url" }
  );
  assert.deepEqual(utils.testResultView({ ok: false, message: "" }), {
    type: "error",
    text: "发送失败"
  });
  const reject = (status, data) => ({ response: { status, data } });
  assert.deepEqual(
    utils.testErrorView(reject(429, { success: false, message: "too many" })),
    { type: "warning", text: "操作太频繁，请稍后再试" }
  );
  assert.equal(utils.testErrorView(reject(401, {})), null);
  assert.deepEqual(
    utils.testErrorView(
      reject(422, {
        success: false,
        message: "没有已保存的机器人地址，请先填写"
      })
    ),
    { type: "error", text: "没有已保存的机器人地址，请先填写" }
  );
  assert.deepEqual(
    utils.testErrorView(reject(502, "<html>Bad Gateway</html>")),
    {
      type: "error",
      text: "发送测试消息失败（HTTP 502）"
    }
  );
  assert.deepEqual(
    utils.testErrorView({
      code: "ECONNABORTED",
      message: "timeout of 30000ms exceeded"
    }),
    { type: "error", text: "发送测试消息超时，请稍后再试" }
  );
  assert.deepEqual(utils.testErrorView(new Error("Network Error")), {
    type: "error",
    text: "发送测试消息失败：连不上服务，请稍后再试"
  });
});

test("新增一行：已单独配置的集群在下拉里不能选；删掉又加回来沿用已保存的", () => {
  assert.deepEqual(
    utils.envOptions(["prod-k8s", "test-k8s"], [{ env: "prod-k8s" }]),
    [
      { value: "prod-k8s", label: "prod-k8s（已单独配置）", disabled: true },
      { value: "test-k8s", label: "test-k8s", disabled: false }
    ]
  );
  assert.deepEqual(
    utils.newClusterRow("prod-k8s", settings(), form({ type: "slack" })),
    {
      env: "prod-k8s",
      type: "dingding",
      token: "",
      at: "13800000000",
      saved: savedProd()
    }
  );
  // 新集群的 IM 类型默认跟全局配置一样
  assert.deepEqual(
    utils.newClusterRow("test-k8s", settings(), form({ type: "feishu" })),
    {
      env: "test-k8s",
      type: "feishu",
      token: "",
      at: "",
      saved: null
    }
  );
});

test("未保存修改的判断：按已保存配置生成的表单没有修改，集群顺序不算修改", () => {
  const data = settings({
    clusters: [
      savedProd(),
      savedProd({ env: "dev-k8s", type: "slack", at: "" })
    ]
  });
  const forms = utils.formsFromSettings(data);
  assert.deepEqual(forms.global, { type: "wecom", token: "", at: "zhangsan" });
  assert.equal(forms.rows.length, 2);
  assert.equal(forms.rows[1].saved.env, "dev-k8s");
  const saved = utils.savedDefault(data);
  const key = utils.formKey(saved, forms.global, forms.rows);
  assert.equal(
    utils.formKey(saved, { ...forms.global }, [...forms.rows].reverse()),
    key
  );
  assert.notEqual(
    utils.formKey(saved, { ...forms.global, token: "x" }, forms.rows),
    key
  );
  assert.notEqual(utils.formKey(saved, forms.global, forms.rows.slice(1)), key);
  // 没有全局配置:只改 IM 类型不算修改,填了地址才算
  const empty = utils.formsFromSettings(
    settings({ default: null }),
    "dingding"
  );
  assert.deepEqual(empty.global, { type: "dingding", token: "", at: "" });
  const emptyKey = utils.formKey(null, empty.global, []);
  assert.equal(utils.formKey(null, form({ type: "slack" }), []), emptyKey);
  assert.notEqual(utils.formKey(null, form({ token: "x" }), []), emptyKey);
  assert.equal(utils.savedDefault(settings({ default: null })), null);
  assert.equal(utils.savedDefault(null), null);
});

/** 保存接口的假实现:按请求体生成返回(token 留空的沿用原来的提示) */
function echo(payload, previous) {
  const hint = (item, old) =>
    item.token ? `****${item.token.slice(-4)}` : old?.token_hint || "****keep";
  return {
    default: payload.default
      ? {
          type: payload.default.type,
          at: payload.default.at,
          configured: true,
          token_hint: hint(payload.default, previous.default)
        }
      : null,
    clusters: payload.clusters.map(item => ({
      env: item.env,
      type: item.type,
      at: item.at,
      configured: true,
      token_hint: hint(
        item,
        previous.clusters.find(old => old.env === item.env)
      )
    })),
    envs: previous.envs,
    updated_by: "kubedoor",
    updated_at: "2026-11-07T12:30:00+08:00"
  };
}

async function harness(t, initial = settings()) {
  const oldWindow = global.window;
  global.window = { addEventListener() {}, removeEventListener() {} };
  const calls = { save: [], test: [] };
  const messages = [];
  const hooks = {
    load: async () => ({ success: true, data: initial }),
    test: async () => ({
      success: true,
      data: { ok: true, message: "发送成功" }
    }),
    confirm: async () => {}
  };
  let current = initial;
  const api = {
    getNotificationSettings: () => hooks.load(),
    saveNotificationSettings: async payload => {
      calls.save.push(payload);
      current = echo(payload, current);
      return { success: true, data: current };
    },
    testNotification: async request => {
      calls.test.push(request);
      return hooks.test(request);
    }
  };
  const component = compile(
    "../src/views/system/notification/index.vue",
    {
      "@/api/notification": api,
      "@/utils/message": {
        message: (text, options) => messages.push([text, options?.type])
      },
      "element-plus": { ElMessageBox: { confirm: () => hooks.confirm() } },
      "./notification-utils": utils,
      // 全局配置的表单项是共用组件(模板不渲染,脚本里只用到 import)
      "./NotifyChannelFields.vue": { default: {} }
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
  const settle = async () => {
    for (let i = 0; i < 3; i++) {
      await nextTick();
      await new Promise(resolve => setImmediate(resolve));
    }
  };
  // 挂载前可以改 hooks(比如让读取失败)
  const mount = async () => {
    renderer.render(vnode, container);
    await settle();
    return vnode.component.setupState;
  };
  t.after(() => {
    renderer.render(null, container);
    global.window = oldWindow;
  });
  return { mount, calls, messages, hooks };
}

test("页面：读取失败时不能保存，避免用空表单覆盖已保存的配置", async t => {
  const { mount, calls, hooks } = await harness(t);
  hooks.load = async () => {
    throw { response: { status: 500, data: { message: "数据库不可用" } } };
  };
  const vm = await mount();
  assert.equal(vm.loaded, false);
  assert.equal(vm.loadError, "数据库不可用");
  await vm.save();
  await vm.removeDefault();
  assert.deepEqual(calls.save, []);
});

test("页面：新增集群要填地址才能保存，保存后清空已填的地址", async t => {
  const { mount, calls, messages } = await harness(t);
  const vm = await mount();
  assert.equal(vm.loaded, true);
  assert.equal(vm.rows.length, 1);
  assert.equal(vm.dirty, false);
  assert.deepEqual(
    vm.envChoices.map(item => [item.value, item.disabled]),
    [
      ["prod-k8s", true],
      ["test-k8s", false]
    ]
  );
  // 已经单独配置的集群不能重复添加
  vm.newEnv = "prod-k8s";
  vm.addRow();
  assert.equal(vm.rows.length, 1);
  vm.newEnv = "test-k8s";
  vm.addRow();
  assert.equal(vm.rows.length, 2);
  assert.equal(vm.newEnv, "");
  assert.equal(vm.rows[1].type, "wecom");
  assert.equal(vm.dirty, true);

  await vm.save();
  assert.deepEqual(calls.save, []);
  assert.deepEqual(messages.at(-1), [
    "集群 test-k8s 请填写机器人地址",
    "warning"
  ]);

  vm.rows[1].token =
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123";
  vm.globalForm.at = "lisi";
  await vm.save();
  assert.deepEqual(calls.save, [
    {
      default: { type: "wecom", token: "", at: "lisi" },
      clusters: [
        { env: "prod-k8s", type: "dingding", token: "", at: "13800000000" },
        {
          env: "test-k8s",
          type: "wecom",
          token: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123",
          at: ""
        }
      ]
    }
  ]);
  assert.deepEqual(messages.at(-1), ["已保存，马上生效", "success"]);
  assert.equal(vm.rows[1].token, "");
  assert.equal(vm.rows[1].saved.token_hint, "****c123");
  assert.equal(vm.globalForm.at, "lisi");
  assert.equal(vm.dirty, false);
});

test("页面：删除全局配置要二次确认，确认后只删全局，表格里没保存的修改保留", async t => {
  const { mount, calls, hooks } = await harness(t);
  const vm = await mount();
  vm.rows[0].at = "13900000000";
  hooks.confirm = async () => {
    throw "cancel";
  };
  await vm.removeDefault();
  assert.deepEqual(calls.save, []);
  assert.ok(vm.globalSaved);

  hooks.confirm = async () => {};
  await vm.removeDefault();
  assert.deepEqual(calls.save, [
    {
      default: null,
      clusters: [
        { env: "prod-k8s", type: "dingding", token: "", at: "13800000000" }
      ]
    }
  ]);
  assert.equal(vm.globalSaved, null);
  assert.deepEqual(
    {
      type: vm.globalForm.type,
      token: vm.globalForm.token,
      at: vm.globalForm.at
    },
    { type: "wecom", token: "", at: "" }
  );
  assert.equal(vm.rows[0].at, "13900000000");
  assert.equal(vm.dirty, true);
});

test("页面：测试按钮没填地址用已保存的，填了用填的；IM 报错和 429 分别提示", async t => {
  const { mount, calls, messages, hooks } = await harness(t);
  const vm = await mount();
  await vm.testGlobal();
  assert.deepEqual(calls.test.at(-1), {
    env: "",
    type: "wecom",
    token: "",
    at: "zhangsan"
  });
  assert.deepEqual(vm.globalTest.result, { type: "success", text: "发送成功" });
  assert.equal(vm.globalTest.testing, false);

  vm.rows[0].token = " new-token ";
  hooks.test = async () => ({
    success: true,
    data: { ok: false, message: "errcode 310000: sign not match" }
  });
  await vm.testRow(vm.rows[0]);
  assert.deepEqual(calls.test.at(-1), {
    env: "prod-k8s",
    type: "dingding",
    token: "new-token",
    at: "13800000000"
  });
  assert.deepEqual(vm.rows[0].result, {
    type: "error",
    text: "发送失败：errcode 310000: sign not match"
  });
  assert.deepEqual(messages.at(-1), [
    "发送失败，原因见页面上的测试结果",
    "warning"
  ]);

  hooks.test = async () => {
    throw { response: { status: 429, data: { success: false, message: "x" } } };
  };
  await vm.testGlobal();
  assert.deepEqual(messages.at(-1), ["操作太频繁，请稍后再试", "warning"]);
  assert.deepEqual(vm.globalTest.result, {
    type: "warning",
    text: "操作太频繁，请稍后再试"
  });

  // 换了 IM 类型又没填地址:不发请求,提示重新填写
  const count = calls.test.length;
  vm.globalForm.type = "feishu";
  await vm.testGlobal();
  assert.equal(calls.test.length, count);
  assert.deepEqual(messages.at(-1), [
    "全局配置换了 IM 类型，请重新填写机器人地址",
    "warning"
  ]);
});

test("页面：没有全局配置时留空保存，default 为 null", async t => {
  const { mount, calls } = await harness(
    t,
    settings({ default: null, clusters: [] })
  );
  const vm = await mount();
  assert.equal(vm.globalSaved, null);
  vm.globalForm.type = "slack";
  assert.equal(vm.dirty, false);
  await vm.save();
  assert.deepEqual(calls.save, [{ default: null, clusters: [] }]);
});
