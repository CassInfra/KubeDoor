<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { copyTextToClipboard } from "@pureadmin/utils";
import { useDebounceFn, useResizeObserver } from "@vueuse/core";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { WebglAddon } from "@xterm/addon-webgl";
import "@xterm/xterm/css/xterm.css";
import QuestionFilled from "@iconify-icons/ep/question-filled";
import { createNodeExecUrl, createPodExecUrl } from "@/api/monit";
import { useClusterPermission } from "@/utils/permission";

defineOptions({ name: "PodShell" });

/**
 * Pod / 节点终端:浏览器 ⇄ master(/ws/pod-exec | /ws/node-exec)⇄ agent ⇄ K8S exec
 *
 * 带 node 参数即为节点终端:agent 在 kubedoor 命名空间建临时特权 pod,nsenter 进宿主机。
 * 二进制帧是终端输出,文本帧是控制消息(started / exit / error)。
 * 流控:xterm 每写完 ACK_BYTES 字节回一次 ack,agent 未确认的输出超过窗口就暂停读容器,
 * 所以 cat 大文件不会把浏览器和 agent 的连接塞爆,Ctrl+C 也能很快生效。
 */
type Status = "connecting" | "connected" | "closed";

const ACK_BYTES = 32 * 1024; // 要明显小于 agent 的窗口(256KB)
const INPUT_CHUNK = 4096; // 粘贴大段文本时分片发送(master 单条消息上限 64KB)
const FONT_KEY = "kubedoor-shell-font-size";
const COPY_KEY = "kubedoor-shell-copy-on-select";
const MIN_FONT = 10;
const MAX_FONT = 28;
const SHELL_OPTIONS = [
  { label: "自动识别", value: "auto" },
  { label: "bash", value: "bash" },
  { label: "ash", value: "ash" },
  { label: "sh", value: "sh" }
];
// VS Code Dark+ 配色
const THEME = {
  background: "#1e1e1e",
  foreground: "#d4d4d4",
  cursor: "#aeafad",
  cursorAccent: "#1e1e1e",
  selectionBackground: "#264f78",
  black: "#000000",
  red: "#cd3131",
  green: "#0dbc79",
  yellow: "#e5e510",
  blue: "#2472c8",
  magenta: "#bc3fbc",
  cyan: "#11a8cd",
  white: "#e5e5e5",
  brightBlack: "#666666",
  brightRed: "#f14c4c",
  brightGreen: "#23d18b",
  brightYellow: "#f5f543",
  brightBlue: "#3b8eea",
  brightMagenta: "#d670d6",
  brightCyan: "#29b8db",
  brightWhite: "#e5e5e5"
};

const route = useRoute();
const router = useRouter();
const { canWrite } = useClusterPermission();

const env = String(route.query.env || "");
const namespace = String(route.query.namespace || "");
const pod = String(route.query.pod || "");
const node = String(route.query.node || "");
const isNode = !!node; // 带 node 参数即节点终端
const container = ref(String(route.query.container || ""));
const shell = ref(
  SHELL_OPTIONS.some(item => item.value === route.query.shell)
    ? String(route.query.shell)
    : "auto"
);
const containers = ref<string[]>([]);
const status = ref<Status>("connecting");
const fontSize = ref(clampFont(Number(localStorage.getItem(FONT_KEY)) || 14));
const copyOnSelect = ref(localStorage.getItem(COPY_KEY) !== "0");
const copiedTip = ref(false);
const termEl = ref<HTMLElement>();

let term: Terminal | null = null;
let fitAddon: FitAddon | null = null;
let socket: WebSocket | null = null;
let generation = 0; // 每次连接 +1,旧连接迟到的回调直接丢弃
let unacked = 0;
let ended = false; // 已经提示过结束原因(exit / error)
let gotMessage = false;
let blocked = false; // 参数不全或没有权限:不连接,回车也不重连
let addedDark = false;
let copiedTimer: number | undefined;

