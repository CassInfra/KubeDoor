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
const options = (extra = {}) => ({
  env: "production",
  aliases: [],
  namespace: "kubedoor",
  master_url: "https://kubedoor.example.com",
  mode: "agent",
  image_repo: "registry.example.com/kubedoor",
  agent_tag: "2.3.0",
  oss_url: "",
  ksm: "auto",
  node_exporter: "auto",
  remote_write_url: "",
  ...extra
});
const task = (extra = {}) => ({
  id: "installation-1",
  status: "draft",
  revision: 1,
  context: "production",
  contexts: [{ name: "production", server: "https://k8s.example.com" }],
  options: options(),
  ...extra
});

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

async function harness(t, overrides = {}) {
  const oldStorage = global.localStorage;
  const saved = new Map();
  global.localStorage = {
    getItem: key => saved.get(key) || null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key)
  };
  const calls = { start: 0, inspect: 0 };
  const api = {
    defaults: async () => ({ ...options(), metrics_available: true }),
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
  const component = compile(
    "../src/views/workbench/AgentInstallDialog.vue",
    {
      "@/api/agent-install": { agentInstallApi: api },
      "@/store/modules/user": {
        useUserStoreHook: () => ({ username: "alice" })
      },
      "./install-utils": utils,
      "element-plus": { ElInputTag: {} }
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
  });
  await flush();
  return { vm, api, calls, saved };
}

test("每个安装参数和 context 的变更都会使已检测的计划失效", () => {
  const before = utils.installFingerprint("production", options());
  for (const [key, value] of Object.entries({
    env: "other",
    aliases: ["prod"],
    namespace: "other",
    master_url: "https://other.example.com",
    mode: "full",
    image_repo: "other/repo",
    image_pull_secret: "registry-key",
    agent_tag: "new",
    oss_url: "https://objects.example.com",
    ksm: "reuse",
    node_exporter: "install",
    remote_write_url: "https://metrics.example.com/write"
  }))
    assert.notEqual(
      before,
      utils.installFingerprint("production", options({ [key]: value })),
      key
    );
  assert.notEqual(before, utils.installFingerprint("staging", options()));
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
