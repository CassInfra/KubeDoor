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

const utils = compile("../src/views/system/log-sources/log-sources-utils.ts");

/** GET /api/admin/log-sources 的返回：一个 ClickHouse、一个 Elasticsearch，两个日志源，两个集群 */
const settings = (extra = {}) => ({
  enabled: true,
  architecture: "- 入口网关（ingress）→ 服务（app）",
  connections: [
    {
      id: "ck",
      name: "日志 ClickHouse",
      type: "clickhouse",
      url: "http://clickhouse.example.com:8123",
      auth_type: "basic",
      username: "kubedoor_ro",
      has_password: true,
      has_api_key: false,
      headers: [{ name: "X-Org", has_value: true }],
      timeout: 30,
      tls_verify: true,
      max_concurrency: 4,
      max_rows_to_read: 1000000000,
      allow_raw: true
    },
    {
      id: "es",
      name: "日志 Elasticsearch",
      type: "elasticsearch",
      url: "https://es.example.com:9200",
      auth_type: "api_key",
      username: "",
      has_password: false,
      has_api_key: true,
      headers: [],
      timeout: 20,
      tls_verify: false,
      max_concurrency: 2,
      max_rows_to_read: 0,
      allow_raw: false
    }
  ],
  correlation_keys: [
    {
      key: "trace_id",
      label: "链路 ID",
      description: "",
      pattern: "[0-9a-f]{32}",
      trace: true
    },
    {
      key: "request_id",
      label: "请求 ID",
      description: "入口网关生成",
      pattern: "",
      trace: false
    }
  ],
  sources: [
    {
      id: "ingress",
      name: "入口网关访问日志",
      enabled: true,
      connection: "ck",
      kind: "access",
      description: "入口网关的访问日志",
      time_field: "start_time",
      fields: {
        status: "response_code",
        duration: "duration",
        path: "path",
        host: "authority"
      },
      duration_unit: "ms",
      level_values: {},
      correlations: [
        { key: "request_id", field: "x_request_id" },
        { key: "trace_id", field: "trace_id" }
      ],
      columns: [{ name: "upstream_cluster", description: "上游集群" }],
      default_fields: [],
      mask_fields: [],
      mask_patterns: [],
      default_target: "logs.ingress_access",
      cluster_field: "env"
    },
    {
      id: "app",
      name: "Java 应用日志",
      enabled: false,
      connection: "es",
      kind: "app",
      description: "",
      time_field: "@timestamp",
      fields: {
        message: "message",
        level: "log.level",
        pod: "kubernetes.pod.name"
      },
      duration_unit: "ms",
      level_values: { error: ["ERROR", "FATAL"] },
      correlations: [{ key: "trace_id", text: "TID:{value}" }],
      columns: [],
      default_fields: ["@timestamp", "message"],
      mask_fields: ["x-sign"],
      mask_patterns: ["card=(\\d+)"],
      default_target: "app-logs-*",
      cluster_field: "k8s_cluster"
    }
  ],
  clusters: [
    {
      env: "prod-a",
      note: "入口流量先经过外部 nginx",
      bindings: [
        {
          source: "ingress",
          connection: "",
          target: "logs.ingress_access",
          conditions: [{ field: "env", op: "in", values: ["prod-a"] }],
          note: ""
        },
        {
          source: "app",
          connection: "",
          target: "app-logs-*",
          conditions: [{ field: "k8s_cluster", op: "eq", values: ["prod-a"] }],
          note: ""
        }
      ]
    },
    {
      env: "old-cluster",
      note: "",
      bindings: [
        {
          source: "ingress",
          connection: "",
          target: "logs.ingress_access",
          conditions: [],
          note: "整张表都是这个集群的"
        }
      ]
    }
  ],
  clusters_known: [
    { env: "prod-b", aliases: [], online: false },
    { env: "prod-a", aliases: ["prod-a-k8s"], online: true }
  ],
  semantic_fields: [
    { key: "message", label: "正文" },
    { key: "level", label: "级别" }
  ],
  mask_builtin: ["authorization", "token"],
  updated_by: "kubedoor",
  updated_at: "2026-11-23T10:00:00+08:00",
  ...extra
});

/** POST /api/admin/log-sources/scan 的返回（约定 §10.4） */
const scanResult = () => ({
  connection: {
    id: "ck",
    type: "clickhouse",
    version: "26.7.1",
    timezone: "Asia/Shanghai"
  },
  partial: false,
  elapsed_ms: 1234,
  notes: [],
  groups: [
    {
      ref: "g1",
      name_guess: "ingress_access",
      kind_guess: "access",
      columns: [
        { name: "start_time", type: "DateTime64(3)" },
        { name: "env", type: "LowCardinality(String)" },
        { name: "response_code", type: "UInt16" },
        { name: "path", type: "String" },
        { name: "duration", type: "UInt32" },
        { name: "x_request_id", type: "String" }
      ],
      fields_guess: {
        time: "start_time",
        status: "response_code",
        path: "path",
        duration: "duration",
        duration_unit: "ms"
      },
      cluster_field_guess: "env",
      id_fields: [{ field: "x_request_id", shape: "uuid" }],
      text_ids: [],
      meta_description: "",
      targets: [
        {
          target: "logs.ingress_access",
          local: "logs.ingress_access_local",
          cluster: "ck1",
          rows: 123456789,
          bytes: 4567890123,
          first: "2026-11-01T00:00:00+08:00",
          last: "2026-11-23T15:00:00+08:00",
          retention_days: 15,
          values: { env: ["prod-a", "PROD-B", "test"] },
          samples: [{ path: "/a" }, { path: "/b" }, { path: "/c" }]
        },
        {
          target: "logs.ingress_access_v2",
          rows: 1000,
          retention_days: 30,
          values: { env: ["prod-a"] }
        }
      ]
    },
    {
      ref: "g2",
      name_guess: "app_log",
      kind_guess: "app",
      columns: [
        { name: "timestamp", type: "DateTime" },
        { name: "msg", type: "String" },
        { name: "level", type: "LowCardinality(String)" },
        { name: "podname", type: "String" }
      ],
      fields_guess: {
        time: "timestamp",
        message: "msg",
        level: "level",
        pod: "podname"
      },
      cluster_field_guess: "",
      id_fields: [],
      text_ids: [{ text: "TID:{value}", shape: "hex32", count: 12 }],
      targets: [{ target: "logs.app_log", rows: 500 }]
    }
  ],
  suggested_bindings: [
    {
      env: "prod-a",
      group: "g1",
      target: "logs.ingress_access",
      conditions: [{ field: "env", op: "in", values: ["prod-a"] }],
      match: "exact"
    },
    {
      env: "prod-b",
      group: "g1",
      target: "logs.ingress_access",
      conditions: [{ field: "env", op: "in", values: ["PROD-B"] }],
      match: "exact"
    },
    {
      env: "test",
      group: "g2",
      target: "logs.app_log",
      conditions: [],
      match: "table"
    }
  ],
  clusters: [
    { env: "prod-a", aliases: ["prod-a-k8s"] },
    { env: "prod-b", aliases: [] },
    { env: "test", aliases: [] }
  ]
});

const validForm = () => utils.formFromSettings(settings());
const texts = errors => errors.map(item => item.text);
const findSource = (form, id) => form.sources.find(item => item.id === id);
const findCluster = (form, env) => form.clusters.find(item => item.env === env);

test("语义字段以 GET 返回的为准（顺序、名称），没返回的用内置的补齐；内置打码名同理", () => {
  const all = utils.semanticFields(null);
  assert.deepEqual(
    all.map(item => item.key),
    [
      "message",
      "level",
      "namespace",
      "pod",
      "service",
      "status",
      "duration",
      "method",
      "path",
      "host",
      "client_ip"
    ]
  );
  assert.equal(all[0].label, "正文");
  const server = utils.semanticFields({
    semantic_fields: [
      { key: "pod", label: "容器组" },
      { key: "message", label: "日志正文" },
      { key: "unknown", label: "不认识的" }
    ]
  });
  assert.deepEqual(
    server.slice(0, 3).map(item => [item.key, item.label]),
    [
      ["pod", "容器组"],
      ["message", "日志正文"],
      ["level", "级别"]
    ]
  );
  assert.equal(server.length, 11);
  assert.ok(server.every(item => item.hint));
  assert.deepEqual(utils.builtinMaskNames({ mask_builtin: ["token"] }), [
    "token"
  ]);
  assert.ok(utils.builtinMaskNames(null).includes("passphrase"));
  assert.ok(utils.builtinMaskNames(null).includes("authorization"));
});

test("GET 返回的配置转成表单：密码、API Key、请求头的值不回显，语义字段补齐", () => {
  const form = validForm();
  assert.equal(form.enabled, true);
  const ck = form.connections[0];
  assert.equal(ck.saved, true);
  assert.equal(ck.password, "");
  assert.equal(ck.has_password, true);
  assert.equal(ck.clear_password, false);
  assert.deepEqual(
    ck.headers.map(row => [row.name, row.value]),
    [["X-Org", ""]]
  );
  assert.deepEqual(ck.savedHeaders, ["x-org"]);
  const es = form.connections[1];
  assert.equal(es.auth_type, "api_key");
  assert.equal(es.has_api_key, true);
  assert.equal(es.api_key, "");
  assert.equal(es.allow_raw, false);

  const ingress = findSource(form, "ingress");
  assert.equal(Object.keys(ingress.fields).length, 11);
  assert.equal(ingress.fields.status, "response_code");
  assert.equal(ingress.fields.message, "");
  assert.deepEqual(ingress.level_values, {
    error: [],
    warn: [],
    info: [],
    debug: []
  });
  assert.deepEqual(
    findSource(form, "app").correlations.map(row => [
      row.key,
      row.mode,
      row.text
    ]),
    [["trace_id", "text", "TID:{value}"]]
  );
  assert.equal(findSource(form, "app").enabled, false);
  assert.equal(findCluster(form, "prod-a").bindings.length, 2);
  // 行 id 不重复（v-for 的 key）
  const ids = [
    ...form.connections.map(row => row.rowId),
    ...form.sources.map(row => row.rowId),
    ...form.clusters.flatMap(row => row.bindings.map(item => item.rowId))
  ];
  assert.equal(new Set(ids).size, ids.length);

  // ClickHouse 的认证方式固定 basic；不认识的值换成默认
  const odd = utils.formFromSettings({
    connections: [
      { id: "x", type: "clickhouse", auth_type: "api_key" },
      { id: "y", type: "elasticsearch", auth_type: "oauth" }
    ],
    sources: [{ id: "s", kind: "weird", duration_unit: "h" }]
  });
  assert.equal(odd.connections[0].auth_type, "basic");
  assert.equal(odd.connections[1].auth_type, "none");
  assert.equal(odd.sources[0].kind, "other");
  assert.equal(odd.sources[0].duration_unit, "ms");
});

