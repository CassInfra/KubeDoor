/**
 * CoreDNS 高级编辑用的 Monaco 设置:黑底主题,Corefile / hosts 文件的语法着色
 *
 * Corefile 按 caddy v1 的写法着色:server 块地址、插件名(块里每行的第一个词)、注释、IP、域名、字符串;
 * hosts 格式(k3s 的 NodeHosts 这类)只有注释、IP 和主机名。两种都把 # 设成行注释,Ctrl+/ 可以注释 / 取消注释选中的行
 */
import * as monaco from "monaco-editor";

// Monaco 的主题是全局的:编辑框打开时切到黑底,关闭时换回其他页面编辑器用的 vs-dark
export const BLACK_THEME = "kubedoor-black";

const IPV4 = /\d{1,3}(?:\.\d{1,3}){3}(?:\/\d{1,2})?/;
const IPV6 =
  /(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?:%[\w.]+)?(?:\/\d{1,3})?/;

const COREFILE: monaco.languages.IMonarchLanguage = {
  defaultToken: "",
  tokenizer: {
    root: [
      { include: "@space" },
      [/\{/, "delimiter.bracket", "@block"],
      [/\}/, "delimiter.bracket"],
      // server 块的地址(.:53、example.org:1053)、import、片段名
      [/[^\s{}#"]+/, "tag"]
    ],
    block: [
      // 每行第一个词是插件名 / 指令;hosts 记录每行开头是 IP,不会命中
      [/^(\s*)([A-Za-z_][\w-]*)/, ["white", "keyword"]],
      { include: "@space" },
      [/\{/, "delimiter.bracket", "@push"],
      [/\}/, "delimiter.bracket", "@pop"],
      [IPV4, "number"],
      [IPV6, "number"],
      [/:\d+/, "number"],
      [/\/[^\s{}#"]*/, ""],
      [/[A-Za-z0-9_*-]+(?:\.[A-Za-z0-9_*-]+)+\.?/, "type"],
      [/\d+[a-z]*\b/, "number"],
      [/[^\s{}#"]+/, ""]
    ],
    space: [
      [/\s+/, "white"],
      [/#.*$/, "comment"],
      [/"/, "string", "@string"]
    ],
    string: [
      [/[^\\"]+/, "string"],
      [/\\./, "string.escape"],
      [/\\/, "string"],
      [/"/, "string", "@pop"]
    ]
  }
};

const HOSTS: monaco.languages.IMonarchLanguage = {
  defaultToken: "",
  tokenizer: {
    root: [
      [/\s+/, "white"],
      [/#.*$/, "comment"],
      [IPV4, "number"],
      [IPV6, "number"],
      [/[^\s#]+/, "type"]
    ]
  }
};

let ready = false;

export function setupMonaco() {
  if (ready) return;
  ready = true;
  monaco.editor.defineTheme(BLACK_THEME, {
    base: "vs-dark",
    inherit: true,
    rules: [],
    colors: {
      "editor.background": "#000000",
      "editorGutter.background": "#000000"
    }
  });
  monaco.languages.register({ id: "corefile" });
  monaco.languages.setMonarchTokensProvider("corefile", COREFILE);
  monaco.languages.setLanguageConfiguration("corefile", {
    comments: { lineComment: "#" },
    brackets: [["{", "}"]],
    autoClosingPairs: [
      { open: "{", close: "}" },
      { open: '"', close: '"', notIn: ["string", "comment"] }
    ],
    surroundingPairs: [
      { open: "{", close: "}" },
      { open: '"', close: '"' }
    ]
  });
  monaco.languages.register({ id: "hosts" });
  monaco.languages.setMonarchTokensProvider("hosts", HOSTS);
  monaco.languages.setLanguageConfiguration("hosts", {
    comments: { lineComment: "#" }
  });
}

/** 按 data 里的文件名选着色方式 */
export function languageOf(name: string): string {
  if (name === "Corefile" || /\.(server|override)$/i.test(name))
    return "corefile";
  if (/hosts/i.test(name)) return "hosts";
  if (/\.ya?ml$/i.test(name)) return "yaml";
  if (/\.json$/i.test(name)) return "json";
  return "plaintext";
}
