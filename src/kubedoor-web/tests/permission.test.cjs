const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");

const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");

// src/utils/permission.ts 只有 type import,单文件编译后直接加载
function compileTs(file) {
  const sourcePath = path.resolve(__dirname, "../src/utils", file);
  const output = transformSync(fs.readFileSync(sourcePath, "utf8"), {
    loader: "ts",
    target: "es2020",
    format: "cjs"
  });
  const loaded = new Module(sourcePath, module);
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath));
  loaded._compile(output.code, sourcePath);
  return loaded.exports;
}
const perm = compileTs("permission.ts");

// 默认配置里 CoreDNS / Istio 只有管理员,其余修改读写及以上
const OPERATIONS = {
  "pod.shell": ["rw", "admin"],
  "network.coredns": ["admin"],
  "network.istio": ["admin"],
  "pod.exec.view": ["read", "rw", "admin"],
  "balance.run": ["rw", "admin"],
  "alert.silence": ["rw", "admin"],
  closed: []
};

const user = (role, clusters = {}, extra = {}) => ({
  role,
  defaultPermission: perm.roleDefaultPermission(role),
  clusters,
  operations: OPERATIONS,
  ...extra
});

test("角色的默认集群权限:系统管理员和集群管理员都是管理员", () => {
  assert.equal(perm.roleDefaultPermission("admin"), "admin");
  assert.equal(perm.roleDefaultPermission("cluster_admin"), "admin");
  assert.equal(perm.roleDefaultPermission("rw"), "rw");
  assert.equal(perm.roleDefaultPermission("read"), "read");
  assert.equal(perm.roleDefaultPermission("none"), "none");
  assert.equal(perm.roleDefaultPermission("agent"), "none");
  assert.equal(perm.roleDefaultPermission(""), "none");
});

test("有效权限:系统管理员一律管理员,单独授权优先,其余按全局权限兜底", () => {
  assert.equal(
    perm.permissionOf(user("admin", { prod: "read" }), "prod"),
    "admin"
  );
  const clusterAdmin = user("cluster_admin", { prod: "rw", hidden: "none" });
  assert.equal(perm.permissionOf(clusterAdmin, "prod"), "rw");
  assert.equal(perm.permissionOf(clusterAdmin, "hidden"), "none");
  assert.equal(perm.permissionOf(clusterAdmin, "test"), "admin");
  assert.equal(
    perm.permissionOf(user("none", { test: "admin" }), "test"),
    "admin"
  );
  assert.equal(perm.permissionOf(user("none"), "test"), "none");
  // 没传 env 时给全局兜底权限;没有 defaultPermission 时按角色推算
  assert.equal(perm.permissionOf(user("read"), ""), "read");
  assert.equal(perm.permissionOf({ role: "rw", clusters: {} }, "x"), "rw");
});

test("can:按集群的有效权限查 operations", () => {
  const rw = user("rw", { prod: "admin", staging: "read" });
  assert.equal(perm.can("pod.shell", "test", rw), true);
  assert.equal(perm.can("network.coredns", "test", rw), false);
  assert.equal(perm.can("network.coredns", "prod", rw), true);
  assert.equal(perm.can("pod.shell", "staging", rw), false);
  assert.equal(perm.can("pod.exec.view", "staging", rw), true);
  // 系统管理员也受「管理员」列约束:关闭的操作谁都不能做
  assert.equal(perm.can("closed", "test", user("admin")), false);
  assert.equal(perm.can("network.istio", "test", user("admin")), true);
});

test("can:没有集群、看不到集群、未知操作、没有 operations 一律不能", () => {
  const admin = user("admin");
  assert.equal(perm.can("pod.shell", "", admin), false);
  assert.equal(perm.can("pod.shell", undefined, admin), false);
  assert.equal(perm.can("unknown.op", "test", admin), false);
  assert.equal(perm.can("pod.exec.view", "test", user("none")), false);
  assert.equal(
    perm.can("pod.exec.view", "x", user("read", { x: "none" })),
    false
  );
  assert.equal(perm.can("pod.shell", "test", { role: "admin" }), false);
});