test("没有保存过时是空表单；新建连接和日志源的 ID 不重复", () => {
  const form = utils.emptyForm();
  assert.equal(form.enabled, false);
  assert.deepEqual(
    [form.connections, form.sources, form.clusters, form.correlation_keys],
    [[], [], [], []]
  );
  const ck = utils.emptyConnection("clickhouse", ["ck"]);
  assert.equal(ck.id, "ck-2");
  assert.equal(ck.saved, false);
  assert.equal(ck.auth_type, "basic");
  assert.equal(ck.timeout, 30);
  assert.equal(ck.max_concurrency, 4);
  assert.equal(ck.max_rows_to_read, 1000000000);
  assert.equal(ck.allow_raw, true);
  const es = utils.emptyConnection("elasticsearch", []);
  assert.equal(es.id, "es");
  assert.equal(es.auth_type, "none");

  const filled = validForm();
  const source = utils.emptySource(filled);
  assert.equal(source.id, "logs");
  assert.equal(source.connection, "ck");
  assert.equal(source.enabled, true);
  assert.equal(utils.emptySource(filled, "es").connection, "es");
});

test("表单转成提交数据：不带 enabled，只提交非空项，空集群不提交", () => {
  const form = validForm();
  form.connections[0].url = " http://clickhouse.example.com:8123/ ";
  form.connections[0].headers.push({ id: 900, name: " X-New ", value: "v" });
  form.connections[0].headers.push({ id: 901, name: "", value: "" });
  const ingress = findSource(form, "ingress");
  ingress.fields.method = " method ";
  ingress.level_values.error = [" 500 ", "500", "", "502"];
  ingress.correlations.push({
    rowId: 902,
    key: "",
    mode: "field",
    field: "",
    text: ""
  });
  ingress.columns.push({
    rowId: 903,
    name: "x_forwarded_for",
    description: ""
  });
  ingress.columns.push({ rowId: 904, name: " route ", description: " 路由 " });
  ingress.default_target = " logs.ingress_access ";
  const app = findSource(form, "app");
  app.mask_fields = ["X-Sign", "x-sign", " card_no "];
  app.default_target = "app-logs-* , other-logs-* ,";
  form.clusters.push(utils.emptyCluster("prod-b"));

  const payload = utils.formPayload(form);
  assert.equal("enabled" in payload, false);
  assert.equal(
    payload.connections[0].url,
    "http://clickhouse.example.com:8123"
  );
  assert.deepEqual(payload.connections[0].headers, [
    { name: "X-Org", value: null },
    { name: "X-New", value: "v" }
  ]);
  const ingressOut = payload.sources[0];
  assert.deepEqual(ingressOut.fields, {
    status: "response_code",
    duration: "duration",
    method: "method",
    path: "path",
    host: "authority"
  });
  assert.deepEqual(ingressOut.level_values, { error: ["500", "502"] });
  assert.deepEqual(ingressOut.correlations, [
    { key: "request_id", field: "x_request_id" },
    { key: "trace_id", field: "trace_id" }
  ]);
  // 没写说明的字段（从扫描带出后没填的）不提交
  assert.deepEqual(ingressOut.columns, [
    { name: "upstream_cluster", description: "上游集群" },
    { name: "route", description: "路由" }
  ]);
  assert.equal(ingressOut.default_target, "logs.ingress_access");
  const appOut = payload.sources[1];
  assert.deepEqual(appOut.correlations, [
    { key: "trace_id", text: "TID:{value}" }
  ]);
  assert.deepEqual(appOut.mask_fields, ["X-Sign", "card_no"]);
  assert.equal(appOut.default_target, "app-logs-*,other-logs-*");
  assert.deepEqual(appOut.level_values, { error: ["ERROR", "FATAL"] });
  // 没有说明也没有绑定的集群不提交
  assert.deepEqual(
    payload.clusters.map(item => item.env),
    ["prod-a", "old-cluster"]
  );
  assert.deepEqual(payload.clusters[0].bindings[0], {
    source: "ingress",
    connection: "",
    target: "logs.ingress_access",
    conditions: [{ field: "env", op: "in", values: ["prod-a"] }],
    note: ""
  });
});

test("密码 / API Key：留空不传（沿用），填了才传，点了清除只带 clear_*；只提交当前认证方式的", () => {
  const form = validForm();
  const snapshot = utils.payloadKey(form);
  const [ck, es] = form.connections;
  let [ckOut, esOut] = utils.formPayload(form).connections;
  assert.equal("password" in ckOut, false);
  assert.equal("clear_password" in ckOut, false);
  assert.equal("has_password" in ckOut, false);
  assert.equal(ckOut.auth_type, "basic");
  assert.equal(ckOut.username, "kubedoor_ro");
  assert.equal("api_key" in esOut, false);
  assert.equal(esOut.username, "");

  ck.password = "s3cret";
  es.api_key = " a2V5OnZhbHVl ";
  [ckOut, esOut] = utils.formPayload(form).connections;
  assert.equal(ckOut.password, "s3cret");
  assert.equal(esOut.api_key, "a2V5OnZhbHVl");
  assert.notEqual(utils.payloadKey(form), snapshot);

  // 点了「清除」：填过的也不提交
  ck.clear_password = true;
  es.clear_api_key = true;
  [ckOut, esOut] = utils.formPayload(form).connections;
  assert.equal(ckOut.clear_password, true);
  assert.equal("password" in ckOut, false);
  assert.equal(esOut.clear_api_key, true);
  assert.equal("api_key" in esOut, false);
  assert.equal(utils.passwordPlaceholder(ck), "将清除，保存后生效");
  assert.equal(utils.apiKeyPlaceholder(es), "将清除，保存后生效");

  // 认证方式改成无：不带账号和任何密钥（master 清掉已保存的）
  es.auth_type = "none";
  es.username = "elastic";
  esOut = utils.formPayload(form).connections[1];
  assert.equal(esOut.username, "");
  assert.equal("api_key" in esOut, false);
  assert.equal("clear_api_key" in esOut, false);
  assert.equal("password" in esOut, false);
  // 用户名密码：只传密码，API Key 的清除标记不带
  es.auth_type = "basic";
  es.password = "pw";
  esOut = utils.formPayload(form).connections[1];
  assert.equal(esOut.username, "elastic");
  assert.equal(esOut.password, "pw");
  assert.equal("clear_api_key" in esOut, false);

  // ClickHouse 不管表单里是什么都按 basic 提交
  ck.auth_type = "none";
  assert.equal(utils.formPayload(form).connections[0].auth_type, "basic");

  const saved = utils.formFromSettings(settings());
  assert.equal(
    utils.passwordPlaceholder(saved.connections[0]),
    "已保存，留空表示不修改"
  );
  assert.equal(
    utils.apiKeyPlaceholder(saved.connections[1]),
    "已保存，留空表示不修改"
  );
  const fresh = utils.emptyConnection("clickhouse", []);
  assert.equal(utils.passwordPlaceholder(fresh), "没有密码可以不填");
});

test("未保存判断：内容不变时快照一致；只是点开一个集群不算修改", () => {
  const form = validForm();
  const snapshot = utils.payloadKey(form);
  form.clusters.push(utils.emptyCluster("prod-b"));
  assert.equal(utils.payloadKey(form), snapshot);
  form.connections[0].url += "/";
  assert.equal(utils.payloadKey(form), snapshot);
  findCluster(form, "prod-b").note = "测试";
  assert.notEqual(utils.payloadKey(form), snapshot);
});

test("测试连接、扫描只带这一个连接；预览、验证只带用到的日志源、连接和关联字段", () => {
  const form = validForm();
  // 表单别处没填完（日志源缺时间字段）不影响测试连接
  findSource(form, "app").time_field = "";
  const test = utils.testPayload(form, "es");
  assert.equal(test.enabled, false);
  assert.deepEqual(
    test.connections.map(item => item.id),
    ["es"]
  );
  assert.deepEqual(
    [test.sources, test.clusters, test.correlation_keys],
    [[], [], []]
  );
  assert.deepEqual(utils.testPayload(form, "missing").connections, []);

  const preview = utils.previewPayload(form, "prod-a", "ingress");
  assert.equal(preview.enabled, false);
  assert.deepEqual(
    preview.connections.map(item => item.id),
    ["ck"]
  );
  assert.deepEqual(
    preview.sources.map(item => item.id),
    ["ingress"]
  );
  assert.deepEqual(
    preview.correlation_keys.map(item => item.key),
    ["trace_id", "request_id"]
  );
  assert.deepEqual(preview.clusters, [
    {
      env: "prod-a",
      note: "",
      bindings: [
        {
          source: "ingress",
          connection: "",
          target: "logs.ingress_access",
          conditions: [{ field: "env", op: "in", values: ["prod-a"] }],
          note: ""
        }
      ]
    }
  ]);
  // 绑定里单独指定的连接也带上
  findCluster(form, "prod-a").bindings[0].connection = "ck2";
  form.connections.push(utils.emptyConnection("clickhouse", ["ck"]));
  form.connections[2].id = "ck2";
  assert.deepEqual(
    utils
      .previewPayload(form, "prod-a", "ingress")
      .connections.map(item => item.id),
    ["ck", "ck2"]
  );
  assert.deepEqual(utils.previewPayload(form, "prod-b", "ingress").sources, []);

  const verify = utils.verifyPayload(validForm(), "prod-a", "trace_id");
  assert.deepEqual(
    verify.sources.map(item => item.id),
    ["ingress", "app"]
  );
  assert.deepEqual(
    verify.connections.map(item => item.id),
    ["ck", "es"]
  );
  assert.equal(verify.clusters[0].bindings.length, 2);
  const onlyRequest = utils.verifyPayload(validForm(), "prod-a", "request_id");
  assert.deepEqual(
    onlyRequest.sources.map(item => item.id),
    ["ingress"]
  );
  assert.deepEqual(
    onlyRequest.clusters[0].bindings.map(item => item.source),
    ["ingress"]
  );
});

test("校验：合法的配置没有错误", () => {
  assert.deepEqual(utils.validateForm(validForm()), []);
  assert.deepEqual(utils.validateForm(utils.emptyForm()), []);
});

test("校验：连接（ID、名称、地址、认证、请求头、数值范围、ID 重复）", () => {
  const form = validForm();
  const [ck, es] = form.connections;
  ck.id = "CK";
  ck.name = "";
  ck.url = "clickhouse.example.com:8123";
  ck.timeout = 200;
  ck.max_concurrency = 0;
  ck.max_rows_to_read = -1;
  ck.headers.push({ id: 1, name: "X-New", value: "" });
  ck.headers.push({ id: 2, name: "Host", value: "x" });
  es.auth_type = "basic";
  es.username = "";
  const errors = utils.validateForm(form);
  const connections = errors.filter(item => item.section === "connections");
  assert.deepEqual(texts(connections), [
    "连接 CK · ID：只能用小写字母、数字、_ 和 -，以小写字母开头，最长 32 个字符",
    "连接 CK · 名称：请填写",
    "连接 CK · 地址：要以 http:// 或 https:// 开头",
    "连接 CK · 请求头：请填写 X-New 的值",
    "连接 CK · 请求头：Host 由系统设置，不能自定义",
    "连接 CK · 超时：取 5~120 秒 的整数",
    "连接 CK · 并发：取 1~20 的整数",
    "连接 CK · 单次扫描行数上限：填 0（不限）或正整数，最大 10 的 13 次方",
    // Elasticsearch 用户名密码方式只要求账号（同 master，密码可以空）
    "连接 es · 账号：请填写"
  ]);
  assert.equal(connections[0].connection, "CK");
  // 日志源引用的 ck 不存在了
  assert.ok(texts(errors).includes("日志源 ingress · 连接：ck 不存在"));

  const dup = validForm();
  dup.connections[1].id = "ck";
  dup.connections[1].type = "clickhouse";
  assert.ok(texts(utils.validateForm(dup)).includes("连接 ID ck 重复了"));

  // API Key：没保存过、也没填时要填；保存过的可以留空
  const key = validForm();
  key.connections[1].has_api_key = false;
  assert.deepEqual(utils.connectionErrors(key.connections[1]), [
    "API Key：请填写"
  ]);
  key.connections[1].api_key = "abc def";
  assert.deepEqual(utils.connectionErrors(key.connections[1]), [
    "API Key：不能有空格或换行"
  ]);
  // ClickHouse 可以不填账号和密码（default 用户）
  const ckOnly = utils.emptyConnection("clickhouse", []);
  ckOnly.url = "https://clickhouse.example.com:8443";
  assert.deepEqual(utils.connectionErrors(ckOnly), []);
  ckOnly.username = "a b";
  assert.deepEqual(utils.connectionErrors(ckOnly), [
    "账号：不能有空格，最长 128 个字符"
  ]);
  // 改了名称的请求头不能沿用原来的值；改回原名（不区分大小写）可以
  const header = validForm().connections[0];
  header.headers[0].name = "X-Other";
  assert.deepEqual(utils.headerErrors(header), ["请填写 X-Other 的值"]);
  header.headers[0].name = "x-org";
  assert.deepEqual(utils.headerErrors(header), []);
});

