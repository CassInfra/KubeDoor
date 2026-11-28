import { load } from "js-yaml";

/** 环境变量值的来源 */
export type EnvValueSource =
  | "literal"
  | "configmap"
  | "secret"
  | "field"
  | "resource"
  | "other";

const SOURCE_LABELS: Record<EnvValueSource, string> = {
  literal: "直接设置",
  configmap: "ConfigMap",
  secret: "Secret",
  field: "字段引用",
  resource: "资源引用",
  other: "引用"
};

/** 引用关系:ConfigMap / Secret 的键,fieldRef 的字段路径,resourceFieldRef 的资源 */
export interface EnvRef {
  name?: string;
  key?: string;
  path?: string;
  resource?: string;
  container?: string;
  divisor?: string;
  optional?: boolean;
}

export interface EnvVarView {
  name: string;
  /** 直接写死的值;引用来的没有值 */
  value: string;
  source: EnvValueSource;
  sourceLabel: string;
  /** 来源明细,如 app-config / LOG_LEVEL */
  sourceDetail: string;
  ref: EnvRef | null;
  /** 值来自 Secret,页面拿不到 */
  fromSecret: boolean;
  /** 变量名像密码 / 密钥,默认打码 */
  sensitive: boolean;
}

export interface EnvFromView {
  kind: "configmap" | "secret";
  name: string;
  prefix: string;
  optional: boolean;
}

export interface ContainerEnvView {
  name: string;
  image: string;
  env: EnvVarView[];
  envFrom: EnvFromView[];
}

export interface DeploymentEnvView {
  /** Pod 模板里的业务容器,第一个是主容器 */
  containers: ContainerEnvView[];
  /** 主容器(容器列表第一个);模板里没有容器时为 null */
  main: ContainerEnvView | null;
}

/** Deployment YAML 解析失败 */
export class DeploymentEnvError extends Error {}

/** 打码后显示的值(长度固定,不暴露原值长度) */
export const ENV_MASK = "••••••••";

const SENSITIVE_NAME =
  /(PASSWORD|PASSWD|PWD|SECRET|TOKEN|CREDENTIAL|PRIVATE[-_]?KEY|ACCESS[-_]?KEY|API[-_]?KEY|(?:^|[_-])KEY$|(?:^|[_-])PASS(?:$|[_-])|AUTH(?:$|[_-]))/i;

/** 变量名看着像密码 / 密钥,默认打码 */
export function isSensitiveEnvName(name: string): boolean {
  return SENSITIVE_NAME.test(name);
}

type Raw = Record<string, unknown>;

const isRecord = (value: unknown): value is Raw =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const asRecord = (value: unknown): Raw => (isRecord(value) ? value : {});

const asArray = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

const text = (value: unknown): string =>
  value === undefined || value === null ? "" : String(value);

const flag = (value: unknown): boolean => value === true;

const detail = (...parts: string[]) => parts.filter(Boolean).join(" / ");

