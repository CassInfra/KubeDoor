const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

/** 单文件编译加载（被测文件只能 import type） */
function compile(filename) {
  const target = path.resolve(__dirname, filename);
  const source = fs.readFileSync(target, "utf8");
  const instance = new Module(target, module);
  instance.paths = Module._nodeModulePaths(path.dirname(target));
  instance._compile(
    transformSync(source, { loader: "ts", target: "es2020", format: "cjs" })
      .code,
    target
  );
  return instance.exports;
}

const utils = compile("../src/views/system/tracing/tracing-utils.ts");

const FIELD_DEFAULTS = {
  status_code: ["http.response.status_code", "http.status_code"],
  method: ["http.request.method", "http.method"],
  route: ["http.route"],
  path: ["url.path", "http.target"],
  url: ["url.full", "http.url"],
  query: ["url.query"],
  host: ["server.address", "http.host", "net.host.name", "net.peer.name"],
  client_ip: ["client.address", "http.client_ip", "net.sock.peer.addr"],
  exception_type: ["exception.type"],
  pod: ["k8s.pod.name", "host.name"],
  namespace: ["k8s.namespace.name"],
  deployment: ["k8s.deployment.name"]
};

const CLUSTERS = [
  { env: "prod-penglai", aliases: ["cassmall-prod-penglai", "蓬莱"] },
  { env: "prod-kunlun", aliases: ["cassmall-prod-kunlun"] },
  { env: "test", aliases: [] }
];

/** GET /api/admin/tracing 的返回：存过请求头、ClickHouse 密码，状态码只保留新版字段名 */
const settings = (extra = {}) => ({
  enabled: true,
  url: "http://10.113.0.108:31686",
  ui_url: "",
  headers: [{ name: "Authorization", has_value: true }],
  timeout: 30,
  tls_verify: true,
  cluster_key: "k8sname",
  fields: {
    ...FIELD_DEFAULTS,
    status_code: ["http.response.status_code"],
    query: []
  },
  custom_fields: [{ key: "biz.order_no", label: "订单号" }],
  mask_keys: ["x-sign"],
  clickhouse: {
    enabled: true,
    url: "http://10.113.0.88:8123",
    database: "jaeger",
    table: "trace_search",
    dim_table: "trace_search_dim",
    username: "kubedoor_ro",
    has_password: true,
    timeout: 20,
    tls_verify: true
  },
  field_defaults: FIELD_DEFAULTS,
  field_labels: { status_code: "HTTP 状态码" },
  clusters: CLUSTERS,
  updated_by: "kubedoor",
  updated_at: "2026-11-21T10:00:00+08:00",
  ...extra
});

/** 一个能通过校验的表单 */
const validForm = () => utils.formFromSettings(settings());

const texts = errors => errors.map(item => item.text);

test("字段映射的顺序、名称和默认候选与约定 §2.2 一致", () => {
  assert.deepEqual(utils.FIELD_KEYS, Object.keys(FIELD_DEFAULTS));
  for (const key of utils.FIELD_KEYS)
    assert.deepEqual(utils.FIELD_META[key].defaults, FIELD_DEFAULTS[key]);
  assert.equal(utils.FIELD_META.exception_type.level, "event");
  assert.equal(utils.FIELD_META.host.label, "域名 / 被调方地址");
  // GET 没返回时用内置的；返回了以 GET 的为准
  assert.deepEqual(utils.fieldDefaults(null), FIELD_DEFAULTS);
  const labels = utils.fieldLabels({ field_labels: { pod: "容器组" } });
  assert.equal(labels.pod, "容器组");
  assert.equal(labels.status_code, "HTTP 状态码");
  // master 的层级：空 = 不限，event = 异常事件
  const levels = utils.fieldLevels({
    field_levels: { status_code: "", exception_type: "event" }
  });
  assert.equal(levels.status_code, "any");
  assert.equal(levels.exception_type, "event");
  assert.equal(levels.pod, "any");
  assert.equal(utils.fieldLevels(null).exception_type, "event");
  assert.deepEqual(utils.builtinMaskKeys({ mask_builtin: ["token"] }), [
    "token"
  ]);
  assert.ok(utils.builtinMaskKeys(null).includes("jsessionid"));
});

