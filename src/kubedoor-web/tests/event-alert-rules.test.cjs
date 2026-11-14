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
    source = compileScript(descriptor, {
      id: "event-alert-rules-test"
    }).content;
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

const utils = compile("../src/views/system/event-alert-rules/rules-utils.ts");
// 与 master 的严格校验共用的用例:错误信息必须逐字一致
const SHARED = JSON.parse(
  fs.readFileSync(
    path.resolve(
      __dirname,
      "../../kubedoor-master/tests/fixtures/event_rules_cases.json"
    ),
    "utf8"
  )
);
const DEFAULT_TEXT = fs.readFileSync(
  path.resolve(
    __dirname,
    "../../kubedoor-master/k8s_event/default_alert_rules.json"
  ),
  "utf8"
);
const DEFAULT_RULES = JSON.parse(DEFAULT_TEXT);
const clone = value => JSON.parse(JSON.stringify(value));

const rule = (name, conditions, extra = {}) => ({
  name,
  enabled: true,
  severity: "warning",
  conditions,
  ...extra
});
const doc = (alert = [], ignore = []) => ({
  global_ignore_rules: ignore,
  alert_rules: alert
});

// ---------------------------------------------------------------------------
// 严格校验
// ---------------------------------------------------------------------------
test("与 master 共用的校验用例逐字一致", () => {
  assert.ok(SHARED.cases.length >= 40);
  for (const item of SHARED.cases) {
    const { normalized, issues } = utils.checkRules(clone(item.rules));
    assert.deepEqual(issues, item.errors ?? [], item.name);
    assert.deepEqual(normalized, item.normalized ?? null, item.name);
  }
});

test("条数、值的个数和长度的上限与 master 一致", () => {
  const many = doc(
    Array.from({ length: utils.MAX_RULES + 1 }, (_, n) =>
      rule(`r${n}`, { k8s: { contains: ["x"] } })
    )
  );
  assert.deepEqual(utils.checkRules(many).issues, [
    { path: ["alert_rules"], message: "告警规则最多 200 条，现在有 201 条" }
  ]);
  const values = Array.from({ length: 201 }, (_, n) => `v${n}`);
  assert.equal(
    utils.checkRules(doc([rule("A", { reason: { contains: values } })]))
      .issues[0].message,
    "告警规则第 1 条「A」的条件 reason：contains 最多 200 个值"
  );
  // 按字符数(不是 UTF-16 码元数)算长度,和 Python 一样
  const emoji = "😀".repeat(1000);
  assert.deepEqual(
    utils.checkRules(doc([rule("A", { reason: { contains: [emoji] } })]))
      .issues,
    []
  );
  assert.deepEqual(
    utils.checkRules(doc([rule("长".repeat(100), { k8s: { equals: "x" } })]))
      .issues,
    []
  );
});

test("重复的键要求改掉，指出行号", () => {
  const text = `{
  "global_ignore_rules": [],
  "alert_rules": [
    {"name": "A", "conditions": {"reason": {"contains": ["x"]}, "reason": {"contains": ["y"]}}}
  ]
}`;
  const parsed = utils.parseJson(text);
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.duplicates, [
    { path: ["alert_rules", 0, "conditions", "reason"], key: "reason", line: 4 }
  ]);
  const issues = utils.validateRules(parsed.value, parsed.duplicates);
  assert.equal(issues.length, 1);
  assert.match(issues[0].message, /^第 4 行：键 "reason" 重复了/);
});

// ---------------------------------------------------------------------------
// 表单 ↔ JSON
// ---------------------------------------------------------------------------
test("出厂默认规则可以无损地在界面里编辑", () => {
  const { form, lost } = utils.rulesToForm(clone(DEFAULT_RULES));
  assert.deepEqual(lost, []);
  assert.equal(form.ignore.length, 2);
  assert.equal(form.alert.length, 7);
  assert.deepEqual(utils.formToRules(form), DEFAULT_RULES);
  assert.deepEqual(utils.validateRules(utils.formToRules(form)), []);
  const health = form.alert[2];
  // 条件按字段的固定顺序显示(name、reason、count)
  assert.deepEqual(
    health.conditions.map(item => [item.field, item.op]),
    [
      ["name", "not_starts_with"],
      ["reason", "contains"],
      ["count", "greater_than"]
    ]
  );
  assert.equal(health.conditions[2].num, 1);
});

