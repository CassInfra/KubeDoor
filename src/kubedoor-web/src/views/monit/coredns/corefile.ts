/**
 * CoreDNS Corefile 里 hosts 插件的解析与改写(纯函数,单测见 tests/coredns.test.cjs)
 *
 * 词法与 CoreDNS(caddy v1 的 Caddyfile)一致:引号外的 # 到行尾是注释,
 * " 开头的记号到下一个未转义的 " 为止(可以跨行),{ } 单独成一个记号才是块。
 *
 * hosts 块里的每一行:
 * - 空行、指令(fallthrough / ttl / reload / no_reverse)原样保留,不在表格里展示;
 * - 记录:IP 域名1 [域名2 ...] [# 备注];
 * - 以 # 开头、去掉 # 后仍是一条合法记录的,是注释掉(停用)的记录;
 * - 其余以 # 开头的是整行注释:只能是注释,去掉 # 会被 CoreDNS 当成记录或指令解析,可能直接报错。
 *
 * 改写时没改过的行原样输出,只重新生成改过的行(按块里已有记录的列位置对齐);
 * 只切换了启用/停用的行只在行首加减 #,和手工注释一样。
 */

export const COREFILE_KEY = "Corefile";
const HOSTS_DIRECTIVES = ["fallthrough", "ttl", "reload", "no_reverse"];

// ------------------------------------------------------------------------------------------ 词法
export interface CaddyToken {
  text: string;
  line: number; // 从 0 开始
  endLine: number; // 引号里的记号可以跨行
}

export function lexCorefile(text: string): {
  tokens: CaddyToken[];
  error: string;
} {
  const tokens: CaddyToken[] = [];
  let value = "";
  let inToken = false;
  let quoted = false;
  let escaped = false;
  let comment = false;
  let line = 0;
  let start = 0;
  const push = () => {
    tokens.push({ text: value, line: start, endLine: line });
    value = "";
    inToken = false;
  };
  for (const ch of text) {
    if (quoted) {
      if (!escaped) {
        if (ch === "\\") {
          escaped = true;
          continue;
        }
        if (ch === '"') {
          quoted = false;
          push();
          continue;
        }
      }
      if (ch === "\n") line++;
      if (escaped && ch !== '"') value += "\\";
      value += ch;
      escaped = false;
      continue;
    }
    if (/\s/.test(ch)) {
      if (inToken) push();
      if (ch === "\n") {
        line++;
        comment = false;
      }
      continue;
    }
    if (ch === "#") comment = true;
    if (comment) continue;
    if (!inToken) {
      inToken = true;
      start = line;
      if (ch === '"') {
        quoted = true;
        continue;
      }
    }
    value += ch;
  }
  if (quoted) return { tokens, error: `第 ${start + 1} 行的引号没有闭合` };
  if (inToken) push();
  return { tokens, error: "" };
}

function checkBraces(tokens: CaddyToken[]): string {
  const opened: number[] = [];
  for (const token of tokens) {
    if (token.text === "{") {
      opened.push(token.line);
    } else if (token.text === "}") {
      if (!opened.length) return `第 ${token.line + 1} 行多了一个 }`;
      opened.pop();
    }
  }
  return opened.length
    ? `第 ${opened[opened.length - 1] + 1} 行的 { 没有闭合`
    : "";
}

// ------------------------------------------------------------------------------------------ IP / 域名
const IPV4 =
  /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const LABEL = /^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$/i;

export const isIPv4 = (value: string) => IPV4.test(value);