test("GET 返回的配置转成表单：请求头和密码不回显，缺的字段映射用默认补", () => {
  const form = utils.formFromSettings(
    settings({
      fields: { status_code: ["http.response.status_code"], query: [] },
      headers: [{ name: "Authorization", has_value: true }, { name: "X-Org" }]
    })
  );
  assert.equal(form.url, "http://10.113.0.108:31686");
  assert.deepEqual(
    form.headers.map(row => [row.name, row.value]),
    [
      ["Authorization", ""],
      ["X-Org", ""]
    ]
  );
  // 只回显名称（没有 has_value）的也是存过值的
  assert.deepEqual(form.savedHeaders, ["authorization", "x-org"]);
  assert.deepEqual(form.fields.status_code, ["http.response.status_code"]);
  // 存的是空列表：保留空列表（不用这项），不换成默认
  assert.deepEqual(form.fields.query, []);
  assert.deepEqual(form.fields.method, FIELD_DEFAULTS.method);
  assert.equal(form.clickhouse.password, "");
  assert.equal(form.clickhouse.has_password, true);
  assert.equal(form.custom_fields[0].key, "biz.order_no");
  assert.ok(form.custom_fields[0].id > 0);
  // 行 id 不重复（v-for 的 key）
  const ids = [...form.headers, ...form.custom_fields].map(row => row.id);
  assert.equal(new Set(ids).size, ids.length);
});

test("没有保存过时用默认值，ClickHouse 默认库表名", () => {
  const form = utils.emptyForm();
  assert.equal(form.enabled, false);
  assert.equal(form.timeout, 30);
  assert.equal(form.tls_verify, true);
  assert.deepEqual(form.fields, FIELD_DEFAULTS);
  assert.deepEqual(
    {
      enabled: form.clickhouse.enabled,
      database: form.clickhouse.database,
      table: form.clickhouse.table,
      dim_table: form.clickhouse.dim_table,
      timeout: form.clickhouse.timeout,
      has_password: form.clickhouse.has_password
    },
    {
      enabled: false,
      database: "jaeger",
      table: "trace_search",
      dim_table: "trace_search_dim",
      timeout: 20,
      has_password: false
    }
  );
  assert.equal(utils.clickhouseExpanded(null), false);
  assert.equal(utils.clickhouseExpanded(settings()), true);
  assert.equal(
    utils.clickhouseExpanded({ clickhouse: { enabled: false, url: "" } }),
    false
  );
});

test("表单转成提交数据：去空白和末尾的 /，值留空表示沿用，不带 enabled", () => {
  const form = validForm();
  form.url = "  http://10.113.0.108:31686/jaeger/ ";
  form.ui_url = "https://jaeger.example.com/";
  form.headers.push({ id: 900, name: " X-Org ", value: "ops" });
  form.headers.push({ id: 901, name: "", value: "" });
  form.cluster_key = " k8sname ";
  form.fields.pod = [" k8s.pod.name", "k8s.pod.name", "", "host.name"];
  form.custom_fields.push({ id: 902, key: "", label: "" });
  form.custom_fields.push({ id: 903, key: " user.id ", label: " 用户 ID " });
  form.mask_keys = ["X-Sign", "x-sign", " biz.secret ", ""];
  form.clickhouse.url = "http://10.113.0.88:8123/";
  form.clickhouse.username = " kubedoor_ro ";

  const payload = utils.formPayload(form);
  assert.equal("enabled" in payload, false);
  assert.equal(payload.url, "http://10.113.0.108:31686/jaeger");
  assert.equal(payload.ui_url, "https://jaeger.example.com");
  assert.deepEqual(payload.headers, [
    { name: "Authorization", value: null },
    { name: "X-Org", value: "ops" }
  ]);
  assert.equal(payload.cluster_key, "k8sname");
  assert.deepEqual(payload.fields.pod, ["k8s.pod.name", "host.name"]);
  // 存完整映射：12 项都在
  assert.deepEqual(Object.keys(payload.fields), utils.FIELD_KEYS);
  assert.deepEqual(payload.custom_fields, [
    { key: "biz.order_no", label: "订单号" },
    { key: "user.id", label: "用户 ID" }
  ]);
  assert.deepEqual(payload.mask_keys, ["X-Sign", "biz.secret"]);
  assert.equal(payload.clickhouse.url, "http://10.113.0.88:8123");
  assert.equal(payload.clickhouse.username, "kubedoor_ro");
  // 密码留空：不传，沿用已保存的
  assert.equal("password" in payload.clickhouse, false);
  assert.equal("has_password" in payload.clickhouse, false);

  form.clickhouse.password = "s3cret";
  assert.equal(utils.formPayload(form).clickhouse.password, "s3cret");
  // 测试 / 识别时带 enabled: false（测试不看启用状态）
  assert.equal(utils.testPayload(form).enabled, false);
});