function clampFont(size: number) {
  return Math.min(MAX_FONT, Math.max(MIN_FONT, Math.round(size)));
}

const setTitle = () => {
  if (isNode) document.title = node ? `${node} - 节点终端` : "节点终端";
  else document.title = pod ? `${pod} - 终端` : "Pod 终端";
};

// 地址栏参数可能被构造成终端控制序列,写进终端前去掉控制字符
const plainText = (text: string) =>
  Array.from(text)
    .filter(ch => {
      const code = ch.codePointAt(0) ?? 0;
      return code >= 0x20 && (code < 0x7f || code > 0x9f);
    })
    .join("");

const notice = (text: string, color = "90") => {
  term?.write(`\r\n\x1b[${color}m${text}\x1b[0m\r\n`);
};

const send = (msg: object) => {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(msg));
  }
};

const sendInput = (data: string, binary = false) => {
  for (let start = 0; start < data.length; ) {
    let end = Math.min(start + INPUT_CHUNK, data.length);
    const code = data.charCodeAt(end - 1);
    if (end < data.length && code >= 0xd800 && code <= 0xdbff) end--; // 不拆开代理对(emoji 等)
    const chunk = data.slice(start, end);
    send(
      binary
        ? { type: "stdin", data: chunk, binary }
        : { type: "stdin", data: chunk }
    );
    start = end;
  }
};

const writeOutput = (data: Uint8Array, gen: number) => {
  term?.write(data, () => {
    if (gen !== generation) return;
    unacked += data.length;
    if (unacked >= ACK_BYTES) {
      send({ type: "ack", bytes: unacked });
      unacked = 0;
    }
  });
};

const handleControl = (msg: any) => {
  if (msg.type === "started") {
    status.value = "connected";
    containers.value = Array.isArray(msg.containers) ? msg.containers : [];
    if (!isNode && msg.container && msg.container !== container.value) {
      container.value = msg.container;
      syncQuery();
    }
  } else if (msg.type === "exit") {
    ended = true;
    if (msg.message) notice(msg.message, "31");
    else
      notice(
        typeof msg.code === "number" && msg.code !== 0
          ? `会话已结束(退出码 ${msg.code})`
          : "会话已结束"
      );
  } else if (msg.type === "error") {
    ended = true;
    notice(msg.message || "终端出错", "31");
  }
};

const closeSocket = () => {
  if (!socket) return;
  socket.onmessage = null;
  socket.onclose = null;
  socket.close();
  socket = null;
};

const connect = () => {
  if (!term || blocked) return;
  closeSocket();
  const gen = ++generation;
  ended = false;
  gotMessage = false;
  unacked = 0;
  status.value = "connecting";
  // 清掉上个会话留下的状态(备用屏幕、鼠标模式、隐藏光标等)
  term.reset();
  const target = isNode
    ? `${env} / ${node}`
    : `${env} / ${namespace} / ${pod}${container.value ? ` (${container.value})` : ""}`;
  const hint = isNode ? "(首次会在节点上创建终端容器,可能较慢)" : "";
  term.write(`\x1b[90m正在连接 ${plainText(target)} ...${hint}\x1b[0m\r\n`);
  const url = isNode
    ? createNodeExecUrl({
        env,
        node,
        shell: shell.value,
        cols: term.cols,
        rows: term.rows
      })
    : createPodExecUrl({
        env,
        namespace,
        pod,
        container: container.value,
        shell: shell.value,
        cols: term.cols,
        rows: term.rows
      });
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  socket = ws;
  ws.onmessage = event => {
    if (gen !== generation) return;
    gotMessage = true;
    if (typeof event.data !== "string") {
      writeOutput(new Uint8Array(event.data), gen);
      return;
    }
    try {
      handleControl(JSON.parse(event.data));
    } catch {
      // 忽略无法解析的控制消息
    }
  };
  ws.onclose = () => {
    if (gen !== generation) return;
    socket = null;
    if (!ended) {
      notice(
        gotMessage
          ? "连接已断开"
          : "无法建立连接:登录可能已过期,或没有该集群的读写权限,请刷新页面重试",
        "31"
      );
    }
    status.value = "closed";
    term?.write("\x1b[33m按回车键重新连接\x1b[0m\r\n");
  };
};

