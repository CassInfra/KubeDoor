const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");
const yaml = require("js-yaml");

const { transformSync } = Module.createRequire(require.resolve("vite/package.json"))("esbuild");

function compileTs(file) {
  const sourcePath = path.resolve(__dirname, "../src/views/monit/coredns", file);
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
const cf = compileTs("corefile.ts");
const df = compileTs("diff.ts");

// 用户给的真实 ConfigMap(Helm 安装的 CoreDNS,Corefile 用 |2- 块)
const CONFIGMAP = `apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system

  labels:
    addonmanager.kubernetes.io/mode: EnsureExists
    app.kubernetes.io/managed-by: Helm
    k8slens-edit-resource-version: v1
  annotations:
    helm.sh/resource-policy: keep
    meta.helm.sh/release-name: coredns
    meta.helm.sh/release-namespace: kube-system

data:
  Corefile: |2-
        .:53 {
            template ANY HINFO . {
                rcode NXDOMAIN
            }
        hosts {
          10.114.1.4    beta-order2-es.casstime.cn
          #10.114.0.10   beta-new-bill-bosch-wms-rds.casstime.cn       # 10.2.8.49     beta-new-bill-bosch-wms
          #10.114.0.13   beta-yizhangtong-rds.casstime.cn              # 10.2.35.184   beta-yizhangtong
          #10.114.0.11   beta-beihuo-rds.casstime.cn                   # 10.2.33.10    beta-备货计划
          10.114.0.5    beta-bizme-rds.casstime.cn                    # 10.2.17.219   beta-bizme-new

          # ── Elasticsearch ──
          10.114.0.230    beta-sand-es.casstime.cn                      # 10.2.21.92    beta-sand-7102
          10.114.1.0    beta-zeebe-kunlun-es.casstime.cn              # 10.2.53.169   hwbeta-zeebe-kunlun

          fallthrough
        }
            errors
            health {
                lameduck 30s
            }
            ready
            kubernetes cluster.local. in-addr.arpa ip6.arpa {
                pods insecure
                fallthrough in-addr.arpa ip6.arpa
            }
            prometheus :9153
            forward . /etc/resolv.conf {
                prefer_udp
            }
            cache 30
            reload
            loadbalance
        }
`;
const COREFILE = yaml.load(CONFIGMAP).data.Corefile;

function load(text) {
  const model = cf.parseCorefile(text);
  let id = 0;
  const rows = model.blocks.flatMap(block =>
    block.lines.filter(cf.isRowLine).map(line => cf.rowFromLine(line, block.index, ++id))
  );
  return { model, rows, nextId: () => ++id };
}

const lineOf = (text, n) => text.split("\n")[n];
const changedLines = (a, b) => {
  const x = a.split("\n");
  const y = b.split("\n");
  return { removed: x.filter(l => !y.includes(l)), added: y.filter(l => !x.includes(l)) };
};

test("parses the hosts block of the real-world Corefile", () => {
  const { model, rows } = load(COREFILE);
  assert.equal(model.error, "");
  assert.equal(model.servers.length, 1);
  assert.equal(model.servers[0].label, ".:53");
  assert.equal(model.servers[0].port, 53);
  assert.ok(model.servers[0].directives.includes("reload"));
  assert.equal(model.blocks.length, 1);
  const block = model.blocks[0];
  assert.equal(block.editable, true);
  assert.equal(block.hasFallthrough, true);
  assert.deepEqual(block.zones, ["."]);
  assert.deepEqual(
    block.lines.map(l => l.kind),
    ["entry", "entry", "entry", "entry", "entry", "blank", "comment", "entry", "entry", "blank", "directive"]
  );
  assert.equal(rows.length, 8);
  assert.deepEqual(
    rows.map(r => [r.kind, r.enabled, r.ip, r.names, r.remark, r.text]),
    [
      ["entry", true, "10.114.1.4", "beta-order2-es.casstime.cn", "", ""],
      ["entry", false, "10.114.0.10", "beta-new-bill-bosch-wms-rds.casstime.cn", "10.2.8.49     beta-new-bill-bosch-wms", ""],
      ["entry", false, "10.114.0.13", "beta-yizhangtong-rds.casstime.cn", "10.2.35.184   beta-yizhangtong", ""],
      ["entry", false, "10.114.0.11", "beta-beihuo-rds.casstime.cn", "10.2.33.10    beta-备货计划", ""],
      ["entry", true, "10.114.0.5", "beta-bizme-rds.casstime.cn", "10.2.17.219   beta-bizme-new", ""],
      ["comment", true, "", "", "", "── Elasticsearch ──"],
      ["entry", true, "10.114.0.230", "beta-sand-es.casstime.cn", "10.2.21.92    beta-sand-7102", ""],
      ["entry", true, "10.114.1.0", "beta-zeebe-kunlun-es.casstime.cn", "10.2.53.169   hwbeta-zeebe-kunlun", ""]
    ]
  );
  // 列位置:域名都从内容的第 14 列开始,备注最常见的是第 60 列
  assert.deepEqual(cf.blockLayout(model, block), { indent: "      ", nameCol: 14, commentCol: 60 });
});

test("unchanged rows round-trip byte for byte", () => {
  const { model, rows } = load(COREFILE);
  const all = new Set(rows.map(r => r.id));
  assert.equal(cf.buildCorefile(model, rows, all).text, COREFILE);
  assert.equal(cf.buildCorefile(model, rows, new Set()).text, COREFILE);
});

test("editing a record rewrites only that line, aligned with its neighbours", () => {
  const { model, rows } = load(COREFILE);
  rows[0].ip = "10.114.1.44";
  rows[0].remark = "新的备注";
  const { text, lineOf: map } = cf.buildCorefile(model, rows, new Set([rows[0].id]));
  const line = lineOf(text, map.get(rows[0].id));
  assert.equal(line, "      " + "10.114.1.44".padEnd(14) + "beta-order2-es.casstime.cn".padEnd(46) + "# 新的备注");
  assert.equal(line.indexOf("beta-order2"), 6 + 14);
  assert.equal(line.indexOf("#"), 6 + 60);
  assert.deepEqual(changedLines(COREFILE, text), {
    removed: ["      10.114.1.4    beta-order2-es.casstime.cn"],
    added: [line]
  });
});

test("toggling enabled only adds or removes the leading #", () => {
  const { model, rows } = load(COREFILE);
  rows[1].enabled = true;
  rows[4].enabled = false;
  const { text } = cf.buildCorefile(model, rows, new Set([rows[1].id, rows[4].id]));
  assert.deepEqual(changedLines(COREFILE, text), {
    removed: [
      "      #10.114.0.10   beta-new-bill-bosch-wms-rds.casstime.cn       # 10.2.8.49     beta-new-bill-bosch-wms",
      "      10.114.0.5    beta-bizme-rds.casstime.cn                    # 10.2.17.219   beta-bizme-new"
    ],
    added: [
      "      10.114.0.10   beta-new-bill-bosch-wms-rds.casstime.cn       # 10.2.8.49     beta-new-bill-bosch-wms",
      "      #10.114.0.5    beta-bizme-rds.casstime.cn                    # 10.2.17.219   beta-bizme-new"
    ]
  });
  // 改完再解析,停用的行仍是停用的记录
  const again = load(text).rows;
  assert.deepEqual(again.map(r => r.enabled), [true, true, false, false, false, true, true, true]);
});

test("only rows in the apply set are written", () => {
  const { model, rows } = load(COREFILE);
  rows[0].ip = "10.0.0.1";
  rows[6].names = "beta-sand-es2.casstime.cn";
  const { text } = cf.buildCorefile(model, rows, new Set([rows[6].id]));
  assert.ok(text.includes("10.114.1.4    beta-order2-es.casstime.cn"));
  assert.ok(text.includes("beta-sand-es2.casstime.cn"));
  assert.ok(!text.includes("10.0.0.1 "));
});

test("new rows are inserted after the nearest saved row above them", () => {
  const { model, rows, nextId } = load(COREFILE);
  const top = { id: nextId(), block: 0, kind: "entry", enabled: true, ip: "10.1.0.1", names: "top.example.com", remark: "", text: "", origin: null };
  const afterComment = { id: nextId(), block: 0, kind: "entry", enabled: false, ip: "10.1.0.2", names: "es.example.com,  es2.example.com", remark: "停用", text: "", origin: null };
  const comment = { id: nextId(), block: 0, kind: "comment", enabled: true, ip: "", names: "", remark: "", text: "── Redis ──", origin: null };
  const unsaved = { id: nextId(), block: 0, kind: "entry", enabled: true, ip: "10.1.0.3", names: "skip.example.com", remark: "", text: "", origin: null };
  const list = [top, ...rows.slice(0, 6), afterComment, ...rows.slice(6), unsaved, comment];
  const { text, lineOf: map } = cf.buildCorefile(model, list, new Set([top.id, afterComment.id, comment.id]));
  const lines = text.split("\n");
  const hosts = lines.indexOf("    hosts {");
  assert.equal(lines[hosts + 1], "      " + "10.1.0.1".padEnd(14) + "top.example.com");
  assert.equal(lines[map.get(top.id)], lines[hosts + 1]);
  const es = lines.indexOf("      # ── Elasticsearch ──");
  assert.equal(lines[es + 1], "      #" + "10.1.0.2".padEnd(14) + "es.example.com es2.example.com".padEnd(46) + "# 停用");
  const zeebe = lines.findIndex(l => l.includes("beta-zeebe-kunlun-es"));
  assert.equal(lines[zeebe + 1], "      # ── Redis ──");
  assert.ok(!text.includes("skip.example.com"));
  // 新写入的行重新解析后类型不变,并且都在 fallthrough 之前
  const again = load(text);
  assert.deepEqual(cf.validateCorefile(text), { errors: [], warnings: [] });
  const kinds = new Map(again.model.blocks[0].lines.map(l => [l.line, l]));
  assert.equal(kinds.get(map.get(afterComment.id)).kind, "entry");
  assert.equal(kinds.get(map.get(afterComment.id)).enabled, false);
  assert.equal(kinds.get(map.get(comment.id)).kind, "comment");
  // 其他行的行号也对得上
  for (const row of rows) assert.equal(lines[map.get(row.id)], row.origin.raw);
});

test("deleting a row removes its line, other lines keep their position", () => {
  const { model, rows } = load(COREFILE);
  const remaining = rows.filter((_, i) => i !== 5); // 删除整行注释
  const { text, lineOf: map } = cf.buildCorefile(model, remaining, new Set());
  assert.deepEqual(changedLines(COREFILE, text), { removed: ["      # ── Elasticsearch ──"], added: [] });
  const lines = text.split("\n");
  for (const row of remaining) assert.equal(lines[map.get(row.id)], row.origin.raw);
});

test("a Corefile without hosts gets a new hosts block with fallthrough", () => {
  const text = ".:53 {\n    errors\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n       pods insecure\n    }\n    forward . /etc/resolv.conf\n    reload\n}\n";
  const { model } = load(text);
  assert.equal(model.blocks.length, 1);
  assert.equal(model.blocks[0].virtual, true);
  const row = { id: 1, block: 0, kind: "entry", enabled: true, ip: "10.0.0.8", names: "db.example.com", remark: "数据库", text: "", origin: null };
  const out = cf.buildCorefile(model, [row], new Set([1])).text;
  const entry = "      " + "10.0.0.8".padEnd(16) + "db.example.com".padEnd(30) + "# 数据库";
  assert.equal(
    out,
    `.:53 {\n    hosts {\n${entry}\n      fallthrough\n    }\n    errors\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n       pods insecure\n    }\n    forward . /etc/resolv.conf\n    reload\n}\n`
  );
  const again = cf.parseCorefile(out);
  assert.equal(again.blocks[0].virtual, false);
  assert.equal(again.blocks[0].lines[0].ip, "10.0.0.8");
});

test("k3s style hosts block (directives only) and single-line hosts", () => {
  const k3s = ".:53 {\n    hosts /etc/coredns/NodeHosts {\n      ttl 60\n      reload 15s\n      fallthrough\n    }\n}\nimport /etc/coredns/custom/*.server\n";
  const a = load(k3s);
  assert.deepEqual(a.model.blocks[0].args, ["/etc/coredns/NodeHosts"]);
  assert.equal(a.rows.length, 0);
  const row = { id: 1, block: 0, kind: "entry", enabled: true, ip: "10.0.0.8", names: "a.example.com", remark: "", text: "", origin: null };
  const out = cf.buildCorefile(a.model, [row], new Set([1])).text;
  assert.ok(out.includes(`      reload 15s\n      ${"10.0.0.8".padEnd(16)}a.example.com\n      fallthrough\n`));

  const single = "example.org:1053 {\n  hosts /etc/hosts example.org # 本地\n  log\n}\n";
  const b = load(single);
  assert.equal(b.model.blocks[0].server.port, 1053);
  assert.deepEqual(b.model.blocks[0].zones, ["example.org"]);
  const out2 = cf.buildCorefile(b.model, [{ ...row, names: "a.example.org" }], new Set([1])).text;
  assert.equal(out2, `example.org:1053 {\n  hosts /etc/hosts example.org { # 本地\n    ${"10.0.0.8".padEnd(16)}a.example.org\n  }\n  log\n}\n`);
  assert.equal(cf.validateCorefile(out2).errors.length, 0);
  // 域名不在 hosts 的区域里:提示不会生效
  const issues = cf.validateRow({ ...row, names: "a.example.com" }, b.model.blocks[0].zones);
  assert.match(issues.warnings[0], /不在 hosts 生效的区域/);
});

test("unusual hosts blocks are read-only, broken Corefiles report errors", () => {
  const oneLine = ".:53 {\n  hosts {\n    10.0.0.1 a.example.com }\n}\n";
  const block = cf.parseCorefile(oneLine).blocks[0];
  assert.equal(block.editable, false);
  assert.match(block.problem, /}/);
  const nested = ".:53 {\n  hosts {\n    10.0.0.1 a.example.com\n    x {\n    }\n  }\n}\n";
  assert.equal(cf.parseCorefile(nested).blocks[0].editable, false);

  assert.equal(cf.parseCorefile(".:53 {\n  hosts {\n}\n").error, "第 1 行的 { 没有闭合");
  assert.deepEqual(cf.validateCorefile(".:53 {\n}\n}\n").errors, ["第 3 行多了一个 }"]);
  const afterFall = ".:53 {\n  hosts {\n    fallthrough\n    10.0.0.1 a.example.com\n    #10.0.0.2 b.example.com\n  }\n}\n";
  assert.deepEqual(cf.validateCorefile(afterFall).errors, [
    "第 4 行:hosts 记录不能写在 fallthrough 之后,CoreDNS 会报错"
  ]);
  const badIp = ".:53 {\n  hosts {\n    relaod 10s\n    fallthrough\n  }\n}\n";
  assert.match(cf.validateCorefile(badIp).warnings[0], /不是合法的 IP/);
});