test("未保存判断：改了内容或填了请求头的值 / 密码就算修改", () => {
  const form = validForm();
  const snapshot = utils.payloadKey(form);
  assert.equal(utils.payloadKey(form), snapshot);
  form.headers[0].value = "Bearer abc";
  assert.notEqual(utils.payloadKey(form), snapshot);
  form.headers[0].value = "";
  form.clickhouse.password = "x";
  assert.notEqual(utils.payloadKey(form), snapshot);
  form.clickhouse.password = "";
  // 只是末尾多了 / 不算修改（保存时会去掉）
  form.url += "/";
  assert.equal(utils.payloadKey(form), snapshot);
});

test("清除 ClickHouse 密码：保存和测试都带 clear_password，不传密码；重新加载后复位", () => {
  const form = validForm();
  assert.equal(form.clickhouse.clear_password, false);
  assert.equal(
    utils.passwordPlaceholder(form.clickhouse),
    "已配置，留空表示不修改"
  );
  // 没点清除时不带这个键
  assert.equal("clear_password" in utils.formPayload(form).clickhouse, false);
  const snapshot = utils.payloadKey(form);

  // 点了「清除」：填过的密码也不提交（master 会忽略同时传来的密码）
  form.clickhouse.password = "typed-before";
  form.clickhouse.clear_password = true;
  assert.equal(
    utils.passwordPlaceholder(form.clickhouse),
    "将清除，保存后生效"
  );
  const payload = utils.formPayload(form).clickhouse;
  assert.equal(payload.clear_password, true);
  assert.equal("password" in payload, false);
  assert.equal(utils.testPayload(form).clickhouse.clear_password, true);
  assert.notEqual(utils.payloadKey(form), snapshot);

  // 撤销清除：恢复按 has_password 显示，填的密码照常提交
  form.clickhouse.clear_password = false;
  assert.equal(utils.formPayload(form).clickhouse.password, "typed-before");

  // 保存后按返回的 has_password 重新加载：标记复位
  const saved = utils.formFromSettings(
    settings({ clickhouse: { ...settings().clickhouse, has_password: false } })
  );
  assert.equal(saved.clickhouse.clear_password, false);
  assert.equal(saved.clickhouse.has_password, false);
  assert.equal(utils.passwordPlaceholder(saved.clickhouse), "没有密码可以不填");
});

test("校验：合法的配置没有错误，Jaeger 查询地址必填", () => {
  assert.deepEqual(utils.validateForm(validForm()), []);
  const form = validForm();
  form.url = " ";
  assert.deepEqual(texts(utils.validateForm(form)), ["请填写 Jaeger 查询地址"]);
  assert.equal(utils.validateForm(form)[0].section, "jaeger");
  // 只测 ClickHouse 时不要求 Jaeger 地址
  assert.deepEqual(utils.jaegerErrors(form, false), []);
});

test("校验：地址格式", () => {
  const cases = [
    ["ftp://jaeger:16686", "要以 http:// 或 https:// 开头"],
    ["jaeger:16686", "要以 http:// 或 https:// 开头"],
    ["http://jaeger:16686/search?service=a", "不能带查询参数（?）或 #"],
    ["http://jaeger:16686/#/trace", "不能带查询参数（?）或 #"],
    ["http://jaeger 16686", "不能有空格或换行"],
    [`http://jaeger/${"a".repeat(2050)}`, "不能超过 2048 个字符"],
    ["http://:8080", "格式不正确"],
    // 末尾的 / 去掉后只剩 http:
    ["http://", "要以 http:// 或 https:// 开头"]
  ];
  for (const [url, error] of cases)
    assert.equal(utils.urlError(url, true), error, url);
  assert.equal(utils.urlError("https://ops.example.com/jaeger/", true), "");
  assert.equal(utils.urlError("", false), "");
  assert.equal(utils.urlError("", true), "必须填写");

  const form = validForm();
  form.ui_url = "jaeger.example.com";
  assert.deepEqual(texts(utils.validateForm(form)), [
    "Jaeger UI 地址：要以 http:// 或 https:// 开头"
  ]);
});

