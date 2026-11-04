import type {
  ImageRegistry,
  ImageRegistryInput,
  ImageUpdateRule
} from "@/api/image-settings";

export interface RegistryForm extends ImageRegistry {
  ak: string;
  sk: string;
}

const ENV = /^[A-Za-z0-9_-]{1,200}$/;
const USERNAME = /^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$/;
const TIME = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const validEndTime = (value: string) => TIME.test(value) || value === "24:00";

export function rulesPayload(rules: ImageUpdateRule[]): ImageUpdateRule[] {
  const seen = new Set<string>();
  let hasDefault = false;
  const result = rules.map((rule, index) => {
    const env = rule.env.trim();
    const label = env === "default" ? "默认规则" : `第 ${index + 1} 条规则`;
    if (!ENV.test(env)) throw new Error(`${label}请填写有效的集群标识`);
    if (seen.has(env)) throw new Error(`集群 ${env} 的规则重复了`);
    seen.add(env);
    if (env === "default") hasDefault = true;
    const users = [
      ...new Set(rule.users.map(user => user.trim().toLowerCase()))
    ];
    if (users.some(user => !USERNAME.test(user)))
      throw new Error(`${label}中的用户名格式不正确`);
    const start_time = rule.start_time || "";
    const end_time = rule.end_time || "";
    if (!TIME.test(start_time) || !validEndTime(end_time))
      throw new Error(`${label}请填写允许开始和结束时间`);
    if (start_time === end_time)
      throw new Error(`${label}的开始和结束时间不能相同`);
    if (rule.enabled) {
      if (!users.length) throw new Error(`${label}至少选择一个允许用户`);
    }
    return { env, enabled: rule.enabled, start_time, end_time, users };
  });
  if (!hasDefault) throw new Error("请保留默认规则");
  return result;
}

function validDomain(domain: string): boolean {
  if (!domain || /[\s/?#@]/.test(domain)) return false;
  try {
    const url = new URL(`https://${domain}`);
    return !!url.hostname && !url.username && !url.password;
  } catch {
    return false;
  }
}

export function registriesPayload(
  registries: RegistryForm[]
): ImageRegistryInput[] {
  const seen = new Set<string>();
  return registries.map((item, index) => {
    const domain = item.domain.trim().toLowerCase();
    const env = item.env.trim();
    if (!validDomain(domain))
      throw new Error(
        `第 ${index + 1} 个仓库请填写域名，可带端口，不含协议或路径`
      );
    if (!ENV.test(env)) throw new Error(`仓库 ${domain} 请填写有效的集群标识`);
    const key = `${domain}\0${env}`;
    if (seen.has(key)) throw new Error(`仓库 ${domain} 的 ${env} 凭据重复了`);
    seen.add(key);
    if (!item.ak.trim() && !item.has_ak)
      throw new Error(`仓库 ${domain} 请填写用户名或 Access Key`);
    if (!item.sk && !item.has_sk)
      throw new Error(`仓库 ${domain} 请填写密码或 Secret Key`);
    return {
      ...(item.id ? { id: item.id } : {}),
      domain,
      env,
      ...(item.ak ? { ak: item.ak } : {}),
      ...(item.sk ? { sk: item.sk } : {})
    };
  });
}

export function timeSummary(rule: ImageUpdateRule): string {
  if (!rule.enabled) return "不通过此规则放行";
  if (!TIME.test(rule.start_time) || !validEndTime(rule.end_time))
    return "请选择允许操作的时间";
  if (rule.start_time === "00:00" && rule.end_time === "24:00")
    return "全天（00:00 至 24:00）";
  return `${rule.start_time} 至 ${rule.end_time}${rule.start_time > rule.end_time ? "（次日）" : ""}`;
}
