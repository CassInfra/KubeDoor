const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { createRenderer, h, reactive, nextTick } = require("vue");
const { parse, compileScript } = require("vue/compiler-sfc");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

function compile(source, filename, overrides = {}) {
  const loaded = new Module(filename, module);
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const originalRequire = loaded.require.bind(loaded);
  loaded.require = name => overrides[name] || originalRequire(name);
  loaded._compile(
    transformSync(source, { loader: "ts", target: "es2020", format: "cjs" })
      .code,
    filename
  );
  return loaded.exports;
}
const utilityPath = path.resolve(__dirname, "../src/utils/ai.ts");
const utils = compile(fs.readFileSync(utilityPath, "utf8"), utilityPath);
const componentPath = path.resolve(
  __dirname,
  "../src/components/AI/AIScopeSelector.vue"
);
const { descriptor } = parse(fs.readFileSync(componentPath, "utf8"));
const source = compileScript(descriptor, {
  id: "ai-scope-cascade-test"
}).content;

async function flush() {
  await nextTick();
  await new Promise(resolve => setImmediate(resolve));
  await nextTick();
}

function harness(t, initial = {}) {
  const calls = [];
  const state = reactive({
    scope: {
      env: "",
      namespace: null,
      deployment: null,
      pod: null,
      ...initial
    },
    clusters: [
      { env: "cluster-a", online: true },
      { env: "cluster-b", configured: true }
    ]
  });
  const selector = compile(source, componentPath, {
    "@/utils/ai": utils,
    "@/api/ai": {
      aiApi: {
        resources(kind, scope, signal) {
          let resolve, reject;
          const promise = new Promise((yes, no) => {
            resolve = yes;
            reject = no;
          });
          // Deliberately ignore abort: guards must also protect against late replies.
          calls.push({
            kind,
            scope,
            signal,
            resolve: items => resolve({ items }),
            reject
          });
          return promise;
        }
      }
    }
  }).default;
  selector.render = () => null;
  const renderer = createRenderer({
    insert() {},
    remove() {},
    patchProp() {},
    createElement: () => ({}),
    createText: () => ({}),
    createComment: () => ({}),
    setText() {},
    setElementText() {},
    parentNode: () => null,
    nextSibling: () => null
  });
  const root = {};
  const app = renderer.createApp({
    render: () =>
      h(selector, {
        modelValue: state.scope,
        clusters: state.clusters,
        "onUpdate:modelValue": scope => {
          state.scope = scope;
        }
      })
  });
  app.mount(root);
  const view = root._vnode.component.subTree.component.setupState;
  t.after(() => app.unmount());
  return { state, calls, view, app };
}

test("cluster selection fetches only namespaces, each child selection loads its next menu", async t => {
  const { state, view, calls } = harness(t);
  assert.equal(calls.length, 0);
  view.changeCluster("cluster-a");
  await flush();
  assert.deepEqual(
    calls.map(call => call.kind),
    ["namespaces"]
  );
  calls[0].resolve([{ name: "ops" }]);
  await flush();
  view.changeNamespace("ops");
  await flush();
  assert.deepEqual(
    calls.map(call => call.kind),
    ["namespaces", "deployments"]
  );
  assert.equal(calls[1].scope.namespace, "ops");
  calls[1].resolve([{ namespace: "ops", name: "exporter" }]);
  await flush();
  view.changeDeployment(
    utils.resourceKey({ namespace: "ops", name: "exporter" })
  );
  await flush();
  assert.deepEqual(
    calls.map(call => call.kind),
    ["namespaces", "deployments", "pods"]
  );
  assert.deepEqual(calls[2].scope.deployment, {
    namespace: "ops",
    name: "exporter"
  });
  calls[2].resolve([{ namespace: "ops", name: "exporter-1" }]);
  await flush();
  view.changePod(utils.resourceKey({ namespace: "ops", name: "exporter-1" }));
  await flush();
  assert.equal(calls.length, 3);
  assert.equal(state.scope.pod.name, "exporter-1");
  assert.deepEqual(view.namespaces, [{ name: "ops" }]);
  assert.deepEqual(view.deployments, [{ namespace: "ops", name: "exporter" }]);
});

test("All clears dependent choices without fetching unfiltered deployments or pods", async t => {
  const { state, view, calls } = harness(t, {
    env: "cluster-a",
    namespace: "ops",
    deployment: { namespace: "ops", name: "exporter" },
    pod: { namespace: "ops", name: "exporter-1" }
  });
  calls.forEach(call => call.resolve([{ name: "existing" }]));
  await flush();
  const count = calls.length;
  view.changeDeployment("");
  await flush();
  assert.equal(state.scope.deployment, null);
  assert.equal(state.scope.pod, null);
  assert.deepEqual(view.pods, []);
  assert.equal(calls.length, count);
  view.changeNamespace("");
  await flush();
  assert.equal(state.scope.namespace, null);
  assert.deepEqual(view.deployments, []);
  assert.equal(calls.length, count);
});