test("校验：地址格式（同链路追踪）", () => {
  const cases = [
    ["ftp://clickhouse:8123", "要以 http:// 或 https:// 开头"],
    ["http://clickhouse:8123/?database=logs", "不能带查询参数（?）或 #"],
    ["http://clickhouse 8123", "不能有空格或换行"],
    [`http://es/${"a".repeat(2050)}`, "不能超过 2048 个字符"],
    ["http://:9200", "格式不正确"]
  ];
  for (const [url, error] of cases)
    assert.equal(utils.urlError(url, true), error, url);
  // ES 可以带路径前缀
  assert.equal(utils.urlError("https://gateway.example.com/es/", true), "");
  assert.equal(utils.urlError("", true), "请填写");
});

test("校验：关联字段（key、名称、取值格式、链路 ID 最多一个、重复）", () => {
  const form = validForm();
  form.correlation_keys.push(
    utils.keyForm({ key: "Bad-Key", label: "", pattern: "^abc$" }),
    utils.keyForm({
      key: "span_id",
      label: "Span ID 很长很长很长很长很长很长很长很长很长很长很长很长",
      pattern: "[0-9a-f{16"
    }),
    utils.keyForm({ key: "request_id", label: "重复", trace: true })
  );
  const errors = utils.validateForm(form);
  assert.deepEqual(texts(errors), [
    "关联字段 Bad-Key · Key：只能用小写字母、数字和 _，以小写字母开头，最长 32 个字符",
    "关联字段 Bad-Key · 名称：请填写",
    "关联字段 Bad-Key · 取值格式：不要写 ^ 和 $（会嵌进别的正则里用）",
    "关联字段 span_id · 名称：最多 30 个字",
    "关联字段 span_id · 取值格式：不是合法的正则表达式",
    "关联字段 request_id 重复了",
    "只能有一个关联字段是链路 ID"
  ]);
  assert.ok(errors.every(item => item.section === "correlations"));
  // 结尾转义的 \$ 可以；字符类里的 ^ 可以；Python 的命名组能识别
  assert.equal(utils.patternError("price\\$"), "");
  assert.equal(utils.patternError("[^\\s]+"), "");
  assert.equal(utils.patternError("(?P<id>[0-9]+)"), "");
  assert.equal(
    utils.patternError("abc$"),
    "不要写 ^ 和 $（会嵌进别的正则里用）"
  );
  assert.equal(utils.regexError("(?i)error"), "");
  assert.equal(utils.regexError("a".repeat(201)), "最长 200 个字符");
});

test("校验：日志源（必填、字段名、关联字段的列 / 正文写法、字段说明、打码、默认表）", () => {
  const form = validForm();
  const ingress = findSource(form, "ingress");
  ingress.time_field = "";
  ingress.fields.path = "request path";
  ingress.correlations.push(
    { rowId: 1, key: "span_id", mode: "field", field: "span_id", text: "" },
    { rowId: 2, key: "trace_id", mode: "text", field: "", text: "TID:" }
  );
  ingress.columns.push(
    { rowId: 3, name: "upstream_cluster", description: "重复" },
    { rowId: 4, name: "", description: "没有名称" }
  );
  ingress.mask_patterns = ["(unclosed"];
  ingress.default_target = "ingress_access";
  const app = findSource(form, "app");
  app.correlations[0].text = "TID:{value} SID:{value}";
  app.fields.message = "";
  app.level_values.warn = ["W".repeat(51)];
  const errors = utils.validateForm(form);
  assert.deepEqual(texts(errors), [
    "日志源 ingress · 时间字段：请填写",
    "日志源 ingress · 路径字段：request path 不是合法的字段名（只能用字母、数字和 _ . @ -，以字母、_ 或 @ 开头）",
    "日志源 ingress · 关联字段 span_id：在「关联字段」卡片里还没有，先在那里添加",
    // ingress 已经用列登记过 trace_id
    "日志源 ingress · 关联字段 trace_id：登记了两次",
    "日志源 ingress · 关联字段 trace_id：正文写法要包含一个 {value}",
    "日志源 ingress · 关联字段 trace_id：用正文写法要先配置「正文」字段",
    "日志源 ingress · 字段说明：upstream_cluster 重复了",
    "日志源 ingress · 字段说明：有一行写了说明但没有字段名",
    "日志源 ingress · 打码正则：(unclosed 不是合法的正则表达式",
    "日志源 ingress · 默认表 / 索引：ingress_access 要写成 库.表（字母、数字和下划线，不能以数字开头），例如 logs.ingress_access",
    `日志源 app · 级别取值 warn：${"W".repeat(20)} 太长了（每个最多 50 个字，不能换行）`,
    "日志源 app · 关联字段 trace_id：正文写法只能有一个 {value}",
    "日志源 app · 关联字段 trace_id：用正文写法要先配置「正文」字段"
  ]);
  assert.equal(errors[0].section, "sources");
  assert.equal(errors[0].source, "ingress");

  const other = validForm();
  findSource(other, "app").id = "app";
  findSource(other, "ingress").id = "app";
  findSource(other, "app").connection = "";
  const texts2 = texts(utils.validateForm(other));
  assert.ok(texts2.includes("日志源 ID app 重复了"));
  assert.ok(texts2.includes("日志源 app · 连接：请选择"));

  const limits = validForm();
  const source = findSource(limits, "app");
  source.default_fields = Array.from({ length: 51 }, (_, i) => `f${i}`);
  source.mask_patterns = Array.from({ length: 21 }, (_, i) => `p${i}`);
  for (let i = 0; i < 11; i++)
    limits.correlation_keys.push(
      utils.keyForm({ key: `k${i}`, label: `k${i}` })
    );
  source.correlations = Array.from({ length: 11 }, (_, i) => ({
    rowId: 100 + i,
    key: `k${i}`,
    mode: "field",
    field: `f${i}`,
    text: ""
  }));
  source.description = "说".repeat(8001);
  assert.deepEqual(utils.sourceErrors(limits, source), [
    "说明：最多 8000 个字",
    "关联字段：最多 10 个",
    "默认返回字段：最多 50 个",
    "打码正则：最多 20 个"
  ]);
});

test("校验：集群绑定（日志源、连接类型、表 / 索引、过滤条件、重复绑定）", () => {
  const form = validForm();
  const prod = findCluster(form, "prod-a");
  prod.bindings[0].conditions = [
    { field: "env", op: "eq", values: ["prod-a", "prod-b"] },
    { field: "env name", op: "in", values: [] },
    { field: "", op: "prefix", values: ["x"] }
  ];
  prod.bindings[1].connection = "ck";
  prod.bindings.push(utils.bindingForm({ source: "ghost", target: "logs.x" }));
  prod.bindings.push(
    utils.bindingForm({ source: "ingress", target: "logs.ingress_access" })
  );
  findCluster(form, "old-cluster").bindings[0].target = "logs.a.b";
  const errors = utils.validateForm(form);
  assert.deepEqual(texts(errors), [
    "集群 prod-a · 绑定 ingress · 过滤条件 env：要填一个值",
    "集群 prod-a · 绑定 ingress · 过滤条件 env name：env name 不是合法的字段名（只能用字母、数字和 _ . @ -，以字母、_ 或 @ 开头）",
    "集群 prod-a · 绑定 ingress · 过滤条件 env name：至少填一个值",
    "集群 prod-a · 绑定 ingress · 过滤条件 3：请填写字段",
    "集群 prod-a · 绑定 app · 连接：要和日志源的连接同类型（Elasticsearch）",
    "集群 prod-a · 绑定 ghost · 日志源：ghost 不存在",
    "集群 prod-a · 日志源 ingress 绑定了两次，一个集群里每个日志源只能绑定一次",
    "集群 old-cluster · 绑定 ingress · 表 / 索引：logs.a.b 要写成 库.表（字母、数字和下划线，不能以数字开头），例如 logs.ingress_access"
  ]);
  assert.ok(errors.every(item => item.section === "clusters"));
  assert.equal(errors[0].env, "prod-a");
  assert.equal(errors[errors.length - 1].env, "old-cluster");

  const many = validForm();
  const binding = findCluster(many, "prod-a").bindings[0];
  binding.conditions = Array.from({ length: 6 }, () => ({
    field: "env",
    op: "in",
    values: Array.from({ length: 51 }, (_, i) => `v${i}`)
  }));
  binding.note = "注".repeat(2001);
  const problems = utils.bindingErrors(many, binding);
  assert.equal(problems[0], "过滤条件：最多 5 个");
  assert.ok(problems.includes("过滤条件 env：最多 50 个值"));
  assert.equal(problems[problems.length - 1], "说明：最多 2000 个字");
  binding.conditions = [{ field: "env", op: "eq", values: ["长".repeat(201)] }];
  binding.note = "";
  assert.deepEqual(utils.bindingErrors(many, binding), [
    `过滤条件 env：${"长".repeat(20)}… 太长了（每个值最多 200 个字）`
  ]);
  // 数字值可以
  binding.conditions = [{ field: "status", op: "in", values: [500, "502"] }];
  assert.deepEqual(utils.bindingErrors(many, binding), []);

  // 集群名：不能有空白；只有空集群（没有内容）不校验
  const env = validForm();
  env.clusters.push({ env: "bad env", note: "x", bindings: [] });
  env.clusters.push({ env: "bad two", note: "", bindings: [] });
  assert.deepEqual(texts(utils.validateForm(env)), [
    "集群 bad env · 集群名不能有空格"
  ]);
  assert.equal(utils.envError(""), "集群名不能为空");
  assert.equal(utils.envError("x".repeat(101)), "集群名最长 100 个字符");
});

