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

/** 单文件编译加载；vue 为 true 时只编译 SFC 的脚本（模板不渲染） */
function compile(filename, overrides = {}, vue = false) {
  const target = path.resolve(__dirname, filename);
  let source = fs.readFileSync(target, "utf8");
  if (vue) {
    const { descriptor } = parse(source, { filename: target });
    source = compileScript(descriptor, { id: "general-settings-test" }).content;
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

const utils = compile(
  "../src/views/system/general-settings/general-settings-utils.ts"
);

const DEFAULTS = {
  prom_type: "Victoria-Metrics-Single",
  prom_url: "",
  metrics_write_url: "",
  prom_k8s_tag_key: "origin_prometheus",
  kubedoor_exturl: "",
  alertmanager_exturl: "",
  oss_url: "",
  image_repo: "swr.cn-south-1.myhuaweicloud.com/starsl.cn",
  image_vmagent: "registry.cn-shenzhen.aliyuncs.com/starsl/vmagent:stable",
  image_kube_state_metrics:
    "registry.cn-shenzhen.aliyuncs.com/starsl/kube-state-metrics:v2.14.0",
  image_node_exporter: "registry.cn-shenzhen.aliyuncs.com/starsl/node-exporter",
  node_shell_image: "",
  retention_events_days: 90,
  retention_alerts_days: 365,
  retention_resources_days: 365,
  retention_audit_days: 180,
  retention_ai_checkpoint_days: 7,
  auth_session_hours: 12,
  auth_idle_hours: 4,
  auth_remember_days: 7,
  auth_login_max_failures: 5,
  auth_lock_minutes: 15,
  auth_cookie_secure: "auto",
  agent_auth_required: true,
  alert_dedup_window: 300,
  silence_cache_ttl: 15,
  admis_cache_ttl: 60
};

const QUERY_URL = "http://monit:******@192.0.2.11:8428";
const WRITE_URL = "http://monit:******@192.0.2.11:8428/api/v1/write";

/** GET 的返回：存过查询 / 远程写地址、镜像仓库（和默认值相同）、关掉了 Agent 账号、改了审计保留天数 */
const settingsData = (extra = {}) => ({
  values: {
    ...DEFAULTS,
    prom_url: QUERY_URL,
    metrics_write_url: WRITE_URL,
    agent_auth_required: false,
    retention_audit_days: 30
  },
  defaults: { ...DEFAULTS },
  stored: [
    "prom_type",
    "prom_url",
    "metrics_write_url",
    "image_repo",
    "agent_auth_required",
    "retention_audit_days"
  ],
  problems: {},
  recommended_agent_tag: "2.6.0",
  updated_by: "kubedoor",
  updated_at: "2026-11-15T10:00:00+08:00",
  ...extra
});

/** 读取后的表单状态 */
function contextOf(data = settingsData()) {
  const defaults = utils.defaultsOf(data);
  const form = utils.formFromValues(data.values, defaults);
  return { form, initial: { ...form }, restores: new Set(), defaults };
}

const allPayloads = ctx =>
  Object.fromEntries(
    utils.CARDS.map(card => [card.id, utils.cardPayload(card.id, ctx)])
  );

test("卡片顺序、配置项和中文名称与约定第 2 节一致", () => {
  assert.deepEqual(
    utils.CARDS.map(card => card.title),
    [
      "时序数据库",
      "访问地址",
      "对象存储",
      "Agent 安装",
      "节点终端",
      "数据保留",
      "登录与安全",
      "高级"
    ]
  );
  assert.deepEqual(
    utils.CARDS.filter(card => card.collapsed).map(card => card.id),
    ["advanced"]
  );
  assert.deepEqual(utils.SETTING_KEYS, Object.keys(DEFAULTS));
  assert.deepEqual(Object.keys(utils.FIELDS), Object.keys(DEFAULTS));
  assert.deepEqual(
    utils.SETTING_KEYS.map(key => utils.FIELDS[key].label),
    [
      "时序库类型",
      "查询地址",
      "远程写地址",
      "集群指标标签 key",
      "KubeDoor 访问地址",
      "Alertmanager 外部地址",
      "对象存储上传地址",
      "镜像仓库",
      "vmagent 镜像",
      "kube-state-metrics 镜像",
      "node-exporter 镜像",
      "节点终端镜像",
      "K8S 事件保留天数",
      "指标告警保留天数",
      "高峰期资源保留天数",
      "审计日志保留天数",
      "AI 对话检查点保留天数",
      "登录会话有效期（小时）",
      "空闲超时（小时）",
      "「记住我」有效天数",
      "登录失败锁定次数",
      "锁定时长（分钟）",
      "Cookie Secure",
      "Agent 连接必须带账号",
      "事件告警去重窗口（秒）",
      "告警屏蔽规则缓存（秒）",
      "准入查询缓存（秒）"
    ]
  );
  assert.deepEqual(
    utils.PROM_TYPE_OPTIONS.map(option => [option.value, option.label]),
    [
      ["Victoria-Metrics-Single", "VictoriaMetrics 单机版"],
      ["Victoria-Metrics-Cluster", "VictoriaMetrics 集群版"],
      ["Prometheus", "Prometheus"]
    ]
  );
  // 兜底默认值和约定一致（镜像以 master 返回的为准）
  for (const key of utils.SETTING_KEYS)
    if (!key.startsWith("image_"))
      assert.equal(utils.FALLBACK_DEFAULTS[key], DEFAULTS[key], key);
});

test("读取后没有改动：每张卡片都不需要保存，没改的 ****** 地址不提交", () => {
  const ctx = contextOf();
  assert.equal(ctx.form.prom_url, QUERY_URL);
  assert.equal(ctx.form.agent_auth_required, false);
  assert.equal(ctx.form.retention_audit_days, 30);
  for (const card of utils.CARDS)
    assert.deepEqual(utils.changedKeys(card.id, ctx), [], card.id);
  assert.deepEqual(
    allPayloads(ctx),
    Object.fromEntries(utils.CARDS.map(card => [card.id, {}]))
  );
  // 只差首尾空白和末尾的 / 不算改动
  ctx.form.prom_url = ` ${QUERY_URL}/ `;
  ctx.form.image_repo = `${DEFAULTS.image_repo}/`;
  assert.deepEqual(utils.changedKeys("metrics", ctx), []);
  assert.deepEqual(utils.changedKeys("agent", ctx), []);
});

test("每张卡片只提交自己改过的项：清空的项提交 null，文本去空白、地址路径去末尾 /", () => {
  const ctx = contextOf();
  ctx.form.retention_events_days = 30;
  ctx.form.auth_session_hours = 24;
  ctx.form.auth_cookie_secure = "true";
  // 主机、端口、账号没变，保留 ****** 沿用已保存的密码
  ctx.form.prom_url = "http://monit:******@192.0.2.11:8428/prometheus/";
  ctx.form.metrics_write_url =
    "http://monit:******@192.0.2.11:8428/api/v1/write/?extra_label=dc=a/";
  ctx.form.prom_k8s_tag_key = "  ";
  ctx.form.kubedoor_exturl = " https://kd.example.com/ ";
  ctx.form.retention_alerts_days = null;
  assert.deepEqual(allPayloads(ctx), {
    metrics: {
      prom_url: "http://monit:******@192.0.2.11:8428/prometheus",
      metrics_write_url:
        "http://monit:******@192.0.2.11:8428/api/v1/write?extra_label=dc=a/",
      prom_k8s_tag_key: null
    },
    access: { kubedoor_exturl: "https://kd.example.com" },
    oss: {},
    agent: {},
    node_shell: {},
    retention: { retention_events_days: 30, retention_alerts_days: null },
    auth: { auth_session_hours: 24, auth_cookie_secure: "true" },
    advanced: {}
  });
  // 改回原值就不算改动
  ctx.form.retention_events_days = 90;
  ctx.form.retention_alerts_days = 365;
  assert.deepEqual(utils.changedKeys("retention", ctx), []);
  // 清空存过的地址 = 不配置
  ctx.form.metrics_write_url = "";
  assert.equal(utils.cardPayload("metrics", ctx).metrics_write_url, null);
});

test("恢复默认：存的值和默认值一样也提交 null；之后又改了值就按新值提交", () => {
  const ctx = contextOf();
  const restore = key => {
    ctx.form[key] = utils.defaultFormValue(key, ctx.defaults);
    ctx.restores.add(key);
  };
  // 镜像仓库存的就是默认值：恢复默认后表单不变，但要提交 null（以后跟随默认值）
  restore("image_repo");
  assert.equal(ctx.form.image_repo, DEFAULTS.image_repo);
  assert.equal(utils.restorePending("image_repo", ctx), true);
  assert.deepEqual(utils.cardPayload("agent", ctx), { image_repo: null });
  ctx.form.image_repo = "harbor.example.com/kubedoor";
  assert.equal(utils.restorePending("image_repo", ctx), false);
  assert.deepEqual(utils.cardPayload("agent", ctx), {
    image_repo: "harbor.example.com/kubedoor"
  });

  restore("agent_auth_required");
  assert.equal(ctx.form.agent_auth_required, true);
  restore("retention_audit_days");
  assert.equal(ctx.form.retention_audit_days, 180);
  restore("prom_url");
  assert.equal(ctx.form.prom_url, "");
  assert.deepEqual(utils.cardPayload("auth", ctx), {
    agent_auth_required: null
  });
  assert.deepEqual(utils.cardPayload("retention", ctx), {
    retention_audit_days: null
  });
  assert.deepEqual(utils.cardPayload("metrics", ctx), { prom_url: null });
});

test("地址校验：完整 http(s)、端口 1 到 65535、无锚点空白反斜杠控制字符、长度不超过 2048", () => {
  const check = (key, value, initial = "") =>
    utils.validateField(key, value, initial);
  for (const value of [
    "",
    "http://192.0.2.11:8428",
    "https://vm.example.com/select/0/prometheus/",
    "http://[2001:db8::1]:8428",
    "http://monit:p%40ss@vm.example.com:8428",
    "http://monit@vm.example.com:8428",
    "http://vm.example.com:/api/v1/write"
  ])
    assert.equal(check("prom_url", value), "", value);
  assert.equal(check("prom_url", QUERY_URL, QUERY_URL), "");
  const cases = [
    ["vm.example.com:8428", /http:\/\/ 或 https:\/\//],
    ["ftp://vm.example.com", /http:\/\/ 或 https:\/\//],
    ["http://", /http:\/\/ 或 https:\/\//],
    ["http:vm.example.com", /http:\/\/ 或 https:\/\//],
    ["http://monit:pw@/api", /http:\/\/ 或 https:\/\//],
    ["http://vm.example.com:99999", /端口无效/],
    ["http://vm.example.com:0", /端口无效/],
    ["http://vm.example.com:84a", /端口无效/],
    ["http://vm.example.com/#/graph", /锚点/],
    ["http://vm.example.com/a b", /空白、反斜杠或控制字符/],
    ["http://vm.example.com\\api", /空白、反斜杠或控制字符/],
    ["http://vm.example.com/\u0001", /空白、反斜杠或控制字符/],
    ["http://:pw@vm.example.com:8428", /@ 前面缺少账号/],
    // 查询地址后面要拼 /api/v1/query，不能带查询参数
    ["http://vm.example.com:8428/?nocache=1", /查询参数/],
    [`http://vm.example.com/${"a".repeat(2048)}`, /2048/]
  ];
  for (const [value, pattern] of cases)
    assert.match(check("prom_url", value), pattern, value);
  // 远程写地址可以带查询参数（例如 extra_label）
  assert.equal(
    check(
      "metrics_write_url",
      "http://vm.example.com:8428/api/v1/write?extra_label=dc=a"
    ),
    ""
  );
});

test("****** 只能在原来存过密码、账号协议主机端口都没变时沿用", () => {
  const check = (value, initial = WRITE_URL) =>
    utils.validateField("metrics_write_url", value, initial);
  // 只改路径可以沿用
  assert.equal(
    check("http://monit:******@192.0.2.11:8428/insert/0/prometheus"),
    ""
  );
  // 主机名不分大小写
  assert.equal(
    check(
      "http://monit:******@vm.example.com:8428",
      "http://monit:******@VM.Example.com:8428"
    ),
    ""
  );
  for (const value of [
    "http://other:******@192.0.2.11:8428/api/v1/write",
    "https://monit:******@192.0.2.11:8428/api/v1/write",
    "http://monit:******@192.0.2.12:8428/api/v1/write",
    "http://monit:******@192.0.2.11:8480/api/v1/write"
  ])
    assert.match(
      check(value),
      /改了账号、协议、主机或端口就要重新填写密码/,
      value
    );
  assert.match(
    check("http://monit:******@192.0.2.11:8428/api/v1/write", ""),
    /请把 \*{6} 换成实际密码/
  );
  assert.match(
    check(
      "http://monit:******@192.0.2.11:8428/api/v1/write",
      "http://monit@192.0.2.11:8428/api/v1/write"
    ),
    /请把 \*{6} 换成实际密码/
  );
  assert.equal(utils.hasMaskedPassword(WRITE_URL), true);
  assert.equal(utils.hasMaskedPassword("http://monit:secret@vm:8428"), false);
  assert.equal(utils.hasMaskedPassword(""), false);
  // 地址里换了新密码就不用比较
  assert.equal(check("http://other:new-secret@vm:8428"), "");
});

test("访问地址、Alertmanager 地址和对象存储地址的规则各不相同", () => {
  const check = (key, value) => utils.validateField(key, value, "");
  assert.equal(check("kubedoor_exturl", "https://kd.example.com/"), "");
  assert.equal(check("kubedoor_exturl", "http://192.0.2.10:30080"), "");
  assert.match(
    check("kubedoor_exturl", "https://kd.example.com/kubedoor"),
    /只填写协议、主机和端口，不包含路径/
  );
  assert.match(
    check("kubedoor_exturl", "https://kd.example.com?a=1"),
    /查询参数/
  );
  assert.match(
    check("kubedoor_exturl", "https://admin:pw@kd.example.com"),
    /不需要填写账号或密码/
  );
  assert.equal(
    check("alertmanager_exturl", "http://am.example.com:9093/alertmanager"),
    ""
  );
  assert.match(
    check("alertmanager_exturl", "http://u@am.example.com:9093"),
    /不需要填写账号或密码/
  );
  assert.match(check("alertmanager_exturl", "http://am:9093/#/alerts"), /锚点/);
  assert.match(check("alertmanager_exturl", "http://am:9093/?a=1"), /查询参数/);
  assert.equal(check("oss_url", "http://u:p@oss.example.com/bucket/"), "");
  assert.match(
    check("oss_url", "http://oss.example.com/bucket?x=1"),
    /查询参数/
  );
});

test("标签 key、镜像、数字和下拉项的校验", () => {
  const check = (key, value) => utils.validateField(key, value);
  assert.equal(check("prom_k8s_tag_key", "origin_prometheus"), "");
  assert.equal(check("prom_k8s_tag_key", "_cluster"), "");
  for (const value of ["1cluster", "k8s-name", "a.b"])
    assert.match(check("prom_k8s_tag_key", value), /字母、数字、下划线/);
  assert.match(check("prom_k8s_tag_key", "a".repeat(129)), /128/);

  assert.equal(check("image_repo", "harbor.example.com:5000/kubedoor/"), "");
  assert.match(check("image_repo", "-harbor"), /字母或数字开头/);
  assert.match(check("image_repo", "harbor example"), /字母或数字开头/);
  assert.match(check("image_repo", `h${"a".repeat(256)}`), /256/);

  assert.equal(check("image_vmagent", "harbor/vmagent:v1.0"), "");
  for (const value of [
    'harbor/"vmagent"',
    "harbor/vm agent",
    "a\tb",
    "harbor\\vmagent"
  ])
    assert.match(check("image_vmagent", value), /空白、引号、反斜杠或控制字符/);
  assert.match(check("image_node_exporter", "a".repeat(257)), /256/);
  assert.equal(check("node_shell_image", "a".repeat(512)), "");
  assert.match(check("node_shell_image", "a".repeat(513)), /512/);

  assert.equal(check("retention_events_days", 0), "");
  assert.equal(check("retention_events_days", 3650), "");
  assert.equal(check("retention_events_days", null), "");
  for (const value of [3651, -1, 1.5])
    assert.match(
      check("retention_events_days", value),
      /需为 0 到 3650 之间的整数/
    );
  assert.match(check("auth_login_max_failures", 0), /1 到 100/);
  assert.match(check("auth_lock_minutes", 1441), /1 到 1440/);
  assert.match(check("alert_dedup_window", 86401), /0 到 86400/);
  assert.match(check("silence_cache_ttl", 0), /1 到 3600/);
  assert.equal(check("admis_cache_ttl", 0), "");
  assert.equal(check("auth_session_hours", 0.5), "");
  assert.equal(check("auth_session_hours", 720), "");
  for (const value of [0, -1, 720.5])
    assert.match(check("auth_session_hours", value), /需大于 0 且不超过 720/);
  assert.match(check("auth_remember_days", 366), /不超过 365/);
  // 空闲超时 0 = 不限（和 master 原来的语义一致）
  assert.equal(check("auth_idle_hours", 0), "");
  assert.equal(check("auth_idle_hours", 0.5), "");
  for (const value of [-1, 721])
    assert.match(check("auth_idle_hours", value), /需在 0 到 720 之间/);

  assert.match(check("prom_type", "victoria"), /下拉列表/);
  assert.match(check("auth_cookie_secure", "yes"), /下拉列表/);
  assert.equal(check("auth_cookie_secure", "false"), "");
  assert.equal(check("agent_auth_required", false), "");
});

test("校验不通过时指出是哪一项：cardErrors 列出原因，cardPayload 抛出「名称：原因」", () => {
  const ctx = contextOf();
  ctx.form.retention_events_days = 5000;
  ctx.form.retention_ai_checkpoint_days = 3;
  assert.deepEqual(utils.cardErrors("retention", ctx), {
    retention_events_days: "需为 0 到 3650 之间的整数"
  });
  assert.throws(
    () => utils.cardPayload("retention", ctx),
    /^Error: K8S 事件保留天数：需为 0 到 3650 之间的整数$/
  );
  // 没改的项不校验：库里已有的值以服务端为准
  const legacy = contextOf(
    settingsData({
      values: { ...settingsData().values, kubedoor_exturl: "https://kd/x" }
    })
  );
  legacy.form.alertmanager_exturl = "http://am.example.com:9093";
  assert.deepEqual(utils.cardErrors("access", legacy), {});
  assert.deepEqual(utils.cardPayload("access", legacy), {
    alertmanager_exturl: "http://am.example.com:9093"
  });
  // 恢复默认的项不用校验
  legacy.form.kubedoor_exturl = "";
  legacy.restores.add("kubedoor_exturl");
  assert.deepEqual(utils.cardPayload("access", legacy), {
    kubedoor_exturl: null,
    alertmanager_exturl: "http://am.example.com:9093"
  });
});

test("默认值和输入框提示的显示", () => {
  const defaults = utils.defaultsOf(settingsData());
  const text = key => utils.defaultText(key, defaults);
  assert.equal(text("prom_type"), "VictoriaMetrics 单机版");
  assert.equal(text("prom_url"), "未配置");
  assert.equal(text("prom_k8s_tag_key"), "origin_prometheus");
  assert.equal(text("image_repo"), DEFAULTS.image_repo);
  assert.equal(text("node_shell_image"), "Agent 内置的镜像");
  assert.equal(text("retention_events_days"), "90 天");
  assert.equal(text("auth_session_hours"), "12 小时");
  assert.equal(text("auth_lock_minutes"), "15 分钟");
  assert.equal(text("auth_cookie_secure"), "auto");
  assert.equal(text("agent_auth_required"), "开启");
  assert.equal(text("alert_dedup_window"), "300 秒");
  const placeholder = key => utils.placeholderOf(key, defaults);
  assert.equal(placeholder("retention_events_days"), "默认 90");
  assert.equal(
    placeholder("prom_k8s_tag_key"),
    "留空使用默认值 origin_prometheus"
  );
  assert.equal(
    placeholder("node_shell_image"),
    "留空使用 Agent 内置的默认镜像"
  );
  assert.match(placeholder("prom_url"), /^例如 http:\/\//);
  // 接口没给默认值时用约定里的
  assert.equal(
    utils.defaultText("retention_audit_days", utils.defaultsOf(null)),
    "180 天"
  );
});

test("时序库地址的格式说明跟着类型变化", () => {
  assert.match(
    utils.fieldHint("prom_url", "Victoria-Metrics-Single"),
    /格式：http:\/\/<主机>:8428；/
  );
  assert.match(
    utils.fieldHint("prom_url", "Victoria-Metrics-Cluster"),
    /:8481\/select\/<租户 ID>\/prometheus/
  );
  assert.match(
    utils.fieldHint("metrics_write_url", "Victoria-Metrics-Cluster"),
    /:8480\/insert\/<租户 ID>\/prometheus/
  );
  assert.match(
    utils.fieldHint("metrics_write_url", "Prometheus"),
    /9090\/api\/v1\/write/
  );
  assert.equal(
    utils.fieldHint("oss_url", "Prometheus"),
    utils.FIELDS.oss_url.hint
  );
  assert.match(
    utils.FIELDS.oss_url.hint,
    /<地址>\/<集群>\/<类型>\/<文件名>.*旧版 Agent 继续用安装时的地址/
  );
  assert.match(
    utils.FIELDS.prom_k8s_tag_key.warning,
    /^要和时序库里已有数据的标签一致；修改不会改 vmalert 规则、Alertmanager 分组、Grafana 看板和已安装集群的采集配置/
  );
});

test("保存后刷新表单：刚保存的和没改的换成返回值，其他卡片没保存的修改保留", () => {
  const ctx = contextOf();
  ctx.form.retention_events_days = 30;
  ctx.form.auth_session_hours = 24;
  ctx.form.agent_auth_required = true;
  ctx.restores.add("agent_auth_required");
  const after = settingsData();
  after.values.retention_events_days = 30;
  // 别人同时改了告警屏蔽缓存
  after.values.silence_cache_ttl = 30;
  const fresh = utils.formFromValues(after.values, utils.defaultsOf(after));
  const next = utils.mergeForm(ctx, fresh, ["retention_events_days"]);
  assert.equal(next.form.retention_events_days, 30);
  assert.equal(next.initial.retention_events_days, 30);
  assert.equal(next.form.silence_cache_ttl, 30);
  assert.equal(next.form.auth_session_hours, 24);
  assert.equal(next.initial.auth_session_hours, 12);
  assert.deepEqual(next.keep, ["auth_session_hours", "agent_auth_required"]);
  // 重新读取：全部换成返回值
  const reload = utils.mergeForm(ctx, fresh, null);
  assert.equal(reload.form.auth_session_hours, 12);
  assert.deepEqual(reload.keep, []);
});

test("测试连接的请求体用表单里当前的值，标签 key 留空用默认值", () => {
  const ctx = contextOf();
  assert.deepEqual(utils.metricsTestRequest(ctx), {
    prom_url: QUERY_URL,
    prom_k8s_tag_key: "origin_prometheus"
  });
  ctx.form.prom_url = "https://vm.example.com/select/0/prometheus/";
  ctx.form.prom_k8s_tag_key = " cluster ";
  assert.deepEqual(utils.metricsTestRequest(ctx), {
    prom_url: "https://vm.example.com/select/0/prometheus",
    prom_k8s_tag_key: "cluster"
  });
  ctx.form.prom_k8s_tag_key = "";
  assert.equal(
    utils.metricsTestRequest(ctx).prom_k8s_tag_key,
    "origin_prometheus"
  );
  ctx.form.prom_k8s_tag_key = "bad-key";
  assert.throws(
    () => utils.metricsTestRequest(ctx),
    /^Error: 集群指标标签 key：/
  );
  ctx.form.prom_k8s_tag_key = "cluster";
  ctx.form.prom_url = "http://other:******@192.0.2.11:8428";
  assert.throws(
    () => utils.metricsTestRequest(ctx),
    /^Error: 查询地址：改了账号、协议、主机或端口/
  );
  // 改了主机还想沿用已保存的密码：不能把密码发到别的地方
  ctx.form.prom_url = "http://monit:******@192.0.2.99:8428";
  assert.throws(() => utils.metricsTestRequest(ctx), /重新填写密码/);
  ctx.form.prom_url = "http://monit:new-secret@192.0.2.99:8428";
  assert.equal(
    utils.metricsTestRequest(ctx).prom_url,
    "http://monit:new-secret@192.0.2.99:8428"
  );
  ctx.form.prom_url = "  ";
  assert.throws(() => utils.metricsTestRequest(ctx), /请先填写查询地址/);
});

test("测试结果、请求出错和保存出错的提示", () => {
  assert.deepEqual(
    utils.testResultView({
      ok: true,
      level: "success",
      message: "连接正常，标签下有 3 个集群"
    }),
    { type: "success", text: "连接正常，标签下有 3 个集群" }
  );
  // 连接正常但查不到带标签的数据：master 返回 ok + warning
  assert.deepEqual(
    utils.testResultView({
      ok: true,
      level: "warning",
      message: "连接正常，但最近一天没有查到带 origin_prometheus 标签的数据"
    }),
    {
      type: "warning",
      text: "连接正常，但最近一天没有查到带 origin_prometheus 标签的数据"
    }
  );
  // 没有 level 时按 ok
  assert.deepEqual(utils.testResultView({ ok: true, message: "" }), {
    type: "success",
    text: "连接正常"
  });
  assert.deepEqual(utils.testResultView({ ok: false, message: "" }), {
    type: "error",
    text: "测试未通过"
  });
  assert.equal(utils.testErrorView({ response: { status: 401 } }), null);
  assert.deepEqual(
    utils.testErrorView({
      response: { status: 429, data: { message: "测试太频繁，请 2 秒后再试" } }
    }),
    { type: "warning", text: "测试太频繁，请 2 秒后再试" }
  );
  assert.deepEqual(utils.testErrorView({ response: { status: 429 } }), {
    type: "warning",
    text: "操作太频繁，请稍后再试"
  });
  assert.deepEqual(
    utils.testErrorView({
      response: { status: 422, data: { message: "请重新填写查询地址的密码" } }
    }),
    { type: "error", text: "请重新填写查询地址的密码" }
  );
  assert.deepEqual(utils.testErrorView({ response: { status: 502 } }), {
    type: "error",
    text: "测试失败（HTTP 502）"
  });
  assert.deepEqual(
    utils.testErrorView({ code: "ECONNABORTED", message: "timeout" }),
    { type: "error", text: "测试超时，请稍后再试" }
  );
  assert.match(utils.testErrorView(new Error("Network Error")).text, /连不上/);

  const http422 = {
    isAxiosError: true,
    response: { status: 422, data: { message: "查询地址：端口无效" } }
  };
  assert.equal(utils.errorText(http422, "保存失败"), "查询地址：端口无效");
  assert.equal(utils.fromHttp(http422), true);
  assert.equal(utils.fromHttp(new Error("x")), false);
  assert.equal(utils.errorText(new Error("x"), "保存失败"), "x");
  assert.equal(utils.errorText(null, "保存失败"), "保存失败");
});

test("上次修改人和有问题的项", () => {
  assert.equal(utils.updatedByLabel("legacy-import"), "旧配置导入");
  assert.equal(utils.updatedByLabel("kubedoor"), "kubedoor");
  assert.equal(utils.updatedByLabel(null), "系统");
  assert.deepEqual(
    utils.problemList({
      future_key: "新版本的配置项",
      oss_url: "已保存的地址无法解密（加密密钥变了？），请重新填写",
      prom_url: "无法解密",
      silence_cache_ttl: ""
    }),
    [
      { key: "prom_url", label: "查询地址", message: "无法解密" },
      {
        key: "oss_url",
        label: "对象存储上传地址",
        message: "已保存的地址无法解密（加密密钥变了？），请重新填写"
      },
      { key: "future_key", label: "future_key", message: "新版本的配置项" }
    ]
  );
  assert.deepEqual(utils.problemList(undefined), []);
});

/** 假的 PUT：null、空串和等于默认值的删掉这一项（同 master），其他值写进去 */
function echo(current, values) {
  const next = { ...current.values };
  const stored = new Set(current.stored);
  for (const [key, value] of Object.entries(values)) {
    if (value === null || value === "" || value === current.defaults[key]) {
      next[key] = current.defaults[key];
      stored.delete(key);
    } else {
      next[key] = value;
      stored.add(key);
    }
  }
  return {
    ...current,
    values: next,
    stored: [...stored],
    problems: {},
    updated_by: "admin",
    updated_at: "2026-11-15T11:00:00+08:00"
  };
}

async function harness(t, initial = settingsData()) {
  const oldWindow = global.window;
  global.window = { addEventListener() {}, removeEventListener() {} };
  const calls = { load: 0, save: [], test: [] };
  const messages = [];
  let current = initial;
  const hooks = {
    load: async () => ({ success: true, data: current }),
    save: async values => {
      current = echo(current, values);
      return { success: true, data: current };
    },
    test: async () => ({
      success: true,
      data: {
        ok: true,
        level: "success",
        message: "连接正常，标签 origin_prometheus 下有 2 个集群"
      }
    }),
    confirm: async () => {}
  };
  const api = {
    getGeneralSettings: () => {
      calls.load += 1;
      return hooks.load();
    },
    saveGeneralSettings: values => {
      calls.save.push(values);
      return hooks.save(values);
    },
    testMetricsSettings: request => {
      calls.test.push(request);
      return hooks.test(request);
    }
  };
  const component = compile(
    "../src/views/system/general-settings/index.vue",
    {
      "@/api/general-settings": api,
      "@/utils/message": {
        message: (text, options) => messages.push([text, options?.type])
      },
      "element-plus": { ElMessageBox: { confirm: () => hooks.confirm() } },
      "vue-router": { onBeforeRouteLeave() {} },
      "./general-settings-utils": utils
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
  // 挂载前可以改 hooks（比如让读取失败）
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

test("页面：读取失败时不能保存，避免用默认值覆盖已保存的配置", async t => {
  const { mount, calls, hooks } = await harness(t);
  hooks.load = async () => {
    throw {
      isAxiosError: true,
      response: { status: 503, data: { message: "数据库不可用" } }
    };
  };
  const vm = await mount();
  assert.equal(vm.loaded, false);
  assert.equal(vm.loadError, "数据库不可用");
  vm.form.retention_events_days = 30;
  assert.equal(vm.cardDirty.retention, false);
  await vm.saveCard("retention");
  await vm.runMetricsTest();
  assert.deepEqual(calls.save, []);
  assert.deepEqual(calls.test, []);
});

test("页面：每张卡片单独保存，只提交这张卡片改过的项，其他卡片的修改保留", async t => {
  const { mount, calls, messages } = await harness(t);
  const vm = await mount();
  assert.equal(vm.loaded, true);
  assert.equal(vm.dirty, false);
  assert.equal(vm.expanded.advanced, false);
  assert.equal(vm.expanded.metrics, true);
  assert.equal(vm.modifiedText, "上次修改：kubedoor · 2026-11-15 10:00:00");
  assert.equal(vm.isStored("prom_url"), true);
  assert.equal(vm.isStored("oss_url"), false);

  vm.form.retention_events_days = 30;
  vm.form.auth_session_hours = 24;
  assert.equal(vm.cardDirty.retention, true);
  assert.equal(vm.cardDirty.auth, true);
  assert.equal(vm.cardDirty.metrics, false);
  assert.equal(vm.changed("retention_events_days"), true);

  await vm.saveCard("retention");
  assert.deepEqual(calls.save, [{ retention_events_days: 30 }]);
  assert.deepEqual(messages.at(-1), ["已保存「数据保留」", "success"]);
  assert.equal(vm.cardDirty.retention, false);
  assert.equal(vm.isStored("retention_events_days"), true);
  assert.equal(vm.cardDirty.auth, true);
  assert.equal(vm.form.auth_session_hours, 24);
  assert.equal(vm.modifiedText, "上次修改：admin · 2026-11-15 11:00:00");

  await vm.saveCard("auth");
  assert.deepEqual(calls.save.at(-1), { auth_session_hours: 24 });
  assert.equal(vm.dirty, false);
  // 没改动时不发请求
  await vm.saveCard("metrics");
  assert.equal(calls.save.length, 2);
});

test("页面：校验不通过不发请求并指出是哪一项，改对后再保存", async t => {
  const { mount, calls, messages } = await harness(t);
  const vm = await mount();
  vm.form.alert_dedup_window = 90000;
  await vm.saveCard("advanced");
  assert.deepEqual(calls.save, []);
  assert.deepEqual(messages.at(-1), [
    "事件告警去重窗口（秒）：需为 0 到 86400 之间的整数",
    "warning"
  ]);
  assert.equal(vm.expanded.advanced, true);
  assert.equal(vm.fieldErrors.alert_dedup_window, "需为 0 到 86400 之间的整数");
  vm.form.alert_dedup_window = 600;
  assert.equal(vm.fieldErrors.alert_dedup_window, undefined);
  await vm.saveCard("advanced");
  assert.deepEqual(calls.save, [{ alert_dedup_window: 600 }]);
});

test("页面：保存失败以服务端返回的原因为准，不重复弹提示，修改保留", async t => {
  const { mount, calls, messages, hooks } = await harness(t);
  const vm = await mount();
  hooks.save = async () => {
    throw {
      isAxiosError: true,
      response: {
        status: 422,
        data: {
          success: false,
          code: "invalid_config",
          message: "请重新填写远程写地址的密码"
        }
      }
    };
  };
  vm.form.metrics_write_url =
    "http://monit:******@192.0.2.11:8428/insert/0/prometheus";
  const count = messages.length;
  await vm.saveCard("metrics");
  assert.deepEqual(calls.save, [
    {
      metrics_write_url:
        "http://monit:******@192.0.2.11:8428/insert/0/prometheus"
    }
  ]);
  assert.equal(vm.serverErrors.metrics, "请重新填写远程写地址的密码");
  // http 封装已经弹过，页面不再弹
  assert.equal(messages.length, count);
  assert.equal(vm.cardDirty.metrics, true);
  assert.equal(vm.saving, "");

  // 不是 http 错误（接口返回 success: false）时由页面提示
  hooks.save = async () => ({ success: false, message: "保存失败了" });
  await vm.saveCard("metrics");
  assert.equal(vm.serverErrors.metrics, "保存失败了");
  assert.deepEqual(messages.at(-1), ["保存失败了", "error"]);
});

test("页面：恢复默认在保存时提交 null，撤销后不提交", async t => {
  const { mount, calls } = await harness(t);
  const vm = await mount();
  assert.equal(vm.canRestore("agent_auth_required"), true);
  assert.equal(vm.canRestore("oss_url"), false);
  vm.restoreDefault("agent_auth_required");
  assert.equal(vm.form.agent_auth_required, true);
  assert.equal(vm.pending("agent_auth_required"), true);
  assert.equal(vm.canRestore("agent_auth_required"), false);
  vm.undoRestore("agent_auth_required");
  assert.equal(vm.form.agent_auth_required, false);
  assert.equal(vm.cardDirty.auth, false);

  vm.restoreDefault("image_repo");
  assert.equal(vm.cardDirty.agent, true);
  await vm.saveCard("agent");
  assert.deepEqual(calls.save, [{ image_repo: null }]);
  assert.equal(vm.isStored("image_repo"), false);
  assert.equal(vm.pending("image_repo"), false);
  assert.equal(vm.form.image_repo, DEFAULTS.image_repo);
  assert.equal(vm.cardDirty.agent, false);

  // 手动改成默认值：master 同样删掉这一项，显示为「默认」
  vm.form.retention_audit_days = 180;
  await vm.saveCard("retention");
  assert.deepEqual(calls.save.at(-1), { retention_audit_days: 180 });
  assert.equal(vm.isStored("retention_audit_days"), false);
  assert.equal(vm.cardDirty.retention, false);
});

test("页面：测试连接用表单里当前的值，结果按级别显示，429 用服务端的提示", async t => {
  const { mount, calls, messages, hooks } = await harness(t);
  const vm = await mount();
  vm.form.prom_url = "http://monit:new-secret@192.0.2.30:8428";
  await vm.runMetricsTest();
  assert.deepEqual(calls.test, [
    {
      prom_url: "http://monit:new-secret@192.0.2.30:8428",
      prom_k8s_tag_key: "origin_prometheus"
    }
  ]);
  assert.deepEqual(vm.testResult, {
    type: "success",
    text: "连接正常，标签 origin_prometheus 下有 2 个集群"
  });
  assert.deepEqual(messages.at(-1), ["连接正常", "success"]);
  // 测试不会保存，也不影响未保存标记
  assert.deepEqual(calls.save, []);
  assert.equal(vm.cardDirty.metrics, true);

  hooks.test = async () => ({
    success: true,
    data: {
      ok: true,
      level: "warning",
      message: "连接正常，但最近一天没有查到带 origin_prometheus 标签的数据"
    }
  });
  await vm.runMetricsTest();
  assert.equal(vm.testResult.type, "warning");
  assert.deepEqual(messages.at(-1), [
    "连接正常，但有需要留意的地方，见页面上的测试结果",
    "warning"
  ]);

  hooks.test = async () => ({
    success: true,
    data: { ok: false, level: "error", message: "时序库连接失败或超时" }
  });
  await vm.runMetricsTest();
  assert.deepEqual(vm.testResult, {
    type: "error",
    text: "时序库连接失败或超时"
  });
  assert.deepEqual(messages.at(-1), [
    "测试未通过，原因见页面上的测试结果",
    "warning"
  ]);

  hooks.test = async () => {
    throw {
      response: { status: 429, data: { message: "测试太频繁，请 2 秒后再试" } }
    };
  };
  await vm.runMetricsTest();
  assert.deepEqual(messages.at(-1), ["测试太频繁，请 2 秒后再试", "warning"]);
  assert.equal(vm.testing, false);

  // 地址不合法、改了主机还保留 ****** 时不发请求
  const count = calls.test.length;
  vm.form.prom_url = "192.0.2.30:8428";
  await vm.runMetricsTest();
  vm.form.prom_url = "http://monit:******@192.0.2.30:8428";
  await vm.runMetricsTest();
  assert.equal(calls.test.length, count);
  assert.match(messages.at(-1)[0], /^查询地址：改了账号、协议、主机或端口/);
});

test("页面：有未保存的修改时重新加载要确认；有问题的项展开所在卡片", async t => {
  const problems = {
    admis_cache_ttl: "库里的值无效，已使用默认值",
    oss_url: "已保存的地址无法解密（加密密钥变了？），请重新填写"
  };
  const { mount, calls, hooks } = await harness(
    t,
    settingsData({ problems, stored: [...settingsData().stored, "oss_url"] })
  );
  const vm = await mount();
  assert.equal(vm.expanded.advanced, true);
  assert.deepEqual(
    vm.problemItems.map(item => item.label),
    ["对象存储上传地址", "准入查询缓存（秒）"]
  );
  assert.equal(vm.problems.oss_url, problems.oss_url);

  vm.form.retention_events_days = 30;
  hooks.confirm = async () => {
    throw "cancel";
  };
  await vm.load(true);
  assert.equal(calls.load, 1);
  assert.equal(vm.form.retention_events_days, 30);

  hooks.confirm = async () => {};
  await vm.load(true);
  assert.equal(calls.load, 2);
  assert.equal(vm.form.retention_events_days, 90);
  assert.equal(vm.dirty, false);
});

test("路由：通用配置在镜像管理之前，事件告警规则在消息通知之后，只对管理员开放", () => {
  const routes = compile("../src/router/modules/system.ts").default;
  const names = routes.children.map(route => route.name);
  const general = names.indexOf("SystemGeneralSettings");
  const rules = names.indexOf("SystemEventAlertRules");
  assert.equal(names[general + 1], "SystemImageSettings");
  assert.equal(names[rules - 1], "SystemNotification");
  assert.deepEqual(
    [routes.children[general], routes.children[rules]].map(route => [
      route.path,
      route.meta.title,
      route.meta.roles
    ]),
    [
      ["/system/general-settings", "通用配置", ["admin"]],
      ["/system/event-alert-rules", "事件告警规则", ["admin"]]
    ]
  );
});

test("审计页登记了通用配置、事件告警规则、消息通知的修改动作", () => {
  const source = fs.readFileSync(
    path.resolve(__dirname, "../src/views/system/audit/index.vue"),
    "utf8"
  );
  for (const [action, label] of [
    ["general_settings.update", "修改通用配置"],
    ["event_rules.update", "修改事件告警规则"],
    ["notification.update", "修改消息通知"]
  ])
    assert.ok(source.includes(`"${action}": "${label}"`), action);
});
