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
  const instance = new Module(filename, module);
  instance.paths = Module._nodeModulePaths(path.dirname(filename));
  const original = instance.require.bind(instance);
  instance.require = id =>
    Object.hasOwn(overrides, id) ? overrides[id] : original(id);
  instance._compile(
    transformSync(source, { loader: "ts", format: "cjs", target: "es2020" })
      .code,
    filename
  );
  return instance.exports;
}
const utilsPath = path.resolve(__dirname, "../src/utils/ai.ts");
const utils = compile(fs.readFileSync(utilsPath, "utf8"), utilsPath);
const apiPath = path.resolve(__dirname, "../src/api/ai.ts");
const realApi = compile(fs.readFileSync(apiPath, "utf8"), apiPath, {
  "@/utils/ai": utils
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
async function flush() {
  await nextTick();
  await new Promise(resolve => setImmediate(resolve));
  await nextTick();
}
const metadata = (id, version = 1) => ({
  id,
  title: `记忆 ${id}`,
  version,
  created_by: "alice",
  updated_by: "alice",
  created_at: "2026-10-05T10:00:00Z",
  updated_at: "2026-10-05T10:00:00Z"
});
const memory = (id, version = 1) => ({
  ...metadata(id, version),
  content: `# ${id}\n\n| 服务 | 状态 |\n| --- | --- |\n| web | 正常 |`
});

function componentHarness(t, filename, overrides = {}, api = {}) {
  const state = reactive({
    visible: false,
    selected: [],
    writable: true,
    busy: false,
    owner: "alice",
    secret: "private-key",
    draft: { title: "测试草稿", content: "# 内容" },
    ...overrides
  });
  const componentPath = path.resolve(
    __dirname,
    "../src/components/AI",
    filename
  );
  const descriptor = parse(fs.readFileSync(componentPath, "utf8")).descriptor;
  const script = compileScript(descriptor, {
    id: `memory-${filename}`
  }).content;
  const component = compile(script, componentPath, {
    "@/api/ai": { aiApi: api, AIRequestError: realApi.AIRequestError },
    "element-plus": { ElMessageBox: { confirm: async () => "confirm" } },
    "./AIMarkdown.vue": { render: () => null },
    "./AIMemoryEditor.vue": { render: () => null }
  }).default;
  component.render = () => null;
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
  const events = { selected: [], saved: [], deleted: [] };
  const app = renderer.createApp({
    render: () =>
      h(component, {
        ...state,
        modelValue: state.visible,
        "onUpdate:modelValue": value => {
          state.visible = value;
        },
        onSelect: value => events.selected.push(value),
        onSaved: value => events.saved.push(value),
        onDeleted: id => events.deleted.push(id)
      })
  });
  app.mount(root);
  const view = root._vnode.component.subTree.component.setupState;
  t.after(() => app.unmount());
  return { state, view, events };
}

test("memory manager stays unloaded until opened and retains selections across pages and search", async t => {
  const calls = [];
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    {},
    {
      listMemories: (params, signal) => {
        const task = deferred();
        calls.push({ params, signal, ...task });
        return task.promise;
      }
    }
  );
  assert.equal(calls.length, 0);
  h.state.visible = true;
  await flush();
  assert.deepEqual(calls[0].params, { search: "", page: 1, page_size: 20 });
  calls[0].resolve({ memories: [metadata("a")], total: 21 });
  await flush();
  h.view.toggle(h.view.rows[0], true);
  h.view.changePage(2);
  await flush();
  calls[1].resolve({ memories: [metadata("b")], total: 21 });
  await flush();
  h.view.toggle(h.view.rows[0], true);
  h.view.search = "  CPU  ";
  h.view.searchMemories();
  await flush();
  assert.deepEqual(calls[2].params, { search: "CPU", page: 1, page_size: 20 });
  calls[2].resolve({ memories: [], total: 0 });
  await flush();
  assert.deepEqual(
    h.view.selectedItems.map(row => row.id),
    ["a", "b"]
  );
  h.view.applySelection();
  await flush();
  assert.deepEqual(
    h.events.selected[0].map(row => row.id),
    ["a", "b"]
  );
  assert.equal(h.state.visible, false);
});

test("rapid list changes and closing abort previous reads and ignore their late results", async t => {
  const calls = [];
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true },
    {
      listMemories: (params, signal) => {
        const task = deferred();
        calls.push({ params, signal, ...task });
        return task.promise;
      }
    }
  );
  h.view.changePage(2);
  await flush();
  assert.equal(calls[0].signal.aborted, true);
  calls[1].resolve({ memories: [metadata("page-2")], total: 40 });
  await flush();
  calls[0].resolve({ memories: [metadata("page-1")], total: 40 });
  await flush();
  assert.equal(h.view.rows[0].id, "page-2");
  h.view.changePage(3);
  await flush();
  h.state.visible = false;
  await flush();
  assert.equal(calls[2].signal.aborted, true);
  calls[2].reject(new Error("private-key obsolete error"));
  await flush();
  assert.equal(h.view.rows.length, 0);
  assert.equal(h.view.error, "");
});