test("校验：和 master 一致的其他规则（长度、数量、重复登记、正文写法、取值格式、启用时要有连接）", () => {
  // 密码、API Key 的长度；清除时不看填的密码
  const conn = validForm().connections[0];
  conn.password = "p".repeat(257);
  assert.deepEqual(utils.connectionErrors(conn), [
    "密码：不能换行，最长 256 个字符"
  ]);
  conn.clear_password = true;
  assert.deepEqual(utils.connectionErrors(conn), []);
  const es = validForm().connections[1];
  es.api_key = "k".repeat(1025);
  assert.deepEqual(utils.connectionErrors(es), ["API Key：最长 1024 个字符"]);
  es.api_key = "";
  es.auth_type = "basic";
  es.username = "elastic";
  assert.deepEqual(utils.connectionErrors(es), []);
  const rows = validForm().connections[0];
  rows.max_rows_to_read = 10 ** 13;
  assert.deepEqual(utils.connectionErrors(rows), []);
  rows.max_rows_to_read = 10 ** 13 + 1;
  assert.equal(utils.connectionErrors(rows).length, 1);

  // 取值格式不能匹配空字符串
  assert.equal(
    utils.patternError("[0-9a-f]*"),
    "不能匹配空字符串（例如 .* 要改成 .+）"
  );
  assert.equal(utils.patternError("[0-9a-f]+"), "");

  // 同一个日志源里一个关联字段只能登记一次；正文写法要有固定的文字、不能换行
  const form = validForm();
  const ingress = findSource(form, "ingress");
  ingress.fields.message = "message";
  ingress.correlations.push({
    rowId: 1,
    key: "trace_id",
    mode: "text",
    field: "",
    text: " {value} "
  });
  const app = findSource(form, "app");
  app.correlations[0].text = "TID:\n{value}";
  assert.deepEqual(texts(utils.validateForm(form)), [
    "日志源 ingress · 关联字段 trace_id：登记了两次",
    "日志源 ingress · 关联字段 trace_id：正文写法除了 {value} 还要有固定的文字，例如 TID:{value}",
    "日志源 app · 关联字段 trace_id：正文写法最多 100 个字，不能换行"
  ]);

  // 过滤条件的值不能换行
  const cond = validForm();
  findCluster(cond, "prod-a").bindings[0].conditions[0].values = ["prod\na"];
  assert.deepEqual(texts(utils.validateForm(cond)), [
    "集群 prod-a · 绑定 ingress · 过滤条件 env：取值不能换行"
  ]);

  // 数量上限
  const many = validForm();
  for (let index = 0; index < 19; index++) {
    const item = utils.emptyConnection(
      "clickhouse",
      many.connections.map(c => c.id)
    );
    item.url = "http://clickhouse.example.com:8123";
    many.connections.push(item);
  }
  for (let index = 0; index < 49; index++)
    many.correlation_keys.push(
      utils.keyForm({ key: `k${index}`, label: `字段 ${index}` })
    );
  findCluster(many, "prod-a").bindings.push(
    ...Array.from({ length: 49 }, () =>
      utils.bindingForm({ source: "app", target: "app-logs-*" })
    )
  );
  const limits = texts(utils.validateForm(many));
  assert.equal(limits[0], "连接最多 20 个（现在 21 个）");
  assert.equal(limits[1], "关联字段最多 50 个（现在 51 个）");
  assert.ok(limits.includes("集群 prod-a · 绑定最多 50 个"));

  // 已经启用时至少要有一个连接（没启用时可以全删）
  const empty = utils.emptyForm();
  assert.deepEqual(utils.validateForm(empty), []);
  empty.enabled = true;
  assert.deepEqual(utils.validateForm(empty), [
    {
      section: "connections",
      text: "已经启用了，至少要有一个连接（不用了可以先关掉右上角的开关）"
    }
  ]);
});

test("校验：网络架构说明长度、整份配置大小", () => {
  const form = validForm();
  form.architecture = "架".repeat(20001);
  assert.deepEqual(utils.validateForm(form), [
    { section: "architecture", text: "网络架构说明：最多 20000 个字" }
  ]);
  // 按字符数算（同 master）：每个日志源 300 项 × 300 字（约 10 万字），11 个就超过 1MB
  const big = validForm();
  for (let index = 0; index < 11; index++) {
    const source = utils.sourceForm({
      ...settings().sources[0],
      id: `big${index}`,
      correlations: []
    });
    source.columns = Array.from({ length: 300 }, (_, i) => ({
      rowId: i,
      name: `c${i}`,
      description: "说".repeat(300)
    }));
    big.sources.push(source);
  }
  const errors = utils.validateForm(big);
  assert.equal(errors.length, 1);
  assert.match(errors[0].text, /^配置太大了（约 1\.\d+ MB，上限 1 MB）/);
});

test("表 / 索引的格式：ClickHouse 库.表，Elasticsearch 索引列表", () => {
  const ok = [
    ["clickhouse", "logs.ingress_access"],
    ["clickhouse", "_logs.T1"],
    ["elasticsearch", "app-logs-*"],
    ["elasticsearch", "app-logs-*, -app-logs-debug ,nginx-2026.11.*"],
    ["elasticsearch", "logs-app+prod"]
  ];
  for (const [type, target] of ok)
    assert.equal(utils.targetError(type, target), "", target);
  const bad = [
    ["clickhouse", "", "请填写表名（库.表）"],
    [
      "clickhouse",
      "ingress_access",
      "ingress_access 要写成 库.表（字母、数字和下划线，不能以数字开头），例如 logs.ingress_access"
    ],
    [
      "clickhouse",
      "1logs.access",
      "1logs.access 要写成 库.表（字母、数字和下划线，不能以数字开头），例如 logs.ingress_access"
    ],
    ["clickhouse", `${"d".repeat(65)}.access`, "库名最长 64 个字符"],
    ["elasticsearch", " , ", "请填写索引，例如 app-logs-*"],
    [
      "elasticsearch",
      "remote:app-logs-*",
      "remote:app-logs-* 是跨集群索引（带 :），不支持"
    ],
    ["elasticsearch", ".security", ".security 是 . 开头的系统索引，不支持"],
    ["elasticsearch", "-.kibana", "-.kibana 是 . 开头的系统索引，不支持"],
    [
      "elasticsearch",
      "App-Logs",
      "App-Logs 不是合法的索引名（只能用小写字母、数字和 _ . * + -），例如 app-logs-*"
    ],
    [
      "elasticsearch",
      Array.from({ length: 21 }, (_, i) => `logs-${i}`).join(","),
      "最多 20 个索引"
    ]
  ];
  for (const [type, target, error] of bad)
    assert.equal(utils.targetError(type, target), error, target);
  assert.equal(utils.normalizeTarget(" a-* ,, b-* "), "a-*,b-*");
});

test("字段名：字母、数字和 _ . @ -，以字母、_ 或 @ 开头，最长 200", () => {
  for (const name of [
    "@timestamp",
    "kubernetes.pod.name",
    "_id",
    "log-level",
    "X_Forwarded_For"
  ])
    assert.equal(utils.fieldNameError(name), "", name);
  assert.equal(utils.fieldNameError(" "), "请填写字段名");
  assert.equal(
    utils.fieldNameError("1abc"),
    "1abc 不是合法的字段名（只能用字母、数字和 _ . @ -，以字母、_ 或 @ 开头）"
  );
  assert.match(utils.fieldNameError("a b"), /不是合法的字段名/);
  assert.match(utils.fieldNameError("a$b"), /不是合法的字段名/);
  assert.match(
    utils.fieldNameError("a".repeat(201)),
    /太长了（最多 200 个字符）/
  );
});

test("过滤条件的文字", () => {
  assert.equal(
    utils.conditionText({ field: "env", op: "in", values: ["prod-a"] }),
    "env ∈ {prod-a}"
  );
  assert.equal(
    utils.conditionText({ field: "env", op: "not_in", values: ["a", "b"] }),
    "env ∉ {a, b}"
  );
  assert.equal(
    utils.conditionText({ field: "status", op: "eq", values: [500] }),
    "status = 500"
  );
  assert.equal(
    utils.conditionText({ field: "env", op: "ne", values: ["test"] }),
    "env ≠ test"
  );
  assert.equal(
    utils.conditionText({ field: "host", op: "prefix", values: ["api."] }),
    "host 以 api. 开头"
  );
  assert.equal(
    utils.conditionsText([
      { field: "env", op: "in", values: ["prod-a"] },
      { field: "app", op: "eq", values: ["gateway"] }
    ]),
    "env ∈ {prod-a} 且 app = gateway"
  );
  assert.equal(utils.conditionsText([]), "");
  assert.equal(utils.isMultiOp("in"), true);
  assert.equal(utils.isMultiOp("prefix"), false);
});

test("集群列表：已登记的按名称排序，配置里有但已不在的标未登记、排在后面", () => {
  const form = validForm();
  const rows = utils.clusterRows(form, settings().clusters_known);
  assert.deepEqual(
    rows.map(row => [
      row.env,
      row.registered,
      row.online,
      row.bindings,
      row.hasNote
    ]),
    [
      ["prod-a", true, true, 2, true],
      ["prod-b", true, false, 0, false],
      ["old-cluster", false, false, 1, false]
    ]
  );
  assert.deepEqual(rows[0].aliases, ["prod-a-k8s"]);
  assert.deepEqual(utils.clusterRows(utils.emptyForm(), null), []);
});

test("每个日志源绑定了几个集群；删除日志源时一并删除各集群里它的绑定（不改原表单）", () => {
  const form = validForm();
  assert.deepEqual(utils.sourceUsage(form), { ingress: 2, app: 1 });
  assert.deepEqual(utils.boundEnvs(form, "ingress"), ["prod-a", "old-cluster"]);
  const next = utils.removeSource(form, "ingress");
  assert.deepEqual(
    next.sources.map(item => item.id),
    ["app"]
  );
  assert.deepEqual(
    next.clusters.map(item => [item.env, item.bindings.map(b => b.source)]),
    [
      ["prod-a", ["app"]],
      ["old-cluster", []]
    ]
  );
  assert.deepEqual(utils.sourceUsage(next), { app: 1 });
  // 原表单不变
  assert.equal(form.sources.length, 2);
  assert.equal(findCluster(form, "prod-a").bindings.length, 2);
});

test("编辑日志源：按 rowId 写回，改了 ID 同步各集群的绑定；新的追加", () => {
  const form = validForm();
  const edited = utils.cloneDeep(findSource(form, "ingress"));
  edited.id = "gateway";
  edited.name = "网关访问日志";
  const next = utils.upsertSource(form, edited, "ingress");
  assert.deepEqual(
    next.sources.map(item => item.id),
    ["gateway", "app"]
  );
  assert.deepEqual(
    next.clusters.flatMap(item => item.bindings.map(b => b.source)),
    ["gateway", "app", "gateway"]
  );
  assert.equal(findSource(form, "ingress").name, "入口网关访问日志");
  const added = utils.upsertSource(next, utils.emptySource(next), null);
  assert.deepEqual(
    added.sources.map(item => item.id),
    ["gateway", "app", "logs"]
  );
});