const syncQuery = () => {
  const query = { ...route.query };
  if (!isNode && container.value) query.container = container.value;
  else delete query.container;
  if (shell.value !== "auto") query.shell = shell.value;
  else delete query.shell;
  router.replace({ query }).then(setTitle);
};

const reconnect = () => {
  syncQuery();
  connect();
  term?.focus();
};

const clearScreen = () => {
  term?.clear();
  term?.focus();
};

const fit = () => {
  try {
    fitAddon?.fit();
  } catch {
    // 页面不可见时量不出尺寸
  }
};

const changeFont = (delta: number) => {
  fontSize.value = clampFont(fontSize.value + delta);
  localStorage.setItem(FONT_KEY, String(fontSize.value));
  if (term) {
    term.options.fontSize = fontSize.value;
    fit();
    term.focus();
  }
};

const saveCopyOnSelect = () => {
  localStorage.setItem(COPY_KEY, copyOnSelect.value ? "1" : "0");
  term?.focus();
};

// ---- 复制粘贴 ----
const flashCopied = () => {
  copiedTip.value = true;
  window.clearTimeout(copiedTimer);
  copiedTimer = window.setTimeout(() => (copiedTip.value = false), 1200);
};

const copyText = async (text: string) => {
  // 先用同步的 execCommand:HTTP 访问(非安全上下文)时没有 Clipboard API,它也能用
  try {
    if (copyTextToClipboard(text)) return true;
  } catch {
    // 再试 Clipboard API
  }
  if (!window.isSecureContext || !navigator.clipboard) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
};

const copySelection = (clear: boolean) => {
  const text = term?.getSelection();
  if (!text) return;
  copyText(text).then(ok => {
    if (ok) flashCopied();
    else ElMessage.warning("复制失败,请检查浏览器的剪贴板权限");
  });
  if (clear) term?.clearSelection();
};

const pasteFromClipboard = async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) term?.paste(text);
  } catch {
    ElMessage.warning("浏览器没有授权读取剪贴板,请用 Ctrl+V 粘贴");
  }
  term?.focus();
};

const handleKey = (ev: KeyboardEvent) => {
  if (ev.type !== "keydown") return true;
  const ctrl = ev.ctrlKey && !ev.altKey && !ev.metaKey;
  // 复制:Ctrl+Shift+C、Ctrl+Insert,以及有选中内容时的 Ctrl+C(没选中时 Ctrl+C 照常中断命令)
  if (
    ctrl &&
    ((ev.code === "KeyC" && (ev.shiftKey || term?.hasSelection())) ||
      ev.code === "Insert")
  ) {
    ev.preventDefault();
    copySelection(true);
    return false;
  }
  // 粘贴:Ctrl+V、Ctrl+Shift+V、Shift+Insert 交给浏览器触发原生 paste 事件,由 xterm 处理(支持 bracketed paste)
  const shiftOnly = ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey;
  if ((ctrl && ev.code === "KeyV") || (shiftOnly && ev.code === "Insert")) {
    return false;
  }
  return true;
};

// 右键:有选中内容就复制,否则粘贴(同 Windows Terminal)
const handleContextMenu = (ev: MouseEvent) => {
  if (term?.hasSelection()) {
    ev.preventDefault();
    copySelection(true);
  } else if (window.isSecureContext && navigator.clipboard?.readText) {
    ev.preventDefault();
    pasteFromClipboard();
  }
  // HTTP 访问时网页读不到剪贴板:不拦截,用浏览器原生右键菜单里的"粘贴"
};