test("memory bodies are fetched only on click and stale detail reads cannot replace the current record", async t => {
  const details = [];
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true },
    {
      listMemories: async () => ({
        memories: [metadata("a"), metadata("b")],
        total: 2
      }),
      memory: (id, signal) => {
        const task = deferred();
        details.push({ id, signal, ...task });
        return task.promise;
      }
    }
  );
  await flush();
  assert.equal(details.length, 0);
  const a = h.view.readMemory(metadata("a"));
  const b = h.view.readMemory(metadata("b"));
  assert.equal(details[0].signal.aborted, true);
  details[1].resolve(memory("b", 3));
  await b;
  details[0].resolve(memory("a"));
  await a;
  assert.equal(h.view.detail.id, "b");
  assert.equal(h.view.detail.version, 3);
  await h.view.readMemory(metadata("b"));
  assert.equal(h.view.detail, null);
  assert.equal(details.length, 2);
});

test("busy conversations can browse but cannot change or apply selected memories", async t => {
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true, busy: true, selected: [metadata("a")] },
    {
      listMemories: async () => ({ memories: [metadata("b")], total: 1 }),
      memory: async id => memory(id)
    }
  );
  await flush();
  h.view.toggle(metadata("b"), true);
  h.view.toggle(metadata("a"), false);
  h.view.applySelection();
  assert.deepEqual(
    h.view.selectedItems.map(row => row.id),
    ["a"]
  );
  assert.equal(h.events.selected.length, 0);
  await h.view.readMemory(metadata("b"));
  assert.equal(h.view.detail.id, "b");
});

test("selection is limited to ten records and read-only users cannot mutate global memory", async t => {
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true, writable: false },
    {
      listMemories: async () => ({ memories: [], total: 0 }),
      deleteMemory: () => {
        throw new Error("must never mutate");
      }
    }
  );
  await flush();
  for (let i = 0; i < 11; i++) h.view.toggle(metadata(String(i)), true);
  assert.equal(h.view.selectedItems.length, 10);
  assert.match(h.view.error, /10/);
  h.view.create();
  await h.view.remove(metadata("0"));
  assert.equal(h.view.editorVisible, false);
  h.view.applySelection();
  assert.equal(h.events.selected[0].length, 10);
});

test("delete uses the observed version and a conflict preserves selection", async t => {
  const deleted = [];
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true, selected: [metadata("a", 2)] },
    {
      listMemories: async () => ({ memories: [metadata("a", 2)], total: 1 }),
      deleteMemory: async (id, version) => {
        deleted.push({ id, version });
        throw new realApi.AIRequestError("changed", 409, "memory_conflict");
      }
    }
  );
  await flush();
  await h.view.remove(metadata("a", 2));
  assert.deepEqual(deleted, [{ id: "a", version: 2 }]);
  assert.equal(h.view.selectedItems[0].id, "a");
  assert.match(h.view.error, /已更新/);
  assert.equal(h.events.deleted.length, 0);
});

