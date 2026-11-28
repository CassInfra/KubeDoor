const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");
const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

/** 编译加载单个 TS 模块(同 general-settings.test.cjs 的做法) */
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

const {
  DeploymentEnvError,
  parseDeploymentEnv,
  filterEnvRows,
  isSensitiveEnvName,
  toEnvText
} = compile("../src/views/monit/utils/deploymentEnv.ts");

const YAML = `
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo
  namespace: ops
spec:
  template:
    spec:
      containers:
        - name: app
          image: registry/demo:v1
          env:
            - name: APP_NAME
              value: demo
            - name: EMPTY_VALUE
              value: ""
            - name: DB_PASSWORD
              value: root
            - name: LOG_LEVEL
              valueFrom:
                configMapKeyRef:
                  name: app-config
                  key: LOG_LEVEL
            - name: DB_PASS
              valueFrom:
                secretKeyRef:
                  name: app-secret
                  key: password
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: CPU_LIMIT
              valueFrom:
                resourceFieldRef:
                  containerName: app
                  resource: limits.cpu
                  divisor: 100m
          envFrom:
            - configMapRef:
                name: app-config
              prefix: APP_
            - secretRef:
                name: app-secret
        - name: sidecar
          image: registry/sidecar:v1
          env:
            - name: SIDECAR_PORT
              value: "8080"
`;

test("解析主容器的 env:直接值、ConfigMap / Secret 键、字段和资源引用", () => {
  const parsed = parseDeploymentEnv(YAML);
  assert.equal(parsed.containers.length, 2);
  assert.equal(parsed.main.name, "app");
  assert.equal(parsed.main.image, "registry/demo:v1");
  assert.equal(parsed.main.env.length, 7);

  const byName = Object.fromEntries(
    parsed.main.env.map(row => [row.name, row])
  );

  assert.deepEqual(byName.APP_NAME, {
    name: "APP_NAME",
    value: "demo",
    source: "literal",
    sourceLabel: "直接设置",
    sourceDetail: "",
    ref: null,
    fromSecret: false,
    sensitive: false
  });
  // 空字符串也是显式的值,不能当成"没有值"
  assert.equal(byName.EMPTY_VALUE.value, "");
  assert.equal(byName.EMPTY_VALUE.source, "literal");
  assert.equal(byName.EMPTY_VALUE.sensitive, false);

  assert.equal(byName.LOG_LEVEL.source, "configmap");
  assert.equal(byName.LOG_LEVEL.sourceDetail, "app-config / LOG_LEVEL");
  assert.equal(byName.LOG_LEVEL.value, "");

  assert.equal(byName.DB_PASS.source, "secret");
  assert.equal(byName.DB_PASS.fromSecret, true);
  assert.equal(byName.DB_PASS.sensitive, true);
  assert.equal(byName.DB_PASS.sourceDetail, "app-secret / password");

  assert.equal(byName.POD_NAME.source, "field");
  assert.equal(byName.POD_NAME.sourceDetail, "metadata.name");

  assert.equal(byName.CPU_LIMIT.source, "resource");
  assert.equal(byName.CPU_LIMIT.sourceDetail, "app / limits.cpu");
  assert.equal(byName.CPU_LIMIT.ref.divisor, "100m");
});

test("envFrom 保留类型、名字和前缀", () => {
  const { main } = parseDeploymentEnv(YAML);
  assert.deepEqual(main.envFrom, [
    {
      kind: "configmap",
      name: "app-config",
      prefix: "APP_",
      optional: false
    },
    { kind: "secret", name: "app-secret", prefix: "", optional: false }
  ]);
});

test("没有 env / envFrom / 容器时返回空结构", () => {
  const parsed = parseDeploymentEnv(`
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          image: registry/demo:v1
`);
  assert.equal(parsed.containers.length, 1);
  assert.deepEqual(parsed.main.env, []);
  assert.deepEqual(parsed.main.envFrom, []);

  const empty = parseDeploymentEnv("apiVersion: apps/v1\nkind: Deployment\n");
  assert.deepEqual(empty.containers, []);
  assert.equal(empty.main, null);
});

test("YAML 解析失败抛 DeploymentEnvError", () => {
  assert.throws(() => parseDeploymentEnv("a: [1, 2"), DeploymentEnvError);
  assert.throws(
    () => parseDeploymentEnv("a: [1, 2"),
    /Deployment YAML 解析失败/
  );
});

test("变量名像密码 / 密钥时默认打码", () => {
  for (const name of [
    "PASSWORD",
    "password",
    "DB_PASSWORD",
    "PASSWD",
    "PWD",
    "DB_PWD",
    "PASS",
    "SECRET",
    "SECRET_KEY",
    "TOKEN",
    "JWT_TOKEN",
    "CREDENTIALS",
    "API_KEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "KEY",
    "MY_KEY",
    "AUTH",
    "AUTH_MODE"
  ]) {
    assert.equal(isSensitiveEnvName(name), true, name);
  }
  for (const name of [
    "APP_NAME",
    "NODE_ENV",
    "JAVA_OPTS",
    "KEYCLOAK_URL",
    "BYPASS_URL",
    "AUTHOR",
    "MONITOR",
    "SPRING_PROFILES_ACTIVE"
  ]) {
    assert.equal(isSensitiveEnvName(name), false, name);
  }
});

test("复制成 KEY=value,引用来的写成注释", () => {
  const { main } = parseDeploymentEnv(YAML);
  assert.equal(
    toEnvText(main.env),
    [
      "APP_NAME=demo",
      "EMPTY_VALUE=",
      "DB_PASSWORD=root",
      "# LOG_LEVEL 来自 ConfigMap app-config / LOG_LEVEL",
      "# DB_PASS 来自 Secret app-secret / password",
      "# POD_NAME 来自 字段引用 metadata.name",
      "# CPU_LIMIT 来自 资源引用 app / limits.cpu"
    ].join("\n")
  );
});

test("按变量名和来源过滤,不看值", () => {
  const { main } = parseDeploymentEnv(YAML);
  assert.deepEqual(
    filterEnvRows(main.env, "pass").map(row => row.name),
    ["DB_PASSWORD", "DB_PASS"]
  );
  assert.deepEqual(
    filterEnvRows(main.env, "configmap").map(row => row.name),
    ["LOG_LEVEL"]
  );
  assert.deepEqual(
    filterEnvRows(main.env, "  ").map(row => row.name),
    main.env.map(row => row.name)
  );
});