const handleMouseUp = (ev: MouseEvent) => {
  if (ev.button !== 0 || !copyOnSelect.value) return;
  // 等 xterm 处理完这次 mouseup(它监听在 document 上)再取选中内容
  window.setTimeout(() => {
    if (term?.hasSelection()) copySelection(false);
  }, 0);
};

// 终端里常用 Ctrl+W 删词,浏览器却会关标签页:连接中离开页面先确认
const handleBeforeUnload = (ev: BeforeUnloadEvent) => {
  if (status.value === "closed") return;
  ev.preventDefault();
  ev.returnValue = "";
};

useResizeObserver(termEl, useDebounceFn(fit, 60));

onMounted(() => {
  setTitle();
  const root = document.documentElement;
  addedDark = !root.classList.contains("dark");
  root.classList.add("dark");

  const el = termEl.value as HTMLElement;
  term = new Terminal({
    allowProposedApi: true, // unicode11 需要
    cursorBlink: true,
    fontFamily:
      'Menlo, Monaco, Consolas, "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New", monospace',
    fontSize: fontSize.value,
    macOptionIsMeta: true,
    rightClickSelectsWord: false,
    scrollback: 10000,
    theme: THEME
  });
  fitAddon = new FitAddon();
  term.loadAddon(fitAddon);
  term.loadAddon(new WebLinksAddon());
  term.loadAddon(new Unicode11Addon());
  term.unicode.activeVersion = "11";
  term.open(el);
  try {
    const webgl = new WebglAddon();
    webgl.onContextLoss(() => webgl.dispose());
    term.loadAddon(webgl);
  } catch {
    // 不支持 WebGL 时用默认的 DOM 渲染
  }
  fit();
  document.fonts?.ready.then(fit);

  term.attachCustomKeyEventHandler(handleKey);
  term.onData(data => {
    if (status.value !== "closed") sendInput(data);
    else if (data === "\r") connect();
  });
  term.onBinary(data => sendInput(data, true));
  term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
  el.addEventListener("contextmenu", handleContextMenu);
  el.addEventListener("mouseup", handleMouseUp);
  window.addEventListener("beforeunload", handleBeforeUnload);
  term.focus();

  const problem = isNode
    ? !env || !node
      ? "缺少集群或节点参数,请从节点管理页打开终端"
      : !canWrite(env)
        ? `当前账号对集群 ${env} 没有读写权限,不能打开节点终端`
        : ""
    : !env || !namespace || !pod
      ? "缺少集群、命名空间或 Pod 参数,请从 Pod 列表打开终端"
      : !canWrite(env)
        ? `当前账号对集群 ${env} 没有读写权限,不能打开终端`
        : "";
  if (problem) {
    blocked = true;
    status.value = "closed";
    term.write(`\x1b[31m${problem}\x1b[0m\r\n`);
    return;
  }
  connect();
});

onBeforeUnmount(() => {
  window.removeEventListener("beforeunload", handleBeforeUnload);
  window.clearTimeout(copiedTimer);
  closeSocket();
  term?.dispose();
  term = null;
  if (addedDark) document.documentElement.classList.remove("dark");
});
</script>