test("界面表示不了的内容一条条列出来", () => {
  const legacy = {
    global_ignore_rules: [
      {
        name: "I",
        severity: "warning",
        enabled: "yes",
        conditions: { k8s: { contains: "test" } }
      },
      "x"
    ],
    alert_rules: [
      {
        name: 123,
        severity: "error",
        description: "说明",
        conditions: {
          Reason: { contains: ["a"] },
          message: ["oops"],
          kind: { contain: ["Pod"] },
          name: { not_contains: ["x"], contains: ["y"], equals: "z" },
          count: { greater_than: "2" },
          level: { equals: null },
          namespace: { starts_with: ["ok", null, 3, true] }
        }
      },
      { name: "没有条件" },
      { name: "条件是数组", conditions: [] }
    ],
    comment: "kept"
  };
  const { form, lost } = utils.rulesToForm(legacy);
  assert.deepEqual(
    lost.map(item => item.message),
    [
      "顶层的 comment 会被丢掉",
      "忽略规则第 1 条「I」的 severity 会被丢掉（忽略规则不用级别）",
      "忽略规则第 1 条「I」的 enabled 不是 true / false，按原来的效果改成了 true",
      "忽略规则第 2 条不是对象，会被丢掉",
      "告警规则第 1 条的 description 会被丢掉",
      "告警规则第 1 条的名称不是字符串，改成了「123」",
      '告警规则第 1 条的级别 "error" 不认识，改成了 warning',
      "告警规则第 1 条的条件字段 Reason 不认识，会被丢掉",
      '告警规则第 1 条的条件 message 不是 {"运算符": 值} 的形式，会被丢掉',
      "告警规则第 1 条的条件 kind 没有认识的运算符（原来视为成立），会被丢掉",
      "告警规则第 1 条的条件 name 只保留生效的 contains，丢掉 not_contains、equals",
      "告警规则第 1 条的条件 count：greater_than 的值「2」是字符串（原来这个条件永远不成立），改成了数字 2",
      "告警规则第 1 条的条件 level：equals 的值不是字符串或数字，会被丢掉",
      "告警规则第 1 条的条件 namespace：starts_with 的第 2 个值不是字符串，会被丢掉",
      "告警规则第 3 条「条件是数组」的条件 conditions 不是对象，会被丢掉"
    ]
  );
  assert.equal(form.ignore.length, 1);
  assert.equal(form.ignore[0].enabled, true);
  // 单个字符串等同于只有一个元素的数组,不算丢失
  assert.deepEqual(form.ignore[0].conditions[0].values, ["test"]);
  const first = form.alert[0];
  assert.equal(first.name, "123");
  assert.equal(first.severity, "warning");
  assert.deepEqual(
    first.conditions.map(item => item.field),
    ["namespace", "name", "count"]
  );
  // 数字、布尔按 Python 的 str() 转成字符串(以前匹配时就是这么比较的)
  assert.deepEqual(first.conditions[0].values, ["ok", "3", "True"]);
  assert.equal(form.alert[1].conditions.length, 0);
  // 转回 JSON 后交给校验:没有条件的规则要求补上
  assert.match(
    utils
      .validateRules(utils.formToRules(form))
      .map(item => item.message)
      .join("\n"),
    /告警规则第 2 条「没有条件」至少要有一个条件/
  );
  assert.deepEqual(utils.rulesToForm(null).form, { ignore: [], alert: [] });
  assert.equal(utils.rulesToForm([]).lost.length, 1);
  assert.deepEqual(
    utils.rulesToForm({ alert_rules: {} }).lost.map(item => item.message),
    ["alert_rules（告警规则）不是数组，会被当成没有规则"]
  );
});

test("Python 的真假判断还原 enabled 原来的效果", () => {
  const cases = [
    [0, false],
    ["", false],
    [[], false],
    [{}, false],
    [null, false],
    ["false", true],
    [[0], true],
    [1, true]
  ];
  for (const [value, expected] of cases) {
    const { form } = utils.rulesToForm(
      doc([rule("A", { k8s: { contains: ["x"] } }, { enabled: value })])
    );
    assert.equal(form.alert[0].enabled, expected, JSON.stringify(value));
  }
});

