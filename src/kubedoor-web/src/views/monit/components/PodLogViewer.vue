<template>
  <el-dialog
    v-model="logDialogVisible"
    width="99%"
    top="0.5vh"
    :style="{ 'padding-top': '7px' }"
    :close-on-click-modal="false"
    @close="closeLogDialog"
  >
    <template #header>
      <div class="log-dialog-header">
        <div class="log-controls-header">
          <!-- Pod 下拉：pod 级固定只读置灰；deployment 级为“全部 + 各 Pod” -->
          <el-select
            v-model="selectedPod"
            placeholder="选择Pod"
            size="small"
            style="width: 220px; margin-right: 8px"
            filterable
            :disabled="mode === 'pod' || isLogConnected"
          >
            <el-option
              v-if="mode === 'deployment'"
              label="全部"
              :value="ALL_PODS"
            />
            <el-option
              v-for="pod in podList"
              :key="pod.name"
              :label="pod.name"
              :value="pod.name"
            />
          </el-select>
          <!-- 容器下拉：展示该 Pod 的全部容器（含 init），多个时显示，默认第一个 -->
          <el-select
            v-if="currentContainers.length > 1"
            v-model="selectedContainer"
            placeholder="选择容器"
            size="small"
            style="width: 180px; margin-right: 8px"
            :disabled="isLogConnected"
          >
            <el-option
              v-for="container in currentContainers"
              :key="container"
              :label="container"
              :value="container"
            />
          </el-select>
          <el-button
            v-if="!isLogConnected"
            type="primary"
            size="small"
            :loading="logConnecting"
            @click="startLogStream"
          >
            开始查看日志
          </el-button>
          <el-button v-else type="danger" size="small" @click="stopLogStream">
            停止查看
          </el-button>
          <el-button size="small" @click="clearLogs">清空日志</el-button>
          <el-button size="small" @click="scrollToBottom">滚动到底部</el-button>
          <el-button
            size="small"
            type="success"
            :disabled="isAllPodsMode"
            @click="downloadLogs"
            >下载日志</el-button
          >
          <div class="log-status">
            <span
              :class="{
                'status-connected': isLogConnected,
                'status-disconnected': !isLogConnected
              }"
            >
              {{ isLogConnected ? "已连接" : "未连接" }}
            </span>
          </div>
          <!-- 日志搜索功能 -->
          <div class="log-search-container">
            <el-input
              v-model="searchKeyword"
              placeholder="搜索日志内容"
              size="small"
              style="width: 200px; margin-right: 8px"
              @keyup.enter="() => performSearch(true)"
            >
              <template #append>
                <el-button size="small" @click="() => performSearch(true)">
                  搜索
                </el-button>
              </template>
            </el-input>
            <el-button
              size="small"
              :type="isFilterMode ? 'primary' : 'default'"
              :disabled="!searchKeyword.trim() || totalMatches === 0"
              @click="toggleFilterMode"
            >
              {{ isFilterMode ? "取消筛选" : "筛选" }}
            </el-button>
            <span v-if="totalMatches > 0" class="search-info">
              {{ currentMatchIndex + 1 }}/{{ totalMatches }}
            </span>
            <el-button
              size="small"
              :disabled="totalMatches === 0"
              @click="goToPreviousMatch"
            >
              上一个
            </el-button>
            <el-button
              size="small"
              :disabled="totalMatches === 0"
              @click="goToNextMatch"
            >
              下一个
            </el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="isAllPodsMode"
              @click="getPreviousLogs"
            >
              重启前日志
            </el-button>
          </div>
        </div>
        <span class="dialog-title">{{ dialogTitle }}</span>
      </div>
    </template>
    <div class="log-container">
      <div
        ref="logContentRef"
        v-loading="logConnecting"
        class="log-content"
        element-loading-text="正在连接日志流..."
        @scroll="handleScroll"
      >
        <div v-if="logMessages.length === 0" class="no-logs">暂无日志数据</div>
        <div
          v-for="(message, index) in filteredLogMessages"
          :key="getLogKey(message, index)"
          class="log-line"
          :class="{
            'log-error':
              message.text.includes('ERROR') ||
              message.text.includes('Exception'),
            'log-warn': message.text.includes('WARN'),
            'log-info': message.text.includes('INFO')
          }"
        >
          <span v-if="message.pod" class="pod-badge">{{ message.pod }}</span
          ><span
            class="log-text"
            v-html="
              highlightSearchKeyword(
                message.text,
                getOriginalIndex(message, index)
              )
            "
          />
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useThrottleFn, useDebounceFn } from "@vueuse/core";
import { AnsiUp } from "ansi_up";
import {
  createPodLogStreamUrl,
  downloadPodLogs,
  getPodPreviousLogs
} from "@/api/monit";