test("连接：被日志源、绑定引用时列出来；没保存过的连接改 ID 时同步引用", () => {
  const form = validForm();
  findCluster(form, "prod-a").bindings[0].connection = "ck";
  assert.deepEqual(utils.connectionUsage(form, "ck"), {
    sources: ["ingress"],
    bindings: ["prod-a / ingress"]
  });
  assert.deepEqual(utils.connectionUsage(form, "none"), {
    sources: [],
    bindings: []
  });
  const edited = utils.cloneDeep(form.connections[0]);
  edited.id = "ck-logs";
  const next = utils.upsertConnection(form, edited, "ck");
  assert.equal(next.connections[0].id, "ck-logs");
  assert.equal(findSource(next, "ingress").connection, "ck-logs");
  assert.equal(findCluster(next, "prod-a").bindings[0].connection, "ck-logs");
  assert.equal(findSource(form, "ingress").connection, "ck");
  const removed = utils.removeConnection(next, "es");
  assert.deepEqual(
    removed.connections.map(item => item.id),
    ["ck-logs"]
  );
  const added = utils.upsertConnection(
    form,
    utils.emptyConnection("elasticsearch", ["ck", "es"]),
    null
  );
  assert.deepEqual(
    added.connections.map(item => item.id),
    ["ck", "es", "es-2"]
  );
});

test("关联字段：改 key 同步日志源里的登记，删除时连带删除登记，链路 ID 只能有一个", () => {
  const form = validForm();
  assert.deepEqual(utils.keyUsage(form, "trace_id"), ["ingress", "app"]);
  const edited = utils.cloneDeep(form.correlation_keys[1]);
  edited.key = "req_id";
  edited.trace = true;
  const next = utils.upsertKey(form, edited, "request_id");
  assert.deepEqual(
    next.correlation_keys.map(item => [item.key, item.trace]),
    [
      ["trace_id", false],
      ["req_id", true]
    ]
  );
  assert.deepEqual(
    findSource(next, "ingress").correlations.map(item => item.key),
    ["req_id", "trace_id"]
  );
  assert.equal(form.correlation_keys[0].trace, true);

  const removed = utils.removeKey(form, "trace_id");
  assert.deepEqual(
    removed.correlation_keys.map(item => item.key),
    ["request_id"]
  );
  assert.deepEqual(
    findSource(removed, "ingress").correlations.map(item => item.key),
    ["request_id"]
  );
  assert.deepEqual(findSource(removed, "app").correlations, []);

  const traced = utils.setTraceKey(form, "request_id", true);
  assert.deepEqual(
    traced.correlation_keys.map(item => item.trace),
    [false, true]
  );
  const off = utils.setTraceKey(form, "trace_id", false);
  assert.deepEqual(
    off.correlation_keys.map(item => item.trace),
    [false, false]
  );
});

test("绑定和集群：按 rowId 写回，集群还没有时新建；删除未登记的集群", () => {
  const form = validForm();
  const binding = utils.bindingForm({
    source: "app",
    target: "app-logs-*",
    conditions: [{ field: "k8s_cluster", op: "in", values: ["prod-b"] }]
  });
  let next = utils.upsertBinding(form, "prod-b", binding);
  assert.deepEqual(
    findCluster(next, "prod-b").bindings.map(item => item.source),
    ["app"]
  );
  const edited = { ...binding, note: "只有应用日志" };
  next = utils.upsertBinding(next, "prod-b", edited);
  assert.equal(findCluster(next, "prod-b").bindings.length, 1);
  assert.equal(findCluster(next, "prod-b").bindings[0].note, "只有应用日志");
  next = utils.removeBinding(next, "prod-b", binding.rowId);
  assert.deepEqual(findCluster(next, "prod-b").bindings, []);
  next = utils.removeCluster(next, "old-cluster");
  assert.deepEqual(
    next.clusters.map(item => item.env),
    ["prod-a", "prod-b"]
  );
  assert.equal(form.clusters.length, 2);
});

test("ID：小写短名，重复时加 -2，最长 32", () => {
  assert.equal(utils.slugId("Ingress Access"), "ingress-access");
  assert.equal(utils.slugId("2026_logs"), "source-2026_logs");
  assert.equal(utils.slugId("日志"), "source");
  assert.equal(utils.slugId("", "logs"), "logs");
  assert.equal(utils.uniqueId("ingress", ["ingress"]), "ingress-2");
  assert.equal(
    utils.uniqueId("ingress", ["ingress", "ingress-2"]),
    "ingress-3"
  );
  const long = "a".repeat(40);
  assert.equal(utils.uniqueId(long, []), "a".repeat(32));
  assert.equal(utils.uniqueId(long, ["a".repeat(32)]), `${"a".repeat(30)}-2`);
  assert.equal(utils.slugKey("X-Request-Id"), "x_request_id");
  assert.equal(utils.slugKey("traceId"), "trace_id");
  assert.equal(utils.slugKey("9id"), "id_9id");
  assert.equal(utils.correlationKeyOf("X-Request-Id"), "request_id");
  assert.equal(utils.correlationKeyOf("traceId"), "trace_id");
  assert.equal(utils.correlationKeyOf("TID"), "trace_id");
  assert.equal(utils.correlationKeyOf("spanId"), "span_id");
  assert.equal(utils.correlationKeyOf("order_no"), "order_no");
  assert.equal(utils.textIdName("TID:{value}"), "TID");
  assert.equal(utils.textIdName('"traceId":"{value}"'), "traceId");
  assert.equal(utils.patternForShape("hex32"), "[0-9a-fA-F]{32}");
  assert.match(utils.patternForShape("uuid"), /^\[0-9a-fA-F\]\{8\}-/);
  assert.equal(utils.patternForShape("other"), "");
});

test("新建绑定的默认值：有扫描建议用建议，否则按集群字段出条件（取值能对上集群名或别名时用表里的写法）", () => {
  const form = validForm();
  const ingress = findSource(form, "ingress");
  const scan = scanResult();
  // 扫描有这个集群的建议：用建议的表和条件
  const suggested = utils.defaultBinding(ingress, "prod-b", scan);
  assert.equal(suggested.source, "ingress");
  assert.equal(suggested.target, "logs.ingress_access");
  assert.deepEqual(suggested.conditions, [
    { field: "env", op: "in", values: ["PROD-B"] }
  ]);
  // 没有建议：按集群字段，取值不区分大小写对上集群名
  const byValue = utils.defaultBinding(ingress, "test", scan);
  assert.deepEqual(byValue.conditions, [
    { field: "env", op: "in", values: ["test"] }
  ]);
  // 对不上时用集群名；别名也算
  assert.deepEqual(utils.defaultBinding(ingress, "prod-c", scan).conditions, [
    { field: "env", op: "in", values: ["prod-c"] }
  ]);
  const aliasScan = scanResult();
  aliasScan.suggested_bindings = [];
  aliasScan.groups[0].targets[0].values.env = ["Prod-A-K8s"];
  assert.deepEqual(
    utils.defaultBinding(ingress, "prod-a", aliasScan).conditions,
    [{ field: "env", op: "in", values: ["Prod-A-K8s"] }]
  );
  // 没有扫描结果 / 扫描的不是这个连接：用集群名
  assert.deepEqual(utils.defaultBinding(ingress, "prod-b", null).conditions, [
    { field: "env", op: "in", values: ["prod-b"] }
  ]);
  const app = findSource(form, "app");
  assert.deepEqual(utils.defaultBinding(app, "prod-b", scan).conditions, [
    { field: "k8s_cluster", op: "in", values: ["prod-b"] }
  ]);
  assert.equal(utils.defaultBinding(app, "prod-b", scan).target, "app-logs-*");
  // 没有集群字段：没有条件（整张表 / 整个索引）
  app.cluster_field = "";
  assert.deepEqual(utils.defaultBinding(app, "prod-b", null).conditions, []);
});

test("扫描结果的候选：字段（时间类型在前）、同组的表、取值", () => {
  const scan = scanResult();
  const form = validForm();
  const group = utils.scanGroupFor(
    { ck: scan },
    form,
    findSource(form, "ingress")
  );
  assert.equal(group.ref, "g1");
  assert.equal(utils.scanGroupFor({}, form, findSource(form, "ingress")), null);
  // 默认表不在扫描结果里时按绑定的表找；本地表也认
  const source = { ...findSource(form, "ingress"), default_target: "" };
  assert.equal(utils.scanGroupFor({ ck: scan }, form, source).ref, "g1");
  assert.equal(
    utils.groupOfTarget(scan, "logs.ingress_access_local").ref,
    "g1"
  );
  assert.equal(utils.groupOfTarget(scan, "logs.none"), null);

  const options = utils.columnOptions(scan.groups[1], true);
  assert.deepEqual(options[0], { value: "timestamp", note: "DateTime" });
  assert.deepEqual(
    utils
      .columnOptions(scan.groups[0])
      .map(item => item.value)
      .slice(0, 2),
    ["start_time", "env"]
  );
  assert.deepEqual(utils.targetOptions(scan.groups[0]), [
    { value: "logs.ingress_access", note: "1.2 亿 行 · 保留 15 天" },
    { value: "logs.ingress_access_v2", note: "1000 行 · 保留 30 天" }
  ]);
  assert.deepEqual(utils.valueOptions(scan, "logs.ingress_access", "env"), [
    "prod-a",
    "PROD-B",
    "test"
  ]);
  // 这个表没有取值时用同组其他表的
  delete scan.groups[0].targets[1].values;
  assert.deepEqual(utils.valueOptions(scan, "logs.ingress_access_v2", "env"), [
    "prod-a",
    "PROD-B",
    "test"
  ]);
  assert.deepEqual(utils.valueOptions(scan, "logs.app_log", "level"), []);
  assert.equal(utils.isTimeType("Nullable(DateTime64(3))"), true);
  assert.equal(utils.isTimeType("date_nanos"), true);
  assert.equal(utils.isTimeType("String"), false);
});

test("按规则生成草稿：只自动加请求追踪类 ID，正文写法优先于 ES 运行时字段", () => {
  const form = utils.emptyForm();
  form.connections.push(
    utils.connectionForm({ id: "es", type: "elasticsearch" }, true)
  );
  const scan = {
    connection: { id: "es", type: "elasticsearch", version: "9.5.2" },
    partial: false,
    notes: [],
    clusters: [],
    suggested_bindings: [],
    groups: [
      {
        ref: "g1",
        name_guess: "app-node",
        kind_guess: "app",
        columns: [],
        fields_guess: { time: "@timestamp", message: "message" },
        cluster_field_guess: "",
        id_fields: [
          { field: "x_request_id", shape: "uuid" },
          { field: "ec_accessId", shape: "hex24", runtime: true }
        ],
        text_ids: [
          { text: '"accessId":"{value}"', shape: "hex24", count: 18 },
          { text: "TID:{value}", shape: "hex32", count: 9 },
          { text: "productId={value}", shape: "hex20", count: 30 },
          { text: "sign={value}", shape: "hex32", count: 2 }
        ],
        meta_description: "",
        targets: [{ target: "app-node", rows: 10, values: {}, samples: [] }]
      }
    ]
  };
  const draft = utils.draftFromScan(scan, ["g1"], form);
  assert.deepEqual(draft.sources[0].correlations, [
    { key: "request_id", field: "x_request_id" },
    { key: "access_id", text: '"accessId":"{value}"' },
    { key: "trace_id", text: "TID:{value}" }
  ]);
  assert.deepEqual(
    draft.correlation_keys.map(item => [item.key, item.trace]),
    [
      ["request_id", false],
      ["access_id", false],
      ["trace_id", true]
    ]
  );
});