/** IPv6 展开成 8 组 4 位小写十六进制,不合法返回 null(和 hosts 插件一样忽略 %zone) */
export function expandIPv6(value: string): string | null {
  const addr = value.split("%")[0].toLowerCase();
  if (!addr || /[^0-9a-f:.]/.test(addr)) return null;
  const halves = addr.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const all = [...head, ...tail];
  const groups: string[] = [];
  let headCount = 0;
  for (let i = 0; i < all.length; i++) {
    const group = all[i];
    if (group.includes(".")) {
      // 内嵌的 IPv4 只能在最后
      const last =
        i === all.length - 1 && (halves.length === 1 || tail.length > 0);
      if (!last || !isIPv4(group)) return null;
      const [a, b, c, d] = group.split(".").map(Number);
      groups.push(((a << 8) | b).toString(16), ((c << 8) | d).toString(16));
    } else if (/^[0-9a-f]{1,4}$/.test(group)) {
      groups.push(group);
    } else {
      return null;
    }
    if (i === head.length - 1) headCount = groups.length;
  }
  if (halves.length === 2) {
    if (groups.length > 7) return null;
    groups.splice(headCount, 0, ...Array(8 - groups.length).fill("0"));
  } else if (groups.length !== 8) {
    return null;
  }
  return groups.map(group => group.padStart(4, "0")).join(":");
}

export const isIP = (value: string) =>
  isIPv4(value) || expandIPv6(value) !== null;

/** 检查时查哪种记录、期望哪个 IP(::ffff:a.b.c.d 在 hosts 插件里按 IPv4 处理) */
export function dnsTarget(ip: string): { type: "A" | "AAAA"; ip: string } {
  if (isIPv4(ip)) return { type: "A", ip };
  const expanded = expandIPv6(ip) || ip;
  const mapped = /^(0000:){5}ffff:([0-9a-f]{4}):([0-9a-f]{4})$/.exec(expanded);
  if (mapped) {
    const n = parseInt(mapped[2] + mapped[3], 16);
    return {
      type: "A",
      ip: [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".")
    };
  }
  return { type: "AAAA", ip: expanded };
}

export function sameIP(a: string, b: string): boolean {
  if (isIPv4(a) || isIPv4(b)) return a === b;
  const x = expandIPv6(a);
  return x !== null && x === expandIPv6(b);
}

export function hostnameError(name: string): string {
  if (/[^\x20-\x7e]/.test(name)) {
    return `${name}:域名只能包含字母、数字、- 和 _(中文域名请先转换成 punycode)`;
  }
  if (name.includes("*")) return `${name}:hosts 插件不支持通配符`;
  const bare = name.endsWith(".") ? name.slice(0, -1) : name;
  if (
    !bare ||
    bare.length > 253 ||
    !bare.split(".").every(l => LABEL.test(l))
  ) {
    return `${name}:域名格式不正确`;
  }
  return "";
}

export const isDotted = (name: string) => name.replace(/\.$/, "").includes(".");

/** 页面上域名可以用空格、逗号、分号分隔 */
export const splitNames = (value: string) =>
  value.split(/[\s,，;；]+/).filter(Boolean);

function inZones(name: string, zones: string[]): boolean {
  if (!zones.length || zones.some(zone => zone === "." || zone.includes("/"))) {
    return true;
  }
  const bare = name.toLowerCase().replace(/\.$/, "");
  return zones.some(zone => {
    const z = zone.toLowerCase().replace(/\.$/, "");
    return bare === z || bare.endsWith(`.${z}`);
  });
}

// ------------------------------------------------------------------------------------------ 模型
export type HostsLineKind = "entry" | "comment" | "blank" | "directive";

export interface HostsLine {
  line: number; // 在 Corefile 里的行号(从 0 开始)
  kind: HostsLineKind;
  raw: string; // 原始行(CRLF 文件带行尾的 \r)
  indent: string;
  prefix: string; // 停用记录 / 注释行首的 #(含后面的空白)
  enabled: boolean;
  ip: string;
  names: string[];
  remark: string;
  hasRemark: boolean;
  text: string; // 注释内容(不含 #)
  nameStart: number; // 记录:去掉缩进和行首 # 之后,域名所在的列
  namesEnd: number; // 记录:最后一个域名结束的列
  commentStart: number; // 记录:# 备注 所在的列,没有为 -1
}

export interface ServerBlock {
  label: string;
  zones: string[];
  port: number;
  openLine: number;
  childIndent: string;
  directives: string[];
  snippet: boolean;
}