defineOptions({
  name: "PodLogViewer"
});

// 每条日志：text 为内容；pod 仅在“全部Pod”模式下用于展示黄底黑字前缀
interface LogEntry {
  text: string;
  pod?: string;
}

// 容器：直接用容器名列表（含 init，展示全部让用户选，不排序不过滤）
// 组件内 Pod 结构：name + 该 Pod 的所有容器名
interface PodEntry {
  name: string;
  containers: string[];
}

// 传入的容器可能是字符串数组，或带 name 的对象数组，统一取所有容器名
type ContainerInput = string | { name?: string } | null | undefined;

const normalizeContainers = (containers?: ContainerInput[]): string[] => {
  if (!Array.isArray(containers)) return [];
  return containers
    .map(c => (typeof c === "string" ? c : c?.name))
    .filter((n): n is string => !!n);
};

const ALL_PODS = "";
const MAX_ALL_PODS_CONNECTIONS = 60;

// 弹窗与连接状态
const logDialogVisible = ref(false);
const mode = ref<"pod" | "deployment">("pod");
const target = ref({ env: "", namespace: "", deployment: "" });
const podList = ref<PodEntry[]>([]);
const selectedPod = ref<string>(ALL_PODS);
const selectedContainer = ref("");

const logMessages = ref<LogEntry[]>([]);
const isLogConnected = ref(false);
const logConnecting = ref(false);
const logSocket = ref<WebSocket | null>(null);
const logSockets = ref<WebSocket[]>([]);
const logReconnectAttempts = ref(0);
const maxReconnectAttempts = 3;
let logReconnectTimer: ReturnType<typeof setTimeout> | null = null;
const logContentRef = ref<HTMLElement | null>(null);
const isUserScrolling = ref(false);

// 日志搜索相关
const searchKeyword = ref("");
const searchMatches = ref<number[]>([]);
const currentMatchIndex = ref(-1);
const totalMatches = ref(0);
const isFilterMode = ref(false);

// 是否“全部Pod”模式：deployment 级且选择了“全部”
const isAllPodsMode = computed(
  () => mode.value === "deployment" && selectedPod.value === ALL_PODS
);

// 当前容器下拉可选项：
// - 全部模式：各 Pod 容器名并集（同一 Deployment 通常一致）
// - 指定 Pod：该 Pod 的容器名
const currentContainers = computed<string[]>(() => {
  if (isAllPodsMode.value) {
    const union: string[] = [];
    for (const pod of podList.value) {
      for (const c of pod.containers) {
        if (!union.includes(c)) union.push(c);
      }
    }
    return union;
  }
  const pod = podList.value.find(p => p.name === selectedPod.value);
  return pod ? pod.containers : [];
});

// 当前实际用于单连接/下载/重启前日志的 Pod 名
const activePodName = computed(() =>
  isAllPodsMode.value ? "" : selectedPod.value
);

const dialogTitle = computed(() => {
  if (isAllPodsMode.value) {
    return `Deployment日志: ${target.value.env}【${target.value.namespace}】${target.value.deployment}（全部Pod）`;
  }
  return `Pod日志: ${target.value.env}【${target.value.namespace}】${activePodName.value}`;
});

// 筛选后的日志消息
const filteredLogMessages = computed(() => {
  if (!isFilterMode.value || !searchKeyword.value.trim()) {
    return logMessages.value;
  }
  const keyword = searchKeyword.value.toLowerCase();
  return logMessages.value.filter(message =>
    message.text.toLowerCase().includes(keyword)
  );
});