test("校验：请求头（同 master ext_api：保留名称、重复、新加的要填值）", () => {
  const form = validForm();
  form.headers.push({ id: 1, name: "authorization", value: "x" });
  form.headers.push({ id: 2, name: "Host", value: "x" });
  form.headers.push({ id: 3, name: "X-New", value: "" });
  form.headers.push({ id: 4, name: "Bad Name", value: "x" });
  assert.deepEqual(utils.headerErrors(form), [
    "authorization 重复了",
    "Host 由系统设置，不能自定义",
    "请填写 X-New 的值",
    "名称不合法：Bad Name（只能用字母、数字和 - _ . 等符号，不能有空格和冒号）"
  ]);
  // 改名后的请求头不能沿用原来的值；改回已保存的名称（不区分大小写）可以
  const renamed = validForm();
  renamed.headers[0].name = "X-Token";
  assert.deepEqual(utils.headerErrors(renamed), ["请填写 X-Token 的值"]);
  renamed.headers[0].name = "AUTHORIZATION";
  assert.deepEqual(utils.headerErrors(renamed), []);
  assert.equal(utils.keepsSavedHeader(renamed, renamed.headers[0]), true);

  const many = validForm();
  for (let i = 0; i < 10; i++)
    many.headers.push({ id: 100 + i, name: `X-H${i}`, value: "v" });
  assert.equal(utils.headerErrors(many)[0], "最多 10 个");
});

test("校验：集群字段、超时", () => {
  const form = validForm();
  form.cluster_key = "k8s cluster";
  form.timeout = 3;
  assert.deepEqual(texts(utils.validateForm(form)), [
    "Jaeger 连接：超时取 5~120 秒的整数",
    "集群字段：只能用字母、数字和 _ . - / : @，最长 128 个字符"
  ]);
  form.cluster_key = "k8s.cluster.name";
  form.timeout = 120;
  assert.deepEqual(utils.validateForm(form), []);
  form.cluster_key = "a".repeat(129);
  assert.equal(utils.validateForm(form)[0].section, "cluster");
  form.cluster_key = "team/app:cluster@v1_x-y";
  assert.deepEqual(utils.validateForm(form), []);
  // 留空 = 不按集群过滤，合法
  form.cluster_key = "";
  assert.deepEqual(utils.validateForm(form), []);
});

test("校验：ClickHouse 加速", () => {
  const form = validForm();
  form.clickhouse.url = "";
  form.clickhouse.database = "1jaeger";
  form.clickhouse.table = "";
  form.clickhouse.dim_table = "";
  form.clickhouse.timeout = 200;
  assert.deepEqual(texts(utils.validateForm(form)), [
    "ClickHouse 加速 · 请填写 HTTP 地址",
    "ClickHouse 加速 · 库名只能用字母、数字和下划线，不能以数字开头，最长 64 个字符",
    "ClickHouse 加速 · 请填写搜索表",
    "ClickHouse 加速 · 超时取 5~120 秒的整数"
  ]);
  // 没启用时地址可以不填（汇总表本来就可以空）；但点「测试」时要填
  const off = validForm();
  off.clickhouse.enabled = false;
  off.clickhouse.url = "";
  assert.deepEqual(utils.validateForm(off), []);
  assert.deepEqual(texts(utils.clickhouseErrors(off, true)), [
    "ClickHouse 加速 · 请填写 HTTP 地址"
  ]);
  off.clickhouse.dim_table = "trace-search-dim";
  assert.equal(
    utils.validateForm(off)[0].text,
    "ClickHouse 加速 · 汇总表只能用字母、数字和下划线，不能以数字开头，最长 64 个字符"
  );
  // 账号同 master：不能有空格，最长 128
  off.clickhouse.dim_table = "";
  off.clickhouse.username = "kube door";
  assert.deepEqual(texts(utils.validateForm(off)), [
    "ClickHouse 加速 · 账号不能有空格，最长 128 个字符"
  ]);
  off.clickhouse.username = " kubedoor_ro ";
  assert.deepEqual(utils.validateForm(off), []);
});

test("校验：字段映射、业务字段、打码字段", () => {
  const form = validForm();
  form.fields.host = ["a", "b", "c", "d", "e", "f"];
  form.fields.pod = ["k8s pod"];
  form.custom_fields = [
    { id: 1, key: "biz.order_no", label: "订单号" },
    { id: 2, key: "biz.order_no", label: "订单" },
    { id: 3, key: "user.id", label: "" },
    { id: 4, key: "", label: "租户" },
    { id: 5, key: "x", label: "长".repeat(51) }
  ];
  form.mask_keys = ["k".repeat(129), "x sign"];
  assert.deepEqual(texts(utils.validateForm(form)), [
    "字段映射 · 域名 / 被调方地址：最多 5 个属性名",
    "字段映射 · Pod：「k8s pod」不能有空格或换行",
    "业务字段：biz.order_no 重复了",
    "业务字段：user.id 缺少中文说明",
    "业务字段：第 4 行缺少属性名",
    "业务字段：x 的说明最多 50 个字",
    `打码字段：${"k".repeat(40)}… 太长了（最多 128 个字符）`,
    "打码字段：「x sign」不能有空格或换行"
  ]);
  assert.deepEqual(
    [...new Set(utils.validateForm(form).map(item => item.section))],
    ["fields", "custom", "mask"]
  );
  // 留空 = 不用这项，合法
  const empty = validForm();
  empty.fields.route = [];
  assert.deepEqual(utils.validateForm(empty), []);
});

