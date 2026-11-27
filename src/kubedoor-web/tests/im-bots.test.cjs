const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { createRenderer, h, nextTick } = require("vue");
const { parse, compileScript } = require("vue/compiler-sfc");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

function compile(filename, overrides = {}, vue = false) {
  const target = path.resolve(__dirname, filename);
  let source = fs.readFileSync(target, "utf8");
  if (vue) {
    const { descriptor } = parse(source, { filename: target });
    source = compileScript(descriptor, {
      id: `im-bots-${path.basename(filename)}`
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

const aiUtils = compile("../src/utils/ai.ts");
const permUtils = compile("../src/utils/permission.ts");
const imUtils = compile("../src/views/system/wecom-ai/im-bots-utils.ts");
const aiApiModule = compile("../src/api/ai.ts", { "@/utils/ai": aiUtils });

const flush = async () => {
  for (let i = 0; i < 4; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
};

function mountComponent(t, component) {
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
  t.after(() => renderer.render(null, container));
  renderer.render(vnode, container);
  return async () => {
    await flush();
    return vnode.component.setupState;
  };
}

test("im-bots-utils 暴露准确的平台、群策略与飞书域名选项", () => {
  assert.deepEqual(imUtils.PLATFORM_LABELS, {
    wecom: "企业微信",
    feishu: "飞书 / Lark"
  });
  assert.deepEqual(
    imUtils.GROUP_POLICY_OPTIONS.map(item => item.value),
    ["disabled", "mention", "allowlist"]
  );
  assert.deepEqual(
    imUtils.FEISHU_DOMAIN_OPTIONS.map(item => [item.value, item.domain]),
    [
      ["feishu", "https://open.feishu.cn"],
      ["lark", "https://open.larksuite.com"],
      ["custom", ""]
    ]
  );
  assert.equal(imUtils.platformLabel("feishu"), "飞书 / Lark");
  assert.equal(imUtils.platformLabel("wecom"), "企业微信");
  assert.equal(imUtils.feishuDomainLabel("lark"), "open.larksuite.com");
  assert.equal(
    imUtils.feishuDomainLabel("custom", "https://feishu.internal"),
    "https://feishu.internal"
  );
  assert.deepEqual(
    imUtils.botStatusInfo({
      enabled: true,
      configured: true,
      connected: true,
      authenticated: true,
      reconnecting: false
    }),
    { label: "已连接", type: "success" }
  );
  assert.deepEqual(
    imUtils.botStatusInfo({
      enabled: false,
      configured: true,
      connected: false,
      authenticated: false,
      reconnecting: false
    }),
    { label: "已停用", type: "info" }
  );
});

test("parseAllowedChatIds 支持换行、中英文逗号、去空白和保序去重", () => {
  assert.deepEqual(
    imUtils.parseAllowedChatIds(" oc_1, oc_2\noc_3，oc_1 \r\n , "),
    ["oc_1", "oc_2", "oc_3"]
  );
  assert.deepEqual(imUtils.parseAllowedChatIds("  \n "), []);
});

test("公共默认模型校验与请求体构造：保留空 API Key 留空不覆盖、裁剪尾部斜杠", () => {
  const valid = {
    base_url: " https://api.deepseek.com/v1/ ",
    api_key: "",
    model: " deepseek-chat ",
    max_input_tokens: 1048576,
    max_output_tokens: 393216,
    compression_ratio: 0.5,
    reasoning_effort: "low"
  };
  assert.equal(imUtils.validateSharedModelForm(valid), "");
  assert.deepEqual(imUtils.buildSharedModelPayload(valid), {
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    max_input_tokens: 1048576,
    max_output_tokens: 393216,
    compression_ratio: 0.5,
    reasoning_effort: "low"
  });
  assert.deepEqual(
    imUtils.buildSharedModelPayload({ ...valid, api_key: " sk-test " }),
    {
      base_url: "https://api.deepseek.com/v1",
      api_key: "sk-test",
      model: "deepseek-chat",
      max_input_tokens: 1048576,
      max_output_tokens: 393216,
      compression_ratio: 0.5,
      reasoning_effort: "low"
    }
  );
  assert.match(
    imUtils.validateSharedModelForm({ ...valid, base_url: "ftp://bad" }),
    /HTTP\/HTTPS/
  );
  assert.match(
    imUtils.validateSharedModelForm({
      ...valid,
      base_url: "https://u:p@example.com/v1"
    }),
    /HTTP\/HTTPS/
  );
  assert.match(
    imUtils.validateSharedModelForm({ ...valid, model: "  " }),
    /模型名/
  );
  assert.match(
    imUtils.validateSharedModelForm({ ...valid, compression_ratio: 0.9 }),
    /20% 到 80%/
  );
});

test("botFormFromSaved / validateBotForm / buildBotPayload 覆盖企微、飞书私有化域名、群白名单与可选模型覆盖", () => {
  const feishuSaved = {
    id: "bot-fs-1",
    platform: "feishu",
    name: "SRE 飞书机器人",
    enabled: true,
    configured: true,
    connected: true,
    authenticated: true,
    reconnecting: false,
    credentials: {
      app_id: "cli_abc123",
      domain_preset: "custom",
      custom_domain: "https://open.feishu.internal"
    },
    secret_set: true,
    session_idle_minutes: 90,
    group_policy: "allowlist",
    allowed_chat_ids: ["oc_group_1", "oc_group_2"],
    unbound_username: "ops-guest",
    unbound_default_env: "staging",
    model_override: {
      base_url: "https://llm.internal/v1",
      model: "deepseek-reasoner",
      max_input_tokens: 131072,
      max_output_tokens: 32768,
      compression_ratio: 0.4,
      reasoning_effort: "max",
      api_key_set: true
    }
  };
  const form = imUtils.botFormFromSaved(feishuSaved);
  assert.equal(form.app_id, "cli_abc123");
  assert.equal(form.app_secret, "");
  assert.equal(form.secret_set, true);
  assert.equal(form.domain_preset, "custom");
  assert.equal(form.allowed_chat_ids_text, "oc_group_1\noc_group_2");
  assert.equal(form.model_override.enabled, true);
  assert.equal(form.model_override.api_key_set, true);
  assert.equal(imUtils.validateBotForm(form), "");

  // 飞书私有化域名必须为 HTTPS
  assert.match(
    imUtils.validateBotForm({
      ...form,
      custom_domain: "http://open.feishu.internal"
    }),
    /HTTPS/
  );
  assert.match(
    imUtils.validateBotForm({
      ...form,
      custom_domain: "https://open.feishu.internal?token=1"
    }),
    /HTTPS/
  );
  // 新建时未保存 Secret 必填；已保存 secret_set 时留空通过
  assert.match(
    imUtils.validateBotForm({
      ...imUtils.defaultBotForm("feishu"),
      name: "new-fs",
      app_id: "cli_1"
    }),
    /App Secret/
  );
  assert.match(
    imUtils.validateBotForm({
      ...imUtils.defaultBotForm("wecom"),
      name: "new-wc",
      bot_id: "wc_1"
    }),
    /Bot Secret/
  );
  // allowlist 模式需要群 ID
  assert.match(
    imUtils.validateBotForm({
      ...form,
      allowed_chat_ids_text: "  "
    }),
    /Chat ID/
  );
  // model_override 校验
  assert.match(
    imUtils.validateBotForm({
      ...form,
      model_override: { ...form.model_override, model: "" }
    }),
    /独立模型名/
  );

  // buildBotPayload 不带空 secret 与空 api_key，保留其他字段
  const payload = imUtils.buildBotPayload({
    ...form,
    custom_domain: "https://open.feishu.internal/"
  });
  assert.deepEqual(payload, {
    platform: "feishu",
    name: "SRE 飞书机器人",
    enabled: true,
    credentials: {
      app_id: "cli_abc123",
      domain_preset: "custom",
      custom_domain: "https://open.feishu.internal"
    },
    session_idle_minutes: 90,
    group_policy: "allowlist",
    allowed_chat_ids: ["oc_group_1", "oc_group_2"],
    unbound_username: "ops-guest",
    unbound_default_env: "staging",
    model_override: {
      base_url: "https://llm.internal/v1",
      model: "deepseek-reasoner",
      max_input_tokens: 131072,
      max_output_tokens: 32768,
      compression_ratio: 0.4,
      reasoning_effort: "max"
    }
  });

  // 关闭覆盖模型且清空未绑定默认集群
  const disabledOverride = imUtils.buildBotPayload({
    ...form,
    domain_preset: "lark",
    unbound_username: "",
    unbound_default_env: "staging",
    model_override: { ...form.model_override, enabled: false }
  });
  assert.equal(disabledOverride.credentials.custom_domain, undefined);
  assert.equal(disabledOverride.unbound_username, null);
  assert.equal(disabledOverride.unbound_default_env, null);
  assert.equal(disabledOverride.model_override, null);
});

test("src/api/ai.ts 保留全部 wecom* 方法并准确发送 /shared-model 与 /im/* 请求", async t => {
  const prevFetch = global.fetch;
  const prevWindow = global.window;
  global.window = { setTimeout, clearTimeout };
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({
      url,
      method: options.method,
      body: options.body ? JSON.parse(options.body) : undefined
    });
    return new Response(JSON.stringify({ ok: true, bots: [], users: [] }), {
      status: 200
    });
  };
  t.after(() => {
    global.fetch = prevFetch;
    global.window = prevWindow;
  });

  const api = aiApiModule.aiApi;
  // 确认原有 wecom* 接口全部保留
  for (const fn of [
    "wecomConfig",
    "saveWecomConfig",
    "testWecomProvider",
    "wecomUsers",
    "saveWecomUser",
    "saveWecomUnbound",
    "deleteWecomUser",
    "purgeWecomUser",
    "saveWecomRemark",
    "wecomBinding",
    "createWecomBindCode",
    "unbindWecom"
  ]) {
    assert.equal(typeof api[fn], "function", `${fn} must stay intact`);
  }

  await api.sharedModelConfig();
  await api.saveSharedModel({
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o",
    max_input_tokens: null,
    max_output_tokens: null,
    compression_ratio: null,
    reasoning_effort: null
  });
  await api.testSharedModel({
    base_url: "https://api.openai.com/v1",
    api_key: "",
    model: "gpt-4o"
  });
  await api.imBots();
  await api.createIMBot({
    platform: "feishu",
    name: "fs",
    enabled: true,
    credentials: {
      app_id: "cli_1",
      app_secret: "sec",
      domain_preset: "feishu"
    },
    session_idle_minutes: 120,
    group_policy: "mention",
    allowed_chat_ids: []
  });
  await api.updateIMBot("bot/1", {
    platform: "wecom",
    name: "wc",
    enabled: false,
    credentials: { bot_id: "b1" },
    session_idle_minutes: 60,
    group_policy: "disabled",
    allowed_chat_ids: []
  });
  await api.deleteIMBot("bot/1");
  await api.saveIMBotUnbound("bot/1", "guest", "prod");
  await api.imUsers("bot 1");
  await api.saveIMUser({
    bot_id: "bot-1",
    external_user_id: "ou_123",
    username: "alice",
    default_env: "prod"
  });
  await api.deleteIMUser("bot-1", "ou/123");
  await api.purgeIMUser("bot-1", "ou/123");
  await api.saveIMUserRemark("bot-1", "ou/123", "张三");
  await api.imBinding();
  await api.createIMBindCode();
  await api.unbindSelfIM("bot-1", "ou/123");

  assert.deepEqual(
    calls.map(item => [item.method, item.url]),
    [
      ["GET", "/api/ai/shared-model"],
      ["PUT", "/api/ai/shared-model"],
      ["POST", "/api/ai/shared-model/test"],
      ["GET", "/api/ai/im/bots"],
      ["POST", "/api/ai/im/bots"],
      ["PUT", "/api/ai/im/bots/bot%2F1"],
      ["DELETE", "/api/ai/im/bots/bot%2F1"],
      ["PUT", "/api/ai/im/bots/bot%2F1/unbound"],
      ["GET", "/api/ai/im/users?bot_id=bot%201"],
      ["POST", "/api/ai/im/users"],
      ["DELETE", "/api/ai/im/users/bot-1/ou%2F123"],
      ["POST", "/api/ai/im/users/bot-1/ou%2F123/purge"],
      ["PUT", "/api/ai/im/users/bot-1/ou%2F123/remark"],
      ["GET", "/api/ai/im/binding"],
      ["POST", "/api/ai/im/binding/code"],
      ["DELETE", "/api/ai/im/binding/bot-1/ou%2F123"]
    ]
  );
  assert.deepEqual(calls[7].body, { username: "guest", default_env: "prod" });
  assert.deepEqual(calls[12].body, { remark: "张三" });
});

test("WecomBindDialog.vue 支持多平台 IM 绑定与 404 回退旧版企微接口", async t => {
  const { EventEmitter } = require("node:events");
  const bus = new EventEmitter();
  const emitter = {
    on: (ev, fn) => bus.on(ev, fn),
    off: (ev, fn) => bus.off(ev, fn),
    emit: (ev, val) => bus.emit(ev, val)
  };
  const messages = [];
  const calls = {
    imBinding: 0,
    wecomBinding: 0,
    imCode: 0,
    wecomCode: 0,
    unbindSelfIM: [],
    unbindWecom: []
  };
  let legacyMode = false;
  const mockApi = {
    imBinding: async () => {
      calls.imBinding += 1;
      if (legacyMode) throw new aiApiModule.AIRequestError("not found", 404);
      return {
        enabled: true,
        connected: true,
        platforms: ["wecom", "feishu"],
        bots: [
          {
            id: "b1",
            name: "飞书 SRE",
            platform: "feishu",
            enabled: true,
            connected: true
          }
        ],
        members: [
          {
            bot_id: "b1",
            bot_name: "飞书 SRE",
            platform: "feishu",
            external_user_id: "ou_fs_1",
            self_name: "李四",
            bound_by: "alice",
            bound_at: "2026-11-27T10:00:00Z",
            last_seen_at: "2026-11-27T10:05:00Z"
          }
        ],
        code_minutes: 10
      };
    },
    wecomBinding: async () => {
      calls.wecomBinding += 1;
      return {
        enabled: true,
        connected: true,
        members: [
          {
            wecom_userid: "wx_1",
            self_name: "王五",
            bound_by: "alice",
            bound_at: "2026-11-27T09:00:00Z",
            last_seen_at: "2026-11-27T09:10:00Z"
          }
        ],
        code_minutes: 10
      };
    },
    createIMBindCode: async () => {
      calls.imCode += 1;
      if (legacyMode) throw new aiApiModule.AIRequestError("not found", 404);
      return { code: "654321", expires_at: "", minutes: 10 };
    },
    createWecomBindCode: async () => {
      calls.wecomCode += 1;
      return { code: "112233", expires_at: "", minutes: 10 };
    },
    unbindSelfIM: async (botId, userid) => {
      calls.unbindSelfIM.push([botId, userid]);
      return { success: true };
    },
    unbindWecom: async userid => {
      calls.unbindWecom.push(userid);
      return { success: true };
    }
  };

  const Dialog = compile(
    "../src/components/WecomBindDialog.vue",
    {
      "@/api/ai": {
        aiApi: mockApi,
        AIRequestError: aiApiModule.AIRequestError
      },
      "@/utils/mitt": { emitter },
      "@pureadmin/utils": { copyTextToClipboard: () => true },
      "element-plus": {
        ElMessage: {
          success: text => messages.push(["success", text]),
          warning: text => messages.push(["warning", text]),
          error: text => messages.push(["error", text])
        },
        ElMessageBox: { confirm: async () => "confirm" }
      }
    },
    true
  ).default;

  const getVm = mountComponent(t, Dialog);
  const vm = await getVm();
  emitter.emit("openWecomBind");
  await flush();
  assert.equal(vm.visible, true);
  assert.equal(calls.imBinding, 1);
  assert.equal(vm.binding.members[0].platform, "feishu");
  assert.equal(vm.supportedPlatformText, "企业微信或飞书 / Lark");

  await vm.generate();
  assert.equal(calls.imCode, 1);
  assert.equal(vm.code, "654321");
  assert.equal(vm.command, "绑定 654321");
  vm.stopTimer();

  await vm.unbind("b1", "ou_fs_1");
  assert.deepEqual(calls.unbindSelfIM, [["b1", "ou_fs_1"]]);

  // 旧版后端只有 /wecom/binding 时自动 404 回退
  legacyMode = true;
  await vm.load();
  assert.equal(calls.wecomBinding, 1);
  assert.equal(vm.binding.members[0].bot_id, "legacy-wecom");
  assert.equal(vm.binding.members[0].external_user_id, "wx_1");
  await vm.generate();
  assert.equal(calls.wecomCode, 1);
  assert.equal(vm.code, "112233");
  vm.stopTimer();
  await vm.unbind("legacy-wecom", "wx_1");
  assert.deepEqual(calls.unbindWecom, ["wx_1"]);
});

test("views/system/wecom-ai/index.vue 完整驱动公共模型、多机器人管理与成员绑定", async t => {
  const messages = [];
  const state = {
    shared: {
      available: true,
      configured: true,
      base_url: "https://api.deepseek.com",
      model: "deepseek-chat",
      max_input_tokens: 1048576,
      max_output_tokens: 393216,
      compression_ratio: 0.5,
      reasoning_effort: "low",
      api_key_set: true
    },
    bots: [
      {
        id: "bot-wecom-1",
        platform: "wecom",
        name: "默认企微机器人",
        enabled: true,
        configured: true,
        connected: true,
        authenticated: true,
        reconnecting: false,
        credentials: { bot_id: "wc_bot" },
        secret_set: true,
        session_idle_minutes: 120,
        group_policy: "disabled",
        allowed_chat_ids: [],
        model_override: null,
        unbound_username: "guest",
        unbound_default_env: "prod"
      }
    ],
    users: [
      {
        bot_id: "bot-wecom-1",
        bot_name: "默认企微机器人",
        platform: "wecom",
        external_user_id: "wc_u1",
        username: null,
        default_env: null,
        current_env: null,
        remark: null,
        self_name: "张三",
        bound_by: null,
        bound_at: null,
        first_seen_at: "2026-11-27T08:00:00Z",
        last_seen_at: "2026-11-27T08:10:00Z",
        pending_approvals: 0
      }
    ]
  };
  const calls = {
    saveShared: [],
    testShared: [],
    createBot: [],
    updateBot: [],
    deleteBot: [],
    saveUnbound: [],
    saveUser: [],
    remark: [],
    purge: [],
    deleteUser: []
  };

  const mockApi = {
    sharedModelConfig: async () => state.shared,
    saveSharedModel: async payload => {
      calls.saveShared.push(payload);
      state.shared = { ...state.shared, ...payload, api_key_set: true };
      return state.shared;
    },
    testSharedModel: async provider => {
      calls.testShared.push(provider);
      return { success: true, tool_calling: true };
    },
    imBots: async () => ({ bots: state.bots }),
    createIMBot: async payload => {
      calls.createBot.push(payload);
      const created = {
        id: "bot-feishu-2",
        ...payload,
        configured: true,
        connected: false,
        authenticated: false,
        reconnecting: false,
        secret_set: true
      };
      state.bots.push(created);
      return created;
    },
    updateIMBot: async (id, payload) => {
      calls.updateBot.push([id, payload]);
      const idx = state.bots.findIndex(item => item.id === id);
      state.bots[idx] = { ...state.bots[idx], ...payload };
      return state.bots[idx];
    },
    deleteIMBot: async id => {
      calls.deleteBot.push(id);
      state.bots = state.bots.filter(item => item.id !== id);
      return { success: true };
    },
    saveIMBotUnbound: async (id, username, env) => {
      calls.saveUnbound.push([id, username, env]);
      return { unbound_username: username, unbound_default_env: env };
    },
    imUsers: async botId => ({
      users: botId
        ? state.users.filter(item => item.bot_id === botId)
        : state.users
    }),
    saveIMUser: async payload => {
      calls.saveUser.push(payload);
      return { success: true };
    },
    saveIMUserRemark: async (botId, uid, remark) => {
      calls.remark.push([botId, uid, remark]);
      return { remark };
    },
    purgeIMUser: async (botId, uid) => {
      calls.purge.push([botId, uid]);
      return { success: true, sessions: 1, stopped: 0 };
    },
    deleteIMUser: async (botId, uid) => {
      calls.deleteUser.push([botId, uid]);
      return { success: true };
    }
  };

  const Page = compile(
    "../src/views/system/wecom-ai/index.vue",
    {
      "@/api/ai": { aiApi: mockApi },
      "@/api/system": {
        getUsers: async () => ({
          success: true,
          data: [
            {
              id: 1,
              username: "alice",
              role: "rw",
              enabled: true,
              clusters: { prod: "rw" }
            },
            {
              id: 2,
              username: "guest",
              role: "read",
              enabled: true,
              clusters: { prod: "read" }
            }
          ]
        }),
        getKnownClusters: async () => ({
          success: true,
          data: [{ env: "prod", online: true }]
        })
      },
      "@/components/AI/AIModelLimitsFields.vue": { default: {} },
      "@/components/AI/AIReasoningEffortField.vue": { default: {} },
      "@/utils/ai": aiUtils,
      "@/utils/permission": permUtils,
      "./im-bots-utils": imUtils,
      "element-plus": {
        ElMessage: {
          success: text => messages.push(["success", text]),
          warning: text => messages.push(["warning", text]),
          error: text => messages.push(["error", text])
        },
        ElMessageBox: {
          confirm: async () => "confirm",
          prompt: async () => ({ value: "张三（SRE）" })
        }
      }
    },
    true
  ).default;

  const getVm = mountComponent(t, Page);
  const vm = await getVm();
  assert.equal(vm.activeTab, "shared-model");
  assert.equal(vm.sharedForm.model, "deepseek-chat");
  assert.equal(vm.bots.length, 1);

  await vm.testSharedModel();
  assert.equal(calls.testShared.length, 1);
  vm.sharedForm.model = "gpt-5.5";
  await vm.saveSharedModel();
  assert.equal(calls.saveShared[0].model, "gpt-5.5");

  // 新增飞书机器人（含私有化域名与白名单）
  vm.openCreateBot("feishu");
  vm.botForm.name = "飞书私有机器人";
  vm.botForm.app_id = "cli_fs_2";
  vm.botForm.app_secret = "sec_fs_2";
  vm.botForm.domain_preset = "custom";
  vm.botForm.custom_domain = "https://feishu.corp.local";
  vm.botForm.group_policy = "allowlist";
  vm.botForm.allowed_chat_ids_text = "oc_1\noc_2";
  await vm.saveBot();
  assert.equal(calls.createBot.length, 1);
  assert.equal(calls.createBot[0].platform, "feishu");
  assert.deepEqual(calls.createBot[0].allowed_chat_ids, ["oc_1", "oc_2"]);

  // 停用机器人 & 更新未绑定权限
  await vm.toggleBotEnabled(vm.bots[0], false);
  assert.equal(calls.updateBot[0][0], "bot-wecom-1");
  assert.equal(calls.updateBot[0][1].enabled, false);
  vm.openEditBot(vm.bots[0]);
  vm.botForm.unbound_username = "guest";
  vm.botForm.unbound_default_env = "prod";
  await vm.saveBotUnboundOnly();
  assert.deepEqual(calls.saveUnbound[0], ["bot-wecom-1", "guest", "prod"]);

  // 成员绑定管理
  vm.inspectBotUsers(vm.bots[0]);
  assert.equal(vm.activeTab, "users");
  assert.equal(vm.selectedBotFilter, "bot-wecom-1");
  await vm.editRemark(vm.users[0]);
  assert.deepEqual(calls.remark[0], ["bot-wecom-1", "wc_u1", "张三（SRE）"]);
  vm.editMapping(vm.users[0]);
  vm.mapping.username = "alice";
  vm.mapping.default_env = "prod";
  await vm.saveMapping();
  assert.deepEqual(calls.saveUser[0], {
    bot_id: "bot-wecom-1",
    external_user_id: "wc_u1",
    username: "alice",
    default_env: "prod"
  });
  await vm.purgeMapping(vm.users[0]);
  assert.deepEqual(calls.purge[0], ["bot-wecom-1", "wc_u1"]);
  await vm.removeMapping(vm.users[0]);
  assert.deepEqual(calls.deleteUser[0], ["bot-wecom-1", "wc_u1"]);
  await vm.removeBot(vm.bots[1]);
  assert.deepEqual(calls.deleteBot, ["bot-feishu-2"]);
});

test("lay-user/index.vue 显示「绑定 IM 账号」并在 404 时回退旧版接口", async t => {
  let legacy = false;
  const mockApi = {
    imBinding: async () => {
      if (legacy) throw new aiApiModule.AIRequestError("not found", 404);
      return {
        enabled: true,
        connected: true,
        platforms: ["wecom", "feishu"],
        bots: [],
        members: [
          {
            bot_id: "b1",
            platform: "feishu",
            external_user_id: "ou_1",
            self_name: "张三",
            bound_by: "alice",
            bound_at: null,
            last_seen_at: ""
          }
        ],
        code_minutes: 10
      };
    },
    wecomBinding: async () => ({
      enabled: true,
      connected: true,
      members: [],
      code_minutes: 10
    })
  };
  const Menu = compile(
    "../src/layout/components/lay-user/index.vue",
    {
      "@/utils/mitt": { emitter: { emit() {} } },
      "@/layout/hooks/useNav": {
        useNav: () => ({ userAvatar: "", logout: () => {} })
      },
      "@/store/modules/user": {
        useUserStoreHook: () => ({
          username: "alice",
          nickname: "Alice",
          role: "admin",
          roleLabel: "系统管理员",
          source: "local",
          canChangePassword: true
        })
      },
      "@/api/ai": {
        aiApi: mockApi,
        AIRequestError: aiApiModule.AIRequestError
      },
      "@iconify-icons/ri/logout-circle-r-line": {},
      "@iconify-icons/ri/lock-password-line": {},
      "@iconify-icons/ri/chat-3-line": {}
    },
    true
  ).default;

  const vm1 = await mountComponent(t, Menu)();
  assert.equal(vm1.imBindable, true);
  assert.equal(vm1.imMenuLabel, "绑定 IM 账号（1）");

  legacy = true;
  const vm2 = await mountComponent(t, Menu)();
  assert.equal(vm2.imBindable, true);
  assert.equal(vm2.imMenuLabel, "绑定 IM 账号");
});