test("comment lines vs disabled records", () => {
  const text = ".:53 {\n  hosts {\n    # 10.2.8.49 beta-wms\n    #  10.0.0.1 a.example.com b\n    ## 10.0.0.2 c.example.com # x\n    #\n    # 10.0.0.0/16 网段\n    fallthrough\n  }\n}\n";
  const { rows } = load(text);
  assert.deepEqual(
    rows.map(r => [r.kind, r.ip || r.text]),
    [["comment", "10.2.8.49 beta-wms"], ["entry", "10.0.0.1"], ["entry", "10.0.0.2"], ["comment", ""], ["comment", "10.0.0.0/16 网段"]]
  );
  assert.equal(rows[1].origin.prefix, "#  ");
  // 注释内容像一条记录时不允许保存(保存后会变成停用的记录)
  assert.match(cf.validateRow({ ...rows[0], text: "10.0.0.1 a.example.com" }, ["."]).text, /停用的记录/);
  assert.equal(cf.validateRow(rows[0], ["."]).text, "");
});

test("row validation", () => {
  const row = { id: 1, block: 0, kind: "entry", enabled: true, ip: "10.0.0.1", names: "a.example.com", remark: "", text: "", origin: null };
  assert.deepEqual(cf.validateRow(row, ["."]), { ip: "", names: "", remark: "", text: "", warnings: [] });
  assert.equal(cf.validateRow({ ...row, ip: "" }, ["."]).ip, "请填写 IP");
  assert.equal(cf.validateRow({ ...row, ip: "10.0.0.256" }, ["."]).ip, "IP 格式不正确");
  assert.equal(cf.validateRow({ ...row, ip: "10.01.0.1" }, ["."]).ip, "IP 格式不正确");
  assert.equal(cf.validateRow({ ...row, ip: "fe80::1%eth0" }, ["."]).ip, "");
  assert.equal(cf.validateRow({ ...row, names: "  " }, ["."]).names, "请填写域名");
  assert.match(cf.validateRow({ ...row, names: "*.example.com" }, ["."]).names, /通配符/);
  assert.match(cf.validateRow({ ...row, names: "中文.com" }, ["."]).names, /punycode/);
  assert.match(cf.validateRow({ ...row, names: "-a.example.com" }, ["."]).names, /格式不正确/);
  assert.match(cf.validateRow({ ...row, names: "localhost" }, ["."]).names, /完整域名/);
  assert.equal(cf.validateRow({ ...row, names: "a.example.com. b" }, ["."]).names, "");
});