<template>
  <div class="pod-shell">
    <div class="pod-shell__bar">
      <div v-if="isNode" class="pod-shell__target" :title="`${env} / ${node}`">
        <span class="pod-shell__dot" :class="`is-${status}`" />
        <span class="pod-shell__env">{{ env }}</span>
        <span class="pod-shell__sep">/</span>
        <span class="pod-shell__node">节点</span>
        <span class="pod-shell__pod">{{ node }}</span>
      </div>
      <div
        v-else
        class="pod-shell__target"
        :title="`${env} / ${namespace} / ${pod}`"
      >
        <span class="pod-shell__dot" :class="`is-${status}`" />
        <span class="pod-shell__env">{{ env }}</span>
        <span class="pod-shell__sep">/</span>
        <span>{{ namespace }}</span>
        <span class="pod-shell__sep">/</span>
        <span class="pod-shell__pod">{{ pod }}</span>
      </div>
      <div class="pod-shell__tools">
        <span v-show="copiedTip" class="pod-shell__copied">已复制</span>
        <el-select
          v-if="!isNode"
          v-model="container"
          size="small"
          placeholder="容器"
          style="width: 170px"
          :disabled="containers.length <= 1"
          @change="reconnect"
        >
          <el-option
            v-for="name in containers"
            :key="name"
            :label="name"
            :value="name"
          />
        </el-select>
        <el-select
          v-model="shell"
          size="small"
          style="width: 100px"
          @change="reconnect"
        >
          <el-option
            v-for="item in SHELL_OPTIONS"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
        <el-checkbox
          v-model="copyOnSelect"
          size="small"
          @change="saveCopyOnSelect"
        >
          选中即复制
        </el-checkbox>
        <el-button-group>
          <el-button
            size="small"
            :disabled="fontSize <= MIN_FONT"
            @click="changeFont(-1)"
          >
            A-
          </el-button>
          <el-button
            size="small"
            :disabled="fontSize >= MAX_FONT"
            @click="changeFont(1)"
          >
            A+
          </el-button>
        </el-button-group>
        <el-button size="small" @click="clearScreen">清屏</el-button>
        <el-button
          size="small"
          type="primary"
          :disabled="blocked"
          @click="reconnect"
        >
          重新连接
        </el-button>
        <el-tooltip placement="bottom-end" :show-after="200">
          <template #content>
            <div class="pod-shell__help-text">
              复制:选中文本后按 Ctrl+C / Ctrl+Shift+C,或点右键<br />
              勾选「选中即复制」时,用鼠标选中就会复制<br />
              粘贴:Ctrl+V / Ctrl+Shift+V / Shift+Insert,或没有选中时点右键<br />
              没有选中内容时,Ctrl+C 照常中断当前命令<br />
              连接断开后,在终端里按回车键重新连接
            </div>
          </template>
          <IconifyIconOffline :icon="QuestionFilled" class="pod-shell__help" />
        </el-tooltip>
      </div>
    </div>
    <div class="pod-shell__body">
      <div ref="termEl" class="pod-shell__term" />
    </div>
  </div>
</template>

<style scoped lang="scss">
.pod-shell {
  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
  background: #1e1e1e;
}

.pod-shell__bar {
  display: flex;
  flex: none;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  height: 40px;
  padding: 0 12px;
  font-size: 13px;
  color: #ccc;
  background: #252526;
  border-bottom: 1px solid #333;
}

.pod-shell__target {
  display: flex;
  gap: 6px;
  align-items: center;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
}

.pod-shell__dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;

  &.is-connecting {
    background: #e6a23c;
  }

  &.is-connected {
    background: #67c23a;
  }

  &.is-closed {
    background: #909399;
  }
}

.pod-shell__env {
  color: #4fc1ff;
}

.pod-shell__sep {
  color: #666;
}

.pod-shell__node {
  padding: 1px 6px;
  margin-right: 2px;
  font-size: 12px;
  color: #1e1e1e;
  background: #4fc1ff;
  border-radius: 3px;
}

.pod-shell__pod {
  overflow: hidden;
  font-weight: 600;
  color: #fff;
  text-overflow: ellipsis;
}

.pod-shell__tools {
  display: flex;
  flex: none;
  gap: 10px;
  align-items: center;
}

.pod-shell__copied {
  color: #67c23a;
}

.pod-shell__help {
  font-size: 16px;
  color: #909399;
  cursor: help;
}

.pod-shell__help-text {
  line-height: 1.8;
}

// 内边距放在外层:FitAddon 按终端父元素的尺寸算行列数
.pod-shell__body {
  flex: 1;
  min-height: 0;
  padding: 6px 0 0 10px;
}

.pod-shell__term {
  width: 100%;
  height: 100%;
}
</style>