test("识别结果的建议：可以套用 / 和当前一致 / 样本里没有", () => {
  const form = validForm();
  const result = {
    traces: 3,
    spans: 120,
    keys: [],
    cluster_candidates: [],
    suggestions: {
      cluster_key: "k8sname",
      fields: {
        status_code: ["http.response.status_code"],
        method: ["http.request.method"],
        pod: ["k8s.pod.name", "host.name"]
      }
    }
  };
  const rows = utils.suggestionRows(
    form.fields,
    result,
    utils.fieldLabels(null)
  );
  const states = Object.fromEntries(rows.map(row => [row.key, row.state]));
  assert.equal(states.status_code, "same");
  assert.equal(states.method, "apply");
  assert.equal(states.pod, "same");
  assert.equal(states.route, "none");
  assert.equal(rows[0].label, "HTTP 状态码");

  const before = JSON.stringify(form.fields);
  const { fields, applied } = utils.applyAllSuggestions(form.fields, result);
  assert.deepEqual(applied, ["method"]);
  assert.deepEqual(fields.method, ["http.request.method"]);
  assert.deepEqual(fields.route, FIELD_DEFAULTS.route);
  // 不改传入的对象
  assert.equal(JSON.stringify(form.fields), before);
  // 没有识别结果时都是 none
  assert.ok(
    utils
      .suggestionRows(form.fields, null, utils.fieldLabels(null))
      .every(row => row.state === "none")
  );
});

test("候选属性名的下拉选项：默认在前，样本里同层级的按次数排", () => {
  const observed = [
    { key: "http.status_code", level: "span", types: ["int"], count: 5 },
    { key: "biz.code", level: "span", types: ["int"], count: 40 },
    { key: "exception.type", level: "event", types: ["str"], count: 3 },
    { key: "k8sname", level: "resource", types: ["str"], count: 120 },
    { key: "biz.code", level: "resource", types: ["str"], count: 2 }
  ];
  const options = utils.fieldOptions(
    "any",
    FIELD_DEFAULTS.status_code,
    ["http.response.status_code", "my.status"],
    observed
  );
  assert.deepEqual(
    options.map(item => item.value),
    [
      "http.response.status_code",
      "http.status_code",
      "my.status",
      "k8sname",
      "biz.code"
    ]
  );
  assert.equal(options[0].note, "默认");
  assert.equal(options[1].note, "默认 · 样本 5 次");
  assert.equal(options[2].note, "");
  // 同名属性的次数合并
  assert.equal(options[4].note, "样本 42 次");
  // 异常类型只列事件属性
  assert.deepEqual(
    utils
      .fieldOptions("event", ["exception.type"], [], observed)
      .map(item => [item.value, item.note]),
    [["exception.type", "默认 · 样本 3 次"]]
  );
});

test("集群取值对照：名称、别名精确匹配，歧义、空值、master 的结果兜底", () => {
  assert.deepEqual(utils.matchEnvs("cassmall-prod-penglai", CLUSTERS), [
    "prod-penglai"
  ]);
  assert.deepEqual(utils.matchEnvs("test", CLUSTERS), ["test"]);
  // 区分大小写（搜索时是精确匹配）
  assert.deepEqual(utils.matchEnvs("Cassmall-Prod-Penglai", CLUSTERS), []);
  assert.deepEqual(utils.classifyValue("", null, CLUSTERS), {
    status: "empty",
    envs: []
  });
  const twice = [...CLUSTERS, { env: "dr", aliases: ["test"] }];
  assert.deepEqual(utils.classifyValue("test", null, twice), {
    status: "ambiguous",
    envs: ["test", "dr"]
  });
  assert.deepEqual(utils.classifyValue("other", "test", CLUSTERS), {
    status: "matched",
    envs: ["test"]
  });
  assert.deepEqual(utils.classifyValue("other", null, CLUSTERS), {
    status: "unmatched",
    envs: []
  });
});