test("owner changes close the manager and suppress the previous identity's pending detail", async t => {
  const pending = deferred();
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true },
    {
      listMemories: async () => ({ memories: [], total: 0 }),
      memory: () => pending.promise
    }
  );
  const detail = h.view.readMemory(metadata("a"));
  h.state.owner = "bob";
  await flush();
  pending.resolve(memory("a"));
  await detail;
  assert.equal(h.state.visible, false);
  assert.equal(h.view.detail, null);
});

test("deleting a record cancels its pending detail and prevents a late edit window", async t => {
  const pending = deferred();
  let signal;
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true, selected: [metadata("a")] },
    {
      listMemories: async () => ({ memories: [metadata("a")], total: 1 }),
      memory: (_id, readSignal) => {
        signal = readSignal;
        return pending.promise;
      },
      deleteMemory: async () => ({ success: true })
    }
  );
  await flush();
  const editing = h.view.readMemory(metadata("a"), true);
  await h.view.remove(metadata("a"));
  assert.equal(signal.aborted, true);
  assert.equal(h.view.detailLoading, false);
  assert.equal(h.view.detailId, "");
  assert.equal(h.view.selectedItems.length, 0);
  pending.resolve(memory("a"));
  await editing;
  assert.equal(h.view.editorVisible, false);
  assert.equal(h.view.detail, null);
  assert.deepEqual(h.events.deleted, ["a"]);
});

test("a late delete result cannot change a reopened manager", async t => {
  const pending = deferred();
  const h = componentHarness(
    t,
    "AIMemoryDialog.vue",
    { visible: true, selected: [metadata("a")] },
    {
      listMemories: async () => ({ memories: [metadata("a")], total: 1 }),
      deleteMemory: () => pending.promise
    }
  );
  await flush();
  const removing = h.view.remove(metadata("a"));
  await flush();
  assert.equal(h.view.deleting, "a");
  h.state.visible = false;
  await flush();
  h.state.visible = true;
  await flush();
  pending.resolve({ success: true });
  await removing;
  assert.equal(h.view.selectedItems[0].id, "a");
  assert.equal(h.events.deleted.length, 0);
  assert.equal(h.view.error, "");
});

test("editable summary drafts never write automatically and read-only users may edit but cannot save", async t => {
  const writes = [];
  const h = componentHarness(
    t,
    "AIMemoryEditor.vue",
    {
      visible: true,
      writable: false,
      draft: { title: "总结 private-key", content: "## private-key" }
    },
    {
      createMemory: async draft => {
        writes.push(draft);
        return memory("saved");
      }
    }
  );
  await flush();
  assert.equal(writes.length, 0);
  assert.doesNotMatch(h.view.form.content, /private-key/);
  h.view.form.title = "只读编辑";
  h.view.form.content = "新的草稿";
  await h.view.save();
  assert.equal(writes.length, 0);
  h.state.writable = true;
  await flush();
  await h.view.save();
  assert.deepEqual(writes[0], { title: "只读编辑", content: "新的草稿" });
  assert.equal(h.events.saved.length, 1);
  assert.equal(h.state.visible, false);
});

test("optimistic edit conflict preserves edits, and explicit reload uses the latest version", async t => {
  const updates = [];
  const h = componentHarness(
    t,
    "AIMemoryEditor.vue",
    { visible: true, draft: memory("a", 1) },
    {
      updateMemory: async (id, draft) => {
        updates.push({ id, draft });
        if (updates.length === 1)
          throw new realApi.AIRequestError("conflict", 409, "memory_conflict");
        return { ...memory(id, 3), ...draft };
      },
      memory: async id => memory(id, 2)
    }
  );
  h.view.form.content = "my unsaved edit";
  await h.view.save();
  assert.equal(h.view.conflict, true);
  assert.equal(h.view.form.content, "my unsaved edit");
  assert.equal(updates[0].draft.version, 1);
  await h.view.reload();
  assert.equal(h.view.version, 2);
  assert.equal(h.view.conflict, false);
  h.view.form.content = "resolved edit";
  await h.view.save();
  assert.equal(updates[1].draft.version, 2);
  assert.equal(updates[1].draft.content, "resolved edit");
});