// ---- 日志 key 与索引换算 ----
const getLogKey = (message: LogEntry, index: number) => {
  if (isFilterMode.value) {
    return `${message.text.slice(0, 50)}-${index}`;
  }
  return index;
};

const getOriginalIndex = (message: LogEntry, filteredIndex: number) => {
  if (!isFilterMode.value) {
    return filteredIndex;
  }
  return logMessages.value.findIndex(msg => msg === message);
};

const getFilteredIndex = (originalIndex: number) => {
  if (!isFilterMode.value) {
    return originalIndex;
  }
  const targetMessage = logMessages.value[originalIndex];
  return filteredLogMessages.value.findIndex(msg => msg === targetMessage);
};

const toggleFilterMode = () => {
  isFilterMode.value = !isFilterMode.value;
  if (isFilterMode.value && searchKeyword.value.trim()) {
    performSearch(true);
  }
};

// ---- 日志消息解析 ----
// agent 现以 JSON({type:"pod_logs", log/status/error, pod_name}) 发送，兼容旧版纯文本。
// 状态/错误消息不作为日志行。
const parseLogMessage = (raw: string): { lines: string[]; pod?: string } => {
  if (!raw || !raw.trim()) {
    return { lines: [] };
  }
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object" && data.type === "pod_logs") {
      if (typeof data.log === "string" && data.log.length > 0) {
        return {
          lines: data.log.split("\n").filter((line: string) => line !== ""),
          pod: data.pod_name
        };
      }
      return { lines: [], pod: data.pod_name };
    }
  } catch {
    // 非 JSON，按纯文本处理（旧版本兜底）
  }
  return { lines: raw.split("\n").filter(line => line.trim() !== "") };
};

// 追加日志行，做条数限制与自动滚动。全部模式下带 pod 前缀。
const appendLogLines = (lines: string[], pod?: string) => {
  if (lines.length === 0) return;
  for (const text of lines) {
    logMessages.value.push(isAllPodsMode.value ? { text, pod } : { text });
  }
  if (logMessages.value.length > 1000) {
    logMessages.value.splice(0, logMessages.value.length - 800);
  }
  nextTick(() => {
    if (!isUserScrolling.value || isAtBottom()) {
      scrollToBottom();
    }
  });
};

// ---- 连接管理 ----
// 主动关闭 socket 前先摘掉所有事件处理器，避免 onclose 触发自动重连
const closeSocketSilently = (socket: WebSocket | null) => {
  if (!socket) return;
  socket.onopen = null;
  socket.onmessage = null;
  socket.onerror = null;
  socket.onclose = null;
  try {
    socket.close();
  } catch (e) {
    console.error("关闭日志连接失败:", e);
  }
};

// 全部模式：对每个（含选定容器的）Pod 各开一个日志流，每行标注 Pod 名（黄底黑字）
const startAllPodsLogStream = () => {
  const container = selectedContainer.value || undefined;
  const targetPods = podList.value.filter(
    pod => !container || pod.containers.includes(container)
  );

  if (targetPods.length === 0) {
    ElMessage.warning("该Deployment下暂无匹配的Pod");
    return;
  }
  if (targetPods.length > MAX_ALL_PODS_CONNECTIONS) {
    ElMessage.warning(
      `Pod数量过多（${targetPods.length}），仅连接前 ${MAX_ALL_PODS_CONNECTIONS} 个Pod的日志`
    );
    targetPods.length = MAX_ALL_PODS_CONNECTIONS;
  }

  logConnecting.value = true;
  logMessages.value = [];
  logSockets.value = [];
  let openedCount = 0;

  for (const pod of targetPods) {
    const wsUrl = createPodLogStreamUrl(
      target.value.env,
      target.value.namespace,
      pod.name,
      container
    );
    const socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      openedCount++;
      logConnecting.value = false;
      isLogConnected.value = true;
    };

    socket.onmessage = event => {
      const { lines, pod: podName } = parseLogMessage(event.data);
      appendLogLines(lines, podName || pod.name);
    };

    socket.onerror = error => {
      console.error(`WebSocket错误(${pod.name}):`, error);
    };

    logSockets.value.push(socket);
  }

  nextTick(() => {
    logConnecting.value = false;
    if (openedCount === 0) {
      isLogConnected.value = true;
    }
  });
};