test("changing cluster aborts every old request and ignores replies from that cluster", async t => {
  const { state, view, calls } = harness(t, {
    env: "cluster-a",
    namespace: "ops",
    deployment: { namespace: "ops", name: "exporter" }
  });
  const old = [...calls];
  view.changeCluster("cluster-b");
  await flush();
  assert.equal(calls.length, 4);
  assert.equal(calls[3].kind, "namespaces");
  old.forEach(call => {
    assert.equal(call.signal.aborted, true);
    call.resolve([{ name: "wrong-cluster" }]);
  });
  calls[3].resolve([{ name: "cluster-b-ns" }]);
  await flush();
  assert.equal(state.scope.namespace, null);
  assert.deepEqual(view.namespaces, [{ name: "cluster-b-ns" }]);
  assert.deepEqual(view.deployments, []);
  assert.deepEqual(view.pods, []);
  assert.deepEqual(Object.values(view.loading), [false, false, false]);
});

test("rapid namespace changes cannot place old deployments into the new namespace", async t => {
  const { view, calls } = harness(t, { env: "cluster-a" });
  calls[0].resolve([{ name: "ops" }, { name: "apps" }]);
  await flush();
  view.changeNamespace("ops");
  await flush();
  const old = calls[1];
  view.changeNamespace("apps");
  await flush();
  assert.equal(old.signal.aborted, true);
  calls[2].resolve([{ namespace: "apps", name: "api" }]);
  old.resolve([{ namespace: "ops", name: "stale" }]);
  await flush();
  assert.deepEqual(view.deployments, [{ namespace: "apps", name: "api" }]);
  assert.deepEqual(
    calls.map(call => call.kind),
    ["namespaces", "deployments", "deployments"]
  );
});

test("changing Deployment only reloads pods and clears the prior Pod", async t => {
  const { state, view, calls } = harness(t, {
    env: "cluster-a",
    namespace: "ops"
  });
  calls.forEach(call => call.resolve([]));
  await flush();
  view.changeDeployment(utils.resourceKey({ namespace: "ops", name: "first" }));
  await flush();
  const old = calls[2];
  view.changePod(utils.resourceKey({ namespace: "ops", name: "first-1" }));
  await flush();
  view.changeDeployment(
    utils.resourceKey({ namespace: "ops", name: "second" })
  );
  await flush();
  assert.equal(state.scope.pod, null);
  assert.equal(old.signal.aborted, true);
  old.reject(new Error("late failure"));
  calls[3].resolve([{ namespace: "ops", name: "second-1" }]);
  await flush();
  assert.deepEqual(view.pods, [{ namespace: "ops", name: "second-1" }]);
  assert.equal(view.error, "");
  assert.deepEqual(
    calls.map(call => call.kind),
    ["namespaces", "deployments", "pods", "pods"]
  );
});

test("list errors and retries are local to the failed menu", async t => {
  const { view, calls } = harness(t, { env: "cluster-a", namespace: "ops" });
  calls[0].resolve([{ name: "ops" }]);
  calls[1].reject(new Error("deployment unavailable"));
  await flush();
  assert.equal(view.error, "deployment unavailable");
  assert.deepEqual(view.namespaces, [{ name: "ops" }]);
  view.retry();
  assert.equal(calls.length, 3);
  assert.equal(calls[2].kind, "deployments");
  calls[2].resolve([{ namespace: "ops", name: "exporter" }]);
  await flush();
  assert.equal(view.error, "");
});

test("unchanged bootstrap connectivity does not reload menus; offline state cancels requests", async t => {
  const { state, view, calls } = harness(t, { env: "cluster-a" });
  state.clusters = [
    { env: "cluster-a", online: true },
    { env: "cluster-b", configured: true }
  ];
  await flush();
  assert.equal(calls.length, 1);
  state.clusters = [{ env: "cluster-a", online: false, configured: false }];
  await flush();
  assert.equal(calls[0].signal.aborted, true);
  assert.deepEqual(view.namespaces, []);
  assert.equal(view.loading.namespaces, false);
});

test("unmount cancels in-flight resource requests", async t => {
  const { app, calls } = harness(t, { env: "cluster-a" });
  app.unmount();
  assert.equal(calls[0].signal.aborted, true);
  calls[0].resolve([{ name: "late" }]);
  await flush();
});