test("按规则生成草稿：字段用扫描的猜测，绑定用扫描的建议，ID 字段生成关联字段", () => {
  const form = utils.emptyForm();
  form.connections.push(
    utils.connectionForm({ id: "ck", type: "clickhouse" }, true)
  );
  const draft = utils.draftFromScan(scanResult(), ["g1", "g2"], form);
  assert.equal(draft.origin, "rules");
  assert.equal(draft.connection, "ck");
  assert.deepEqual(
    draft.sources.map(item => [
      item.group,
      item.id,
      item.kind,
      item.default_target
    ]),
    [
      ["g1", "ingress_access", "access", "logs.ingress_access"],
      ["g2", "app_log", "app", "logs.app_log"]
    ]
  );
  const [ingress, app] = draft.sources;
  assert.equal(ingress.time_field, "start_time");
  assert.deepEqual(ingress.fields, {
    status: "response_code",
    duration: "duration",
    path: "path"
  });
  assert.equal(ingress.cluster_field, "env");
  assert.equal(ingress.duration_unit, "ms");
  assert.deepEqual(ingress.correlations, [
    { key: "request_id", field: "x_request_id" }
  ]);
  assert.deepEqual(app.fields, {
    message: "msg",
    level: "level",
    pod: "podname"
  });
  assert.deepEqual(app.correlations, [
    { key: "trace_id", text: "TID:{value}" }
  ]);
  assert.deepEqual(
    draft.correlation_keys.map(item => [
      item.key,
      item.label,
      item.trace,
      item.pattern
    ]),
    [
      [
        "request_id",
        "请求 ID",
        false,
        "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
      ],
      ["trace_id", "链路 ID", true, "[0-9a-fA-F]{32}"]
    ]
  );
  assert.deepEqual(
    draft.bindings.map(item => [
      item.env,
      item.source,
      item.target,
      item.match
    ]),
    [
      ["prod-a", "ingress_access", "logs.ingress_access", "exact"],
      ["prod-b", "ingress_access", "logs.ingress_access", "exact"],
      ["test", "app_log", "logs.app_log", "table"]
    ]
  );
  // 只勾选一组：只生成它和它的绑定
  const one = utils.draftFromScan(scanResult(), ["g2"], form);
  assert.deepEqual(
    one.sources.map(item => item.id),
    ["app_log"]
  );
  assert.deepEqual(
    one.bindings.map(item => item.env),
    ["test"]
  );
});

test("按规则生成草稿：已有的关联字段不重复生成；组里的表已经是某个日志源的默认表时用它的 ID", () => {
  const form = validForm();
  const draft = utils.draftFromScan(scanResult(), ["g1", "g2"], form);
  // g1 的 logs.ingress_access 是 ingress 的默认表
  assert.deepEqual(
    draft.sources.map(item => item.id),
    ["ingress", "app_log"]
  );
  assert.deepEqual(draft.correlation_keys, []);
  assert.deepEqual(
    draft.bindings.map(item => item.source),
    ["ingress", "ingress", "app_log"]
  );
  // 名字和已有日志源 ID 重复的组：加 -2
  const clash = validForm();
  findSource(clash, "ingress").default_target = "logs.other";
  const scan = scanResult();
  scan.groups[0].name_guess = "ingress";
  assert.equal(
    utils.draftFromScan(scan, ["g1"], clash).sources[0].id,
    "ingress-2"
  );
});

test("AI 生成说明的请求体：只放勾选的组，限制表 / 样本 / 取值的数量，existing 只放名字", () => {
  const scan = scanResult();
  scan.groups[0].targets = Array.from({ length: 25 }, (_, index) => ({
    target: `logs.t${index}`,
    rows: index,
    values: { env: Array.from({ length: 15 }, (_, i) => `v${i}`) },
    samples: [{ a: 1 }, { a: 2 }, { a: 3 }]
  }));
  const form = validForm();
  const { payload, dropped, tooLarge, bytes } = utils.describePayload(
    scan,
    ["g1"],
    form,
    "  入口有外部 nginx  "
  );
  assert.deepEqual(dropped, []);
  assert.equal(tooLarge, false);
  assert.ok(bytes > 0);
  assert.deepEqual(
    payload.scan.groups.map(item => item.ref),
    ["g1"]
  );
  assert.deepEqual(payload.scan.connection, scan.connection);
  const targets = payload.scan.groups[0].targets;
  assert.equal(targets.length, 20);
  assert.equal(targets[0].samples.length, 2);
  assert.ok(targets.slice(1).every(item => !("samples" in item)));
  assert.ok(targets.every(item => item.values.env.length === 10));
  assert.deepEqual(payload.clusters, scan.clusters);
  assert.deepEqual(payload.existing, {
    sources: [
      { id: "ingress", name: "入口网关访问日志" },
      { id: "app", name: "Java 应用日志" }
    ],
    correlation_keys: [
      { key: "trace_id", label: "链路 ID" },
      { key: "request_id", label: "请求 ID" }
    ],
    architecture: "- 入口网关（ingress）→ 服务（app）"
  });
  assert.equal(payload.hint, "入口有外部 nginx");
  // 只给发送的组的绑定建议；扫描的说明也带上
  assert.deepEqual(
    payload.scan.suggested_bindings.map(item => [item.env, item.group]),
    [
      ["prod-a", "g1"],
      ["prod-b", "g1"]
    ]
  );
  assert.equal(payload.scan.partial, false);
  assert.deepEqual(payload.scan.notes, []);
  // 一次最多 100 组
  const wide = scanResult();
  wide.groups = Array.from({ length: 102 }, (_, index) => ({
    ...scanResult().groups[1],
    ref: `g${index}`
  }));
  const limited = utils.describePayload(
    wide,
    wide.groups.map(group => group.ref),
    form,
    ""
  );
  assert.equal(limited.payload.scan.groups.length, 100);
  assert.deepEqual(limited.dropped, [
    "一次最多给 100 组生成说明，只发送了前 100 组（共勾选 102 组），其余的分批再生成"
  ]);
  // 原来的扫描结果不变
  assert.equal(scan.groups[0].targets.length, 25);
  assert.equal(scan.groups[0].targets[1].samples.length, 3);
  assert.equal(
    utils.describePayload(scan, ["g1"], form, "长".repeat(5000)).payload.hint
      .length,
    4000
  );
});

test("AI 生成说明的请求体超过 1.5MB：依次去掉取值、样本，再不行减少组数", () => {
  const big = "x".repeat(100);
  const many = n => Array.from({ length: n }, (_, i) => `${big}${i}`);
  const group = (ref, extra) => ({
    ref,
    name_guess: `logs_${ref}`,
    kind_guess: "app",
    columns: [{ name: "msg", type: "String" }],
    fields_guess: {},
    targets: [{ target: `logs.${ref}`, ...extra }]
  });
  const form = utils.emptyForm();
  const base = {
    connection: { id: "ck", type: "clickhouse" },
    clusters: [],
    suggested_bindings: []
  };
  const MB = 1024 * 1024;

  // 只靠取值就超了：去掉取值就够
  let scan = {
    ...base,
    groups: [
      group("g1", {
        values: Object.fromEntries(
          Array.from({ length: 2000 }, (_, i) => [`f${i}`, many(10)])
        )
      })
    ]
  };
  let result = utils.describePayload(scan, ["g1"], form, "");
  assert.deepEqual(result.dropped, ["去掉了各表 / 索引的字段取值"]);
  assert.ok(!("values" in result.payload.scan.groups[0].targets[0]));
  assert.ok(result.bytes <= 1.5 * MB);

  // 样本也很大：再去掉样本
  const sample = { body: "y".repeat(800 * 1024) };
  scan = {
    ...base,
    groups: [
      group("g1", {
        values: Object.fromEntries(
          Array.from({ length: 2000 }, (_, i) => [`f${i}`, many(10)])
        ),
        samples: [sample, sample]
      })
    ]
  };
  result = utils.describePayload(scan, ["g1"], form, "");
  assert.deepEqual(result.dropped, [
    "去掉了各表 / 索引的字段取值",
    "去掉了样本数据"
  ]);
  assert.ok(!("samples" in result.payload.scan.groups[0].targets[0]));
  assert.equal(result.tooLarge, false);

  // 组本身就很大（字段说明多）：只发送前面的组
  const wide = ref => ({
    ...group(ref, {}),
    columns: Array.from({ length: 300 }, (_, i) => ({
      name: `c${i}`,
      type: `String -- ${"z".repeat(2000)}`
    }))
  });
  scan = { ...base, groups: ["g1", "g2", "g3", "g4"].map(wide) };
  result = utils.describePayload(scan, ["g1", "g2", "g3", "g4"], form, "");
  assert.equal(result.tooLarge, false);
  assert.ok(result.bytes <= 1.5 * MB);
  assert.deepEqual(
    result.payload.scan.groups.map(item => item.ref),
    ["g1", "g2"]
  );
  assert.equal(result.dropped.length, 3);
  assert.equal(
    result.dropped[2],
    "请求太大，只发送了前 2 个组（共勾选 4 个），没发送的：logs_g3、logs_g4；可以分批再生成"
  );

  // 只剩一个组也超过：不能发送
  scan = {
    ...base,
    groups: [
      {
        ...group("g1", {}),
        meta_description: "m".repeat(2 * MB)
      }
    ]
  };
  result = utils.describePayload(scan, ["g1"], form, "");
  assert.equal(result.tooLarge, true);
});

test("AI 生成说明的返回转成草稿：丢掉组不在勾选里的、key 不合法的、引用不存在日志源的绑定", () => {
  const form = validForm();
  const result = {
    draft: {
      sources: [
        {
          group: "g2",
          id: "app_log",
          name: "应用日志",
          kind: "app",
          description: "应用输出的日志",
          time_field: "",
          fields: { message: "msg", bogus: "x", level: 5 },
          duration_unit: "ms",
          level_values: { error: ["ERROR", ""], fatal: ["F"] },
          columns: [
            { name: "msg", description: "正文" },
            { name: "", description: "没名字" }
          ],
          correlations: [{ key: "trace_id", text: "TID:{value}" }]
        },
        { group: "g9", id: "ghost", name: "不在勾选里" },
        // 已有日志源：补充说明
        {
          group: "",
          id: "app",
          name: "Java 应用日志",
          description: "Java 应用",
          columns: [{ name: "thread", description: "线程名" }]
        },
        { group: "g2", id: "Bad ID", name: "ID 不合法" }
      ],
      correlation_keys: [
        { key: "request_id", label: "请求 ID", trace: false },
        { key: "Bad-Key", label: "x" },
        { key: "order_no", label: "订单号", pattern: "^SO\\d+$" }
      ],
      bindings: [
        {
          env: "test",
          source: "app_log",
          target: "logs.app_log",
          conditions: [],
          note: "一表一集群"
        },
        { env: "prod-a", source: "nobody", target: "logs.x", conditions: [] }
      ],
      cluster_notes: [
        { env: "test", note: "测试环境没有入口网关" },
        { env: "test", note: "重复的" },
        { env: "", note: "没有集群" }
      ],
      architecture: "- 入口 → 服务（推断）"
    },
    notes: ["丢掉了 1 条字段说明"],
    model: "deepseek-flash"
  };
  const draft = utils.draftFromDescribe(
    result,
    scanResult(),
    ["g1", "g2"],
    form
  );
  assert.equal(draft.origin, "ai");
  assert.equal(draft.model, "deepseek-flash");
  assert.equal(draft.connection, "ck");
  assert.deepEqual(
    draft.sources.map(item => item.id),
    ["app_log", "app", "bad-id"]
  );
  const [appLog] = draft.sources;
  // 时间字段没给：用扫描的猜测；不认识的语义字段、非字符串值丢掉
  assert.equal(appLog.time_field, "timestamp");
  assert.deepEqual(appLog.fields, { message: "msg" });
  assert.deepEqual(appLog.level_values, { error: ["ERROR"] });
  assert.deepEqual(appLog.columns, [{ name: "msg", description: "正文" }]);
  assert.equal(appLog.default_target, "logs.app_log");
  assert.deepEqual(
    draft.correlation_keys.map(item => [item.key, item.pattern]),
    [
      ["request_id", ""],
      // 带 ^ $ 的取值格式不能用：清空
      ["order_no", ""]
    ]
  );
  assert.deepEqual(
    draft.bindings.map(item => [item.env, item.source]),
    [["test", "app_log"]]
  );
  assert.deepEqual(draft.cluster_notes, [
    { env: "test", note: "测试环境没有入口网关" }
  ]);
  assert.equal(draft.architecture, "- 入口 → 服务（推断）");
  assert.deepEqual(draft.notes, [
    "丢掉了 1 条字段说明",
    "丢掉了日志源 ghost：对应的组不在这次勾选里",
    "丢掉了关联字段 Bad-Key：key 不合法或重复",
    "丢掉了绑定 prod-a / nobody：日志源不存在"
  ]);
  // 返回不完整也不出错
  const empty = utils.draftFromDescribe({}, scanResult(), [], form);
  assert.deepEqual(
    [empty.sources, empty.bindings, empty.correlation_keys, empty.architecture],
    [[], [], [], ""]
  );
});