const startLogStream = () => {
  if (isAllPodsMode.value) {
    startAllPodsLogStream();
    return;
  }

  if (!activePodName.value) {
    ElMessage.warning("请先选择Pod");
    return;
  }

  if (logSocket.value) {
    closeSocketSilently(logSocket.value);
    logSocket.value = null;
  }

  logConnecting.value = true;
  logMessages.value = [];

  const wsUrl = createPodLogStreamUrl(
    target.value.env,
    target.value.namespace,
    activePodName.value,
    selectedContainer.value || undefined
  );

  logSocket.value = new WebSocket(wsUrl);

  logSocket.value.onopen = () => {
    logConnecting.value = false;
    isLogConnected.value = true;
    logReconnectAttempts.value = 0;
    ElMessage.success("日志连接成功");
  };

  logSocket.value.onmessage = event => {
    const { lines } = parseLogMessage(event.data);
    appendLogLines(lines);
  };

  logSocket.value.onerror = error => {
    console.error("WebSocket错误:", error);
    logConnecting.value = false;
    isLogConnected.value = false;
  };

  logSocket.value.onclose = () => {
    logConnecting.value = false;
    isLogConnected.value = false;
    if (
      logDialogVisible.value &&
      !isAllPodsMode.value &&
      logReconnectAttempts.value < maxReconnectAttempts
    ) {
      logReconnectAttempts.value++;
      const delay = Math.min(
        1000 * Math.pow(2, logReconnectAttempts.value - 1),
        10000
      );
      logReconnectTimer = setTimeout(() => {
        if (logDialogVisible.value) {
          ElMessage.info(
            `正在重连... (${logReconnectAttempts.value}/${maxReconnectAttempts})`
          );
          startLogStream();
        }
      }, delay);
    } else if (logReconnectAttempts.value >= maxReconnectAttempts) {
      ElMessage.error("日志连接失败，已达最大重试次数");
    }
  };
};

const stopLogStream = () => {
  if (logReconnectTimer) {
    clearTimeout(logReconnectTimer);
    logReconnectTimer = null;
  }
  logReconnectAttempts.value = 0;
  if (logSocket.value) {
    closeSocketSilently(logSocket.value);
    logSocket.value = null;
  }
  if (logSockets.value.length > 0) {
    for (const socket of logSockets.value) {
      closeSocketSilently(socket);
    }
    logSockets.value = [];
  }
  isLogConnected.value = false;
};

const clearLogs = () => {
  logMessages.value = [];
};

const downloadLogs = async () => {
  if (isAllPodsMode.value) {
    ElMessage.warning("全部Pod模式不支持下载，请选择具体Pod");
    return;
  }
  if (!target.value.env || !target.value.namespace || !activePodName.value) {
    ElMessage.warning("缺少Pod信息");
    return;
  }
  try {
    ElMessage.info("正在获取日志...");
    const blob = await downloadPodLogs(
      target.value.env,
      target.value.namespace,
      activePodName.value,
      selectedContainer.value || undefined
    );

    if (blob.type === "application/json") {
      const text = await blob.text();
      const error = JSON.parse(text);
      ElMessage.error(error.message || "获取日志失败");
      return;
    }

    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const timestamp = new Date()
      .toISOString()
      .replace(/[:.]/g, "-")
      .slice(0, 19);
    const container = selectedContainer.value
      ? `_${selectedContainer.value}`
      : "";
    a.download = `${activePodName.value}${container}_${timestamp}.log.gz`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    ElMessage.success("日志下载成功");
  } catch (error) {
    console.error("下载日志失败:", error);
    ElMessage.error("下载日志失败");
  }
};

// ---- 滚动 ----
const scrollToBottom = () => {
  if (logContentRef.value) {
    logContentRef.value.scrollTop = logContentRef.value.scrollHeight;
    isUserScrolling.value = false;
  }
};