export interface HostsBlock {
  index: number;
  server: ServerBlock;
  virtual: boolean; // Corefile 里还没有 hosts,保存第一条记录时自动加上
  hostsLine: number; // `hosts ...` 所在行;virtual 时为 -1
  args: string[];
  closeLine: number; // 结尾 } 所在行;hosts 没有块(单独一行)或 virtual 时为 -1
  lines: HostsLine[];
  editable: boolean;
  problem: string;
  zones: string[]; // hosts 生效的区域:参数里的 ZONES,没有就是 server 块的区域
  hasFallthrough: boolean;
}

export interface CorefileModel {
  text: string;
  lines: string[];
  cr: string;
  error: string;
  servers: ServerBlock[];
  blocks: HostsBlock[];
}

const leading = (text: string) => /^\s*/.exec(text)![0];
const isFallthrough = (line: HostsLine) =>
  line.kind === "directive" && /^fallthrough\b/.test(line.raw.trim());

function parseServerKey(key: string): { zone: string; port: number } {
  let rest = key;
  const scheme = /^[a-z][a-z0-9+.-]*:\/\//i.exec(rest);
  if (scheme) rest = rest.slice(scheme[0].length);
  let port = 0;
  const withPort = /^(.*):(\d{1,5})$/.exec(rest);
  if (withPort) {
    rest = withPort[1];
    port = Number(withPort[2]);
  }
  const zone = (rest || ".").toLowerCase();
  return { zone: zone === "." ? "." : zone.replace(/\.$/, ""), port };
}

function parseEntry(content: string) {
  const hash = content.indexOf("#");
  const data = hash >= 0 ? content.slice(0, hash) : content;
  const fields = data.trim().split(/\s+/).filter(Boolean);
  return {
    ip: fields[0] ?? "",
    names: fields.slice(1),
    remark: hash >= 0 ? content.slice(hash + 1).trim() : "",
    hasRemark: hash >= 0,
    nameStart:
      fields.length > 1
        ? data.indexOf(fields[1], data.indexOf(fields[0]) + fields[0].length)
        : -1,
    namesEnd: data.trimEnd().length,
    commentStart: hash
  };
}

function looksLikeEntry(entry: { ip: string; names: string[] }): boolean {
  return (
    isIP(entry.ip) &&
    entry.names.length > 0 &&
    entry.names.every(name => !hostnameError(name)) &&
    entry.names.some(isDotted)
  );
}