/** 一份 AI 草稿：新日志源两个（ID 相同）、补充已有的 ingress、绑定、关联字段、集群说明、网络架构 */
const aiDraft = () => ({
  origin: "ai",
  connection: "ck",
  sources: [
    {
      group: "g2",
      id: "app",
      name: "应用日志（和已有的 app 同 ID，是补充）",
      kind: "app",
      description: "补充的说明",
      time_field: "timestamp",
      fields: { message: "msg" },
      columns: [{ name: "thread", description: "线程名" }],
      correlations: []
    },
    {
      group: "g1",
      id: "ingress",
      name: "入口网关访问日志",
      kind: "access",
      description: "不会覆盖已有的说明",
      time_field: "start_time",
      fields: {},
      columns: [
        { name: "upstream_cluster", description: "不会覆盖已有的字段说明" },
        { name: "route", description: "路由名" },
        { name: "x_forwarded_for", description: "客户端地址链" }
      ],
      correlations: []
    },
    {
      group: "g3",
      id: "gateway",
      name: "内部网关日志",
      kind: "access",
      description: "内部网关",
      time_field: "ts",
      fields: { path: "path", message: "msg" },
      columns: [],
      correlations: [
        { key: "request_id", field: "x_request_id" },
        { key: "span_id", field: "span_id" },
        { key: "trace_id", text: "TID:{value}" }
      ]
    },
    {
      group: "g4",
      id: "gateway",
      name: "另一个网关（ID 和上一个相同）",
      kind: "access",
      description: "",
      time_field: "ts",
      fields: {},
      columns: [],
      correlations: []
    }
  ],
  correlation_keys: [
    {
      key: "trace_id",
      label: "已有，不覆盖",
      description: "",
      pattern: "",
      trace: true
    },
    {
      key: "span_id",
      label: "Span ID",
      description: "",
      pattern: "",
      trace: true
    },
    {
      key: "biz_id",
      label: "业务 ID",
      description: "",
      pattern: "",
      trace: false
    }
  ],
  bindings: [
    {
      env: "prod-a",
      source: "ingress",
      target: "logs.other",
      conditions: [],
      note: "不覆盖已有绑定"
    },
    {
      env: "prod-a",
      source: "gateway",
      target: "logs.gateway",
      conditions: [{ field: "env", op: "in", values: ["prod-a"] }],
      note: ""
    },
    {
      env: "prod-b",
      source: "ingress",
      target: "logs.ingress_access",
      conditions: [{ field: "env", op: "in", values: ["prod-b"] }],
      note: ""
    },
    {
      env: "prod-c",
      source: "gateway",
      target: "logs.gateway",
      conditions: [],
      note: "新集群"
    }
  ],
  cluster_notes: [
    { env: "prod-a", note: "覆盖原来的说明" },
    { env: "prod-b", note: "生产 B 没有外部 nginx" }
  ],
  architecture: "- 新的网络架构说明",
  notes: [],
  model: "deepseek-flash"
});

test("草稿对话框的分类和默认勾选：已有的不覆盖、原来为空的才默认填写", () => {
  const form = validForm();
  const view = utils.draftView(form, aiDraft(), settings().clusters_known);
  assert.deepEqual(
    view.newSources.map(item => [item.id, item.finalId]),
    [
      ["gateway", "gateway"],
      ["gateway", "gateway-2"]
    ]
  );
  assert.deepEqual(
    view.updateSources.map(item => [item.id, item.name, item.adds]),
    [
      // 已有日志源显示表单里的名称，不是草稿里的
      ["app", "Java 应用日志", ["说明", "1 个字段说明"]],
      ["ingress", "入口网关访问日志", ["2 个字段说明"]]
    ]
  );
  assert.deepEqual(
    view.bindings.map(group => [
      group.env,
      group.registered,
      group.rows.map(row => [row.source, row.exists, row.existingSource])
    ]),
    [
      [
        "prod-a",
        true,
        [
          ["ingress", true, true],
          ["gateway", false, false]
        ]
      ],
      ["prod-b", true, [["ingress", false, true]]],
      ["prod-c", false, [["gateway", false, false]]]
    ]
  );
  assert.equal(view.bindings[0].rows[1].conditions, "env ∈ {prod-a}");
  assert.ok(
    view.bindings.flatMap(group => group.rows).every(row => row.problem === "")
  );
  // 表名格式不对：按日志源的连接类型提示（新日志源用草稿的连接 ck，已有的用自己的连接）
  const bad = aiDraft();
  bad.bindings[1].target = "gateway_access";
  bad.bindings.push({
    env: "prod-b",
    source: "app",
    target: "App-Logs",
    conditions: [],
    note: ""
  });
  const problems = utils
    .draftView(form, bad)
    .bindings.flatMap(group => group.rows)
    .filter(row => row.problem)
    .map(row => [row.source, row.problem]);
  assert.deepEqual(problems, [
    [
      "gateway",
      "gateway_access 要写成 库.表（字母、数字和下划线，不能以数字开头），例如 logs.ingress_access"
    ],
    [
      "app",
      "App-Logs 不是合法的索引名（只能用小写字母、数字和 _ . * + -），例如 app-logs-*"
    ]
  ]);
  assert.deepEqual(
    view.keys.map(item => [item.key, item.exists]),
    [
      ["trace_id", true],
      ["span_id", false],
      ["biz_id", false]
    ]
  );
  assert.deepEqual(
    view.notes.map(item => [item.env, item.overwrite]),
    [
      ["prod-a", true],
      ["prod-b", false]
    ]
  );
  assert.deepEqual(view.architecture, {
    text: "- 新的网络架构说明",
    overwrite: true
  });

  const picks = utils.defaultPicks(form, aiDraft(), settings().clusters_known);
  assert.deepEqual(picks, {
    sources: ["gateway", "gateway", "app", "ingress"],
    bindings: [
      utils.bindingKey("prod-a", "gateway"),
      utils.bindingKey("prod-b", "ingress"),
      utils.bindingKey("prod-c", "gateway")
    ],
    keys: ["span_id", "biz_id"],
    notes: ["prod-b"],
    architecture: false
  });
  // 网络架构原来为空：默认勾选
  const blank = validForm();
  blank.architecture = "";
  assert.equal(utils.defaultPicks(blank, aiDraft()).architecture, true);
});

test("草稿合并进表单：新增去重 ID，已有日志源只补空的说明和字段说明，绑定不覆盖，关联字段按 key 去重", () => {
  const form = validForm();
  const draft = aiDraft();
  const picks = utils.defaultPicks(form, draft);
  picks.architecture = true;
  picks.notes.push("prod-a");
  const { form: next, summary } = utils.mergeDraft(form, draft, picks);
  assert.deepEqual(summary, [
    "新增日志源 2 个",
    "补充已有日志源 2 个",
    "新增绑定 3 个",
    "新增关联字段 2 个",
    "填写集群说明 2 个",
    "填写网络架构说明"
  ]);
  assert.deepEqual(
    next.sources.map(item => item.id),
    ["ingress", "app", "gateway", "gateway-2"]
  );
  // 已有的 ingress：说明、已有的字段说明不变，只补没有的；其他配置不动
  const ingress = findSource(next, "ingress");
  assert.equal(ingress.description, "入口网关的访问日志");
  assert.deepEqual(
    ingress.columns.map(item => [item.name, item.description]),
    [
      ["upstream_cluster", "上游集群"],
      ["route", "路由名"],
      ["x_forwarded_for", "客户端地址链"]
    ]
  );
  assert.equal(ingress.time_field, "start_time");
  assert.equal(ingress.default_target, "logs.ingress_access");
  // 已有的 app：说明原来是空的，补上；字段配置不变
  const app = findSource(next, "app");
  assert.equal(app.description, "补充的说明");
  assert.equal(app.fields.message, "message");
  assert.equal(app.time_field, "@timestamp");
  // 新的 gateway：用扫描的连接；关联字段只留存在的 key（span_id 是这次新增的）
  const gateway = findSource(next, "gateway");
  assert.equal(gateway.connection, "ck");
  assert.equal(gateway.enabled, true);
  assert.deepEqual(
    gateway.correlations.map(item => [item.key, item.mode]),
    [
      ["request_id", "field"],
      ["span_id", "field"],
      ["trace_id", "text"]
    ]
  );
  // 关联字段：trace_id 已有不覆盖；span_id 想当链路 ID，但已经有 trace_id 了
  assert.deepEqual(
    next.correlation_keys.map(item => [item.key, item.label, item.trace]),
    [
      ["trace_id", "链路 ID", true],
      ["request_id", "请求 ID", false],
      ["span_id", "Span ID", false],
      ["biz_id", "业务 ID", false]
    ]
  );
  // 绑定：prod-a 的 ingress 已有不覆盖；新集群 prod-c 新建
  assert.deepEqual(
    findCluster(next, "prod-a").bindings.map(item => [
      item.source,
      item.target
    ]),
    [
      ["ingress", "logs.ingress_access"],
      ["app", "app-logs-*"],
      ["gateway", "logs.gateway"]
    ]
  );
  assert.deepEqual(
    findCluster(next, "prod-b").bindings.map(item => item.source),
    ["ingress"]
  );
  assert.equal(findCluster(next, "prod-b").note, "生产 B 没有外部 nginx");
  assert.equal(findCluster(next, "prod-a").note, "覆盖原来的说明");
  assert.deepEqual(
    findCluster(next, "prod-c").bindings.map(item => item.source),
    ["gateway"]
  );
  assert.equal(next.architecture, "- 新的网络架构说明");
  // 合并后的表单能通过校验（新集群的表名、条件都合法）
  assert.deepEqual(
    utils.validateForm(next).filter(item => item.section !== "sources"),
    []
  );

  // 原表单不变
  assert.deepEqual(
    form.sources.map(item => item.id),
    ["ingress", "app"]
  );
  assert.equal(form.correlation_keys.length, 2);
  assert.equal(findCluster(form, "prod-a").bindings.length, 2);
  assert.equal(findCluster(form, "prod-a").note, "入口流量先经过外部 nginx");
  assert.equal(findSource(form, "ingress").columns.length, 1);
  assert.equal(form.architecture, "- 入口网关（ingress）→ 服务（app）");
});