const isAtBottom = () => {
  if (!logContentRef.value) return false;
  const { scrollTop, scrollHeight, clientHeight } = logContentRef.value;
  return scrollTop + clientHeight >= scrollHeight - 10;
};

const handleScrollRaw = () => {
  if (!logContentRef.value) return;
  isUserScrolling.value = !isAtBottom();
};
const handleScroll = useThrottleFn(handleScrollRaw, 100);

// ---- ANSI + 高亮 ----
const ansiUp = new AnsiUp();
ansiUp.escape_html = true;
ansiUp.use_classes = false;

const convertAnsiToHtml = (message: string) => ansiUp.ansi_to_html(message);

const highlightSearchKeyword = (message: string, index: number) => {
  const processedMessage = convertAnsiToHtml(message);

  if (!searchKeyword.value.trim()) {
    return processedMessage;
  }

  const keyword = searchKeyword.value;
  let isCurrentMatch = false;
  if (isFilterMode.value) {
    const filteredIndex = getFilteredIndex(index);
    isCurrentMatch =
      searchMatches.value[currentMatchIndex.value] === filteredIndex;
  } else {
    isCurrentMatch = searchMatches.value[currentMatchIndex.value] === index;
  }

  if (!message.toLowerCase().includes(keyword.toLowerCase())) {
    return processedMessage;
  }

  const regex = new RegExp(
    `(${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`,
    "gi"
  );
  const highlightClass = isCurrentMatch
    ? "search-highlight-current"
    : "search-highlight";

  return processedMessage.replace(
    regex,
    `<span class="${highlightClass}">$1</span>`
  );
};

// ---- 搜索 ----
const performSearchRaw = (forceFirstMatch = false) => {
  if (!searchKeyword.value.trim()) {
    searchMatches.value = [];
    currentMatchIndex.value = -1;
    totalMatches.value = 0;
    return;
  }

  let currentMatchContent = "";
  if (currentMatchIndex.value >= 0 && searchMatches.value.length > 0) {
    const currentLineIndex = searchMatches.value[currentMatchIndex.value];
    const list = isFilterMode.value
      ? filteredLogMessages.value
      : logMessages.value;
    if (currentLineIndex < list.length) {
      currentMatchContent = list[currentLineIndex].text;
    }
  }

  const matches: number[] = [];
  const keyword = searchKeyword.value.toLowerCase();
  const list = isFilterMode.value
    ? filteredLogMessages.value
    : logMessages.value;

  list.forEach((message, index) => {
    if (message.text.toLowerCase().includes(keyword)) {
      matches.push(index);
    }
  });

  searchMatches.value = matches;
  totalMatches.value = matches.length;

  let newMatchIndex = 0;
  if (matches.length > 0) {
    if (forceFirstMatch) {
      newMatchIndex = 0;
    } else if (currentMatchContent) {
      const sameContentIndex = matches.findIndex(
        matchIndex => list[matchIndex].text === currentMatchContent
      );
      if (sameContentIndex >= 0) {
        newMatchIndex = sameContentIndex;
      } else {
        const oldLineIndex = searchMatches.value[currentMatchIndex.value] || 0;
        let closestIndex = 0;
        let minDistance = Math.abs(matches[0] - oldLineIndex);
        for (let i = 1; i < matches.length; i++) {
          const distance = Math.abs(matches[i] - oldLineIndex);
          if (distance < minDistance) {
            minDistance = distance;
            closestIndex = i;
          }
        }
        newMatchIndex = closestIndex;
      }
    }
    currentMatchIndex.value = newMatchIndex;
    if (forceFirstMatch || !currentMatchContent) {
      scrollToMatch(matches[newMatchIndex]);
    }
  } else {
    currentMatchIndex.value = -1;
  }
};

const performSearchDebounced = useDebounceFn(performSearchRaw, 300);
const performSearch = (forceFirstMatch = false) =>
  performSearchRaw(forceFirstMatch);

const goToPreviousMatch = () => {
  if (searchMatches.value.length === 0) return;
  currentMatchIndex.value =
    currentMatchIndex.value > 0
      ? currentMatchIndex.value - 1
      : searchMatches.value.length - 1;
  scrollToMatch(searchMatches.value[currentMatchIndex.value]);
};