test("数值条件的值不是数字时保留这一行，要求重新填写", () => {
  const { form, lost } = utils.rulesToForm(
    doc([rule("A", { count: { less_than: "abc" } })])
  );
  assert.match(lost[0].message, /需要重新填写/);
  assert.equal(form.alert[0].conditions[0].num, null);
  const issues = utils.validateRules(utils.formToRules(form));
  assert.deepEqual(issues, [
    {
      path: ["alert_rules", 0, "conditions", "count", "less_than"],
      message: "告警规则第 1 条「A」的条件 count：less_than 的值必须是数字"
    }
  ]);
});

test("count 的等于写成数字，其它字段原样保存", () => {
  const form = { ignore: [], alert: [utils.newRule()] };
  const row = form.alert[0].conditions[0];
  form.alert[0].name = "  次数正好 3  ";
  utils.changeField(row, "count");
  assert.equal(row.op, "contains"); // count 也能用「包含」,不用换
  utils.changeOperator(row, "equals");
  row.text = "3";
  const second = utils.newCondition(["count"]);
  second.text = " Warning ";
  utils.changeField(second, "level");
  utils.changeOperator(second, "not_equals");
  second.text = "Normal";
  form.alert[0].conditions.push(second);
  assert.deepEqual(utils.formToRules(form), {
    global_ignore_rules: [],
    alert_rules: [
      {
        name: "次数正好 3",
        enabled: true,
        severity: "warning",
        conditions: { count: { equals: 3 }, level: { not_equals: "Normal" } }
      }
    ]
  });
});

test("换运算符时尽量带上已经填的值，换字段时只在必要时换运算符", () => {
  const row = utils.newCondition();
  assert.equal(row.field, "reason");
  assert.equal(row.op, "contains");
  row.values = ["OOMKilling", "Evicted"];
  utils.changeOperator(row, "equals");
  assert.equal(row.text, "OOMKilling");
  utils.changeOperator(row, "not_starts_with");
  assert.deepEqual(row.values, ["OOMKilling", "Evicted"]);
  utils.changeField(row, "message");
  assert.equal(row.op, "not_starts_with");
  utils.changeField(row, "count");
  assert.equal(row.op, "not_starts_with"); // count 也能用列表型运算符
  utils.changeOperator(row, "greater_equal");
  assert.equal(row.num, null); // "OOMKilling" 不是数字
  row.num = 5;
  utils.changeOperator(row, "equals");
  assert.equal(row.text, "OOMKilling"); // 原来填过的不覆盖
  utils.changeField(row, "name");
  assert.equal(row.op, "equals");
  utils.changeOperator(row, "greater_than");
  utils.changeField(row, "name");
  assert.equal(row.op, "contains"); // name 不能用数值运算符
  const fresh = utils.newCondition([]);
  fresh.text = "";
  utils.changeOperator(fresh, "less_than");
  fresh.num = 7;
  utils.changeOperator(fresh, "contains");
  assert.deepEqual(fresh.values, ["7"]);
  assert.deepEqual(
    utils.newCondition(["reason", "message", "k8s"]).field,
    "namespace"
  );
  assert.deepEqual(utils.cleanValues([" a ", "a", "", "  ", "b", null]), [
    "a",
    "b"
  ]);
});

test("下拉只列合法的组合", () => {
  assert.deepEqual(utils.operatorsFor("count").slice(0, 4), [
    "greater_than",
    "greater_equal",
    "less_than",
    "less_equal"
  ]);
  assert.equal(utils.operatorsFor("count").length, 12);
  assert.equal(utils.operatorsFor("reason").length, 8);
  assert.ok(!utils.operatorsFor("reason").includes("greater_than"));
  assert.equal(utils.FIELDS.length, 14);
  for (const field of utils.FIELDS) {
    assert.ok(utils.FIELD_LABELS[field], field);
    assert.ok(utils.FIELD_HELP[field], field);
  }
  for (const op of utils.OPERATORS) {
    assert.ok(utils.OPERATOR_LABELS[op], op);
    assert.ok(utils.OPERATOR_HELP[op], op);
  }
  assert.deepEqual(utils.valueSuggestions("k8s", ["prod", "test"]), [
    "prod",
    "test"
  ]);
  assert.deepEqual(utils.valueSuggestions("level", []), ["Warning", "Normal"]);
  assert.deepEqual(utils.valueSuggestions("message", []), []);
});

