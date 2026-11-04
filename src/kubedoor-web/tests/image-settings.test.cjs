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
    source = compileScript(descriptor, { id: "image-settings-test" }).content;
  }
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  const original = instance.require.bind(instance);
  instance.require = id =>
    Object.hasOwn(overrides, id) ? overrides[id] : original(id);
  instance._compile(
    transformSync(source, { loader: "ts", target: "es2018", format: "cjs" })
      .code,
    target
  );
  return instance.exports;
}

const utils = compile("../src/views/system/image-settings/settings-utils.ts");
const defaultRule = () => ({
  env: "default",
  enabled: false,
  start_time: "20:00",
  end_time: "08:00",
  users: []
});
const registry = (extra = {}) => ({
  id: "saved-id",
  domain: "registry.example.com",
  env: "default",
  ak: "",
  sk: "",
  has_ak: true,
  has_sk: true,
  ...extra
});

test("跨天规则保留时间并规范用户名，缺少用户或重复集群不能保存", () => {
  const rule = {
    env: "prod",
    enabled: true,
    start_time: "20:00",
    end_time: "08:00",
    users: [" Alice ", "alice"]
  };
  const payload = utils.rulesPayload([defaultRule(), rule]);
  assert.deepEqual(payload[1].users, ["alice"]);
  assert.equal(utils.timeSummary(rule), "20:00 至 08:00（次日）");
  assert.throws(
    () => utils.rulesPayload([defaultRule(), { ...rule, users: [] }]),
    /允许用户/
  );
  assert.throws(() => utils.rulesPayload([defaultRule(), rule, rule]), /重复/);
  assert.throws(
    () => utils.rulesPayload([defaultRule(), { ...rule, end_time: "20:00" }]),
    /不能相同/
  );
  assert.throws(() => utils.rulesPayload([rule]), /默认规则/);
});

test("24:00 仅可作为结束时间，保留全天和当天午夜截止的规则", () => {
  const allDay = {
    env: "prod",
    enabled: true,
    start_time: "00:00",
    end_time: "24:00",
    users: ["alice"]
  };
  assert.equal(
    utils.rulesPayload([defaultRule(), allDay])[1].end_time,
    "24:00"
  );
  assert.equal(utils.timeSummary(allDay), "全天（00:00 至 24:00）");
  assert.equal(
    utils.timeSummary({ ...allDay, start_time: "20:00" }),
    "20:00 至 24:00"
  );
  assert.throws(
    () =>
      utils.rulesPayload([defaultRule(), { ...allDay, start_time: "24:00" }]),
    /允许开始和结束时间/
  );
  assert.throws(
    () => utils.rulesPayload([defaultRule(), { ...allDay, end_time: "24:01" }]),
    /允许开始和结束时间/
  );
});

test("已有凭据留空时只提交记录标识，新凭据必须同时填写两项", () => {
  assert.deepEqual(utils.registriesPayload([registry()]), [
    {
      id: "saved-id",
      domain: "registry.example.com",
      env: "default"
    }
  ]);
  assert.deepEqual(utils.registriesPayload([registry({ ak: "new-user" })])[0], {
    id: "saved-id",
    domain: "registry.example.com",
    env: "default",
    ak: "new-user"
  });
  assert.throws(
    () =>
      utils.registriesPayload([
        registry({ id: "", has_ak: false, has_sk: false })
      ]),
    /Access Key/
  );
  assert.throws(
    () =>
      utils.registriesPayload([
        registry({ id: "", has_ak: false, has_sk: false, ak: "user" })
      ]),
    /Secret Key/
  );
});

test("仓库按域名和集群去重，协议、路径和带凭据的地址不能保存", () => {
  for (const domain of [
    "https://registry.example.com",
    "registry.example.com/project",
    "u:p@registry.example.com",
    "registry.example.com:99999"
  ])
    assert.throws(
      () => utils.registriesPayload([registry({ domain })]),
      /域名/
    );
  assert.throws(
    () =>
      utils.registriesPayload([
        registry(),
        registry({ domain: "REGISTRY.EXAMPLE.COM" })
      ]),
    /重复/
  );
  assert.equal(
    utils.registriesPayload([
      registry(),
      registry({ id: "other-id", env: "prod" })
    ]).length,
    2
  );
});

