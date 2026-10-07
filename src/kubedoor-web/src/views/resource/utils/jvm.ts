export const JVM_PARAMETERS = [
  { key: "jvm_xms_bytes", inputKey: "jvm_xms_mib", label: "Xms", unit: "m" },
  { key: "jvm_xmx_bytes", inputKey: "jvm_xmx_mib", label: "Xmx", unit: "m" },
  { key: "jvm_xss_bytes", inputKey: "jvm_xss_kib", label: "Xss", unit: "k" },
  {
    key: "jvm_max_metaspace_bytes",
    inputKey: "jvm_max_metaspace_mib",
    label: "MaxMeta",
    unit: "m"
  }
] as const;

type JvmBytesKey = (typeof JVM_PARAMETERS)[number]["key"];
type JvmInputKey = (typeof JVM_PARAMETERS)[number]["inputKey"];
export type JvmUnit = (typeof JVM_PARAMETERS)[number]["unit"];
export type JvmControlValues = Partial<Record<JvmBytesKey, number | null>>;
export type JvmFormValues = Partial<Record<JvmInputKey, string>>;

const UNIT_BYTES = { m: BigInt(1048576), k: BigInt(1024) };
const MAX_BYTES = BigInt(Number.MAX_SAFE_INTEGER);
const ZERO_BYTES = BigInt(0);
const DECIMAL_SCALE = BigInt("100000000000000000000");

/** Keep every byte when displaying sizes in binary memory units. */
export function jvmBytesToUnit(value: unknown, unit: JvmUnit): string {
  if (value === null || value === undefined || value === "") return "";
  if (
    (typeof value === "number" &&
      (!Number.isSafeInteger(value) || value < 0)) ||
    (typeof value === "string" && !/^\d+$/.test(value)) ||
    (typeof value !== "number" && typeof value !== "string")
  ) {
    return "";
  }
  const bytes = BigInt(value);
  if (bytes < ZERO_BYTES || bytes > MAX_BYTES) return "";
  const unitBytes = UNIT_BYTES[unit];
  const whole = bytes / unitBytes;
  const remainder = bytes % unitBytes;
  if (remainder === ZERO_BYTES) return whole.toString();
  const fraction = ((remainder * DECIMAL_SCALE) / unitBytes)
    .toString()
    .padStart(20, "0")
    .replace(/0+$/, "");
  return `${whole}.${fraction}`;
}

export function jvmBytesToMiB(value: unknown): string {
  return jvmBytesToUnit(value, "m");
}

export function formatJvmBytes(value: unknown, unit: JvmUnit = "m"): string {
  const size = jvmBytesToUnit(value, unit);
  return size === "" ? "-" : `${size}${unit}`;
}

/** Decimal arithmetic avoids rounding a user's setting to a different size. */
export function jvmUnitToBytes(value: string, unit: JvmUnit): number {
  const input = String(value ?? "").trim();
  if (!input) throw new Error("已采集的 JVM 参数不能为空");
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(input)) {
    throw new Error(
      `请输入非负数，单位 ${unit}（${unit === "k" ? "KiB" : "MiB"}）`
    );
  }
  const [whole, fraction = ""] = input.split(".");
  const decimals = fraction.replace(/0+$/, "");
  const integer = (whole || "0").replace(/^0+(?=\d)/, "");
  const unitBytes = UNIT_BYTES[unit];
  if (integer.length > (MAX_BYTES / unitBytes).toString().length) {
    throw new Error("JVM 参数过大");
  }
  if (decimals.length > 20) {
    throw new Error("该大小不能精确表示为整数 byte，请调整数值");
  }
  const scale = BigInt(`1${"0".repeat(decimals.length)}`);
  const numerator = BigInt(`${integer}${decimals}`) * unitBytes;
  if (numerator % scale !== ZERO_BYTES) {
    throw new Error("该大小不能精确表示为整数 byte，请调整数值");
  }
  const bytes = numerator / scale;
  if (bytes > MAX_BYTES) throw new Error("JVM 参数过大");
  return Number(bytes);
}

export function jvmMiBToBytes(value: string): number {
  return jvmUnitToBytes(value, "m");
}

export function getJvmFormValues(values: JvmControlValues = {}): JvmFormValues {
  const form: JvmFormValues = {};
  for (const { key, inputKey, unit } of JVM_PARAMETERS) {
    const size = jvmBytesToUnit(values[key], unit);
    // An absent parameter stays absent: the editor cannot add uncollected flags.
    if (size !== "") form[inputKey] = size;
  }
  return form;
}

export function validateJvmHeapSizes(form: JvmFormValues): void {
  if (form.jvm_xms_mib === undefined || form.jvm_xmx_mib === undefined) return;
  const xms = jvmMiBToBytes(form.jvm_xms_mib);
  const xmx = jvmMiBToBytes(form.jvm_xmx_mib);
  if (xmx > 0 && xms > xmx) {
    throw new Error("Xms 不能大于 Xmx");
  }
}

export function getJvmControlPayload(form: JvmFormValues): JvmControlValues {
  validateJvmHeapSizes(form);
  const values: JvmControlValues = {};
  for (const { key, inputKey, unit } of JVM_PARAMETERS) {
    if (form[inputKey] !== undefined) {
      values[key] = jvmUnitToBytes(form[inputKey], unit);
    }
  }
  return values;
}