test("duplicate warnings", () => {
  const base = { block: 0, kind: "entry", enabled: true, remark: "", text: "", origin: null };
  const rows = [
    { ...base, id: 1, ip: "10.0.0.1", names: "a.example.com" },
    { ...base, id: 2, ip: "10.0.0.1", names: "A.example.com." },
    { ...base, id: 3, ip: "10.0.0.2", names: "a.example.com b.example.com" },
    { ...base, id: 4, ip: "10.0.0.3", names: "b.example.com", enabled: false }
  ];
  const warnings = cf.duplicateWarnings(rows);
  assert.match(warnings.get(1)[0], /有重复的记录/);
  assert.match(warnings.get(3)[0], /还有其它启用的记录\(10\.0\.0\.1\)/);
  assert.equal(warnings.has(4), false);
});

test("IP helpers", () => {
  assert.equal(cf.expandIPv6("::1"), "0000:0000:0000:0000:0000:0000:0000:0001");
  assert.equal(cf.expandIPv6("FE80::1%eth0"), "fe80:0000:0000:0000:0000:0000:0000:0001");
  assert.equal(cf.expandIPv6("1:2:3:4:5:6:7::"), "0001:0002:0003:0004:0005:0006:0007:0000");
  assert.equal(cf.expandIPv6("1:2:3:4:5:6:1.2.3.4"), "0001:0002:0003:0004:0005:0006:0102:0304");
  for (const bad of ["1::2::3", ":1", "1:2:3:4:5:6:7:8:9", "1.2.3.4::", "::12345", "1:2:3:4:5:6:7:8::"]) {
    assert.equal(cf.expandIPv6(bad), null, bad);
  }
  assert.deepEqual(cf.dnsTarget("10.1.2.3"), { type: "A", ip: "10.1.2.3" });
  assert.deepEqual(cf.dnsTarget("::ffff:10.0.0.1"), { type: "A", ip: "10.0.0.1" });
  assert.deepEqual(cf.dnsTarget("fd00::1"), { type: "AAAA", ip: "fd00:0000:0000:0000:0000:0000:0000:0001" });
  assert.equal(cf.sameIP("fd00::1", "FD00:0:0:0:0:0:0:1"), true);
  assert.equal(cf.sameIP("10.0.0.1", "10.0.0.2"), false);
});

