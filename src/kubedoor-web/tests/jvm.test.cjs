const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");

const { transformSync } = Module.createRequire(require.resolve("vite/package.json"))("esbuild");
const sourcePath = path.resolve(__dirname, "../src/views/resource/utils/jvm.ts");
// Execute the production target output: TypeScript checks cannot detect BigInt
// exponentiation being lowered to Math.pow and crashing at module load.
const output = transformSync(fs.readFileSync(sourcePath, "utf8"), {
  loader: "ts",
  target: "es2015",
  format: "cjs"
});
const helper = new Module(sourcePath, module);
helper._compile(output.code, sourcePath);
const jvm = helper.exports;

test("production-target JVM helper loads without unsupported BigInt literals", () => {
  assert.deepEqual(output.warnings, []);
  assert.equal(jvm.formatJvmBytes(524288, "k"), "512k");
});

test("field-specific display and editing preserve every byte", () => {
  for (const unit of ["k", "m"]) {
    for (const bytes of [0, 1, 511, 512, 524288, 1048576, Number.MAX_SAFE_INTEGER]) {
      assert.equal(jvm.jvmUnitToBytes(jvm.jvmBytesToUnit(bytes, unit), unit), bytes);
    }
  }
  assert.equal(jvm.formatJvmBytes(0), "0m");
  assert.equal(jvm.formatJvmBytes(0, "k"), "0k");
  assert.equal(jvm.formatJvmBytes(null), "-");
  assert.equal(jvm.formatJvmBytes(undefined), "-");
  assert.equal(jvm.jvmMiBToBytes("1024"), 1073741824);
  assert.equal(jvm.jvmUnitToBytes("512", "k"), 524288);
  assert.equal(jvm.formatJvmBytes(1048576, "k"), "1024k");
});

test("invalid sizes and sizes that change fractional bytes are rejected", () => {
  for (const input of ["", "-1", "512k", "NaN", "0.1", "8589934592"]) {
    assert.throws(() => jvm.jvmMiBToBytes(input));
  }
  assert.throws(() => jvm.jvmUnitToBytes("8796093022208", "k"));
});

test("uncollected fields stay absent from the edit payload", () => {
  const form = jvm.getJvmFormValues({ jvm_xms_bytes: 0, jvm_xss_bytes: 524288 });
  assert.deepEqual(form, { jvm_xms_mib: "0", jvm_xss_kib: "512" });
  assert.deepEqual(jvm.getJvmControlPayload(form), { jvm_xms_bytes: 0, jvm_xss_bytes: 524288 });
  assert.deepEqual(jvm.getJvmControlPayload(jvm.getJvmFormValues()), {});
  assert.throws(() => jvm.getJvmControlPayload({ jvm_xss_kib: "" }));
});

test("heap bounds reject inconsistent settings and preserve explicit zero", () => {
  assert.throws(() => jvm.getJvmControlPayload({ jvm_xms_mib: "2048", jvm_xmx_mib: "1024" }));
  assert.deepEqual(jvm.getJvmControlPayload({ jvm_xms_mib: "1024", jvm_xmx_mib: "0" }), {
    jvm_xms_bytes: 1073741824,
    jvm_xmx_bytes: 0
  });
});