const goToNextMatch = () => {
  if (searchMatches.value.length === 0) return;
  currentMatchIndex.value =
    currentMatchIndex.value < searchMatches.value.length - 1
      ? currentMatchIndex.value + 1
      : 0;
  scrollToMatch(searchMatches.value[currentMatchIndex.value]);
};

const scrollToMatch = (lineIndex: number) => {
  if (!logContentRef.value) return;
  const logLines = logContentRef.value.querySelectorAll(".log-line");
  if (logLines[lineIndex]) {
    const targetElement = logLines[lineIndex] as HTMLElement;
    const containerHeight = logContentRef.value.clientHeight;
    const elementTop = targetElement.offsetTop;
    const elementHeight = targetElement.offsetHeight;
    const scrollTop = elementTop - containerHeight / 2 + elementHeight / 2;
    logContentRef.value.scrollTo({
      top: Math.max(0, scrollTop),
      behavior: "smooth"
    });
  }
};

// 获取重启前日志（仅单 Pod 模式）
const getPreviousLogs = async () => {
  if (isAllPodsMode.value) {
    ElMessage.warning("全部Pod模式不支持重启前日志，请选择具体Pod");
    return;
  }
  if (!activePodName.value || !target.value.env || !target.value.namespace) {
    ElMessage.warning("缺少Pod信息，无法获取重启前日志");
    return;
  }

  try {
    stopLogStream();
    clearLogs();
    logConnecting.value = true;

    const data = await getPodPreviousLogs(
      target.value.env,
      target.value.namespace,
      activePodName.value,
      400
    );

    if (data.success && data.message) {
      logMessages.value = data.message
        .split("\n")
        .filter((line: string) => line.trim() !== "")
        .map((line: string) => ({ text: line }));
      ElMessage.success("重启前日志获取成功");
      nextTick(() => scrollToBottom());
    } else {
      ElMessage.warning(data.message || "获取重启前日志失败");
    }
  } catch (error: any) {
    console.error("获取重启前日志失败:", error);
    ElMessage.error(`获取重启前日志失败: ${error?.message || error}`);
  } finally {
    logConnecting.value = false;
  }
};

// ---- 对外方法 ----
const resetState = () => {
  logMessages.value = [];
  searchKeyword.value = "";
  searchMatches.value = [];
  currentMatchIndex.value = -1;
  totalMatches.value = 0;
  isFilterMode.value = false;
  isUserScrolling.value = false;
};

// Pod 级入口：Pod 下拉锁定为该 pod（只读置灰）
const openForPod = (opts: {
  env: string;
  namespace: string;
  deployment?: string;
  pod: string;
  containers?: ContainerInput[];
}) => {
  mode.value = "pod";
  target.value = {
    env: opts.env,
    namespace: opts.namespace,
    deployment: opts.deployment || ""
  };
  const containers = normalizeContainers(opts.containers);
  podList.value = [{ name: opts.pod, containers }];
  selectedPod.value = opts.pod;
  selectedContainer.value = containers[0] || "";
  resetState();
  logDialogVisible.value = true;
  document.body.style.overflow = "hidden";
};

// Deployment 级入口：Pod 下拉“全部 + 各 Pod”，默认“全部”
const openForDeployment = (opts: {
  env: string;
  namespace: string;
  deployment: string;
  pods: Array<{ name: string; containers?: ContainerInput[] }>;
}) => {
  mode.value = "deployment";
  target.value = {
    env: opts.env,
    namespace: opts.namespace,
    deployment: opts.deployment
  };
  podList.value = (opts.pods || []).map(p => ({
    name: p.name,
    containers: normalizeContainers(p.containers)
  }));
  selectedPod.value = ALL_PODS;
  selectedContainer.value = currentContainers.value[0] || "";
  resetState();
  logDialogVisible.value = true;
  document.body.style.overflow = "hidden";
  if (podList.value.length === 0) {
    ElMessage.warning("该Deployment下暂无Pod");
  }
};

defineExpose({ openForPod, openForDeployment });