test("canGlobal:按全局角色对应的默认权限查表", () => {
  assert.equal(perm.canGlobal("balance.run", user("rw")), true);
  assert.equal(perm.canGlobal("balance.run", user("read")), false);
  assert.equal(perm.canGlobal("network.istio", user("rw")), false);
  assert.equal(perm.canGlobal("network.istio", user("cluster_admin")), true);
  assert.equal(perm.canGlobal("network.istio", user("admin")), true);
  // 单独授权不影响全局判断
  assert.equal(
    perm.canGlobal("network.istio", user("none", { prod: "admin" })),
    false
  );
  assert.equal(perm.canGlobal("closed", user("admin")), false);
});

test("canUpdateImage:有「更新镜像」权限放行,只读账号留给后端按白名单判断", () => {
  const ops = levels => ({ ...OPERATIONS, "workload.image": levels });
  const rw = user(
    "rw",
    { staging: "read" },
    { operations: ops(["rw", "admin"]) }
  );
  assert.equal(perm.canUpdateImage("test", rw), true);
  // 只读集群:可能在 UPDATE_IMAGE 白名单里,不置灰
  assert.equal(perm.canUpdateImage("staging", rw), true);
  // 「管理员」列也取消(全站关闭):谁都不能
  const closed = user("read", {}, { operations: ops([]) });
  assert.equal(perm.canUpdateImage("test", closed), false);
  // 读写列取消时读写账号不能(白名单只给只读账号)
  const adminOnly = user("rw", {}, { operations: ops(["admin"]) });
  assert.equal(perm.canUpdateImage("test", adminOnly), false);
  assert.equal(
    perm.canUpdateImage(
      "test",
      user("cluster_admin", {}, { operations: ops(["admin"]) })
    ),
    true
  );
  assert.equal(perm.canUpdateImage("", rw), false);
  assert.equal(
    perm.canUpdateImage(
      "hidden",
      user("none", {}, { operations: ops(["read", "rw", "admin"]) })
    ),
    false
  );
});

test("silenceEnvs:env / k8s / cluster 的「=」条件是限定的集群,否则是全局规则", () => {
  assert.equal(perm.silenceEnvs([]), null);
  assert.equal(perm.silenceEnvs(null), null);
  assert.equal(
    perm.silenceEnvs([{ key: "alertname", op: "=", value: "x" }]),
    null
  );
  assert.equal(
    perm.silenceEnvs([{ key: "env", op: "=~", value: "prod.*" }]),
    null
  );
  assert.equal(
    perm.silenceEnvs([{ key: "env", op: "!=", value: "prod" }]),
    null
  );
  assert.equal(perm.silenceEnvs([{ key: "env", op: "=", value: " " }]), null);
  assert.deepEqual(
    perm.silenceEnvs([
      { key: "env", op: "=", value: "prod" },
      { key: " k8s ", op: "=", value: "test" },
      { key: "cluster", value: "prod" },
      { key: "pod", op: "=", value: "x" }
    ]),
    ["prod", "test"]
  );
});

test("canSilence / canSilenceAny:限定集群的按集群,全局规则按全局角色", () => {
  const rw = user(
    "rw",
    { prod: "read" },
    { clusterPermissions: { prod: "read", test: "rw" } }
  );
  assert.equal(
    perm.canSilence([{ key: "env", op: "=", value: "test" }], rw),
    true
  );
  assert.equal(
    perm.canSilence([{ key: "env", op: "=", value: "prod" }], rw),
    false
  );
  assert.equal(
    perm.canSilence(
      [
        { key: "env", op: "=", value: "test" },
        { key: "k8s", op: "=", value: "prod" }
      ],
      rw
    ),
    false
  );
  assert.equal(
    perm.canSilence([{ key: "alertname", op: "=", value: "x" }], rw),
    true
  );
  const reader = user(
    "read",
    { test: "rw" },
    { clusterPermissions: { test: "rw", prod: "read" } }
  );
  assert.equal(
    perm.canSilence([{ key: "alertname", op: "=", value: "x" }], reader),
    false
  );
  assert.equal(
    perm.canSilence([{ key: "env", op: "=", value: "test" }], reader),
    true
  );
  assert.equal(perm.canSilenceAny(reader), true);
  const readOnly = user("read", {}, { clusterPermissions: { test: "read" } });
  assert.equal(perm.canSilenceAny(readOnly), false);
  assert.equal(perm.canSilenceAny(user("rw")), true);
});