function parseEnvVar(raw: unknown): EnvVarView {
  const item = asRecord(raw);
  const name = text(item.name);
  const sensitive = isSensitiveEnvName(name);
  const valueFrom = asRecord(item.valueFrom);

  if (Object.keys(valueFrom).length) {
    const secretKeyRef = asRecord(valueFrom.secretKeyRef);
    if (Object.keys(secretKeyRef).length) {
      const ref: EnvRef = {
        name: text(secretKeyRef.name),
        key: text(secretKeyRef.key),
        optional: flag(secretKeyRef.optional)
      };
      return {
        name,
        value: "",
        source: "secret",
        sourceLabel: SOURCE_LABELS.secret,
        sourceDetail: detail(ref.name, ref.key),
        ref,
        fromSecret: true,
        sensitive: true
      };
    }

    const configMapKeyRef = asRecord(valueFrom.configMapKeyRef);
    if (Object.keys(configMapKeyRef).length) {
      const ref: EnvRef = {
        name: text(configMapKeyRef.name),
        key: text(configMapKeyRef.key),
        optional: flag(configMapKeyRef.optional)
      };
      return {
        name,
        value: "",
        source: "configmap",
        sourceLabel: SOURCE_LABELS.configmap,
        sourceDetail: detail(ref.name, ref.key),
        ref,
        fromSecret: false,
        sensitive
      };
    }

    const fieldRef = asRecord(valueFrom.fieldRef);
    if (Object.keys(fieldRef).length) {
      const ref: EnvRef = { path: text(fieldRef.fieldPath) };
      return {
        name,
        value: "",
        source: "field",
        sourceLabel: SOURCE_LABELS.field,
        sourceDetail: detail(ref.path),
        ref,
        fromSecret: false,
        sensitive
      };
    }

    const resourceFieldRef = asRecord(valueFrom.resourceFieldRef);
    if (Object.keys(resourceFieldRef).length) {
      const ref: EnvRef = {
        resource: text(resourceFieldRef.resource),
        container: text(resourceFieldRef.containerName),
        divisor: text(resourceFieldRef.divisor)
      };
      return {
        name,
        value: "",
        source: "resource",
        sourceLabel: SOURCE_LABELS.resource,
        sourceDetail: detail(ref.container, ref.resource),
        ref,
        fromSecret: false,
        sensitive
      };
    }

    return {
      name,
      value: "",
      source: "other",
      sourceLabel: SOURCE_LABELS.other,
      sourceDetail: "未识别的引用",
      ref: null,
      fromSecret: false,
      sensitive
    };
  }

  return {
    name,
    value: text(item.value),
    source: "literal",
    sourceLabel: SOURCE_LABELS.literal,
    sourceDetail: "",
    ref: null,
    fromSecret: false,
    sensitive
  };
}

function parseEnvFrom(raw: unknown): EnvFromView | null {
  const item = asRecord(raw);
  const prefix = text(item.prefix);
  const configMapRef = asRecord(item.configMapRef);
  if (Object.keys(configMapRef).length) {
    return {
      kind: "configmap",
      name: text(configMapRef.name),
      prefix,
      optional: flag(configMapRef.optional)
    };
  }
  const secretRef = asRecord(item.secretRef);
  if (Object.keys(secretRef).length) {
    return {
      kind: "secret",
      name: text(secretRef.name),
      prefix,
      optional: flag(secretRef.optional)
    };
  }
  return null;
}

function parseContainer(raw: unknown): ContainerEnvView {
  const item = asRecord(raw);
  return {
    name: text(item.name),
    image: text(item.image),
    env: asArray(item.env)
      .map(parseEnvVar)
      .filter(row => !!row.name),
    envFrom: asArray(item.envFrom)
      .map(parseEnvFrom)
      .filter((row): row is EnvFromView => !!row)
  };
}

/** 从 Deployment YAML 里取 Pod 模板的容器、环境变量和整包引用 */
export function parseDeploymentEnv(yamlText: string): DeploymentEnvView {
  let doc: unknown;
  try {
    doc = load(yamlText);
  } catch (error: any) {
    const reason = error?.reason || error?.message || "格式不正确";
    throw new DeploymentEnvError(`Deployment YAML 解析失败: ${reason}`);
  }
  const template = asRecord(asRecord(asRecord(doc).spec).template);
  const containers = asArray(asRecord(template.spec).containers).map(
    parseContainer
  );
  return { containers, main: containers[0] ?? null };
}

/** 按变量名、来源过滤(不看值,打码时也能用) */
export function filterEnvRows(
  rows: EnvVarView[],
  keyword: string
): EnvVarView[] {
  const kw = keyword.trim().toLowerCase();
  if (!kw) return rows;
  return rows.filter(row =>
    [row.name, row.sourceLabel, row.sourceDetail].some(item =>
      item.toLowerCase().includes(kw)
    )
  );
}

/** 复制成 KEY=value;引用来的没有值,写成注释 */
export function toEnvText(rows: EnvVarView[]): string {
  return rows
    .map(row =>
      row.source === "literal"
        ? `${row.name}=${row.value}`
        : `# ${row.name} 来自 ${row.sourceLabel}${
            row.sourceDetail ? ` ${row.sourceDetail}` : ""
          }`
    )
    .join("\n");
}
