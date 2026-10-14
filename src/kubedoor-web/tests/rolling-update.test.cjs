const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { test } = require("node:test");

const { transformSync } = Module.createRequire(
  require.resolve("vite/package.json")
)("esbuild");
const sourcePath = path.resolve(
  __dirname,
  "../src/views/resource/utils/rolling-update.ts"
);
const output = transformSync(fs.readFileSync(sourcePath, "utf8"), {
  loader: "ts",
  target: "es2015",
  format: "cjs"
});
const helper = new Module(sourcePath, module);
helper._compile(output.code, sourcePath);
const rollingUpdate = helper.exports;

test("rolling update accepts non-negative integers and Kubernetes percentages", () => {
  for (const value of [0, 1, "1", "35%", "0%", "100%", null, undefined, ""]) {
    assert.equal(
      rollingUpdate.isValidRollingUpdateValue(value),
      true,
      String(value)
    );
  }
});

test("rolling update rejects negatives, decimals, malformed values, and percentages over 100", () => {
  for (const value of [-1, "-1", "1.5", "35.5%", "101%", "1 %", "abc"]) {
    assert.equal(
      rollingUpdate.isValidRollingUpdateValue(value),
      false,
      String(value)
    );
  }
});

test("payload preserves integer/percentage values as trimmed strings and blanks as null", () => {
  assert.deepEqual(
    rollingUpdate.getRollingUpdatePayload({
      max_surge: " 35% ",
      max_unavailable: 1
    }),
    { max_surge: "35%", max_unavailable: "1" }
  );
  assert.deepEqual(
    rollingUpdate.getRollingUpdatePayload({
      max_surge: "",
      max_unavailable: null
    }),
    { max_surge: null, max_unavailable: null }
  );
});