// ---- 关闭与清理 ----
const closeLogDialog = () => {
  stopLogStream();
  logDialogVisible.value = false;
  resetState();
  podList.value = [];
  selectedPod.value = ALL_PODS;
  document.body.style.overflow = "";
};

watch(logContentRef, (newRef, oldRef) => {
  if (oldRef) oldRef.removeEventListener("scroll", handleScroll);
  if (newRef) newRef.addEventListener("scroll", handleScroll);
});

watch(logMessages, () => {
  if (searchKeyword.value.trim()) {
    performSearchDebounced();
  }
});

// 切换 Pod（未连接时）：清空历史并同步容器选择
watch(selectedPod, () => {
  logMessages.value = [];
  selectedContainer.value = currentContainers.value[0] || "";
});

onBeforeUnmount(() => {
  if (logContentRef.value) {
    logContentRef.value.removeEventListener("scroll", handleScroll);
  }
  stopLogStream();
  document.body.style.overflow = "";
});
</script>

<style scoped>
.log-container {
  display: flex;
  flex-direction: column;
  height: 92vh;
}

.log-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 28px;
  padding: 4px 0;
  margin: 0;
}

.dialog-title {
  font-size: 12px;
  font-weight: 600;
  color: #303133;
}

.log-controls-header {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.log-controls-header .el-button {
  height: 24px !important;
  padding: 4px 8px !important;
  font-size: 11px !important;
}

.log-status {
  margin-left: 12px;
}

.status-connected {
  font-weight: bold;
  color: #67c23a;
}

.status-disconnected {
  font-weight: bold;
  color: #f56c6c;
}

.log-content {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
  font-family: Consolas, Monaco, "Courier New", monospace;
  font-size: 13px;
  line-height: 1.4;
  color: #d4d4d4;
  word-break: break-all;
  white-space: pre-wrap;
  background-color: #1e1e1e;
  border-radius: 4px;
}

.no-logs {
  padding: 40px 0;
  font-size: 14px;
  color: #909399;
  text-align: center;
}

.log-line {
  padding: 2px 0;
  margin-bottom: 2px;
}

/* “全部Pod”模式下每行前缀：Pod 名标签（黄底黑字） */
.pod-badge {
  display: inline-block;
  padding: 0 6px;
  margin-right: 8px;
  font-weight: 600;
  color: #000;
  white-space: nowrap;
  vertical-align: top;
  background-color: #ffd500;
  border-radius: 2px;
  user-select: text;
}

.log-text {
  white-space: pre-wrap;
  word-break: break-all;
}

.log-error {
  padding: 2px 4px;
  color: #f56c6c;
  background-color: rgb(245 108 108 / 10%);
  border-radius: 2px;
}

.log-warn {
  padding: 2px 4px;
  color: #e6a23c;
  background-color: rgb(230 162 60 / 10%);
  border-radius: 2px;
}

.log-info {
  color: #409eff;
}

.log-search-container {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-left: 16px;
}

.log-search-container .el-button {
  height: 24px !important;
  padding: 4px 8px !important;
  font-size: 11px !important;
}

.search-info {
  font-size: 12px;
  color: #606266;
  white-space: nowrap;
}
</style>

<style>
@keyframes log-viewer-pulse {
  0% {
    box-shadow: 0 0 4px rgb(255 102 0 / 80%);
  }

  50% {
    box-shadow: 0 0 8px rgb(255 102 0 / 100%);
  }

  100% {
    box-shadow: 0 0 4px rgb(255 102 0 / 80%);
  }
}

/* 搜索高亮样式 - 针对黑色背景优化，必须在非 scoped 样式中定义 */
.search-highlight {
  padding: 1px 3px;
  font-weight: bold;
  color: #000 !important;
  background-color: #ff0 !important;
  border-radius: 3px;
  box-shadow: 0 0 2px rgb(255 255 0 / 50%);
}

.search-highlight-current {
  padding: 1px 3px;
  font-weight: bold;
  color: #fff !important;
  background-color: #f60 !important;
  border-radius: 3px;
  box-shadow: 0 0 4px rgb(255 102 0 / 80%);
  animation: log-viewer-pulse 1s infinite;
}
</style>