test("复制规则生成新的 key 和名称", () => {
  const { form } = utils.rulesToForm(clone(DEFAULT_RULES));
  const copy = utils.copyRule(form.alert[0]);
  assert.equal(copy.name, "关键事件告警（副本）");
  assert.notEqual(copy.key, form.alert[0].key);
  assert.notEqual(copy.conditions[0].key, form.alert[0].conditions[0].key);
  copy.conditions[0].values.push("x");
  assert.equal(form.alert[0].conditions[0].values.includes("x"), false);
});

test("问题按规则和条件字段归类", () => {
  const issue = { path: ["alert_rules", 2, "conditions", "count", "gt"] };
  assert.equal(utils.issueOwner(issue), "alert_rules:2");
  assert.equal(utils.issueField(issue), "count");
  assert.equal(utils.issueField({ path: ["alert_rules", 2, "name"] }), "");
  assert.equal(utils.issueOwner({ path: [] }), "");
  assert.equal(utils.issueOwner({ path: ["rules"] }), "");
});

// ---------------------------------------------------------------------------
// JSON 解析、标记、格式化
// ---------------------------------------------------------------------------
const syntax = text => {
  const parsed = utils.parseJson(text);
  assert.equal(parsed.ok, false, text);
  return [parsed.message, parsed.line, parsed.column];
};

test("语法错误给出中文原因和行列", () => {
  assert.deepEqual(syntax('{\n  "a": 1,\n}'), [
    "最后一项后面多了一个逗号（JSON 不允许）",
    2,
    9
  ]);
  // 缺逗号时标在上一项的末尾(要补逗号的地方)
  assert.deepEqual(syntax('{\n  "a": 1\n  "b": 2\n}'), [
    "上一项后面缺少逗号 ,",
    2,
    9
  ]);
  assert.deepEqual(syntax('{\n  // 注释\n  "a": 1\n}'), [
    "JSON 不能写注释（// 或 /* */）",
    2,
    3
  ]);
  assert.equal(
    syntax("{'a': 1}")[0],
    '字符串和键名要用英文双引号 "，不能用单引号'
  );
  assert.equal(syntax('{"a": “b”}')[0], '这里用了中文引号，要用英文双引号 "');
  assert.deepEqual(syntax('{\n  "a": "b\n}'), [
    '字符串没有结束（缺少右边的双引号 "）',
    2,
    8
  ]);
  assert.equal(syntax('{"a" 1}')[0], '键 "a" 后面缺少冒号 :');
  assert.equal(syntax('{"a"：1}')[0], "这里用了中文冒号，要用英文的 :");
  assert.equal(syntax('{"a": 1，"b": 2}')[0], "这里用了中文逗号，要用英文的 ,");
  assert.equal(
    syntax("{}}")[0],
    "JSON 结束后还有多余的内容（是不是多了 } 或 ]？）"
  );
  assert.equal(syntax("   ")[0], "内容是空的");
  assert.equal(syntax('{"a": NaN}')[0], "JSON 不支持 NaN、Infinity、undefined");
  assert.equal(syntax('{"a": 01}')[0], "数字不能以 0 开头");
  assert.equal(
    syntax('{"a": [1, ]}')[0],
    "最后一项后面多了一个逗号（JSON 不允许）"
  );
  assert.equal(syntax('{"a": }')[0], "这里缺少一个值");
  assert.equal(syntax('{"a": [1, 2')[0], "JSON 不完整：缺少 ]");
  assert.equal(syntax('{"a": "\\x"}')[0], "字符串里有不合法的转义 \\x");
  assert.equal(syntax("{a: 1}")[0], '键名要用英文双引号括起来，例如 "name"');
});

