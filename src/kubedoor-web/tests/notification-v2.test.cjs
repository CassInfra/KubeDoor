const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");
const { parse, compileScript } = require("@vue/compiler-sfc");
const { createRenderer, h, nextTick, reactive } = require("vue");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

function compile(filename, overrides = {}, vue = false) {
  const target = path.resolve(__dirname, filename);
  let source = fs.readFileSync(target, "utf8");
  if (vue) {
    const { descriptor } = parse(source, { filename: target });
    source = compileScript(descriptor, {
      id: "notification-v2-test",
      ...(typeof vue === "object" ? vue : {})
    }).content;
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

test("FALLBACK_PROVIDERS 覆盖 M1 全部 6 种通知通道且包含完整字段元数据", () => {
  const ids = utils.FALLBACK_PROVIDERS.map(item => item.id);
  assert.deepEqual(ids, [
    "wecom",
    "feishu",
    "dingding",
    "slack",
    "webhook",
    "smtp"
  ]);
  for (const provider of utils.FALLBACK_PROVIDERS) {
    assert.ok(provider.label);
    assert.ok(provider.description);
    assert.ok(provider.default_rate_limit);
    assert.equal(typeof provider.supports_at, "boolean");
    assert.equal(typeof provider.supports_markdown, "boolean");
    assert.ok(Array.isArray(provider.fields) && provider.fields.length > 0);
  }
  assert.equal(
    utils.formatRateLimit({ max_requests: 100, window_seconds: 60, burst: 5 }),
    "100 次 / 60s（突发 5/s）"
  );
  assert.equal(
    utils.formatRateLimit({ max_requests: 20, window_seconds: 60 }),
    "20 次 / 60s"
  );
});

test("detectProviderFromUrl 识别企微/飞书/Lark/钉钉/Slack 并拒绝伪造域名和 userinfo", () => {
  assert.equal(
    utils.detectProviderFromUrl(
      " https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc "
    ),
    "wecom"
  );
  assert.equal(
    utils.detectProviderFromUrl(
      "https://open.feishu.cn/open-apis/bot/v2/hook/hook-123"
    ),
    "feishu"
  );
  assert.equal(
    utils.detectProviderFromUrl(
      "https://open.larksuite.com/open-apis/bot/v2/hook/hook-456"
    ),
    "feishu"
  );
  assert.equal(
    utils.detectProviderFromUrl(
      "https://OAPI.DINGTALK.COM/robot/send?access_token=xyz"
    ),
    "dingding"
  );
  assert.equal(
    utils.detectProviderFromUrl(
      "https://hooks.slack.com/services/T1/B2/secret"
    ),
    "slack"
  );
  assert.equal(
    utils.detectFeishuPresetFromUrl(
      "https://open.larksuite.com/open-apis/bot/v2/hook/x"
    ),
    "lark"
  );
  assert.equal(
    utils.detectFeishuPresetFromUrl(
      "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    ),
    "feishu"
  );
  // 安全边界：伪造后缀、userinfo、纯 token 和非内置域名都返回空
  assert.equal(
    utils.detectProviderFromUrl("https://open.feishu.cn.evil.com/hook/x"),
    ""
  );
  assert.equal(
    utils.detectProviderFromUrl("https://user@open.larksuite.com/hook/x"),
    ""
  );
  assert.equal(utils.detectProviderFromUrl("plain-token-only"), "");
  assert.equal(utils.detectProviderFromUrl("https://hooks.example.com/a"), "");
  // v1 webhookType 仍保持原有语义（open.larksuite.com 返回 ""）
  assert.equal(
    utils.webhookType("https://open.larksuite.com/open-apis/bot/v2/hook/x"),
    ""
  );
});

test("buildChannelPayloadV2 支持密文留空沿用、切换类型重填校验及多字段类型验证", () => {
  const savedFeishu = {
    id: "ch_feishu_prod",
    name: "生产飞书群",
    provider: "feishu",
    enabled: true,
    settings: { domain_preset: "lark", at: "all" },
    secret_hints: { token: "****9f3c", sign_secret: "****7a11" },
    configured: true
  };
  const form = utils.channelFormFromSavedV2(savedFeishu);
  assert.equal(form.secrets.token, "");
  assert.equal(form.secrets.sign_secret, "");
  assert.equal(
    utils.secretPlaceholderV2(
      utils.findProviderSchema("feishu").fields.find(f => f.key === "token"),
      form
    ),
    "已配置（****9f3c），留空保留"
  );

  // 未换类型且留空 -> 沿用已保存密文
  const reused = utils.buildChannelPayloadV2(form);
  assert.deepEqual(reused, {
    id: "ch_feishu_prod",
    name: "生产飞书群",
    provider: "feishu",
    enabled: true,
    settings: { domain_preset: "lark", at: "all" },
    secrets: { token: "", sign_secret: "" }
  });

  // 切换通道类型后留空 -> 提示重新填写，placeholder 也变更
  const switched = utils.switchChannelProviderV2(form, "dingding");
  assert.equal(
    utils.secretPlaceholderV2(
      utils.findProviderSchema("dingding").fields.find(f => f.key === "token"),
      switched
    ),
    "换了通道类型，请重新填写机器人地址 / Token"
  );
  assert.throws(
    () => utils.buildChannelPayloadV2(switched),
    /切换了类型，请重新填写机器人地址/
  );

  // 填写与类型冲突的 Webhook 地址 -> 拦截
  switched.secrets.token = "https://open.larksuite.com/open-apis/bot/v2/hook/x";
  assert.throws(
    () => utils.buildChannelPayloadV2(switched),
    /属于飞书 \/ Lark，但当前类型选择了钉钉/
  );

  // 填写正确地址 + 可选加签密钥 -> 通过
  switched.secrets.token =
    "https://oapi.dingtalk.com/robot/send?access_token=ding123";
  switched.secrets.sign_secret = "SECabc123";
  const builtDing = utils.buildChannelPayloadV2(switched);
  assert.equal(builtDing.provider, "dingding");
  assert.equal(builtDing.secrets.sign_secret, "SECabc123");

  // Webhook 协议与自定义请求头校验
  const webhookForm = utils.defaultChannelFormV2("webhook", undefined, {
    id: "ch_hook",
    name: "SOAR 回调",
    settings: {
      url: "http://hooks.example.com/alert",
      allow_http: false,
      headers_json: '{"X-Env": "prod"}'
    }
  });
  assert.throws(
    () => utils.buildChannelPayloadV2(webhookForm),
    /必须使用 https:\/\//
  );
  webhookForm.settings.allow_http = true;
  const builtHook = utils.buildChannelPayloadV2(webhookForm);
  assert.deepEqual(builtHook.settings.headers_json, { "X-Env": "prod" });

  // SMTP 端口整数校验
  const smtpForm = utils.defaultChannelFormV2("smtp", undefined, {
    id: "ch_smtp",
    name: "SRE 邮箱",
    settings: {
      host: "smtp.example.com",
      port: 70000,
      security: "ssl",
      from_addr: "kb@example.com",
      to_addrs: "sre@example.com"
    }
  });
  assert.throws(() => utils.buildChannelPayloadV2(smtpForm), /不能大于 65535/);
  smtpForm.settings.port = 465;
  assert.equal(utils.buildChannelPayloadV2(smtpForm).settings.port, 465);

  // 通道 ID 与路由 ID 禁止冒号 ':'，与后端 _ID_PATTERN 对齐；CATEGORY_OPTIONS 含 test；smtp.to_addrs 为 text
  const badIdForm = utils.defaultChannelFormV2("wecom", undefined, {
    id: "ch:bad",
    name: "非法冒号",
    secrets: { token: "key-1" }
  });
  assert.throws(() => utils.buildChannelPayloadV2(badIdForm), /通道 ID.*不合规/);
  const smtpSchema = utils.findProviderSchema("smtp");
  assert.equal(smtpSchema.fields.find(f => f.key === "to_addrs")?.kind, "text");
  assert.ok(utils.CATEGORY_OPTIONS.some(c => c.value === "test"));
});

test("buildConfigV2Payload 校验通道去重、路由多路扇出及默认兜底引用", () => {
  const ch1 = utils.defaultChannelFormV2("wecom", undefined, {
    id: "ch_wecom",
    name: "企微群",
    secrets: { token: "key-1" }
  });
  const ch2 = utils.defaultChannelFormV2("smtp", undefined, {
    id: "ch_mail",
    name: "值班邮箱",
    settings: {
      host: "smtp.example.com",
      port: 465,
      security: "ssl",
      from_addr: "kb@example.com",
      to_addrs: "sre@example.com"
    }
  });
  const rt1 = {
    id: "rt_crit",
    name: "生产严重告警扇出",
    enabled: true,
    envs: ["prod-k8s", "prod-k8s"],
    categories: ["metric_alert", "k8s_event"],
    severities: ["critical"],
    channel_ids: ["ch_wecom", "ch_mail", "ch_wecom"],
    continue: true
  };
  const payload = utils.buildConfigV2Payload(
    [ch1, ch2],
    [rt1],
    ["ch_wecom", "ch_wecom"]
  );
  assert.equal(payload.version, 2);
  assert.deepEqual(payload.default_channel_ids, ["ch_wecom"]);
  assert.deepEqual(payload.routes[0].match, {
    envs: ["prod-k8s"],
    categories: ["metric_alert", "k8s_event"],
    severities: ["critical"]
  });
  assert.deepEqual(payload.routes[0].channel_ids, ["ch_wecom", "ch_mail"]);

  assert.match(
    utils.routeSummary(rt1, { ch_wecom: "企微群", ch_mail: "值班邮箱" }),
    /集群: prod-k8s.*指标告警.*企微群、值班邮箱（继续后续规则）/
  );

  // 通道 ID 与路由 ID 禁止冒号 ':'
  assert.throws(
    () => utils.buildConfigV2Payload([ch1], [{ ...rt1, id: "rt:bad" }], []),
    /路由规则 ID.*格式不合规/
  );

  // 路由未选目标通道 -> 阻止提交
  assert.throws(
    () =>
      utils.buildConfigV2Payload(
        [ch1],
        [{ ...rt1, channel_ids: [] }],
        ["ch_wecom"]
      ),
    /请至少选择一个目标通道/
  );
  // 路由引用不存在的通道 -> 阻止提交
  assert.throws(
    () =>
      utils.buildConfigV2Payload(
        [ch1],
        [{ ...rt1, channel_ids: ["missing"] }],
        ["ch_wecom"]
      ),
    /引用了不存在的通道：missing/
  );
  // 默认兜底引用不存在的通道 -> 阻止提交
  assert.throws(
    () => utils.buildConfigV2Payload([ch1], [], ["missing"]),
    /默认兜底通道引用了不存在的通道/
  );
  // 通道 ID 重复 -> 阻止提交
  assert.throws(
    () => utils.buildConfigV2Payload([ch1, { ...ch1 }], [], []),
    /通道 ID「ch_wecom」重复了/
  );
});

test("v2 API 客户端路径与参数符合后端约定", async () => {
  const requests = [];
  const api = compile("../src/api/notification.ts", {
    "@/utils/http": {
      http: {
        request: async (...args) => (
          requests.push(args),
          { success: true, data: {} }
        )
      }
    }
  });
  await api.getNotificationProviders(true);
  await api.getNotificationConfigV2();
  await api.saveNotificationConfigV2({
    version: 2,
    channels: [],
    routes: [],
    default_channel_ids: []
  });
  await api.testNotificationV2({
    channel_id: "ch_1",
    provider: "feishu",
    settings: {},
    secrets: { token: "" }
  });
  await api.getNotificationDeliveries({ env: "prod-k8s", ok: "false" });

  assert.deepEqual(requests[0], [
    "get",
    "/api/admin/notification/providers",
    undefined,
    { silent: true }
  ]);
  assert.deepEqual(requests[1], [
    "get",
    "/api/admin/notification/v2",
    undefined,
    undefined
  ]);
  assert.equal(requests[2][0], "put");
  assert.equal(requests[2][1], "/api/admin/notification/v2");
  assert.equal(requests[3][0], "post");
  assert.equal(requests[3][1], "/api/admin/notification/test");
  assert.deepEqual(requests[3][3], { silent: true, timeout: 30000 });
  assert.deepEqual(requests[4].slice(0, 3), [
    "get",
    "/api/admin/notification/deliveries",
    { params: { env: "prod-k8s", ok: "false" } }
  ]);
});

test("DynamicChannelFields.vue 脚本编译、可见字段过滤与 Lark 自动域名识别", async t => {
  const component = compile(
    "../src/views/system/notification/DynamicChannelFields.vue",
    {
      "./notification-utils": utils
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
  const feishuSchema = utils.findProviderSchema("feishu");
  const form = reactive(
    utils.defaultChannelFormV2("feishu", undefined, {
      id: "ch_lark",
      name: "Lark 群"
    })
  );
  const container = {};
  const vnode = h(
    { ...component, render: () => null },
    {
      form,
      provider: feishuSchema,
      providers: utils.FALLBACK_PROVIDERS,
      testing: false
    }
  );
  renderer.render(vnode, container);
  t.after(() => renderer.render(null, container));
  await nextTick();

  const vm = vnode.component.setupState;
  // 默认 domain_preset = feishu 时，custom_domain 隐藏
  assert.equal(
    vm.visibleFields.some(f => f.key === "custom_domain"),
    false
  );
  // 切换为 custom 后显示 custom_domain
  form.settings.domain_preset = "custom";
  await nextTick();
  assert.equal(
    vm.visibleFields.some(f => f.key === "custom_domain"),
    true
  );
  // 粘贴 open.larksuite.com 地址自动切换 domain_preset 为 lark
  vm.handleSecretInput(
    "token",
    "https://open.larksuite.com/open-apis/bot/v2/hook/abc"
  );
  assert.equal(form.settings.domain_preset, "lark");
  assert.equal(vm.urlMismatchHint, "");
  assert.equal(vm.rateLimitText, "100 次 / 60s（突发 5/s）");
});

test("index.vue 在 v2 后端下完整挂载：通道增删改测试、多路扇出路由、投递审计日志与 503 保护", async t => {
  const oldWindow = global.window;
  global.window = { addEventListener() {}, removeEventListener() {} };
  const calls = { save: [], test: [], deliveries: [] };
  const messages = [];
  const hooks = {
    loadV2: async () => ({
      success: true,
      data: {
        version: 2,
        channels: [
          {
            id: "ch_default",
            name: "全局企微",
            provider: "wecom",
            enabled: true,
            settings: { domain_preset: "public", at: "zhangsan" },
            secret_hints: { token: "****a1b2" },
            configured: true
          }
        ],
        routes: [
          {
            id: "rt_prod",
            name: "生产规则",
            enabled: true,
            match: { envs: ["prod-k8s"], categories: [], severities: [] },
            channel_ids: ["ch_default"],
            continue: false
          }
        ],
        default_channel_ids: ["ch_default"],
        envs: ["prod-k8s", "staging-k8s"],
        updated_by: "admin",
        updated_at: "2026-11-27T12:00:00+08:00"
      }
    })
  };
  const api = {
    getNotificationSettings: async () => ({ success: true, data: {} }),
    saveNotificationSettings: async () => ({ success: true, data: {} }),
    testNotification: async () => ({
      success: true,
      data: { ok: true, message: "发送成功" }
    }),
    getNotificationProviders: async () => ({
      success: true,
      data: { providers: utils.FALLBACK_PROVIDERS }
    }),
    getNotificationConfigV2: () => hooks.loadV2(),
    saveNotificationConfigV2: async payload => {
      calls.save.push(payload);
      return {
        success: true,
        data: {
          ...payload,
          channels: payload.channels.map(ch => ({
            ...ch,
            secret_hints: { token: "****save" },
            configured: true
          })),
          envs: ["prod-k8s", "staging-k8s"]
        }
      };
    },
    testNotificationV2: async payload => {
      calls.test.push(payload);
      return { success: true, data: { ok: true, message: "发送成功" } };
    },
    getNotificationDeliveries: async params => {
      calls.deliveries.push(params);
      return {
        success: true,
        data: {
          items: [
            {
              id: 1,
              env: "prod-k8s",
              category: "metric_alert",
              severity: "critical",
              channel_id: "ch_default",
              channel_name: "全局企微",
              provider: "wecom",
              ok: true,
              attempts: 1,
              http_status: 200,
              latency_ms: 82,
              created_at: "2026-11-27T12:05:00+08:00"
            }
          ]
        }
      };
    }
  };
  const component = compile(
    "../src/views/system/notification/index.vue",
    {
      "@/api/notification": api,
      "@/utils/message": {
        message: (text, options) => messages.push([text, options?.type])
      },
      "element-plus": { ElMessageBox: { confirm: async () => {} } },
      "./notification-utils": utils,
      "./NotifyChannelFields.vue": { default: {} },
      "./DynamicChannelFields.vue": { default: {} }
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
  const settle = async () => {
    for (let i = 0; i < 3; i++) {
      await nextTick();
      await new Promise(resolve => setImmediate(resolve));
    }
  };
  t.after(() => {
    renderer.render(null, container);
    global.window = oldWindow;
  });

  const vnode = h({ ...component, render: () => null });
  renderer.render(vnode, container);
  await settle();
  const vm = vnode.component.setupState;

  assert.equal(vm.v2Mode, true);
  assert.equal(vm.loaded, true);
  assert.equal(vm.dirty, false);
  assert.equal(vm.channelsV2.length, 1);

  // 新增一个自定义 Webhook 通道并测试、保存
  vm.openNewChannel("webhook");
  vm.editingForm.id = "ch_soar";
  vm.editingForm.name = "SOAR Webhook";
  vm.editingForm.settings.url = "https://soar.example.com/hook";
  vm.confirmEditChannel();
  assert.equal(vm.channelsV2.length, 2);
  assert.equal(vm.dirty, true);

  await vm.quickTestChannel(vm.channelsV2[0]);
  assert.equal(calls.test.at(-1).channel_id, "ch_default");
  assert.equal(calls.test.at(-1).provider, "wecom");

  vm.routesV2[0].channel_ids.push("ch_soar");
  await vm.save();
  assert.equal(calls.save.length, 1);
  assert.deepEqual(calls.save[0].routes[0].channel_ids, [
    "ch_default",
    "ch_soar"
  ]);
  assert.equal(vm.dirty, false);

  // 切换到审计日志 Tab 并加载
  vm.handleTabChange("deliveries");
  await settle();
  assert.equal(calls.deliveries.length, 1);
  assert.equal(vm.deliveries.length, 1);
  assert.equal(vm.deliveries[0].ok, true);

  // 503 读取失败时不覆盖保存
  hooks.loadV2 = async () => {
    throw { response: { status: 503, data: { message: "配置服务不可用" } } };
  };
  const errVnode = h({ ...component, render: () => null });
  renderer.render(errVnode, {});
  await settle();
  const errVm = errVnode.component.setupState;
  assert.equal(errVm.loaded, false);
  assert.equal(errVm.loadError, "配置服务不可用");
  await errVm.save();
  assert.equal(calls.save.length, 1);
});