test("勾选联动:勾上一列时更高的列跟着勾上", () => {
  const none = { read: false, rw: false, admin: false };
  assert.deepEqual(perm.linkLevels(none, "read", true), {
    read: true,
    rw: true,
    admin: true
  });
  assert.deepEqual(perm.linkLevels(none, "rw", true), {
    read: false,
    rw: true,
    admin: true
  });
  assert.deepEqual(perm.linkLevels(none, "admin", true), {
    read: false,
    rw: false,
    admin: true
  });
});

test("勾选联动:取消一列时更低的列跟着取消,不改原对象", () => {
  const all = { read: true, rw: true, admin: true };
  assert.deepEqual(perm.linkLevels(all, "admin", false), {
    read: false,
    rw: false,
    admin: false
  });
  assert.deepEqual(perm.linkLevels(all, "rw", false), {
    read: false,
    rw: false,
    admin: true
  });
  assert.deepEqual(perm.linkLevels(all, "read", false), {
    read: false,
    rw: true,
    admin: true
  });
  assert.deepEqual(all, { read: true, rw: true, admin: true });
});

test("联动的结果总是单调的", () => {
  const levels = ["read", "rw", "admin"];
  for (let bits = 0; bits < 8; bits++) {
    const start = { read: !!(bits & 1), rw: !!(bits & 2), admin: !!(bits & 4) };
    for (const level of levels) {
      for (const checked of [true, false]) {
        const next = perm.linkLevels(start, level, checked);
        if (perm.isMonotonic(start)) assert.ok(perm.isMonotonic(next));
        assert.equal(next[level], checked);
      }
    }
  }
  assert.equal(perm.isMonotonic({ read: true, rw: false, admin: true }), false);
  assert.equal(
    perm.isMonotonic({ read: false, rw: true, admin: false }),
    false
  );
  assert.equal(
    perm.isMonotonic({ read: false, rw: false, admin: false }),
    true
  );
});

test("samePolicy:判断有没有改动", () => {
  const base = () => ({
    ops: {
      "pod.shell": { read: false, rw: true, admin: true, approval: false },
      "pod.delete": { read: false, rw: true, admin: true, approval: true }
    },
    commands: { view: ["ls", "cat"], danger: ["rm"] }
  });
  assert.equal(perm.samePolicy(base(), base()), true);
  const approval = base();
  approval.ops["pod.delete"].approval = false;
  assert.equal(perm.samePolicy(approval, base()), false);
  const level = base();
  level.ops["pod.shell"].read = true;
  assert.equal(perm.samePolicy(level, base()), false);
  const order = base();
  order.commands.view = ["cat", "ls"];
  assert.equal(perm.samePolicy(order, base()), false);
  const missing = base();
  delete missing.ops["pod.delete"];
  assert.equal(perm.samePolicy(missing, base()), false);
  assert.equal(perm.samePolicy(base(), missing), false);
});

test("normalizeCommand:去掉首尾空白、合并中间空白", () => {
  assert.equal(perm.normalizeCommand("  find   -delete "), "find -delete");
  assert.equal(perm.normalizeCommand("\tls\n"), "ls");
  assert.equal(perm.normalizeCommand("   "), "");
});