test("解析结果与 JSON.parse 一致，并记下每个键的位置", () => {
  const text = `{
  "alert_rules": [
    {
      "name": "A\\u0041\\n",
      "conditions": {"count": {"greater_than": -1.5e2}, "level": {"equals": null}}
    }
  ],
  "__proto__": {"x": true}
}`;
  const parsed = utils.parseJson(text);
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.value, JSON.parse(text));
  assert.equal(Object.getPrototypeOf(parsed.value), Object.prototype);
  assert.equal(Object.keys(parsed.value).includes("__proto__"), true);
  const range = utils.issueRange(text, parsed.ranges, {
    path: ["alert_rules", 0, "conditions", "count", "greater_than"],
    message: "m"
  });
  assert.deepEqual(range, {
    startLineNumber: 5,
    startColumn: 32,
    endLineNumber: 5,
    endColumn: 46,
    message: "m"
  });
  // 路径不存在时标在最近的上一级上;数组元素标在它开始的那一行
  const parent = utils.issueRange(text, parsed.ranges, {
    path: ["alert_rules", 0, "severity"],
    message: "m"
  });
  assert.equal(parent.startLineNumber, 3);
  assert.equal(parent.endLineNumber, 3);
  const top = utils.issueRange(text, parsed.ranges, { path: [], message: "m" });
  assert.equal(top.startLineNumber, 1);
  const marker = utils.syntaxRange('{\n  "a": 1,\n}', {
    message: "x",
    offset: 10
  });
  assert.deepEqual(
    [marker.startLineNumber, marker.startColumn, marker.endColumn],
    [2, 9, 10]
  );
});

