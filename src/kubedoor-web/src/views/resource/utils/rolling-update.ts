export type RollingUpdateValue = string | number | null | undefined;

const ROLLING_UPDATE_VALUE_PATTERN = /^(?:0|[1-9]\d*|(?:100|[1-9]?\d)%)$/;

export function isValidRollingUpdateValue(value: RollingUpdateValue): boolean {
  if (value == null || String(value).trim() === "") return true;
  return ROLLING_UPDATE_VALUE_PATTERN.test(String(value).trim());
}

export function normalizeRollingUpdateValue(
  value: RollingUpdateValue
): string | null {
  if (value == null) return null;
  const normalized = String(value).trim();
  return normalized === "" ? null : normalized;
}

export function getRollingUpdatePayload(
  form: Record<string, RollingUpdateValue>
) {
  return {
    max_surge: normalizeRollingUpdateValue(form.max_surge),
    max_unavailable: normalizeRollingUpdateValue(form.max_unavailable)
  };
}