async function harness(t, overrides = {}) {
  const oldWindow = global.window;
  global.window = { addEventListener() {}, removeEventListener() {} };
  const calls = { rules: [], registries: [] };
  const api = {
    getImageUpdateRules: async () => ({
      success: true,
      data: { rules: [defaultRule()] }
    }),
    getImageRegistries: async () => ({
      success: true,
      data: { registries: [registry()] }
    }),
    saveImageUpdateRules: async rules => {
      calls.rules.push(rules);
      return { success: true, data: { rules } };
    },
    saveImageRegistries: async registries => {
      calls.registries.push(registries);
      return {
        success: true,
        data: {
          registries: registries.map(row => ({
            ...row,
            ak: undefined,
            sk: undefined,
            has_ak: true,
            has_sk: true
          }))
        }
      };
    },
    ...overrides
  };
  const component = compile(
    "../src/views/system/image-settings/index.vue",
    {
      "@/api/image-settings": api,
      "@/api/system": {
        getKnownClusters: async () => ({ success: true, data: [] }),
        getUsers: async () => ({ success: true, data: [] })
      },
      "./settings-utils": utils,
      "vue-router": { onBeforeRouteLeave() {} },
      "element-plus": {
        ElMessage: { warning() {}, error() {}, success() {}, info() {} },
        ElMessageBox: { confirm: async () => {} }
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
    global.window = oldWindow;
  });
  for (let i = 0; i < 3; i++) {
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
  }
  return { vm: vnode.component.setupState, calls };
}

test("加载失败后不能用空列表覆盖已有设置", async t => {
  const { vm, calls } = await harness(t, {
    getImageUpdateRules: async () => {
      throw new Error("unavailable");
    },
    getImageRegistries: async () => {
      throw new Error("unavailable");
    }
  });
  assert.equal(vm.rulesLoaded, false);
  assert.equal(vm.registriesLoaded, false);
  await vm.saveRules();
  await vm.saveRegistries();
  assert.deepEqual(calls, { rules: [], registries: [] });
});

test("保存一个页签不会丢失另一页签的未保存修改，保存后清空凭据输入", async t => {
  const { vm, calls } = await harness(t);
  vm.rules[0].users = ["alice"];
  vm.registries[0].sk = "new-secret";
  assert.equal(vm.rulesDirty, true);
  assert.equal(vm.registriesDirty, true);
  await vm.saveRegistries();
  assert.equal(calls.rules.length, 0);
  assert.equal(calls.registries[0][0].sk, "new-secret");
  assert.equal(vm.registries[0].sk, "");
  assert.equal(vm.registriesDirty, false);
  assert.equal(vm.rulesDirty, true);
  assert.deepEqual(vm.rules[0].users, ["alice"]);
});

test("读取旧 24:00 配置后可直接保存，选择全天后保存 00:00-24:00", async t => {
  const saved = {
    env: "default",
    enabled: true,
    start_time: "20:00",
    end_time: "24:00",
    users: ["alice"]
  };
  const { vm, calls } = await harness(t, {
    getImageUpdateRules: async () => ({
      success: true,
      data: { rules: [saved] }
    })
  });
  assert.equal(vm.rules[0].end_time, "24:00");
  assert.equal(vm.rulesDirty, false);
  await vm.saveRules();
  assert.equal(calls.rules[0][0].end_time, "24:00");
  vm.rules[0].end_time = "18:30";
  vm.toggleEndOfDay(vm.rules[0], true);
  assert.equal(vm.rules[0].end_time, "24:00");
  vm.toggleEndOfDay(vm.rules[0], false);
  assert.equal(vm.rules[0].end_time, "18:30");
  vm.setAllDay(vm.rules[0]);
  await vm.saveRules();
  assert.equal(calls.rules[1][0].start_time, "00:00");
  assert.equal(calls.rules[1][0].end_time, "24:00");
});