test("格式化和出厂规则文件的写法一样，比较时不看键的顺序", () => {
  assert.equal(utils.formatJson(DEFAULT_RULES), DEFAULT_TEXT.trimEnd());
  const long = Array.from({ length: 12 }, (_, n) => `value-number-${n}`);
  const text = utils.formatJson({ a: long, b: [], c: {}, d: [{ x: 1 }] });
  assert.match(text, /"a": \[\n {4}"value-number-0",/);
  assert.match(text, /"b": \[\],\n {2}"c": \{\},/);
  assert.deepEqual(JSON.parse(text), { a: long, b: [], c: {}, d: [{ x: 1 }] });
  assert.equal(
    utils.stableStringify({ b: 1, a: [{ d: 2, c: 3 }] }),
    utils.stableStringify({ a: [{ c: 3, d: 2 }], b: 1 })
  );
  assert.notEqual(
    utils.stableStringify({ a: [1, 2] }),
    utils.stableStringify({ a: [2, 1] })
  );
});

test("试运行的说明", () => {
  assert.deepEqual(
    utils.previewNotes({
      limit: 10000,
      truncated: true,
      deleted: 3,
      alerted_level: 2
    }),
    [
      "这段时间的事件太多，只取了最近的 10000 条",
      "其中 3 条在库里已是「删除」状态（K8S 清理了事件对象），按删除前的状态参与匹配",
      "其中 2 条的级别在库里已改成「已告警」（原来的级别没有记录），按 Warning 参与匹配",
      "每个事件只算一次；实际发不发、发几次还要看去重窗口和事件更新的次数。试运行不会发通知，也不会改事件"
    ]
  );
  assert.equal(
    utils.previewNotes({
      limit: 1,
      truncated: false,
      deleted: 0,
      alerted_level: 0
    }).length,
    1
  );
});

// ---------------------------------------------------------------------------
// 页面(index.vue 的脚本逻辑,组件和接口都换成假的)
// ---------------------------------------------------------------------------
const pageState = (rules, source = "database", updatedBy = "kubedoor") => ({
  rules,
  source,
  updated_by: source === "default" ? null : updatedBy,
  updated_at: source === "default" ? null : "2026-11-15T10:00:00+08:00"
});

async function harness(t, overrides = {}) {
  const oldWindow = global.window;
  global.window = { addEventListener() {}, removeEventListener() {} };
  const calls = { saved: [], messages: [], confirms: [] };
  const api = {
    getEventAlertRules: async () => ({
      success: true,
      data: pageState(clone(DEFAULT_RULES), "default")
    }),
    getDefaultEventAlertRules: async () => ({
      success: true,
      data: { rules: clone(DEFAULT_RULES) }
    }),
    saveEventAlertRules: async rules => {
      calls.saved.push(clone(rules));
      return { success: true, data: pageState(clone(rules)) };
    },
    ...overrides.api
  };
  const record =
    type =>
    (...args) =>
      calls.messages.push([type, ...args]);
  const component = compile(
    "../src/views/system/event-alert-rules/index.vue",
    {
      "@/api/event-alert-rules": api,
      "@/api/system": {
        getKnownClusters: async () => ({
          success: true,
          data: [{ env: "prod-a" }, { env: "test-b" }]
        })
      },
      "./rules-utils": utils,
      "./components/RuleCard.vue": {},
      "./components/RulesJsonEditor.vue": {},
      "./components/PreviewDialog.vue": {},
      "@element-plus/icons-vue": { Plus: {} },
      "vue-router": { onBeforeRouteLeave() {} },
      "element-plus": {
        ElMessage: {
          warning: record("warning"),
          error: record("error"),
          success: record("success"),
          info: record("info")
        },
        ElMessageBox: {
          confirm: async (...args) => {
            calls.confirms.push(args);
            if (overrides.reject) throw new Error("cancel");
          }
        }
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

test("页面：没保存过时用默认规则，界面配置打开，改了才算未保存", async t => {
  const { vm, calls } = await harness(t);
  assert.equal(vm.loaded, true);
  assert.equal(vm.mode, "ui");
  assert.equal(vm.state.source, "default");
  assert.equal(vm.form.alert.length, 7);
  assert.equal(vm.dirty, false);
  assert.match(vm.metaText, /内置默认规则/);
  assert.deepEqual(vm.clusters, ["prod-a", "test-b"]);
  await vm.save(); // 没改过:不保存
  assert.deepEqual(calls.saved, []);
  vm.form.alert[1].name = "重启事件告警（改）";
  assert.equal(vm.dirty, true);
  vm.form.alert[1].name = "重启事件告警";
  assert.equal(vm.dirty, false);
});

test("页面：保存提交界面里的规则，成功后提示马上生效", async t => {
  const { vm, calls } = await harness(t);
  vm.form.alert[0].enabled = false;
  vm.moveAlert(0, 1);
  assert.equal(vm.form.alert[1].name, "关键事件告警");
  await vm.save();
  assert.equal(calls.saved.length, 1);
  assert.equal(calls.saved[0].alert_rules[1].name, "关键事件告警");
  assert.equal(calls.saved[0].alert_rules[1].enabled, false);
  assert.deepEqual(calls.messages.at(-1), ["success", "已保存，马上生效"]);
  assert.equal(vm.dirty, false);
  assert.equal(vm.state.source, "database");
  assert.match(vm.metaText, /^上次修改：kubedoor · 2026-11-15/);
});

test("页面：有问题时不保存，在卡片上标出来", async t => {
  const { vm, calls } = await harness(t);
  await vm.addRule("alert");
  assert.equal(vm.showIssues, false);
  await vm.save();
  assert.deepEqual(calls.saved, []);
  assert.equal(vm.showIssues, true);
  assert.match(calls.messages.at(-1)[1], /还有 2 处需要改正，改好后才能保存/);
  assert.deepEqual(
    vm.cardIssues("alert_rules", 7).map(item => item.message),
    [
      "告警规则第 8 条缺少名称 name",
      "告警规则第 8 条的条件 reason：contains 至少要填一个值"
    ]
  );
  vm.openPreview();
  assert.equal(vm.previewVisible, false);
  vm.form.alert[7].name = "新规则";
  vm.form.alert[7].conditions[0].values = ["Evicted"];
  vm.openPreview();
  assert.equal(vm.previewVisible, true);
  assert.equal(vm.previewRules.alert_rules[7].name, "新规则");
});

test("页面：保存时 master 指出的问题列出来，不再重复弹提示", async t => {
  const failure = Object.assign(new Error("422"), {
    response: {
      status: 422,
      data: {
        message: "告警规则第 1 条的问题",
        errors: [{ path: ["alert_rules", 0], message: "告警规则第 1 条的问题" }]
      }
    }
  });
  const { vm, calls } = await harness(t, {
    api: {
      saveEventAlertRules: async () => {
        throw failure;
      }
    }
  });
  vm.form.alert[0].name = "改一下";
  await vm.save();
  assert.deepEqual(vm.summaryIssues, failure.response.data.errors);
  assert.equal(
    calls.messages.some(([type]) => type === "error"),
    false
  );
  vm.form.alert[0].name = "再改一下"; // 改了规则,master 上次指出的问题就清掉
  await nextTick();
  assert.deepEqual(vm.summaryIssues, []);
});

test("页面：旧规则里有界面表示不了的内容时用 JSON 模式打开", async t => {
  const legacy = {
    global_ignore_rules: [],
    alert_rules: [{ name: "旧规则", conditions: { reason: { contain: "x" } } }]
  };
  const { vm } = await harness(t, {
    api: {
      getEventAlertRules: async () => ({
        success: true,
        data: pageState(legacy, "database", "legacy-import")
      })
    }
  });
  assert.equal(vm.mode, "json");
  assert.equal(vm.openedLost.length, 1);
  assert.deepEqual(JSON.parse(vm.jsonText), legacy);
  assert.equal(vm.dirty, false);
  assert.match(vm.metaText, /升级时从旧的规则文件导入/);
  assert.match(vm.jsonIssues[0].message, /不认识的运算符 contain/);
  assert.equal(vm.jsonIssues[0].startLineNumber, 8); // 标在 "contain" 这个键上
});

test("页面：模式切换，界面表示不了的内容要确认，语法错误不能切换", async t => {
  const { vm, calls } = await harness(t, { reject: true });
  await vm.switchMode("json");
  assert.equal(vm.mode, "json");
  assert.equal(vm.dirty, false);
  // 界面里的规则转成 JSON:条件按字段的固定顺序排列
  assert.equal(
    vm.jsonText,
    utils.formatJson(utils.formToRules(utils.rulesToForm(DEFAULT_RULES).form))
  );
  vm.jsonText = vm.jsonText.replace('"enabled": true', '"enabled": true,,');
  assert.equal(vm.dirty, true);
  assert.ok(vm.syntaxError);
  await vm.switchMode("ui");
  assert.equal(vm.mode, "json");
  assert.match(
    calls.messages.at(-1)[1],
    /有语法错误，改好后才能切换到界面配置/
  );
  vm.form.alert[0].name = "x"; // 界面里的旧表单不影响 JSON 模式
  await vm.save();
  assert.deepEqual(calls.saved, []);
  // 能解析但界面表示不了:确认框里点「留在 JSON」
  const value = clone(DEFAULT_RULES);
  value.alert_rules[0].description = "说明";
  vm.jsonText = JSON.stringify(value, null, 2);
  await vm.switchMode("ui");
  assert.equal(vm.mode, "json");
  assert.equal(calls.confirms.length, 1);
  // 改好以后直接切换,不用确认
  delete value.alert_rules[0].description;
  value.alert_rules[0].name = "JSON 里改的名字";
  vm.jsonText = JSON.stringify(value);
  await vm.switchMode("ui");
  assert.equal(vm.mode, "ui");
  assert.equal(calls.confirms.length, 1);
  assert.equal(vm.form.alert[0].name, "JSON 里改的名字");
  assert.equal(vm.dirty, true);
});

test("页面：恢复默认只载入编辑区，保存后才生效", async t => {
  const saved = doc([rule("自己的规则", { reason: { contains: ["x"] } })]);
  const { vm, calls } = await harness(t, {
    api: {
      getEventAlertRules: async () => ({
        success: true,
        data: pageState(saved)
      })
    }
  });
  assert.equal(vm.form.alert.length, 1);
  await vm.restoreDefault();
  assert.equal(calls.confirms.length, 1);
  assert.equal(vm.form.alert.length, 7);
  assert.equal(vm.dirty, true);
  assert.deepEqual(calls.saved, []);
  await vm.save();
  assert.deepEqual(calls.saved[0], DEFAULT_RULES);
});

test("页面：读取失败时不能保存，避免用空规则覆盖", async t => {
  const { vm, calls } = await harness(t, {
    api: {
      getEventAlertRules: async () => {
        throw Object.assign(new Error("boom"), {
          response: {
            status: 503,
            data: { message: "读取事件告警规则失败，请稍后重试" }
          }
        });
      }
    }
  });
  assert.equal(vm.loaded, false);
  assert.equal(vm.loadError, "读取事件告警规则失败，请稍后重试");
  await vm.save();
  vm.openPreview();
  await vm.restoreDefault();
  assert.deepEqual(calls.saved, []);
  assert.equal(vm.previewVisible, false);
  assert.equal(calls.confirms.length, 0);
});