test("closing an editor suppresses a late save error and masks API keys in current errors", async t => {
  const pending = deferred();
  const h = componentHarness(
    t,
    "AIMemoryEditor.vue",
    { visible: true },
    { createMemory: () => pending.promise }
  );
  const saving = h.view.save();
  h.state.visible = false;
  await flush();
  pending.reject(new Error("private-key late failure"));
  await saving;
  assert.equal(h.view.error, "");
  assert.equal(h.events.saved.length, 0);
  h.state.visible = true;
  await flush();
  await h.view.save();
  assert.doesNotMatch(h.view.error, /private-key/);
  assert.match(h.view.error, /已隐藏/);
});

test("permission loss while saving suppresses the late success notification", async t => {
  const pending = deferred();
  const h = componentHarness(
    t,
    "AIMemoryEditor.vue",
    { visible: true },
    { createMemory: () => pending.promise }
  );
  const saving = h.view.save();
  h.state.writable = false;
  await flush();
  pending.resolve(memory("saved"));
  await saving;
  assert.equal(h.events.saved.length, 0);
  assert.equal(h.state.visible, true);
  assert.equal(h.view.saving, false);
});

test("actual memory HTTP contracts preserve versions, memory IDs, message snapshots and timeout limits", async t => {
  const oldFetch = global.fetch,
    oldWindow = global.window;
  const requests = [],
    deadlines = [];
  global.window = {
    setTimeout(callback, delay) {
      deadlines.push(delay);
      return 1;
    },
    clearTimeout() {}
  };
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    const body = url.includes("/runs")
      ? {
          run_id: "run",
          status: "running",
          message: {
            id: "m",
            role: "user",
            content: "check",
            memories: [
              { id: "a", title: "snapshot", content: "old version", version: 1 }
            ]
          }
        }
      : url.includes("memory-summary")
        ? { title: "draft", content: "draft only" }
        : url.includes("?") && options.method === "GET"
          ? { memories: [metadata("a")], total: 1, page: 2, page_size: 20 }
          : memory("a", 2);
    return new Response(JSON.stringify(body), { status: 200 });
  };
  t.after(() => {
    global.fetch = oldFetch;
    global.window = oldWindow;
  });
  const api = realApi.aiApi;
  await api.listMemories({ search: "CPU & 内存", page: 2, page_size: 20 });
  await api.memory("a/b");
  await api.createMemory({ title: "title", content: "body" });
  await api.updateMemory("a", {
    title: "edited",
    content: "new body",
    version: 2
  });
  await api.deleteMemory("a", 2);
  await api.summarizeMemory("session", {
    base_url: "https://model",
    model: "test",
    api_key: "fake"
  });
  const run = await api.run(
    "session",
    {
      message: "check",
      scope: { env: "cluster-a" },
      provider: {},
      memory_ids: ["a"]
    },
    "run-key"
  );
  assert.deepEqual(
    Object.fromEntries(new URL(requests[0].url, "http://local").searchParams),
    { search: "CPU & 内存", page: "2", page_size: "20" }
  );
  assert.equal(requests[1].url, "/api/ai/memories/a%2Fb");
  assert.deepEqual(JSON.parse(requests[3].options.body), {
    title: "edited",
    content: "new body",
    version: 2
  });
  assert.equal(requests[4].url, "/api/ai/memories/a?version=2");
  assert.equal(requests[4].options.method, "DELETE");
  assert.equal(deadlines[5], 120000);
  assert.equal(deadlines[0], 60000);
  assert.deepEqual(JSON.parse(requests[6].options.body).memory_ids, ["a"]);
  assert.equal(run.message.memories[0].content, "old version");
});

test("history memory attachments use their immutable message snapshots and never fetch global records", async t => {
  const h = componentHarness(
    t,
    "AIMemoryAttachments.vue",
    {
      memories: [
        {
          id: "a",
          title: "old private-key title",
          content: "old private-key content",
          version: 1
        }
      ]
    },
    {
      memory() {
        throw new Error("must use attached snapshot");
      }
    }
  );
  assert.equal(h.view.expanded, false);
  h.view.toggle({ target: { open: true } });
  assert.equal(h.view.expanded, true);
  assert.equal(
    h.view.redact(h.state.memories[0].content),
    "old [已隐藏] content"
  );
  assert.equal(h.state.memories[0].version, 1);
});
