import { onBeforeUnmount, ref } from "vue";
import { createWorkloadStreamUrl } from "@/api/monit";

/** agent 推来的 deployment 实时状态(字段与 agent workload_status.status_row 一致) */
export interface DeploymentLive {
  namespace: string;
  deployment: string;
  desired: number;
  ready: number;
  updated: number;
  available: number;
  total: number;
  terminating: number;
  isolated: number;
  pods: number;
  /** complete 已完成 / observing 等控制器处理 / progressing 滚动中 / paused 已暂停 / failed 滚动超时 */
  state: "complete" | "observing" | "progressing" | "paused" | "failed";
  message: string;
}

/** 某个 deployment 的全部 pod(推送不带 cpu/memory,页面保留上次拿到的值) */
export interface PodsPush {
  namespace: string;
  deployment: string;
  items: any[];
  truncated: boolean;
}

/**
 * idle 未连接 / connecting 连接中 / syncing 同步中 / live 实时 /
 * agent_offline agent 离线 / unavailable 不可用(master 或 agent 版本过旧、连不上)
 */
export type StreamState =
  | "idle"
  | "connecting"
  | "syncing"
  | "live"
  | "agent_offline"
  | "unavailable";

interface StreamHandlers {
  /** full=true 是全量快照:以这次为准,没出现的 deployment 都没有实时数据 */
  onDeployments: (
    rows: DeploymentLive[],
    removed: [string, string][],
    full: boolean
  ) => void;
  onPods: (push: PodsPush) => void;
}

const MAX_RETRY_DELAY = 30000;
const FAILS_BEFORE_UNAVAILABLE = 3;

/**
 * Deployment/Pod 实时状态推送(/ws/workload-status)
 *
 * 一个页面一条连接,对应当前选中的 env + namespace;展开明细时 watchPods 订阅该 deployment 的 pod,
 * 断线后指数退避重连并重发订阅;页面重新可见时立即重连。
 */
export function useWorkloadStream(handlers: StreamHandlers) {
  const state = ref<StreamState>("idle");
  let socket: WebSocket | null = null;
  let target: { env: string; namespace: string } | null = null;
  let retryTimer: number | undefined;
  let fails = 0;
  const watched = new Set<string>();
  let snapshotRows: DeploymentLive[] = [];

  const podKey = (namespace: string, deployment: string) =>
    `${namespace}/${deployment}`;

  const send = (msg: object) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(msg));
    }
  };

  const sendWatch = (type: "watch_pods" | "unwatch_pods", key: string) => {
    const [namespace, deployment] = key.split("/");
    send({ type, namespace, deployment });
  };

  const handleMessage = (msg: any) => {
    switch (msg?.type) {
      case "status":
        if (msg.state === "agent_offline") {
          state.value = "agent_offline";
        } else if (msg.state === "unsupported") {
          state.value = "unavailable";
        } else {
          // syncing / resyncing:保留现有数据,等全量快照
          state.value = "syncing";
        }
        break;
      case "workload_snapshot":
        if (!msg.synced) {
          state.value = "syncing";
          break;
        }
        if (msg.first) snapshotRows = [];
        snapshotRows.push(...(msg.deployments || []));
        if (msg.final) {
          handlers.onDeployments(snapshotRows, [], true);
          snapshotRows = [];
          (msg.pods || []).forEach(handlers.onPods);
          state.value = "live";
        }
        break;
      case "workload_update":
        if (msg.deployments?.length || msg.removed?.length) {
          handlers.onDeployments(
            msg.deployments || [],
            msg.removed || [],
            false
          );
        }
        (msg.pods || []).forEach(handlers.onPods);
        break;
    }
  };

  function scheduleReconnect() {
    if (!target) return;
    state.value =
      fails >= FAILS_BEFORE_UNAVAILABLE ? "unavailable" : "connecting";
    const delay = Math.min(1000 * 2 ** fails, MAX_RETRY_DELAY);
    retryTimer = window.setTimeout(open, delay);
  }

  function open() {
    if (!target) return;
    window.clearTimeout(retryTimer);
    retryTimer = undefined;
    if (state.value !== "unavailable") state.value = "connecting";
    const ws = new WebSocket(
      createWorkloadStreamUrl(target.env, target.namespace)
    );
    socket = ws;
    let received = false;
    ws.onopen = () => {
      watched.forEach(key => sendWatch("watch_pods", key));
    };
    ws.onmessage = event => {
      received = true;
      fails = 0;
      try {
        handleMessage(JSON.parse(event.data));
      } catch (error) {
        console.error("实时状态消息处理失败:", error);
      }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      // 连上过又断开(master 重启等)从 1 秒开始重连;一直连不上才累计失败次数
      if (!received) fails += 1;
      scheduleReconnect();
    };
  }

  const onVisible = () => {
    if (document.visibilityState === "visible" && target && !socket) {
      open();
    }
  };
  document.addEventListener("visibilitychange", onVisible);

  /** 连接到 env + namespace(相同参数重复调用不会重连) */
  const connect = (env: string, namespace: string) => {
    const ns = namespace || "";
    if (target && target.env === env && target.namespace === ns) return;
    close();
    target = { env, namespace: ns };
    fails = 0;
    open();
  };

  /** 断开并清空订阅(切换 env / namespace、离开页面时) */
  const close = () => {
    target = null;
    window.clearTimeout(retryTimer);
    retryTimer = undefined;
    if (socket) {
      const ws = socket;
      socket = null;
      ws.onopen = ws.onmessage = ws.onclose = null;
      ws.close();
    }
    watched.clear();
    snapshotRows = [];
    state.value = "idle";
  };

  const watchPods = (namespace: string, deployment: string) => {
    const key = podKey(namespace, deployment);
    if (watched.has(key)) return;
    watched.add(key);
    sendWatch("watch_pods", key);
  };

  const unwatchPods = (namespace: string, deployment: string) => {
    const key = podKey(namespace, deployment);
    if (!watched.delete(key)) return;
    sendWatch("unwatch_pods", key);
  };

  /** 只保留这些 deployment 的 pod 订阅(表格重新加载后清理已不存在的行) */
  const retainPods = (keys: [string, string][]) => {
    const keep = new Set(keys.map(([ns, dep]) => podKey(ns, dep)));
    [...watched].forEach(key => {
      if (!keep.has(key)) {
        watched.delete(key);
        sendWatch("unwatch_pods", key);
      }
    });
  };

  onBeforeUnmount(() => {
    document.removeEventListener("visibilitychange", onVisible);
    close();
  });

  return { state, connect, close, watchPods, unwatchPods, retainPods };
}