test("集群卡片的对照表：ClickHouse 测试和识别样本的取值归到各集群", () => {
  const chTest = {
    ok: true,
    elapsed_ms: 90,
    cluster_values: [
      { value: "cassmall-prod-penglai", spans: 780000, env: "prod-penglai" },
      { value: "cassmall-prod-kunlun", spans: 1200, env: "prod-kunlun" },
      { value: "cassmall-uat", spans: 30, env: null },
      { value: "", spans: 15, env: null },
      { value: "new-cluster", spans: 8, env: "new-env" }
    ]
  };
  const analysis = {
    traces: 2,
    spans: 50,
    keys: [],
    cluster_candidates: [
      {
        key: "k8sname",
        values: [
          { value: "cassmall-prod-penglai", env: "prod-penglai" },
          { value: "cassmall-dev", env: null }
        ],
        matched_envs: ["prod-penglai"]
      }
    ],
    suggestions: {}
  };
  const observed = utils.mergeObserved([
    ...utils.observedFromClickHouse(chTest),
    ...utils.observedFromAnalyze(analysis, "k8sname")
  ]);
  const penglai = observed.find(item => item.value === "cassmall-prod-penglai");
  assert.deepEqual(penglai.sources, ["clickhouse", "sample"]);
  assert.equal(penglai.spans, 780000);
  assert.equal(
    utils.observedText(penglai),
    "cassmall-prod-penglai · 780000 个 Span"
  );
  assert.equal(
    utils.sourceText(penglai),
    "ClickHouse 最近 10 分钟、识别字段的样本"
  );

  const view = utils.clusterOverview(CLUSTERS, observed);
  const byEnv = Object.fromEntries(
    view.rows.map(row => [row.env, row.observed.map(item => item.value)])
  );
  assert.deepEqual(byEnv["prod-penglai"], ["cassmall-prod-penglai"]);
  assert.deepEqual(byEnv["prod-kunlun"], ["cassmall-prod-kunlun"]);
  assert.deepEqual(byEnv.test, []);
  // master 认得、页面上的集群列表里没有的：单独加一行
  assert.deepEqual(byEnv["new-env"], ["new-cluster"]);
  assert.deepEqual(
    view.unmatched.map(item => item.value),
    ["cassmall-uat", "cassmall-dev"]
  );
  assert.equal(view.emptySpans, 15);
  assert.deepEqual(view.ambiguous, []);
  // 没有测试结果时：只有集群和别名
  const bare = utils.clusterOverview(CLUSTERS, []);
  assert.equal(bare.emptySpans, null);
  assert.deepEqual(bare.rows[0].aliases, ["cassmall-prod-penglai", "蓬莱"]);
});

test("识别样本里当前集群字段的取值：候选里没有时用全部属性的示例", () => {
  const analysis = {
    traces: 1,
    spans: 10,
    keys: [
      {
        key: "cluster",
        level: "resource",
        types: ["str"],
        count: 10,
        examples: ["a", "b"]
      },
      { key: "env", level: "span", types: ["str"], count: 10, examples: ["x"] }
    ],
    cluster_candidates: [],
    suggestions: {}
  };
  assert.deepEqual(
    utils.observedFromAnalyze(analysis, "cluster").map(item => item.value),
    ["a", "b"]
  );
  assert.deepEqual(utils.observedFromAnalyze(analysis, "env"), []);
  assert.deepEqual(utils.observedFromAnalyze(analysis, ""), []);
  assert.deepEqual(utils.observedFromAnalyze(null, "cluster"), []);
});

test("识别结果里当前集群字段的检查", () => {
  const analysis = {
    traces: 3,
    spans: 100,
    keys: [
      {
        key: "k8sname",
        level: "resource",
        types: ["str"],
        count: 100,
        examples: ["cassmall-prod-penglai"]
      },
      {
        key: "cluster",
        level: "resource",
        types: ["str"],
        count: 100,
        examples: ["c1", "c2"]
      },
      {
        key: "env",
        level: "span",
        types: ["str"],
        count: 3,
        examples: ["prod"]
      }
    ],
    cluster_candidates: [
      {
        key: "k8sname",
        values: [{ value: "cassmall-prod-penglai", env: "prod-penglai" }],
        matched_envs: ["prod-penglai"]
      }
    ],
    suggestions: { cluster_key: "k8sname" }
  };
  assert.equal(utils.clusterKeyCheck(null, "k8sname"), null);
  assert.equal(utils.clusterKeyCheck(analysis, "").type, "info");
  assert.deepEqual(utils.clusterKeyCheck(analysis, "k8sname"), {
    type: "success",
    text: "当前的集群字段 k8sname 在样本里能对上集群：prod-penglai"
  });
  assert.match(
    utils.clusterKeyCheck(analysis, "cluster").text,
    /取值（c1、c2）对不上 KubeDoor 的集群名或别名/
  );
  assert.match(
    utils.clusterKeyCheck(analysis, "env").text,
    /env 在样本里是Span属性，不是资源属性/
  );
  assert.equal(
    utils.clusterKeyCheck(analysis, "k8s.cluster.name").text,
    "样本里没有属性 k8s.cluster.name：检查字段名有没有写错，或者这个服务还没有设置集群属性"
  );
});