test("CRLF Corefiles keep their line endings, single-spaced files stay single-spaced", () => {
  const text = ".:53 {\r\n  hosts {\r\n    10.0.0.1 a.example.com # x\r\n    fallthrough\r\n  }\r\n}\r\n";
  const { model, rows } = load(text);
  assert.equal(rows[0].ip, "10.0.0.1");
  assert.deepEqual(cf.blockLayout(model, model.blocks[0]), { indent: "    ", nameCol: 0, commentCol: 0 });
  assert.equal(cf.buildCorefile(model, rows, new Set([rows[0].id])).text, text);
  rows[0].names = "b.example.com";
  const out = cf.buildCorefile(model, rows, new Set([rows[0].id])).text;
  assert.equal(out, text.replace("a.example.com", "b.example.com"));
});

// ------------------------------------------------------------------------------ 确认框里的对比
test("line diff groups changes with context", () => {
  const a = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join("\n");
  const b = a.replace("line 3", "line three").replace("line 15\n", "").replace("line 19", "line nineteen");
  const result = df.diffLines(a, b, 2);
  assert.equal(result.added, 2);
  assert.equal(result.removed, 3);
  assert.equal(result.hunks.length, 2);
  assert.deepEqual(
    result.hunks[0].map(l => [l.type, l.text, l.oldNo, l.newNo]),
    [
      ["same", "line 1", 1, 1],
      ["same", "line 2", 2, 2],
      ["del", "line 3", 3, 0],
      ["add", "line three", 0, 3],
      ["same", "line 4", 4, 4],
      ["same", "line 5", 5, 5]
    ]
  );
  // 删除 line 15 和修改 line 19 之间只隔 3 行(不超过 2*context),合成一段;与 git 的规则一致
  assert.deepEqual(
    result.hunks[1].map(l => l.type + ":" + l.text),
    ["same:line 13", "same:line 14", "del:line 15", "same:line 16", "same:line 17", "same:line 18", "del:line 19", "add:line nineteen", "same:line 20"]
  );
  // 相隔超过 2*context 的分成两段
  const far = df.diffLines(a, a.replace("line 3", "x").replace("line 9", "y"), 2);
  assert.equal(far.hunks.length, 2);
  assert.deepEqual(df.diffLines("a\nb", "a\nb"), { hunks: [], added: 0, removed: 0 });
});

