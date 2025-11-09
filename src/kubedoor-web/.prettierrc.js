// @ts-check

/** @type {import("prettier").Config} */
export default {
  // 基础配置
  printWidth: 80,
  tabWidth: 2,
  useTabs: false,
  semi: true,
  singleQuote: false,
  quoteProps: "as-needed",
  trailingComma: "none",
  bracketSpacing: true,
  bracketSameLine: false,
  arrowParens: "avoid",

  // 行尾配置
  endOfLine: "auto",

  // Vue 特定配置
  vueIndentScriptAndStyle: false,

  // HTML 配置
  htmlWhitespaceSensitivity: "css",

  // 嵌入语言格式化
  embeddedLanguageFormatting: "auto"
};