test("集群字段输入框的建议和 OTel 环境变量示例", () => {
  const analysis = {
    cluster_candidates: [
      { key: "k8sname", values: [], matched_envs: ["a", "b"] },
      { key: "k8s.cluster.name", values: [], matched_envs: ["a"] }
    ]
  };
  assert.deepEqual(utils.clusterKeySuggestions("", analysis), [
    { value: "k8sname", note: "样本里对上 2 个集群" },
    { value: "k8s.cluster.name", note: "样本里对上 1 个集群" }
  ]);
  assert.deepEqual(
    utils.clusterKeySuggestions("CLUSTER", null).map(item => item.value),
    ["k8s.cluster.name"]
  );
  assert.equal(
    utils.otelEnvExample("", "prod-penglai"),
    "env:\n  - name: OTEL_RESOURCE_ATTRIBUTES\n    value: k8sname=prod-penglai"
  );
  assert.match(
    utils.otelEnvExample("k8s.cluster.name", ""),
    /value: k8s\.cluster\.name=<集群名>$/
  );
});

test("Jaeger 后端能力的说明", () => {
  const missing = utils.capabilityRows(null);
  assert.equal(missing.length, 1);
  assert.equal(missing[0].ok, false);

  const full = utils.capabilityRows({
    search: {
      withoutServiceName: true,
      sameSpanConjunction: true,
      filter: {
        levels: ["span", "resource"],
        operators: ["and", "or", "not", "eq"]
      }
    }
  });
  assert.deepEqual(
    full.map(row => [row.label, row.value]),
    [
      ["不填服务名搜索", "支持"],
      ["过滤运算符", "and、or、not、eq"],
      ["过滤层级", "span、resource"],
      ["多个条件", "要落在同一个 Span 上（含它的资源属性）"]
    ]
  );
  assert.ok(full.slice(0, 2).every(row => row.ok));

  // false 的字段省略；枚举名统一成小写短名；缺 eq 不能搜索
  const partial = utils.capabilityRows({
    search: { filter: { operators: ["OPERATOR_AND", "OPERATOR_OR"] } }
  });
  assert.equal(partial[0].value, "不支持（搜索时必须指定服务）");
  assert.equal(partial[1].value, "and、or（缺少 eq，不能搜索）");
  assert.equal(partial[1].ok, false);
  assert.equal(partial[2].value, "（无）");
  assert.equal(partial[3].value, "未声明");

  const noFilter = utils.capabilityRows({ search: {} });
  assert.equal(noFilter[1].label, "结构化过滤");
  assert.equal(noFilter[1].ok, false);
});

test("测试结果的说明", () => {
  assert.deepEqual(
    utils.jaegerTestView({
      ok: true,
      elapsed_ms: 120,
      services_count: 449,
      search_ok: true
    }),
    { type: "success", text: "连接正常（449 个服务，耗时 120 ms），可以搜索" }
  );
  assert.equal(
    utils.jaegerTestView({ ok: true, elapsed_ms: 80, services: ["a"] }).type,
    "warning"
  );
  assert.deepEqual(
    utils.jaegerTestView({ ok: false, elapsed_ms: 5000, error: "连接超时" }),
    { type: "error", text: "连接失败：连接超时（耗时 5000 ms）" }
  );

  const ch = {
    ok: true,
    elapsed_ms: 95,
    version: "26.7.1",
    missing_columns: { trace_search: [], trace_search_dim: ["spans", "bucket"] }
  };
  assert.deepEqual(utils.missingColumnLines(ch), [
    "trace_search_dim 缺少列：spans、bucket"
  ]);
  assert.deepEqual(utils.clickhouseTestView(ch), {
    type: "warning",
    text: "连接正常，但表里缺少列（ClickHouse 26.7.1，耗时 95 ms）"
  });
  ch.missing_columns = {};
  assert.equal(utils.clickhouseTestView(ch).type, "success");
  assert.deepEqual(
    utils.clickhouseTestView({
      ok: false,
      elapsed_ms: 20000,
      error: "Authentication failed"
    }),
    {
      type: "error",
      text: "测试未通过：Authentication failed（耗时 20000 ms）"
    }
  );
});

