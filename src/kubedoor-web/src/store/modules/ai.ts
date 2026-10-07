import { computed, ref, watch } from "vue";
import { defineStore } from "pinia";
import { store } from "@/store";
import {
  aiApi,
  streamAIEvents,
  AIRequestError,
  type AIBootstrap,
  type AIEvent,
  type AIMemorySummary,
  type AIMessage,
  type AIProvider,
  type AIRun,
  type AIScope,
  type AISession
} from "@/api/ai";
import {
  isTerminalRun,
  newAIId,
  normalizeAITool,
  providerStorageKey,
  safeAIText
} from "@/utils/ai";

const emptyScope = (): AIScope => ({
  env: "",
  namespace: null,
  deployment: null,
  pod: null
});
const defaultProvider = (): AIProvider => ({
  base_url: "https://api.openai.com/v1",
  api_key: "",
  model: ""
});
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败，请稍后重试。";

export const useAIStore = defineStore("kubedoor-ai", () => {
  const visible = ref(false);
  const mounted = ref(false);
  const loading = ref(false);
  const initialized = ref(false);
  const bootstrap = ref<AIBootstrap | null>(null);
  const provider = ref<AIProvider>(defaultProvider());
  const sessions = ref<AISession[]>([]);
  const sessionId = ref("");
  const sessionLoading = ref(false);
  const sessionReady = ref(true);
  const messages = ref<AIMessage[]>([]);
  const scope = ref<AIScope>(emptyScope());
  const skillIds = ref<string[]>([]);
  const selectedMemories = ref<AIMemorySummary[]>([]);
  const summarizingMemory = ref(false);
  const autoApprove = ref(false);
  const activeRun = ref<AIRun | null>(null);
  const submitting = ref(false);
  const deciding = ref("");
  const cancelling = ref(false);
  const error = ref("");
  const streamError = ref("");
  const streamState = ref("idle");
  const lastSeq = ref(0);
  const writable = computed(() => bootstrap.value?.permission === "rw");
  const configured = computed(
    () => !!provider.value.base_url.trim() && !!provider.value.model.trim()
  );
  const busy = computed(
    () =>
      submitting.value ||
      (!!activeRun.value && !isTerminalRun(activeRun.value.status))
  );
  const selectedCluster = computed(() =>
    bootstrap.value?.clusters.find(cluster => cluster.env === scope.value.env)
  );
  const clusterAvailable = computed(() => !!selectedCluster.value);
  const liveAvailable = computed(
    () =>
      !!selectedCluster.value &&
      (selectedCluster.value.online || selectedCluster.value.configured)
  );
  const pendingTools = computed(() =>
    messages.value
      .flatMap(message => message.tools || [])
      .filter(tool => tool.pending)
  );
  let streamController: AbortController | null = null;
  let streamGeneration = 0;
  let sessionController: AbortController | null = null;
  let sessionGeneration = 0;
  let listGeneration = 0;
  let submissionGeneration = 0;
  let summaryGeneration = 0;
  const sessionCache = new Map<string, { data: AISession; weight: number }>();
  const draftScopes = new Map<string, AIScope>();
  let owner = "";
  let assistantId = "";
  const approvalReady = ref(false);
  const autoApproving = ref(false);
  const approvalActionIds = new Set<string>();
  const settledActions = new Set<string>();
  let approvalGeneration = 0;
  let runStatusRevision = 0;
  let recoveredEventSeq = 0;
  const automaticPending = computed(() =>
    pendingTools.value.filter(
      tool =>
        tool.action_id &&
        approvalActionIds.has(tool.action_id) &&
        !settledActions.has(`${activeRun.value?.id}:${tool.action_id}`)
    )
  );

  function resetApprovalState() {
    approvalGeneration++;
    runStatusRevision++;
    approvalReady.value = false;
    approvalActionIds.clear();
    recoveredEventSeq = 0;
    autoApproving.value = false;
    deciding.value = "";
    cancelling.value = false;
  }

  watch(
    [autoApprove, writable],
    ([enabled, allowed]) => {
      if (enabled && !allowed) autoApprove.value = false;
    },
    { flush: "sync" }
  );
  watch(
    () => [
      autoApprove.value,
      approvalReady.value,
      autoApproving.value,
      activeRun.value?.id,
      activeRun.value?.status,
      deciding.value,
      cancelling.value,
      automaticPending.value.map(tool => tool.action_id).join(",")
    ],
    () => void approveAutomatically()
  );

  async function approveAutomatically() {
    if (
      !autoApprove.value ||
      !writable.value ||
      !approvalReady.value ||
      !sessionReady.value ||
      autoApproving.value ||
      deciding.value ||
      cancelling.value ||
      activeRun.value?.status !== "waiting_approval"
    )
      return;
    const actionId = automaticPending.value[0]?.action_id;
    if (!actionId) return;
    const runId = activeRun.value.id;
    const generation = approvalGeneration;
    const readyRevision = runStatusRevision;
    let submitted = false;
    const canContinue = () =>
      autoApprove.value &&
      writable.value &&
      sessionReady.value &&
      !cancelling.value &&
      !deciding.value &&
      generation === approvalGeneration &&
      activeRun.value?.id === runId &&
      activeRun.value.status === "waiting_approval" &&
      automaticPending.value.some(tool => tool.action_id === actionId);
    autoApproving.value = true;
    // An approval event arrives before the server has saved its checkpoint.
    // Consume one confirmed waiting status for each graph resume, even when
    // several parallel actions are visible in the tool group.
    approvalReady.value = false;
    try {
      const delays = [150, 300, 600, 1200];
      for (let attempt = 0; canContinue(); attempt++) {
        try {
          await decide(actionId, "approve");
          submitted = true;
          return;
        } catch (reason) {
          if (!canContinue()) return;
          if (
            reason instanceof AIRequestError &&
            reason.status === 409 &&
            ["run_active", "checkpoint_pending"].includes(reason.code || "") &&
            attempt < delays.length
          ) {
            await new Promise<void>(resolve =>
              window.setTimeout(resolve, delays[attempt])
            );
            continue;
          }
          autoApprove.value = false;
          error.value = `自动批准失败，已关闭自动批准，请查看工具操作：${errorText(reason)}`;
          return;
        }
      }
    } finally {
      if (
        !submitted &&
        generation === approvalGeneration &&
        readyRevision === runStatusRevision &&
        activeRun.value?.id === runId &&
        ["waiting_approval", "cancelling"].includes(activeRun.value.status)
      )
        approvalReady.value = true;
      if (generation === approvalGeneration) autoApproving.value = false;
    }
  }

  function resetForUser() {
    sessionController?.abort();
    sessionGeneration++;
    listGeneration++;
    submissionGeneration++;
    summaryGeneration++;
    summarizingMemory.value = false;
    sessionCache.clear();
    draftScopes.clear();
    sessionLoading.value = false;
    sessionReady.value = true;
    submitting.value = false;
    deciding.value = "";
    cancelling.value = false;
    autoApproving.value = false;
    streamController?.abort();
    streamGeneration++;
    resetApprovalState();
    autoApprove.value = false;
    settledActions.clear();
    sessions.value = [];
    messages.value = [];
    sessionId.value = "";
    activeRun.value = null;
    scope.value = emptyScope();
    skillIds.value = [];
    selectedMemories.value = [];
    provider.value = defaultProvider();
    lastSeq.value = 0;
    assistantId = "";
    initialized.value = false;
  }

  function saveProvider(value: AIProvider) {
    if (!owner) throw new Error("尚未获取登录身份，请重试连接服务。");
    const settings = {
      base_url: value.base_url.trim().replace(/\/+$/, ""),
      api_key: value.api_key.trim(),
      model: value.model.trim()
    };
    localStorage.setItem(providerStorageKey(owner), JSON.stringify(settings));
    provider.value = settings;
  }

  function forgetProvider() {
    if (owner) localStorage.removeItem(providerStorageKey(owner));
    provider.value = defaultProvider();
  }

  async function refreshBootstrap() {
    const data = await aiApi.bootstrap();
    if (!data.username)
      throw new Error("AI 服务未收到登录身份，请检查认证代理配置。");
    if (owner !== data.username) {
      resetForUser();
      owner = data.username;
      try {
        const saved = JSON.parse(
          localStorage.getItem(providerStorageKey(owner)) || "null"
        );
        if (
          saved &&
          typeof saved.base_url === "string" &&
          typeof saved.model === "string"
        ) {
          provider.value = {
            base_url: saved.base_url,
            model: saved.model,
            api_key: saved.api_key || ""
          };
        }
      } catch {
        error.value = "浏览器中的模型配置无法读取，请重新配置。";
      }
    }
    bootstrap.value = {
      ...data,
      clusters: data.clusters || [],
      skills: data.skills || []
    };
    if (
      !scope.value.env ||
      !data.clusters.some(item => item.env === scope.value.env)
    ) {
      const available = data.clusters.find(
        item => item.online || item.configured
      );
      scope.value = {
        ...emptyScope(),
        env: available?.env || data.clusters[0]?.env || ""
      };
    }
  }

  async function refreshSessions() {
    const generation = ++listGeneration;
    const username = owner;
    const result = await aiApi.sessions();
    if (generation !== listGeneration || username !== owner) return;
    const remote = result.sessions || [];
    sessions.value = [...sessions.value.filter(item => item.draft), ...remote];
    const available = new Set(remote.map(item => item.id));
    for (const id of sessionCache.keys()) {
      if (!available.has(id) && id !== sessionId.value) sessionCache.delete(id);
    }
  }

  async function open(requestedSession?: string) {
    mounted.value = true;
    visible.value = true;
    if (loading.value) return;
    loading.value = true;
    error.value = "";
    try {
      const previousOwner = owner;
      const previousSelection = sessionGeneration;
      await refreshBootstrap();
      const selection =
        previousOwner !== owner ? sessionGeneration : previousSelection;
      if (!bootstrap.value.enabled) return;
      await refreshSessions();
      if (selection !== sessionGeneration) {
        initialized.value = true;
        return;
      }
      if (requestedSession) {
        if (busy.value && requestedSession !== sessionId.value)
          throw new Error("当前会话正在执行，请先停止后再打开另一个会话。");
        if (!busy.value || requestedSession !== sessionId.value) {
          await selectSession(requestedSession);
        }
      } else if (!initialized.value && sessions.value.length) {
        await selectSession(sessions.value[0].id);
      } else if (sessionId.value && !busy.value) {
        // MCP clients can add pending actions while this dialog is closed.
        await selectSession(sessionId.value);
      }
      initialized.value = true;
    } catch (reason) {
      error.value = errorText(reason);
    } finally {
      loading.value = false;
    }
  }

  function recoverActions(run: AIRun) {
    for (const action of run.pending_actions || []) {
      if (action.action_id) approvalActionIds.add(String(action.action_id));
      upsertTool(
        action,
        "approval",
        `${run.id}:${String(action.action_id || newAIId())}`,
        true
      );
    }
  }

  function normalizedSession(data: AISession): AISession {
    return {
      ...data,
      messages: (data.messages || []).map(message => ({
        ...message,
        content:
          typeof message.content === "string"
            ? message.content
            : safeAIText(message.content),
        tools: (message.tools || []).map(tool =>
          normalizeAITool(tool, message.scope?.env)
        ),
        memories: (message.memories || []).map(memory => ({
          id: memory.id,
          title: safeAIText(memory.title),
          content: safeAIText(memory.content),
          version: memory.version
        }))
      }))
    };
  }

  function cacheSession(data: AISession) {
    sessionCache.delete(data.id);
    const history = data.messages || [];
    // Bound both entry count and total history size. A single huge conversation
    // stays visible but is not retained when the user switches away.
    if (history.length > 2000) return;
    const weight = history.reduce(
      (total, message) =>
        total +
        message.content.length +
        JSON.stringify(message.tools || []).length +
        JSON.stringify(message.memories || []).length,
      0
    );
    if (weight > 2000000) return;
    sessionCache.set(data.id, { data, weight });
    let total = [...sessionCache.values()].reduce(
      (sum, entry) => sum + entry.weight,
      0
    );
    while (sessionCache.size > 16 || total > 2000000) {
      const oldest = sessionCache.keys().next().value!;
      total -= sessionCache.get(oldest)!.weight;
      sessionCache.delete(oldest);
    }
  }

  function showHistory(data: AISession) {
    messages.value = data.messages || [];
    const lastScope = [...messages.value]
      .reverse()
      .find(message => message.scope)?.scope;
    if (lastScope) scope.value = JSON.parse(JSON.stringify(lastScope));
  }

  function beginSelection(id: string) {
    if (sessions.value.find(item => item.id === sessionId.value)?.draft)
      draftScopes.set(sessionId.value, JSON.parse(JSON.stringify(scope.value)));
    sessionController?.abort();
    sessionController = null;
    sessionGeneration++;
    summaryGeneration++;
    summarizingMemory.value = false;
    streamController?.abort();
    streamGeneration++;
    resetApprovalState();
    sessionId.value = id;
    selectedMemories.value = [];
    activeRun.value = null;
    assistantId = "";
    lastSeq.value = 0;
    streamState.value = "idle";
    streamError.value = "";
    error.value = "";
  }

  async function selectSession(id: string) {
    if (busy.value && id !== sessionId.value) return;
    beginSelection(id);
    const draft = sessions.value.find(item => item.id === id && item.draft);
    if (draft) {
      messages.value = [];
      if (draftScopes.has(id))
        scope.value = JSON.parse(JSON.stringify(draftScopes.get(id)));
      sessionReady.value = true;
      sessionLoading.value = false;
      return;
    }
    const cached = sessionCache.get(id);
    if (cached) {
      sessionCache.delete(id);
      sessionCache.set(id, cached);
      showHistory(cached.data);
    } else messages.value = [];
    sessionReady.value = false;
    sessionLoading.value = true;
    const generation = sessionGeneration;
    const username = owner;
    const controller = new AbortController();
    sessionController = controller;
    try {
      const response = await aiApi.session(id, controller.signal);
      if (
        generation !== sessionGeneration ||
        username !== owner ||
        controller.signal.aborted
      )
        return;
      if (response.id !== id)
        throw new Error("会话返回的标识不匹配，请刷新后重试。");
      const data = normalizedSession(response);
      cacheSession(data);
      showHistory(data);
      sessionReady.value = true;
      activeRun.value =
        typeof data.active_run === "string"
          ? { id: data.active_run, status: "running" }
          : data.active_run || null;
      if (activeRun.value && !isTerminalRun(activeRun.value.status)) {
        recoveredEventSeq = activeRun.value.event_seq || 0;
        if (activeRun.value.scope)
          scope.value = JSON.parse(JSON.stringify(activeRun.value.scope));
        const lastMessage = messages.value[messages.value.length - 1];
        if (lastMessage?.role === "assistant") assistantId = lastMessage.id;
        recoverActions(activeRun.value);
        approvalReady.value = activeRun.value.status === "waiting_approval";
        void connectRun(activeRun.value.id);
      }
    } catch (reason) {
      if (
        generation !== sessionGeneration ||
        controller.signal.aborted ||
        username !== owner
      )
        return;
      error.value = errorText(reason);
      throw reason;
    } finally {
      if (generation === sessionGeneration) {
        sessionLoading.value = false;
        sessionController = null;
      }
    }
  }

  async function createSession() {
    if (busy.value) return;
    const existing = sessions.value.find(item => item.draft);
    const created: AISession = existing || {
      id: `draft:${newAIId()}`,
      title: "新会话",
      created_at: new Date().toISOString(),
      draft: true
    };
    if (!existing) sessions.value.unshift(created);
    beginSelection(created.id);
    draftScopes.set(created.id, JSON.parse(JSON.stringify(scope.value)));
    messages.value = [];
    sessionLoading.value = false;
    sessionReady.value = true;
    return created;
  }

  async function renameSession(id: string, title: string) {
    const session = sessions.value.find(item => item.id === id);
    const username = owner;
    if (!session?.draft) await aiApi.renameSession(id, title.trim());
    if (username !== owner) return;
    listGeneration++;
    if (session) session.title = title.trim();
    const cached = sessionCache.get(id);
    if (cached) cached.data.title = title.trim();
  }

  async function deleteSession(id: string) {
    if (busy.value) return;
    const username = owner;
    if (!sessions.value.find(item => item.id === id)?.draft)
      await aiApi.deleteSession(id);
    if (username !== owner) return;
    listGeneration++;
    sessionCache.delete(id);
    draftScopes.delete(id);
    sessions.value = sessions.value.filter(item => item.id !== id);
    if (sessionId.value === id) {
      beginSelection("");
      sessionReady.value = true;
      sessionLoading.value = false;
      messages.value = [];
      resetApprovalState();
      activeRun.value = null;
      if (sessions.value.length) await selectSession(sessions.value[0].id);
    }
  }

  function changeScope(value: AIScope) {
    if (busy.value || !sessionReady.value) return;
    scope.value = JSON.parse(JSON.stringify(value));
  }

  function setSelectedMemories(items: AIMemorySummary[]) {
    if (busy.value || !sessionReady.value)
      throw new Error("当前会话正在执行或核验状态，请稍后引入记忆。");
    const unique = [...new Map(items.map(item => [item.id, item])).values()];
    if (unique.length > 10) throw new Error("每轮最多引入 10 条记忆。");
    selectedMemories.value = unique.map(item => ({
      id: item.id,
      title: item.title,
      version: item.version,
      created_by: item.created_by,
      updated_by: item.updated_by,
      created_at: item.created_at,
      updated_at: item.updated_at
    }));
  }

  function removeSelectedMemory(id: string) {
    if (busy.value || !sessionReady.value) return;
    selectedMemories.value = selectedMemories.value.filter(
      item => item.id !== id
    );
  }

  async function summarizeMemory() {
    if (busy.value || !sessionReady.value || summarizingMemory.value)
      throw new Error("请结束当前轮次并完成会话状态核验后再总结。");
    if (
      !sessionId.value ||
      sessions.value.some(item => item.id === sessionId.value && item.draft)
    )
      throw new Error("当前会话没有可总结的已完成对话。");
    if (!configured.value)
      throw new Error("请先配置模型的 Base URL 和模型名。");
    const username = owner;
    const id = sessionId.value;
    const generation = sessionGeneration;
    const summary = ++summaryGeneration;
    summarizingMemory.value = true;
    try {
      const draft = await aiApi.summarizeMemory(id, { ...provider.value });
      if (
        username !== owner ||
        id !== sessionId.value ||
        generation !== sessionGeneration
      )
        throw new Error("会话已切换，请在当前会话重新生成记忆草稿。");
      return draft;
    } finally {
      if (summary === summaryGeneration) summarizingMemory.value = false;
    }
  }

  function assistantMessage(): AIMessage {
    let message = messages.value.find(item => item.id === assistantId);
    if (!message) {
      assistantId = newAIId();
      message = {
        id: assistantId,
        role: "assistant",
        content: "",
        scope: activeRun.value?.scope || { ...scope.value },
        tools: [],
        created_at: new Date().toISOString()
      };
      messages.value.push(message);
      // Work on Vue's reactive item, not the detached original object.
      message = messages.value[messages.value.length - 1];
    }
    return message;
  }

  function upsertTool(
    data: Record<string, any>,
    type: string,
    fallbackId: string,
    replayed = false
  ) {
    const tools = assistantMessage().tools!;
    const actionId = String(data.action_id || data.approval_id || "");
    const callId = String(data.tool_call_id || data.call_id || data.id || "");
    const name = String(data.name || data.tool_name || data.tool || "工具调用");
    let tool = tools.find(
      item =>
        (actionId && item.action_id === actionId) ||
        (callId && (item.tool_call_id === callId || item.id === callId))
    );
    if (!tool && type !== "tool_start") {
      tool = [...tools]
        .reverse()
        .find(
          item =>
            item.name === name &&
            (!actionId || !item.action_id) &&
            ["running", "pending", "waiting_approval"].includes(item.status)
        );
    }
    if (!tool) {
      tools.push({
        id: actionId || callId || fallbackId,
        name,
        status: "running"
      });
      tool = tools[tools.length - 1];
    }
    const result = data.result ?? data.output;
    const meta = result && typeof result === "object" ? result : {};
    Object.assign(tool, {
      name,
      ...(actionId ? { action_id: actionId } : {}),
      ...(callId ? { tool_call_id: callId } : {}),
      env:
        data.env ||
        data.cluster ||
        data.scope?.env ||
        tool.env ||
        activeRun.value?.scope?.env,
      namespace: data.namespace || data.scope?.namespace || tool.namespace,
      source: data.source || data.connector || meta.source || tool.source,
      observed_at:
        data.observed_at ||
        data.timestamp ||
        meta.observed_at ||
        meta.collected_at ||
        tool.observed_at,
      freshness: data.freshness || meta.freshness || tool.freshness,
      truncated: data.truncated ?? meta.truncated ?? tool.truncated,
      arguments: data.arguments ?? data.args ?? tool.arguments,
      command: data.command ?? tool.command,
      diff: data.diff ?? tool.diff,
      preview: data.preview ?? tool.preview
    });
    if (type === "tool_start") {
      if (actionId) settledActions.add(`${activeRun.value?.id}:${actionId}`);
      tool.pending = false;
      tool.status = "running";
    } else if (type === "approval") {
      if (settledActions.has(`${activeRun.value?.id}:${actionId}`)) return;
      if (replayed && !approvalActionIds.has(actionId)) return;
      if (actionId) approvalActionIds.add(actionId);
      tool.pending = true;
      tool.status = "waiting_approval";
      if (activeRun.value && !replayed)
        activeRun.value.status = "waiting_approval";
    } else if (type === "tool_result") {
      if (actionId) settledActions.add(`${activeRun.value?.id}:${actionId}`);
      tool.pending = false;
      tool.result = result ?? data;
      tool.error =
        typeof (data.error || meta.error) === "string"
          ? data.error || meta.error
          : (data.error || meta.error)?.message;
      tool.status =
        data.status ||
        (meta.error?.code === "outcome_unknown"
          ? "unknown"
          : data.error || meta.error || meta.success === false
            ? "failed"
            : "completed");
    }
  }

  function handleEvent(event: AIEvent) {
    if (event.seq && event.seq <= lastSeq.value) return;
    if (event.seq) lastSeq.value = event.seq;
    const replayed = event.seq > 0 && event.seq <= recoveredEventSeq;
    const data: Record<string, any> =
      typeof event.data === "string"
        ? { content: event.data }
        : event.data || {};
    if (event.type === "token") {
      assistantMessage().content += String(
        data.token ?? data.text ?? data.content ?? ""
      );
    } else if (event.type === "message") {
      if (data.content !== undefined)
        assistantMessage().content = safeAIText(data.content);
    } else if (event.type === "tool_start" || event.type === "tool_result") {
      upsertTool(data, event.type, `${activeRun.value?.id}:${event.seq}`);
    } else if (event.type === "approval") {
      for (const action of data.actions || data.pending_actions || [data]) {
        upsertTool(
          action,
          "approval",
          `${activeRun.value?.id}:${event.seq}`,
          replayed
        );
      }
    } else if (event.type === "status") {
      // Rebuild streamed content after session recovery without reusing the
      // snapshot's old waiting status to resume the graph a second time.
      if (replayed) return;
      runStatusRevision++;
      if (activeRun.value)
        activeRun.value.status = String(
          data.status || data.state || data.content || "running"
        );
      approvalReady.value = activeRun.value?.status === "waiting_approval";
      if (data.error) error.value = String(data.error);
    } else if (event.type === "error") {
      if (replayed) return;
      runStatusRevision++;
      approvalReady.value = false;
      error.value = String(
        data.message || data.error || data.content || "本轮执行失败。"
      );
      if (activeRun.value) activeRun.value.status = "failed";
    } else if (event.type === "done") {
      if (replayed) return;
      runStatusRevision++;
      approvalReady.value = false;
      if (activeRun.value)
        activeRun.value.status = String(data.status || "completed");
    }
  }

  async function connectRun(runId: string) {
    streamController?.abort();
    const generation = ++streamGeneration;
    const controller = new AbortController();
    streamController = controller;
    let attempts = 0;
    while (generation === streamGeneration && !controller.signal.aborted) {
      if (
        !activeRun.value ||
        activeRun.value.id !== runId ||
        isTerminalRun(activeRun.value.status)
      )
        break;
      streamState.value = attempts ? "reconnecting" : "connecting";
      try {
        await streamAIEvents(runId, lastSeq.value, controller.signal, event => {
          if (generation !== streamGeneration) return;
          streamState.value = "connected";
          streamError.value = "";
          attempts = 0;
          handleEvent(event);
          if (activeRun.value && isTerminalRun(activeRun.value.status))
            controller.abort();
        });
      } catch (reason) {
        if (controller.signal.aborted || generation !== streamGeneration) break;
        if (
          reason instanceof AIRequestError &&
          [401, 403, 404, 410].includes(reason.status)
        ) {
          streamError.value = `${errorText(reason)}；请刷新会话状态。`;
          streamState.value = "disconnected";
          break;
        }
        streamError.value =
          "流连接已断开，正在恢复进度。已提交的操作不会重复发送。";
      }
      if (activeRun.value && isTerminalRun(activeRun.value.status)) break;
      attempts++;
      await new Promise<void>(resolve => {
        const timer = window.setTimeout(
          resolve,
          Math.min(1000 * 2 ** Math.min(attempts, 4), 20000)
        );
        controller.signal.addEventListener(
          "abort",
          () => {
            window.clearTimeout(timer);
            resolve();
          },
          { once: true }
        );
      });
    }
    if (generation !== streamGeneration) return;
    if (activeRun.value && isTerminalRun(activeRun.value.status)) {
      streamState.value = "idle";
      for (const tool of pendingTools.value) {
        tool.pending = false;
        tool.status =
          activeRun.value.status === "completed"
            ? "completed"
            : activeRun.value.status;
      }
      try {
        const currentSession = sessionId.value;
        const username = owner;
        const data = await aiApi.session(currentSession);
        if (
          generation !== streamGeneration ||
          username !== owner ||
          currentSession !== sessionId.value
        )
          return;
        if (
          data.messages?.length &&
          data.messages[data.messages.length - 1].role === "assistant"
        ) {
          const normalized = normalizedSession(data);
          messages.value = normalized.messages || [];
          cacheSession(normalized);
        }
        await refreshSessions();
      } catch {
        if (generation !== streamGeneration) return;
        streamError.value =
          "执行已结束，但历史同步失败；当前结果保留，可稍后刷新。";
      }
    }
  }

  async function send(message: string) {
    if (busy.value || !message.trim()) return;
    if (!sessionReady.value)
      throw new Error("正在核验会话状态，请稍后发送；可以继续输入或切换会话。");
    if (!configured.value)
      throw new Error("请先配置模型的 Base URL 和模型名。");
    if (!scope.value.env || !clusterAvailable.value)
      throw new Error("请选择已登记的 K8S 集群。");
    error.value = "";
    streamError.value = "";
    const memoryIds = selectedMemories.value.map(item => item.id);
    if (!sessionId.value) {
      // Automatic first-send drafts keep the pending references. Explicit new
      // chats and session switches still clear them in beginSelection.
      const pendingMemories = selectedMemories.value;
      void createSession();
      selectedMemories.value = pendingMemories;
    }
    submitting.value = true;
    const submission = ++submissionGeneration;
    const snapshot: AIScope = JSON.parse(JSON.stringify(scope.value));
    const username = owner;
    try {
      const draft = sessions.value.find(
        item => item.id === sessionId.value && item.draft
      );
      if (draft) {
        const created = await aiApi.createSession(
          draft.id.slice(6),
          draft.title
        );
        if (username !== owner) return;
        listGeneration++;
        const index = sessions.value.findIndex(item => item.id === draft.id);
        if (index >= 0) sessions.value.splice(index, 1, created);
        else sessions.value.unshift(created);
        draftScopes.delete(draft.id);
        sessionId.value = created.id;
      }
      const currentSession = sessionId.value;
      sessionCache.delete(currentSession);
      const run = await aiApi.run(
        currentSession,
        {
          message: message.trim(),
          scope: snapshot,
          provider: { ...provider.value },
          skill_ids: [...skillIds.value],
          memory_ids: memoryIds
        },
        newAIId()
      );
      if (
        username !== owner ||
        currentSession !== sessionId.value ||
        submission !== submissionGeneration
      )
        return;
      selectedMemories.value = [];
      const userMessage = run.message || {
        id: newAIId(),
        role: "user",
        content: message.trim(),
        scope: snapshot,
        created_at: new Date().toISOString()
      };
      messages.value.push(
        normalizedSession({
          id: currentSession,
          title: "",
          messages: [userMessage]
        }).messages![0]
      );
      resetApprovalState();
      activeRun.value = { id: run.run_id, status: run.status, scope: snapshot };
      assistantId = "";
      lastSeq.value = 0;
      assistantMessage();
      void connectRun(run.run_id);
      void refreshSessions().catch(() => undefined);
    } catch (reason) {
      if (username !== owner) return;
      error.value = errorText(reason);
      throw reason;
    } finally {
      if (submission === submissionGeneration) submitting.value = false;
    }
  }

  async function decide(actionId: string, decision: "approve" | "reject") {
    if (!sessionReady.value)
      throw new Error("正在核验会话状态，暂不能批准或拒绝缓存中的操作。");
    if (!activeRun.value || deciding.value) return;
    if (decision === "approve" && !writable.value)
      throw new Error("当前账号仅有只读权限。");
    const runId = activeRun.value.id;
    const generation = approvalGeneration;
    const revision = runStatusRevision;
    const wasReady = approvalReady.value;
    approvalReady.value = false;
    deciding.value = actionId;
    error.value = "";
    try {
      await aiApi.decision(runId, actionId, decision, {
        ...provider.value
      });
      if (generation !== approvalGeneration || activeRun.value?.id !== runId)
        return;
      settledActions.add(`${runId}:${actionId}`);
      const tool = pendingTools.value.find(item => item.action_id === actionId);
      if (tool) {
        tool.pending = false;
        tool.status = decision === "approve" ? "running" : "rejected";
      }
      if (
        revision === runStatusRevision &&
        !isTerminalRun(activeRun.value.status)
      )
        activeRun.value.status = "running";
      if (streamState.value === "disconnected")
        void connectRun(activeRun.value.id);
    } catch (reason) {
      if (generation === approvalGeneration) {
        error.value = errorText(reason);
        if (
          revision === runStatusRevision &&
          activeRun.value?.id === runId &&
          activeRun.value.status === "waiting_approval"
        )
          approvalReady.value = wasReady;
      }
      throw reason;
    } finally {
      if (generation === approvalGeneration) deciding.value = "";
    }
  }

  async function cancel() {
    if (
      !activeRun.value ||
      cancelling.value ||
      isTerminalRun(activeRun.value.status)
    )
      return;
    cancelling.value = true;
    const generation = approvalGeneration;
    const runId = activeRun.value.id;
    const previous = activeRun.value.status;
    activeRun.value.status = "cancelling";
    try {
      const result = await aiApi.cancel(runId);
      if (generation !== approvalGeneration || activeRun.value?.id !== runId)
        return;
      activeRun.value.status = result.status || "cancelled";
      if (isTerminalRun(activeRun.value.status)) {
        for (const tool of pendingTools.value) {
          tool.pending = false;
          tool.status = "cancelled";
        }
        // Keep the subscription until cancellation/results are drained, then
        // the server's terminal event triggers the history synchronization.
      }
    } catch (reason) {
      if (generation !== approvalGeneration || activeRun.value?.id !== runId)
        return;
      activeRun.value.status = previous;
      error.value = errorText(reason);
      throw reason;
    } finally {
      if (generation === approvalGeneration) cancelling.value = false;
    }
  }

  return {
    visible,
    mounted,
    loading,
    initialized,
    bootstrap,
    provider,
    sessions,
    sessionId,
    sessionLoading,
    sessionReady,
    messages,
    scope,
    skillIds,
    selectedMemories,
    summarizingMemory,
    activeRun,
    submitting,
    deciding,
    cancelling,
    error,
    streamError,
    streamState,
    lastSeq,
    writable,
    autoApprove,
    configured,
    busy,
    selectedCluster,
    clusterAvailable,
    liveAvailable,
    pendingTools,
    open,
    refreshBootstrap,
    refreshSessions,
    saveProvider,
    forgetProvider,
    selectSession,
    createSession,
    renameSession,
    deleteSession,
    changeScope,
    setSelectedMemories,
    removeSelectedMemory,
    summarizeMemory,
    send,
    decide,
    cancel
  };
});

export function useAIStoreHook() {
  return useAIStore(store);
}