test("草稿合并：没勾选的不动；新日志源没勾选时它的绑定跳过；没勾选的关联字段不能被新日志源引用", () => {
  const form = validForm();
  const draft = aiDraft();
  const { form: next, summary } = utils.mergeDraft(form, draft, {
    sources: ["gateway"],
    bindings: [
      utils.bindingKey("prod-a", "gateway"),
      utils.bindingKey("prod-b", "ingress")
    ],
    keys: [],
    notes: [],
    architecture: false
  });
  assert.deepEqual(summary, ["新增日志源 2 个", "新增绑定 2 个"]);
  // 两个草稿日志源的 ID 都是 gateway，勾选同一个 ID 时两个都算
  assert.deepEqual(
    next.sources.map(item => item.id),
    ["ingress", "app", "gateway", "gateway-2"]
  );
  // span_id 没勾选：新日志源里对它的登记去掉
  assert.deepEqual(
    findSource(next, "gateway").correlations.map(item => item.key),
    ["request_id", "trace_id"]
  );
  assert.equal(next.architecture, form.architecture);
  assert.equal(findSource(next, "app").description, "");
  // 已有的 ingress 没勾选，但它的绑定照样可以加（日志源本来就在）
  assert.deepEqual(
    findCluster(next, "prod-b").bindings.map(item => item.source),
    ["ingress"]
  );

  // 新日志源没勾选：引用它的绑定跳过
  const skipped = utils.mergeDraft(form, draft, {
    sources: [],
    bindings: [utils.bindingKey("prod-c", "gateway")],
    keys: ["biz_id"],
    notes: [],
    architecture: false
  });
  assert.equal(findCluster(skipped.form, "prod-c"), undefined);
  assert.deepEqual(skipped.summary, ["新增关联字段 1 个"]);

  // 正文写法要有正文字段：没有正文字段的新日志源去掉正文写法的登记
  const noMessage = aiDraft();
  noMessage.sources[2].fields = { path: "path" };
  const merged = utils.mergeDraft(form, noMessage, {
    sources: ["gateway"],
    bindings: [],
    keys: [],
    notes: [],
    architecture: false
  }).form;
  assert.deepEqual(
    findSource(merged, "gateway").correlations.map(item => item.key),
    ["request_id"]
  );
});

test("按规则生成的草稿应用到空表单后能直接通过校验", () => {
  const form = utils.emptyForm();
  const ck = utils.emptyConnection("clickhouse", []);
  ck.url = "http://clickhouse.example.com:8123";
  form.connections.push(ck);
  const draft = utils.draftFromScan(scanResult(), ["g1", "g2"], form);
  const { form: next } = utils.mergeDraft(
    form,
    draft,
    utils.defaultPicks(form, draft)
  );
  assert.deepEqual(utils.validateForm(next), []);
  assert.deepEqual(
    next.clusters.map(item => [item.env, item.bindings.map(b => b.source)]),
    [
      ["prod-a", ["ingress_access"]],
      ["prod-b", ["ingress_access"]],
      ["test", ["app_log"]]
    ]
  );
  // 再生成一次同样的组：已经是日志源的默认表，不会重复新增
  const again = utils.draftFromScan(scanResult(), ["g1", "g2"], next);
  const view = utils.draftView(next, again);
  assert.deepEqual(view.newSources, []);
  assert.deepEqual(
    view.updateSources.map(item => item.id),
    ["ingress_access", "app_log"]
  );
  assert.ok(
    view.bindings.flatMap(group => group.rows).every(row => row.exists)
  );
});

test("验证关联字段的命中矩阵", () => {
  const matrix = utils.verifyMatrix({
    pairs: [
      { from: "ingress", to: "app", sampled: 20, matched: 18, examples: ["a"] },
      { from: "ingress", to: "gateway", sampled: 20, matched: 3, examples: [] },
      { from: "app", to: "ingress", sampled: 20, matched: 0, examples: [] },
      { from: "gateway", to: "app", sampled: 0, matched: 0, examples: [] },
      {
        from: "gateway",
        to: "ingress",
        sampled: 20,
        matched: 0,
        examples: [],
        error: "字段 x_request_id 不存在"
      }
    ],
    notes: []
  });
  assert.deepEqual(matrix.sources, ["ingress", "app", "gateway"]);
  assert.deepEqual(
    matrix.rows.map(row => [
      row.from,
      row.cells.map(cell =>
        cell ? `${cell.matched}/${cell.sampled}:${cell.level}` : null
      )
    ]),
    [
      ["ingress", [null, "18/20:good", "3/20:partial"]],
      ["app", ["0/20:none", null, null]],
      ["gateway", ["0/20:error", "0/0:empty", null]]
    ]
  );
  assert.equal(matrix.rows[2].cells[0].error, "字段 x_request_id 不存在");
  assert.deepEqual(utils.verifyMatrix(null), { sources: [], rows: [] });
});

test("扫描组的概况、数量和大小的显示", () => {
  const summary = utils.groupSummary(scanResult().groups[0]);
  assert.deepEqual(summary, {
    targets: 2,
    rows: 123457789,
    bytes: 4567890123,
    retention: "15~30 天",
    range: "2026-11-01 00:00 ~ 2026-11-23 15:00",
    fields: 6
  });
  const bare = utils.groupSummary(scanResult().groups[1]);
  assert.equal(bare.retention, "—");
  assert.equal(bare.range, "—");
  assert.equal(utils.formatCount(999), "999");
  assert.equal(utils.formatCount(12345), "1.2 万");
  assert.equal(utils.formatCount(123456789), "1.2 亿");
  assert.equal(utils.formatCount(null), "—");
  assert.equal(utils.formatBytes(512), "512 B");
  assert.equal(utils.formatBytes(1536), "1.5 KB");
  assert.equal(utils.formatBytes(4567890123), "4.3 GB");
  assert.equal(
    utils.shortTime("2026-11-23T15:18:49.366+08:00"),
    "2026-11-23 15:18"
  );
});

test("测试连接结果、预览结果、错误提示的显示", () => {
  const ch = utils.testRows({
    type: "clickhouse",
    version: "26.7.1",
    timezone: "Asia/Shanghai",
    user: "kubedoor_ro",
    readonly: 0,
    clusters: ["ck1"]
  });
  assert.deepEqual(
    ch.map(row => [row.label, row.value, !!row.warn]),
    [
      ["版本", "ClickHouse 26.7.1", false],
      ["时区", "Asia/Shanghai", false],
      ["账号", "kubedoor_ro", false],
      ["只读", "否：建议给 KubeDoor 建只读账号（见帮助文档）", true],
      ["集群", "ck1", false]
    ]
  );
  assert.equal(
    utils.testRows({ type: "clickhouse", readonly: "2" })[3].value,
    "是（readonly = 2）"
  );
  const es = utils.testRows({
    type: "elasticsearch",
    version: "8.15.0",
    distribution: "elasticsearch",
    cluster_name: "logs",
    license: "basic",
    esql: false
  });
  assert.equal(es[0].value, "elasticsearch 8.15.0");
  assert.equal(es[3].warn, true);
  assert.equal(
    utils.testSummary({
      type: "elasticsearch",
      distribution: "opensearch",
      version: "2.11.0"
    }),
    "连接正常：opensearch 2.11.0"
  );

  assert.deepEqual(
    utils.previewColumns([
      { path: "/a", "@time": "t1", status: 200 },
      { path: "/b", "@time": "t2", trace_id: "x" }
    ]),
    ["@time", "path", "status", "trace_id"]
  );
  assert.equal(utils.valueText({ a: 1 }), '{"a":1}');
  assert.equal(utils.valueText({ a: 1 }, 2), '{\n  "a": 1\n}');
  assert.equal(utils.valueText(null), "");
  assert.equal(utils.problemText("字段 x 不存在"), "字段 x 不存在");
  assert.equal(
    utils.problemText({
      code: "field_not_found",
      message: "时间字段 ts 不存在"
    }),
    "时间字段 ts 不存在"
  );
  assert.equal(
    utils.problemText({ field: "pod_name", where: "Pod 字段" }),
    "Pod 字段：pod_name 不存在"
  );

  assert.equal(
    utils.errorText(
      { response: { status: 400, data: { message: "日志源 x 不存在" } } },
      "失败"
    ),
    "HTTP 400：日志源 x 不存在"
  );
  assert.equal(
    utils.errorText({ message: "Network Error" }, "失败"),
    "Network Error"
  );
  assert.equal(utils.errorText(null, "失败"), "失败");
  assert.match(
    utils.describeErrorText({ status: 409, code: "shared_model_unavailable" }),
    /企微专用模型还没有配置好/
  );
  assert.match(
    utils.describeErrorText({ status: 504, code: "describe_timeout" }),
    /3 分钟内没有返回/
  );
  assert.match(
    utils.describeErrorText({ status: 422, code: "describe_invalid" }),
    /不是合法的 JSON/
  );
  assert.match(
    utils.describeErrorText({ status: 422, code: "describe_failed" }),
    /调用模型失败/
  );
  assert.equal(utils.describeErrorText({ message: "网络错误" }), "网络错误");
  assert.equal(utils.requestTimeoutMs(30, 2), 75000);
  assert.equal(utils.requestTimeoutMs(500, 1), 135000);
});

test("网络架构说明的示例模板用当前日志源的 ID；认证方式的显示", () => {
  const template = utils.architectureTemplate(validForm());
  assert.match(template, /日志源 `ingress`/);
  assert.match(template, /日志源 `app`/);
  assert.match(template, /关联字段 `trace_id`/);
  assert.match(template, /`request_id`/);
  const blank = utils.architectureTemplate(utils.emptyForm());
  assert.match(blank, /日志源 `gateway`/);

  const [ck, es] = validForm().connections;
  assert.equal(utils.authText(ck), "用户名密码（kubedoor_ro）");
  assert.equal(utils.authText(es), "API Key");
  es.has_api_key = false;
  assert.equal(utils.authText(es), "API Key（未填）");
  es.auth_type = "none";
  assert.equal(utils.authText(es), "无");
  ck.username = "";
  assert.equal(utils.authText(ck), "default 用户");
  assert.equal(utils.kindLabel("access"), "访问日志");
  assert.equal(utils.typeLabel("elasticsearch"), "Elasticsearch");
  assert.equal(
    utils.bindingConnection(validForm(), { source: "app", connection: "" }).id,
    "es"
  );
  assert.equal(
    utils.bindingConnection(validForm(), { source: "app", connection: "ck" })
      .id,
    "ck"
  );
});