test("粘贴的 Trace JSON 粗检查", () => {
  assert.equal(utils.traceJsonError("  "), "请先粘贴 Trace JSON");
  assert.match(utils.traceJsonError("[1,2]"), /要粘贴 JSON 对象/);
  assert.equal(utils.traceJsonError(' {"data": []} '), "");
  assert.equal(utils.byteLength("链路"), 6);
  const big = `{"data":"${"x".repeat(5 * 1024 * 1024)}"}`;
  assert.match(utils.traceJsonError(big), /^内容有 5\.0 MB，超过了 5 MB/);
});

test("其他：等待时长、全部属性筛选、识别摘要、帮助链接", () => {
  assert.equal(utils.requestTimeoutMs(30, 2), 75000);
  assert.equal(utils.requestTimeoutMs(500, 1), 135000);
  assert.equal(utils.requestTimeoutMs(null, 1), 45000);

  const keys = [
    { key: "http.route", level: "span", count: 9 },
    { key: "k8s.pod.name", level: "resource", count: 9 },
    { key: "HTTP.Method", level: "span", count: 2 }
  ];
  assert.deepEqual(
    utils.filterKeys(keys, "http", "").map(item => item.key),
    ["http.route", "HTTP.Method"]
  );
  assert.deepEqual(
    utils.filterKeys(keys, "", "resource").map(item => item.key),
    ["k8s.pod.name"]
  );
  assert.equal(
    utils.analysisSummary({ traces: 3, spans: 1815, keys }),
    "分析了 3 条链路、1815 个 Span，出现 3 个属性"
  );
  assert.equal(utils.levelLabel("resource"), "资源");
  assert.equal(utils.levelLabel("other"), "other");
  assert.ok(
    utils.HELP_URL.startsWith(
      "https://github.com/CassInfra/KubeDoor/blob/main/help/%E9%93%BE"
    )
  );
});

test("取值状态的标签", () => {
  const label = (value, env, clusters = CLUSTERS) =>
    utils.matchLabel(utils.classifyValue(value, env, clusters));
  assert.deepEqual(label("cassmall-prod-kunlun", null), {
    type: "success",
    text: "prod-kunlun"
  });
  assert.deepEqual(label("x", null), {
    type: "warning",
    text: "对不上，到 Agent 管理页加别名"
  });
  assert.deepEqual(label("", null), { type: "info", text: "没有集群字段" });
  assert.deepEqual(
    label("test", null, [...CLUSTERS, { env: "dr", aliases: ["test"] }]),
    { type: "danger", text: "同时对应 test、dr" }
  );
  // master 标了 ambiguous、页面上的集群列表又没有这个取值（列表旧了）
  assert.deepEqual(
    utils.matchLabel(utils.classifyValue("shared", null, CLUSTERS, true)),
    { type: "danger", text: "同时对应几个集群" }
  );
  const view = utils.clusterOverview(
    CLUSTERS,
    utils.observedFromClickHouse({
      cluster_values: [
        { value: "shared", env: null, ambiguous: true, spans: 3 }
      ]
    })
  );
  assert.deepEqual(
    view.ambiguous.map(item => [item.value, item.envs]),
    [["shared", []]]
  );
  assert.deepEqual(view.unmatched, []);
});

test("业务字段属性名的输入建议：只列 Span / 资源属性，按次数排", () => {
  const analysis = {
    keys: [
      { key: "biz.order_no", level: "span", count: 3 },
      { key: "exception.type", level: "event", count: 9 },
      { key: "http.route", level: "span", count: 7 },
      { key: "biz.order_no", level: "resource", count: 5 },
      { key: "otel.scope", level: "scope", count: 9 }
    ]
  };
  assert.deepEqual(
    utils.observedKeySuggestions("", analysis).map(item => item.value),
    ["biz.order_no", "http.route"]
  );
  assert.deepEqual(utils.observedKeySuggestions("ROUTE", analysis), [
    { value: "http.route" }
  ]);
  assert.deepEqual(utils.observedKeySuggestions("", null), []);
});

test("请求失败的原因", () => {
  assert.equal(
    utils.errorText(
      { response: { status: 404, data: { message: "Not Found" } } },
      "读取失败"
    ),
    "HTTP 404：Not Found"
  );
  assert.equal(
    utils.errorText({ response: { status: 502, data: "" } }, "读取失败"),
    "HTTP 502"
  );
  assert.equal(
    utils.errorText({ message: "Network Error" }, "读取失败"),
    "Network Error"
  );
  assert.equal(utils.errorText(null, "读取失败"), "读取失败");
});