function classifyLine(raw: string, line: number): HostsLine {
  const body = raw.replace(/\r$/, "");
  const indent = leading(body);
  const rest = body.slice(indent.length);
  const base: HostsLine = {
    line,
    kind: "blank",
    raw,
    indent,
    prefix: "",
    enabled: true,
    ip: "",
    names: [],
    remark: "",
    hasRemark: false,
    text: "",
    nameStart: -1,
    namesEnd: -1,
    commentStart: -1
  };
  if (!rest) return base;
  if (rest.startsWith("#")) {
    const prefix = /^#+\s*/.exec(rest)![0];
    const content = rest.slice(prefix.length);
    const entry = parseEntry(content);
    if (looksLikeEntry(entry)) {
      return { ...base, ...entry, kind: "entry", prefix, enabled: false };
    }
    return { ...base, kind: "comment", prefix, text: content.trimEnd() };
  }
  if (HOSTS_DIRECTIVES.includes(rest.split(/[\s#]/)[0])) {
    return { ...base, kind: "directive" };
  }
  return { ...base, ...parseEntry(rest), kind: "entry" };
}

export function parseCorefile(text: string): CorefileModel {
  const lines = text.split("\n");
  const model: CorefileModel = {
    text,
    lines,
    cr: text.includes("\r\n") ? "\r" : "",
    error: "",
    servers: [],
    blocks: []
  };
  const { tokens, error } = lexCorefile(text);
  model.error = error || checkBraces(tokens);
  if (model.error) return model;

  const byLine = new Map<number, CaddyToken[]>();
  for (const token of tokens) {
    if (!byLine.has(token.line)) byLine.set(token.line, []);
    byLine.get(token.line)!.push(token);
  }

  // 第一遍:server 块、hosts 指令所在行、hosts 块的结尾
  const stack: Array<{ hosts?: HostsBlock }> = [];
  let keys: string[] = [];
  let hostsOnLine: HostsBlock | null = null;
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    const lineStart = i === 0 || tokens[i - 1].endLine !== token.line;
    if (token.text === "{") {
      if (!stack.length) {
        const names = keys.map(key => key.replace(/,+$/, "")).filter(Boolean);
        const parsed = names.map(parseServerKey);
        model.servers.push({
          label: names.join(" ") || "(未命名)",
          zones: parsed.map(item => item.zone),
          port: parsed.find(item => item.port)?.port || 53,
          openLine: token.line,
          childIndent: "",
          directives: [],
          snippet: names.length === 1 && /^\(.*\)$/.test(names[0])
        });
        keys = [];
        stack.push({});
      } else if (stack.length === 1 && hostsOnLine?.hostsLine === token.line) {
        stack.push({ hosts: hostsOnLine });
      } else {
        stack.push({});
      }
      continue;
    }
    if (token.text === "}") {
      const ctx = stack.pop();
      if (ctx?.hosts) ctx.hosts.closeLine = token.line;
      continue;
    }
    if (!stack.length) {
      // server 块的地址可以用逗号续行;别的顶层语句(import)不带块
      if (lineStart && !keys[keys.length - 1]?.endsWith(",")) keys = [];
      keys.push(token.text);
      continue;
    }
    if (stack.length === 1 && lineStart) {
      const server = model.servers[model.servers.length - 1];
      server.directives.push(token.text);
      if (!server.childIndent) server.childIndent = leading(lines[token.line]);
      if (token.text === "hosts") {
        hostsOnLine = {
          index: model.blocks.length,
          server,
          virtual: false,
          hostsLine: token.line,
          args: [],
          closeLine: -1,
          lines: [],
          editable: true,
          problem: "",
          zones: [],
          hasFallthrough: false
        };
        model.blocks.push(hostsOnLine);
      }
    }
  }

  // 第二遍:hosts 块逐行分类,检查是否是能逐行编辑的常规写法
  for (const block of model.blocks) {
    const own = byLine.get(block.hostsLine)!;
    const opened = own[own.length - 1].text === "{";
    block.args = own.slice(1, opened ? -1 : undefined).map(t => t.text);
    const fileArgs = block.args.slice(1);
    block.zones = fileArgs.length ? fileArgs : block.server.zones;
    const fail = (problem: string) => {
      block.editable = false;
      block.problem ||= problem;
    };
    if (
      own.some(t => t.text === "}" || t.endLine !== t.line) ||
      own.slice(0, -1).some(t => t.text === "{")
    ) {
      fail("hosts 这一行的写法不常见");
    }
    if (!opened) {
      block.closeLine = -1;
      continue;
    }
    if (
      block.closeLine === block.hostsLine ||
      byLine.get(block.closeLine)!.length !== 1
    ) {
      fail("hosts 块结尾的 } 没有单独占一行");
    }
    for (let ln = block.hostsLine + 1; ln < block.closeLine; ln++) {
      const lineTokens = byLine.get(ln) ?? [];
      if (
        lineTokens.some(
          t => t.text === "{" || t.text === "}" || t.endLine !== t.line
        )
      ) {
        fail(`第 ${ln + 1} 行有嵌套的 { } 或跨行的引号`);
      }
      const hostsLine = classifyLine(lines[ln], ln);
      if (
        hostsLine.kind === "entry" &&
        hostsLine.enabled &&
        lines[ln].split("#")[0].includes('"')
      ) {
        fail(`第 ${ln + 1} 行用了引号`);
      }
      block.lines.push(hostsLine);
    }
    block.hasFallthrough = block.lines.some(isFallthrough);
  }

  if (!model.blocks.length) {
    const server =
      model.servers.find(s => !s.snippet && s.zones.includes(".")) ||
      model.servers.find(s => !s.snippet);
    if (server) {
      model.blocks.push({
        index: 0,
        server,
        virtual: true,
        hostsLine: -1,
        args: [],
        closeLine: -1,
        lines: [],
        editable: true,
        problem: "",
        zones: server.zones,
        hasFallthrough: true
      });
    }
  }
  return model;
}

// ------------------------------------------------------------------------------------------ 表格行
export interface HostsRow {
  id: number;
  block: number;
  kind: "entry" | "comment";
  enabled: boolean;
  ip: string;
  names: string; // 页面上编辑的字符串,多个域名用空格分隔
  remark: string;
  text: string;
  origin: HostsLine | null; // 已保存的那一行;新插入、还没保存的为 null
}

export const isRowLine = (line: HostsLine) =>
  line.kind === "entry" || line.kind === "comment";

/** 检查弹框要检查的记录:启用的期望解析到 ip;停用(注释掉)的期望不再解析到 ip */
export interface CheckTarget {
  names: string[];
  ip: string;
  enabled: boolean;
  line: number; // Corefile 里的行号(从 1 开始)
  dirty: boolean; // 表格里还有没保存的修改(检查的是已保存的内容)
}

export function rowFromLine(
  line: HostsLine,
  block: number,
  id: number
): HostsRow {
  return {
    id,
    block,
    kind: line.kind === "comment" ? "comment" : "entry",
    enabled: line.enabled,
    ip: line.ip,
    names: line.names.join(" "),
    remark: line.remark,
    text: line.text,
    origin: line
  };
}

function sameContent(row: HostsRow, line: HostsLine) {
  return (
    row.ip.trim() === line.ip &&
    splitNames(row.names).join(" ") === line.names.join(" ") &&
    row.remark.trim() === line.remark
  );
}

export function rowChanged(row: HostsRow): boolean {
  const origin = row.origin;
  if (!origin) return true;
  if (row.kind === "comment") return row.text.trim() !== origin.text;
  return row.enabled !== origin.enabled || !sameContent(row, origin);
}

/** 按 Corefile 生成全部表格行(都是已保存、没有修改的) */
export function rowsOf(model: CorefileModel, nextId: () => number): HostsRow[] {
  return model.blocks.flatMap(block =>
    block.lines
      .filter(isRowLine)
      .map(line => rowFromLine(line, block.index, nextId()))
  );
}

/**
 * 重新加载集群里的最新配置后,把还没保存的修改合并过去:
 * 已保存行按原始行文本找到新配置里的同一行,把修改搬过去;新插入的行跟着它上面那一行走。
 * 对应的行在新配置里已经不存在(被别人改了或删了)的修改丢弃,返回丢弃的条数。
 */
export function rebaseRows(
  oldRows: HostsRow[],
  model: CorefileModel,
  nextId: () => number
): { rows: HostsRow[]; lost: number } {
  const rows = rowsOf(model, nextId);
  const pool = new Map<string, HostsRow[]>();
  const keyOf = (block: number, raw: string) => `${block}\n${raw}`;
  for (const row of rows) {
    const key = keyOf(row.block, row.origin!.raw);
    if (!pool.has(key)) pool.set(key, []);
    pool.get(key)!.push(row);
  }
  let lost = 0;
  const counterpart = new Map<number, HostsRow>();
  for (const old of oldRows) {
    if (!old.origin) continue;
    const match = pool.get(keyOf(old.block, old.origin.raw))?.shift();
    if (!match) {
      if (rowChanged(old)) lost++;
      continue;
    }
    counterpart.set(old.id, match);
    if (rowChanged(old)) {
      Object.assign(match, {
        enabled: old.enabled,
        ip: old.ip,
        names: old.names,
        remark: old.remark,
        text: old.text
      });
    }
  }
  let anchor: HostsRow | null = null;
  let block = -1;
  for (const old of oldRows) {
    if (old.block !== block) {
      block = old.block;
      anchor = null;
    }
    if (old.origin) {
      anchor = counterpart.get(old.id) ?? anchor;
      continue;
    }
    if (!model.blocks[old.block]) {
      lost++;
      continue;
    }
    const row: HostsRow = { ...old, origin: null };
    // 没有上一行可跟的,放到这个块的最前面(块里没有行时放到最后;行在数组里的先后只在同一个块内有意义)
    const first = rows.findIndex(r => r.block === old.block);
    const index = anchor
      ? rows.indexOf(anchor) + 1
      : first >= 0
        ? first
        : rows.length;
    rows.splice(index, 0, row);
    anchor = row;
  }
  return { rows, lost };
}

export interface Layout {
  indent: string;
  nameCol: number;
  commentCol: number;
}

/** 出现次数最多的值,一样多取大的(列位置宁可靠右,能放下更多内容) */
function mostCommon(values: number[]): number | undefined {
  const counts = new Map<number, number>();
  values.forEach(v => counts.set(v, (counts.get(v) || 0) + 1));
  let best: number | undefined;
  counts.forEach((count, value) => {
    const bestCount = best === undefined ? 0 : counts.get(best)!;
    if (count > bestCount || (count === bestCount && value > best!))
      best = value;
  });
  return best;
}

function commonIndent(values: string[]): string | undefined {
  const counts = new Map<string, number>();
  values.forEach(v => counts.set(v, (counts.get(v) || 0) + 1));
  let best: string | undefined;
  counts.forEach((count, value) => {
    if (best === undefined || count > counts.get(best)!) best = value;
  });
  return best;
}

/**
 * 新生成的记录行怎么排版:缩进、域名列、备注列都跟块里已有的记录一致。
 * 已有记录是按列对齐的就对齐到出现最多的列;字段之间都只隔一个空格的,也只隔一个空格(列为 0)
 */
export function blockLayout(model: CorefileModel, block: HostsBlock): Layout {
  const entries = block.lines.filter(
    l => l.kind === "entry" && l.nameStart > 0
  );
  const remarks = entries.filter(e => e.hasRemark);
  const base = block.virtual
    ? block.server.childIndent || "    "
    : leading(model.lines[block.hostsLine]);
  const indent =
    commonIndent(
      block.lines.filter(l => l.kind === "entry").map(l => l.indent)
    ) ??
    commonIndent(
      block.lines.filter(l => l.kind !== "blank").map(l => l.indent)
    ) ??
    base + (base.includes("\t") ? "\t" : "  ");
  const aligned =
    !entries.length || entries.some(e => e.nameStart - e.ip.length > 1);
  const nameCol = aligned
    ? (mostCommon(entries.map(e => e.nameStart)) ?? 16)
    : 0;
  let commentCol = 0;
  if (remarks.some(e => e.commentStart - e.namesEnd > 1)) {
    commentCol = mostCommon(remarks.map(e => e.commentStart))!;
  } else if (!remarks.length && aligned) {
    commentCol = Math.max(nameCol + 30, ...entries.map(e => e.namesEnd + 4));
  }
  return { indent, nameCol, commentCol };
}

const pad = (text: string, column: number) =>
  text + " ".repeat(Math.max(1, column - text.length));

export function renderRow(row: HostsRow, layout: Layout, cr = ""): string {
  const origin = row.origin;
  if (origin && !rowChanged(row)) return origin.raw;
  const indent = origin ? origin.indent : layout.indent;
  if (row.kind === "comment") {
    const prefix = origin?.prefix || "# ";
    const text = row.text.trim();
    return indent + (text ? prefix + text : prefix.trimEnd()) + cr;
  }
  if (origin && sameContent(row, origin)) {
    // 只切换了启用/停用:行首加减 #,其余原样
    const body = origin.raw.replace(/\r$/, "");
    return row.enabled
      ? indent + body.slice(indent.length + origin.prefix.length) + cr
      : indent + "#" + body.slice(indent.length) + cr;
  }
  const prefix = row.enabled
    ? ""
    : origin && !origin.enabled
      ? origin.prefix
      : "#";
  let content =
    pad(row.ip.trim(), layout.nameCol) + splitNames(row.names).join(" ");
  const remark = row.remark.trim();
  if (remark) content = pad(content, layout.commentCol) + "# " + remark;
  return indent + prefix + content + cr;
}

// ------------------------------------------------------------------------------------------ 生成新的 Corefile
interface Out {
  text: string;
  row?: number;
}

export interface BuildResult {
  text: string;
  /** 行 id → 在新 Corefile 里的行号,保存成功后用来把表格行对应到新的行 */
  lineOf: Map<number, number>;
}

/**
 * 按表格当前的行生成新的 Corefile,只应用 apply 里的行(其余行保持已保存的内容,新行不写入)
 * 已保存、但不在 rows 里的记录/注释行视为删除。新行插在表格里它前面最近的已保存行之后。
 */
export function buildCorefile(
  model: CorefileModel,
  rows: HostsRow[],
  apply: Set<number>
): BuildResult {
  const before = new Map<number, Out[]>();
  const after = new Map<number, Out[]>();
  const replace = new Map<number, string | null>();
  const rowAt = new Map<number, number>();
  const push = (map: Map<number, Out[]>, line: number, ...items: Out[]) => {
    if (!map.has(line)) map.set(line, []);
    map.get(line)!.push(...items);
  };
  const cr = model.cr;

  for (const block of model.blocks) {
    const layout = blockLayout(model, block);
    const own = rows.filter(row => row.block === block.index);
    const kept = new Set(
      own.filter(row => row.origin).map(row => row.origin!.line)
    );
    block.lines
      .filter(line => isRowLine(line) && !kept.has(line.line))
      .forEach(line => replace.set(line.line, null));
    let first = -1;
    let last = -1;
    const head: Out[] = [];
    for (const row of own) {
      if (row.origin) {
        if (first < 0) first = row.origin.line;
        last = row.origin.line;
        rowAt.set(last, row.id);
        if (apply.has(row.id)) replace.set(last, renderRow(row, layout, cr));
      } else if (apply.has(row.id)) {
        const out = { text: renderRow(row, layout, cr), row: row.id };
        if (last >= 0) push(after, last, out);
        else head.push(out);
      }
    }
    if (!head.length) continue;
    if (first >= 0) {
      push(before, first, ...head);
    } else if (block.virtual) {
      const indent = block.server.childIndent || "    ";
      const unit = indent.includes("\t") ? "\t" : "  ";
      push(
        after,
        block.server.openLine,
        { text: `${indent}hosts {${cr}` },
        ...head,
        { text: `${indent}${unit}fallthrough${cr}` },
        { text: `${indent}}${cr}` }
      );
    } else if (block.closeLine < 0) {
      // `hosts FILE` 单独一行:改写成块
      const raw = model.lines[block.hostsLine].replace(/\r$/, "");
      const hash = raw.indexOf("#");
      const code = (hash >= 0 ? raw.slice(0, hash) : raw).trimEnd();
      replace.set(
        block.hostsLine,
        `${code} {${hash >= 0 ? " " + raw.slice(hash) : ""}${cr}`
      );
      push(after, block.hostsLine, ...head, { text: `${leading(raw)}}${cr}` });
    } else {
      // 记录必须写在 fallthrough 之前
      const fall = block.lines.find(isFallthrough);
      push(before, fall ? fall.line : block.closeLine, ...head);
    }
  }

  const out: string[] = [];
  const lineOf = new Map<number, number>();
  const emit = (item: Out) => {
    if (item.row !== undefined) lineOf.set(item.row, out.length);
    out.push(item.text);
  };
  model.lines.forEach((text, line) => {
    before.get(line)?.forEach(emit);
    const value = replace.has(line) ? replace.get(line) : text;
    if (value !== null && value !== undefined)
      emit({ text: value, row: rowAt.get(line) });
    after.get(line)?.forEach(emit);
  });
  return { text: out.join("\n"), lineOf };
}

// ------------------------------------------------------------------------------------------ 校验
export interface RowIssues {
  ip: string;
  names: string;
  remark: string;
  text: string;
  warnings: string[];
}

export function validateRow(row: HostsRow, zones: string[]): RowIssues {
  const issues: RowIssues = {
    ip: "",
    names: "",
    remark: "",
    text: "",
    warnings: []
  };
  if (row.kind === "comment") {
    if (/[\r\n]/.test(row.text)) {
      issues.text = "注释不能换行";
    } else if (looksLikeEntry(parseEntry(row.text.trim()))) {
      issues.text =
        "这样写会被识别成一条停用的记录;要停用记录请添加记录后关闭「启用」";
    }
    return issues;
  }
  const ip = row.ip.trim();
  if (!ip) issues.ip = "请填写 IP";
  else if (!isIP(ip)) issues.ip = "IP 格式不正确";
  const names = splitNames(row.names);
  const bad = names.map(hostnameError).find(Boolean);
  if (!names.length) issues.names = "请填写域名";
  else if (bad) issues.names = bad;
  else if (!names.some(isDotted))
    issues.names = "请填写完整域名,例如 db.example.com";
  else {
    const outside = names.filter(name => !inZones(name, zones));
    if (outside.length) {
      issues.warnings.push(
        `${outside.join("、")} 不在 hosts 生效的区域(${zones.join(" ")})内,不会生效`
      );
    }
  }
  if (/[\r\n]/.test(row.remark)) issues.remark = "备注不能换行";
  return issues;
}

/** 同一个 hosts 块里启用的记录,域名重复的提示(同 IP 是重复记录,不同 IP 会一起返回) */
export function duplicateWarnings(rows: HostsRow[]): Map<number, string[]> {
  const byName = new Map<string, HostsRow[]>();
  for (const row of rows) {
    if (row.kind !== "entry" || !row.enabled) continue;
    for (const name of new Set(
      splitNames(row.names).map(n => n.toLowerCase().replace(/\.$/, ""))
    )) {
      if (!byName.has(name)) byName.set(name, []);
      byName.get(name)!.push(row);
    }
  }
  const warnings = new Map<number, string[]>();
  byName.forEach((list, name) => {
    if (list.length < 2) return;
    for (const row of list) {
      const others = list.filter(other => other !== row);
      const same = others.filter(other =>
        sameIP(other.ip.trim(), row.ip.trim())
      );
      const ips = [...new Set(others.map(other => other.ip.trim()))];
      const text = same.length
        ? `${name} → ${row.ip.trim()} 有重复的记录`
        : `${name} 还有其它启用的记录(${ips.join("、")}),会一起返回`;
      if (!warnings.has(row.id)) warnings.set(row.id, []);
      warnings.get(row.id)!.push(text);
    }
  });
  return warnings;
}

/** 整个 Corefile 的检查:errors 不能保存,warnings 提示后可以保存 */
export function validateCorefile(text: string): {
  errors: string[];
  warnings: string[];
} {
  const model = parseCorefile(text);
  const errors: string[] = [];
  const warnings: string[] = [];
  if (model.error) return { errors: [model.error], warnings };
  if (!model.servers.some(server => !server.snippet))
    errors.push("Corefile 里没有 server 块");
  for (const block of model.blocks) {
    if (block.virtual || !block.editable) continue;
    let fallthrough = false;
    for (const line of block.lines) {
      if (isFallthrough(line)) fallthrough = true;
      if (line.kind !== "entry" || !line.enabled) continue;
      const where = `第 ${line.line + 1} 行`;
      if (fallthrough)
        errors.push(
          `${where}:hosts 记录不能写在 fallthrough 之后,CoreDNS 会报错`
        );
      else if (!isIP(line.ip))
        warnings.push(`${where}:${line.ip} 不是合法的 IP,CoreDNS 会忽略这一行`);
      else if (!line.names.length)
        warnings.push(`${where}:${line.ip} 后面没有域名,CoreDNS 会忽略这一行`);
    }
  }
  return { errors, warnings };
}