test("commandError:通用规则", () => {
  const lists = { view: ["ls"], danger: ["rm"] };
  assert.match(perm.commandError("view", "", lists), /请输入/);
  assert.match(perm.commandError("view", "a".repeat(65), lists), /64/);
  for (const bad of [
    "ls;id",
    "a|b",
    "a&",
    "x>y",
    "$(id)",
    "`id`",
    "'ls'",
    '"ls"',
    "a\\b",
    "(ls)"
  ]) {
    assert.match(perm.commandError("view", bad, lists), /元字符/, bad);
    assert.match(perm.commandError("danger", bad, lists), /元字符/, bad);
  }
  assert.match(perm.commandError("view", "/bin/ls2", lists), /路径/);
  assert.match(perm.commandError("danger", "/bin/rm2", lists), /路径/);
  assert.match(perm.commandError("view", "ls", lists), /已经在清单里/);
  assert.match(perm.commandError("danger", "rm", lists), /已经在清单里/);
  const full = {
    view: Array.from({ length: 300 }, (_, i) => `c${i}`),
    danger: []
  };
  assert.match(perm.commandError("view", "more", full), /300/);
});

test("commandError:查看命令只能是单个命令名", () => {
  const lists = { view: [], danger: ["rm", "mkfs*", "find -delete"] };
  assert.equal(perm.commandError("view", "jq", lists), "");
  assert.match(perm.commandError("view", "ls -l", lists), /单个命令名/);
  assert.match(perm.commandError("view", "ls*", lists), /单个命令名/);
  assert.match(perm.commandError("view", "rm", lists), /高危命令清单/);
  assert.match(perm.commandError("view", "mkfs.ext4", lists), /mkfs\*/);
  // 「命令 参数」是对 find 的细化,不算同名
  assert.equal(perm.commandError("view", "find", lists), "");
});

test("commandError:高危命令是「命令」「前缀*」「命令 参数」", () => {
  const lists = { view: ["ls", "find", "mkfs.ext4"], danger: [] };
  assert.equal(perm.commandError("danger", "shred", lists), "");
  assert.equal(perm.commandError("danger", "kill*", lists), "");
  assert.equal(perm.commandError("danger", "find -delete", lists), "");
  assert.equal(perm.commandError("danger", "ls -R", lists), "");
  assert.match(perm.commandError("danger", "a b c", lists), /命令 参数/);
  assert.match(perm.commandError("danger", "*rm", lists), /末尾/);
  assert.match(perm.commandError("danger", "r*m", lists), /末尾/);
  assert.match(perm.commandError("danger", "*", lists), /末尾/);
  assert.match(perm.commandError("danger", "find -del*", lists), /不能用 \*/);
  assert.match(perm.commandError("danger", "ls", lists), /查看命令清单/);
  assert.match(perm.commandError("danger", "mkfs*", lists), /mkfs\.ext4/);
});

test("默认清单本身合法:逐项加入不报错", () => {
  const defaults = JSON.parse(
    fs.readFileSync(
      path.resolve(
        __dirname,
        "../../kubedoor-tools/kubedoor_tools/default_commands.json"
      ),
      "utf8"
    )
  );
  const lists = { view: [], danger: [] };
  for (const kind of ["view", "danger"]) {
    for (const item of defaults[kind]) {
      assert.equal(
        perm.commandError(kind, item, lists),
        "",
        `${kind}: ${item}`
      );
      lists[kind].push(item);
    }
  }
});

test("内置参数检查说明和高危匹配方式", () => {
  assert.match(perm.VIEW_COMMAND_CHECKS.curl, /GET/);
  assert.match(perm.VIEW_COMMAND_CHECKS.find, /-delete/);
  assert.equal(perm.VIEW_COMMAND_CHECKS.ls, undefined);
  assert.equal(perm.dangerMatchText("rm"), "命令 rm");
  assert.equal(perm.dangerMatchText("mkfs*"), "命令名以 mkfs 开头");
  assert.equal(
    perm.dangerMatchText("find -delete"),
    "find 命令带 -delete 参数"
  );
});

test("选项和标签覆盖新角色、新集群权限", () => {
  assert.equal(perm.ROLE_LABELS.admin, "系统管理员");
  assert.equal(perm.ROLE_LABELS.cluster_admin, "集群管理员");
  assert.equal(perm.PERM_LABELS.admin, "管理员");
  assert.deepEqual(
    perm.PERM_OPTIONS.map(item => item.value),
    ["admin", "rw", "read", "none"]
  );
  for (const value of ["admin", "rw", "read", "none"]) {
    assert.ok(perm.PERM_TAG[value], value);
  }
});