test("reloading keeps unsaved edits whose lines still exist", () => {
  const { rows, nextId } = load(COREFILE);
  rows[0].ip = "10.114.1.99"; // 这一行别人没动:修改保留
  rows[4].remark = "我改的备注"; // 这一行别人改过:修改丢弃
  const fresh = { id: nextId(), block: 0, kind: "entry", enabled: true, ip: "10.9.9.9", names: "new.example.com", remark: "", text: "", origin: null };
  const top = { id: nextId(), block: 0, kind: "comment", enabled: true, ip: "", names: "", remark: "", text: "顶部注释", origin: null };
  const list = [top, ...rows.slice(0, 6), fresh, ...rows.slice(6)];
  // 别人同时修改了 bizme 那一行,并在最前面加了一条记录
  const remote = COREFILE.replace("beta-bizme-rds.casstime.cn   ", "beta-bizme-rds2.casstime.cn  ")
    .replace("    hosts {\n", "    hosts {\n      10.0.0.1      remote.example.com\n");
  let id = 1000;
  const { rows: merged, lost } = cf.rebaseRows(list, cf.parseCorefile(remote), () => ++id);
  assert.equal(lost, 1);
  assert.deepEqual(
    merged.map(r => (r.origin ? `${cf.rowChanged(r) ? "*" : ""}${r.ip || r.text}` : `+${r.ip || r.text}`)),
    ["+顶部注释", "10.0.0.1", "*10.114.1.99", "10.114.0.10", "10.114.0.13", "10.114.0.11", "10.114.0.5", "── Elasticsearch ──", "+10.9.9.9", "10.114.0.230", "10.114.1.0"]
  );
  assert.equal(merged[2].origin.ip, "10.114.1.4");
  assert.equal(merged[6].remark, "10.2.17.219   beta-bizme-new");
  assert.equal(merged[8].id, fresh.id);
});
