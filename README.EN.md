<div align="center">

[简体中文](README.md) | English

[![StarsL.cn](https://img.shields.io/badge/website-StarsL.cn-orange)](https://starsl.cn)
[![Homepage](https://img.shields.io/badge/homepage-KubeDoor-ff69b4)](https://cassinfra.github.io/KubeDoor/)
[![Commits](https://img.shields.io/github/commit-activity/m/CassInfra/KubeDoor?color=ffff00)](https://github.com/CassInfra/KubeDoor/commits/main)
[![open issues](https://img.shields.io/github/issues/CassInfra/KubeDoor)](https://github.com/CassInfra/KubeDoor/issues)
[![Python](https://img.shields.io/badge/python-v3.14-3776ab)](https://www.python.org)
[![Vue](https://img.shields.io/badge/vue-v3-42b883)](https://vuejs.org)
[![PostgreSQL](https://img.shields.io/badge/postgresql-v18-336791)](https://www.postgresql.org)
[![Kubernetes](https://img.shields.io/badge/kubernetes-1.21%2B-326ce5)](https://kubernetes.io)
[![GitHub license](https://img.shields.io/badge/license-MIT-blueviolet)](LICENSE.txt)

<img src="screenshot/logo.png" width="80" alt="kubedoor"/>

# 花折 - KubeDoor

花开堪折直须折🌻莫待无花空折枝

*Pluck the flower while it blooms 🌻 don't wait until only a bare branch is left*

**AI-driven intelligent management platform for multi-cluster Kubernetes**

[Website](https://cassinfra.github.io/KubeDoor/) · [Quick Start](#quick-start) · [Docs](#documentation) · [Community](#kubedoor-community-and-donations)

</div>

---
**If images fail to load for you in mainland China, try the Gitee mirror: <a target="_blank" href="https://gitee.com/starsl/KubeDoor">https://gitee.com/starsl/KubeDoor</a>**

## 🏷Contents
* [🌈Overview](#overview)
* [🎉What's New in 2.x](#whats-new-in-2x)
* [💠Architecture](#architecture)
* [💎Features](#features)
  * [🤖AI Assistant and MCP](#1-ai-assistant-and-mcp)
  * [📡Multi-Cluster Monitoring](#2-multi-cluster-monitoring)
  * [🎛K8S Resource Management](#3-k8s-resource-management)
  * [🧬Alerts, Events and Silences](#4-alerts-events-and-silences)
  * [🚧Peak-Hour Resources and Admission Control](#5-peak-hour-resources-and-admission-control)
  * [☕JVM Control and Diagnostics](#6-jvm-control-and-diagnostics)
  * [⚖Node Balancing and Elastic Scheduling](#7-node-balancing-and-elastic-scheduling)
  * [🌐Istio Route Management (Trial)](#8-istio-route-management-trial)
  * [🔐Platform and Permissions](#9-platform-and-permissions)
* [🚀Quick Start](#quick-start)
* [♻Upgrade Notes](#upgrade-notes)
* [📚Documentation](#documentation)
* [🔔Community and Donations](#kubedoor-community-and-donations)
* [🙇Contributors](#contributors)
* [🥰Acknowledgements](#acknowledgements)

---

## 🌈Overview

🌼**花折 - KubeDoor** is an **AI-driven intelligent management platform for multi-cluster Kubernetes**, built with Python + Vue. Install one KubeDoor control plane plus one agent in each workload cluster, and a single Web UI covers multi-cluster monitoring, resource management, alerts and events, peak-hour resource governance, JVM control and node balancing. It also has a built-in **AI ops assistant with approvals and an audit trail**, which is exposed to external clients via **standard MCP**.

KubeDoor focuses on microservice resources as seen during the daily peak hours: it collects the real resource usage of every microservice during the peak, and uses K8S admission control to keep resource requests in line with that real usage. Building on this, 2.x makes the AI assistant a core capability, consolidates storage on PostgreSQL and reduces deployment to a single script.

- 🤖**AI ops assistant**: troubleshoot, query and make changes in natural language. It automatically calls 63 KubeDoor business operations as well as generic tools such as the K8S API, kubectl and istioctl; queries run automatically, while changes first show their parameters and diff and run only after a human approves them.
- 🌐**Unified multi-cluster management**: agents connect out to the control plane, so workload clusters need no inbound ports; the resources, metrics, events and alerts of all clusters live in one platform.
- 🎯**Peak-hour resource governance**: collect resource data for the business peak hours, then use admission control to enforce replica counts, requests, limits and JVM startup flags in one place.
- 🔔**Closed-loop alerting**: metric alerts, K8S event alerts and alert silences, with notifications sent to WeCom, DingTalk, Feishu and Slack.
- 🚀**Simple deployment**: one script + one config file; PostgreSQL is the only database.

<div align="center">

<img alt="K8S Resource Overview" src="screenshot/k8s-overview.png" />

K8S Resource Overview: multi-cluster resources, at-risk nodes, and today's abnormal alerts and events, all on one screen

</div>

---

## 🎉What's New in 2.x

**Compared with 1.7, 2.x is an architecture-level upgrade: the AI assistant becomes a core capability, PostgreSQL becomes the only database, and deployment becomes a single script.** The current version is 2.1.0; [2.0]/[2.1] below mark the version in which a capability first became available.

**🤖AI**
- [2.1] **Built-in AI assistant** (kubedoor-ai, always installed with the control plane): bring your own model, select a cluster, then troubleshoot and operate it in natural language; changes run only after approval; global memory and direct kubeconfig connections are supported.
- [2.1] **New MCP**: merged into kubedoor-ai and served through the Web entry point as `/mcp` (Streamable HTTP) and `/sse`; it reuses the Web accounts and their read/write permissions, write operations must be approved in the Web UI, and the 16 tool names from 1.x remain compatible.

**🧱Architecture & Deployment**
- [2.0] **PostgreSQL as the only database**: ClickHouse and MySQL are removed; master and kubedoor-ai create their tables automatically at startup, and no extensions are required.
- [2.0] **Script-based deployment**: `deploy/install.sh` + `kubedoor.conf` replace Helm, with render-only preview, per-component toggles and private image registries, plus automatic discovery of an existing kube-state-metrics in the cluster.
- [2.0] **Automatic data cleanup**: K8S events are kept for 90 days, alert records and peak snapshots for 365 days; both are adjustable.
- [2.0] **Parameterized data API**: the frontend now uses `/api/db/*` and no longer sends raw SQL.

**🔔Alerting**
- [2.0] **Alert silences**: Alertmanager-style matchers, match preview and automatic expiry; silenced alerts are still stored, only their notifications are blocked.

**🎛Resources & Operations**
- [2.1] **Live Deployment status**: ready/desired replicas, rollout status and Pod details pushed in real time over WebSocket.
- [2.1] **JVM startup flag control**: collects and enforces `Xms`/`Xmx`/`Xss`/`MaxMetaspaceSize`, and adds the daily-peak P95 heap usage and G1 Eden usage.
- [2.0] **Node balancing**: generates a migration plan from node CPU load and, after manual confirmation, migrates Pods in batches using isolation.
- [2.0] **CCI elastic scaling**: during a temporary scale-out on Huawei Cloud CCE, Pods beyond the local limit burst to CCI.
- [2.0] **Log improvements**: Deployment-level aggregated logs, container switching and full-log gzip download; the Pod page adds node filtering and namespace multi-select.
- [2.0] **Faster admission**: admission lookups now use a single SQL query + a 60-second cache, and changes to switches or the control table take effect immediately.

---

## 💠Architecture

**One control plane, plus one agent in each workload cluster; agents connect outbound, and all data is stored in PostgreSQL (metrics live in the time-series database).**

<div align="center">
<img src="screenshot/kubedoor-arch.png" alt="KubeDoor 2.x architecture overview" />
</div>

- **Control plane** (`./install.sh master`): kubedoor-web (nginx single entry point + Vue 3 console), kubedoor-master (API gateway, agent connection hub), kubedoor-ai (AI assistant + MCP Server), kubedoor-alarm (alert silencing, storage and IM notifications), kubedoor-collect (daily peak-hour collection CronJob), plus optional VictoriaMetrics, vmalert, Alertmanager and Grafana.
- **Cluster side** (`./install.sh agent`): kubedoor-agent, plus optional vmagent, kube-state-metrics and node-exporter. The agent **initiates** the WebSocket connection to the control plane, so workload clusters need no inbound ports; in the same cluster it connects directly to the master Service, while across clusters it goes through the Web entry point and authenticates with a read-write account.
- **PostgreSQL 18 is the only database**: deployed standalone outside K8S with docker compose, or use an existing PostgreSQL (no extensions needed). It stores control settings, peak-hour resource data, K8S events, alert records, silence rules, Istio routes, and AI sessions, approvals and memories.
- **Optional monitoring stack**: use the bundled single-node VictoriaMetrics, or connect an existing Prometheus/VictoriaMetrics; each cluster's metrics are distinguished by the label `<PROM_K8S_TAG_KEY>=<K8S_NAME>`.
- **Single entry point**: the Web UI, Grafana, the AI assistant and MCP share one kubedoor-web NodePort; login uses nginx basic auth, and accounts are either read-write or read-only.
- **AI always installed, MCP on by default**: kubedoor-ai is installed with the control plane; the external MCP endpoints (`/mcp`, `/sse`) can be turned off with `ENABLE_MCP="false"`, which does not affect the AI assistant in the Web UI.

---

## 💎Features

**Covers the full chain of day-to-day K8S operations; most of the queries and operations below can also be handed straight to the AI assistant.**

> UI labels are quoted in Chinese exactly as they appear in the Web UI (「…」), with an English gloss on first mention. The UI is Chinese-first and its English locale is only partial; where a menu entry is named differently in the English locale, that name is given as *English UI: "…"*.

### 1. 🤖AI Assistant and MCP

**Click 「AI 助手」 (AI Assistant) in the middle of the top bar, select a cluster in 「KubeDoor 助手」 (KubeDoor Assistant) and describe the problem in one sentence: queries run automatically, changes are approved before they run, and every step is traceable.** The same tools, permissions and approvals are also exposed via standard MCP to external clients such as Cursor and Claude Code.

- 🧠**Bring your own model**: enter the Base URL, API Key and model name (model ID) in 「模型设置」 (Model Settings); 「测试连接」 (Test Connection) runs a real tool-calling test. Any OpenAI-compatible Chat Completions API works (including self-hosted models without authentication; the model must support tool calling), with dedicated support for DeepSeek thinking mode. The model configuration is stored only in your own browser.
- 🎯**Resource context**: first choose a 「K8S 集群」 (K8S cluster) — each turn only operates on the selected cluster — then optionally narrow it down to a namespace, Deployment or Pod; each cluster is tagged 「Agent 在线」 (agent online), 「已配置直连」 (direct connection configured) or 「仅指标 / 历史」 (metrics / history only).
- 🧰**One governed tool**: every cluster operation goes through a single tool, `kubedoor_tool`, which covers **63 KubeDoor business operations** (37 read-only, 26 write: logs, events, resources and JVM, nodes, scaling, restarts, image changes, scheduled/periodic tasks, node balancing, control settings, etc.) and **5 kinds of generic executors** (K8S API/CRD, kubectl, istioctl, diagnostic commands, Pod exec); existing KubeDoor APIs take precedence.
- 🔌**Multiple connection modes**: existing KubeDoor APIs, the agent's in-cluster identity (no kubeconfig needed), or a direct connection with a kubeconfig uploaded in the 「AI Kubeconfig」 column of 「Agent管理」 (Agent Management; English UI: "Workbench"), stored encrypted with AES-256-GCM; historical and monitoring data can still be queried while the agent is offline.
- ⏰**Immediate / scheduled / periodic operations**: Deployment restarts and scaling can run immediately, once at a specified time, or periodically on a Cron schedule.
- 📈**Free-form metric queries**: when the time-series database is VictoriaMetrics, the AI can write MetricsQL directly; the server forces every query onto the current cluster and limits the time range to 30 days.
- 📝**Global memory**: 「总结为记忆」 (Summarize as memory) distills a conversation into an editable draft; once a read-write account uses 「保存为全局记忆」 (Save as global memory), every logged-in user can view and reference it. When asking a question, 「引入记忆（N）」 (Include memories (N)) brings memories in as needed, up to 10 per turn.
- 🧩**Powered by DeepAgents**: a built-in 「KubeDoor K8S 运维」 (KubeDoor K8S Ops) Skill, sub-agents that investigate in parallel, automatic context compression and checkpoints persisted in PostgreSQL; sessions are stored on the server (visible only to you), with streaming replies and resume after disconnects, and tasks keep running after you close the dialog.

#### 🛡Safety Guardrails

| Guardrail | Details |
| --- | --- |
| Approval | Read-only queries run automatically. Changes, unknown CLI commands, curl/openssl and Pod exec first show 「准确参数」 (exact parameters), 「执行命令」 (command), 「变更预览」 (change preview) and 「配置差异」 (config diff), and run only after you click 「批准这次操作」 (approve this operation); a pending approval expires after 30 minutes. Read-write accounts can tick 「自动批准执行」 (auto-approve), which applies to the current page only |
| Frozen parameters | The complete parameters are written to the database at the preparation stage and executed exactly as-is after approval, so the model cannot swap them; replacing the kubeconfig invalidates earlier approvals |
| Dry-run | Generic K8S API writes first run with `dryRun=All` to produce a config diff, and carry resourceVersion/UID preconditions; `kubectl apply` first runs `--dry-run=server`; KubeDoor business operations show the exact parameters and a change preview |
| No blind replays | A write that times out or loses its connection is marked 「执行结果不确定」 (result uncertain), and no further writes are made in that turn; a service restart never re-sends an operation that was already dispatched, while pending operations can still be approved |
| Redaction | The API Key is only held in memory for a single run and is never written to the database or logs; streamed output, checkpoints, logs and memories are all redacted, and Secret contents are hidden automatically |
| Read-only account limits | Read-only accounts can chat and query, but cannot approve changes, enable auto-approval, upload kubeconfigs or maintain global memory |
| Audit | Sessions, runs, every tool call (with full parameters, preview and result) and events are written to PostgreSQL; external MCP calls also create sessions that can be reviewed in the Web UI |

> Note: through the AI, read-only accounts can run a wider range of read-only queries (e.g. K8S API GET, viewing logs and ConfigMaps) than the classic pages allow, and Secret redaction is best-effort, so hand out accounts accordingly; in production, put HTTPS in front of the Web UI.

#### 🔗Connecting External MCP Clients

- **URL**: `http(s)://<KubeDoor address>/mcp` (Streamable HTTP, recommended); clients that only support SSE use `/sse`. `<KubeDoor address>` is the Web entry point, e.g. `<node IP>:<Web NodePort>`.
- **Authentication**: reuses the Web basic auth accounts via the request header `Authorization: Basic <base64(username:password)>`; read-only accounts can only call read-only tools.
- **Tools**: the 16 tool names from 1.x are kept (e.g. `get_k8s_list`, `get_pods_logs`, `scale_deployment`, `get_pod_jvm_dump`) + the unified tool `kubedoor_tool` + the Skill resource `skills://kubedoor-k8s`.
- **Changes never run directly**: write operations, and JVM diagnostics that have to run inside a Pod, return `pending` and a `browserlink` (relative path); log in to the Web UI with the **same account** and open it to approve. Results are not pushed back to the MCP client, so query again to verify.
- **Turning off the external endpoints**: set `ENABLE_MCP="false"` in `kubedoor.conf`.

Example (Cursor's `mcp.json` format, derived from the interface; for other clients, check their documentation for the field names):

```json
{
  "mcpServers": {
    "kubedoor": {
      "url": "http://<KubeDoor address>/mcp",
      "headers": {
        "Authorization": "Basic <base64(username:password)>"
      }
    }
  }
}
```

Generate `<base64(username:password)>` with `printf '%s' 'username:password' | base64`; a dedicated account for MCP is recommended.

<details close>
<summary>💬Click to expand a sample conversation (illustrative: the cluster, service names and data are all fictional)</summary>

```text
Context: K8S cluster prod-a · namespace demo · Deployment deploy-demo-order

You: This service has been restarting a lot lately. Use the logs, events and metrics to find out why.

AI assistant:
  Tool calls 5 (read-only queries, run automatically)
    ✓ pods                 Pod list and restart counts
    ✓ previous_logs        logs of the last exited container
    ✓ events               related K8S events
    ✓ resource_inventory   limits and JVM flags in the control table
    ✓ metrics              memory curve for the last 6 hours
  Conclusion: container memory stays close to its limit; the last exit reason was OOMKilled.
  Suggestion: raise the memory limit to 1024MB and do a rolling restart off-peak. Shall I submit the change?

You: Go ahead: set the memory limit to 1024MB and restart once tonight at 23:30.

AI assistant:
  1 pending approval  resource_config_update
    Exact parameters: limit_mem_mb=1024 (other fields keep current values)   [Reject] [Approve this operation]
  … after approval …
  1 pending approval  schedule_deployment_restart
    Exact parameters: run_at=<today>T23:30                                   [Reject] [Approve this operation]
  … after approval …
  Control settings saved (effective on the next rollout or restart); the one-off restart task is registered and will run on time.
```

The free-form `metrics` query requires VictoriaMetrics as the time-series database.

</details>

📖Details: [AI assistant guide](docs/ai-assistant.md) (Chinese)

---

### 2. 📡Multi-Cluster Monitoring

**Each cluster's vmagent remote-writes its metrics to the same time-series database, and a single label tells the clusters apart, so you see every cluster in one Web UI.**

- 🖥「K8S资源总览」 (K8s Overview) dashboard: switch between 「全部集群」 (all clusters) and a single cluster; it shows the numbers of clusters, nodes, workloads, Pods and persistent volumes, badges for 4 kinds of at-risk nodes and the 6 highest usage/allocation rates, plus today's Top 10 abnormal Pod alerts, the Top 10 abnormal K8S events and the alert trend of the last 10 days, refreshing automatically every 30 seconds.
- 🌊Unified time-series database: every cluster's metrics carry the label `<PROM_K8S_TAG_KEY>=<K8S_NAME>`; use the bundled single-node VictoriaMetrics, or connect an existing Prometheus/VictoriaMetrics (all clusters must be aggregated into one queryable database).
- 🔌Works with existing monitoring: if the cluster already runs kube-state-metrics and node-exporter, the bundled ones can be turned off, and vmagent automatically discovers the existing kube-state-metrics anywhere in the cluster.
- 📊Grafana dashboards: 「K8S监控看板📊」 (K8S Monitoring Dashboard; English UI: "Resource K8S"), 「节点监控看板📊」 (Node Monitoring Dashboard; English UI: "Resource Node") and 「高峰资源看板📊」 (Peak Resource Dashboard; English UI: "Resource Statistics") are embedded in the Web UI and imported automatically at installation (Grafana is optional; the peak resource dashboard reads its data from PostgreSQL).
- 📈Deployment resource metrics: the Deployment page (English UI: "Realtime Resource") shows CPU and memory usage alongside requests and limits for each service (from the time-series database).

<details close>
<summary>🔍Click to expand screenshots ...</summary>

|<img src="screenshot/1.0/2.jpg" alt="K8S monitoring dashboard" />|
|-|

</details>

---

### 3. 🎛K8S Resource Management

**Manage the Deployments, Pods, Services, Ingresses, ConfigMaps, StatefulSets, DaemonSets and nodes of all clusters in one console, with common operations a single click away.**

- 🗂Unified resource pages: view every resource type, edit its YAML in the Monaco editor (Create / Apply / Replace) and delete it; the selected cluster and namespace are remembered across pages.
- ⚡Live Deployment status [2.1]: WebSocket pushes the ready/desired replica counts in the Pod column and the 「更新中」 (updating), 「已暂停」 (paused) and 「超时」 (timed out) states; the query bar shows 「实时」 (live), 「连接中」 (connecting), 「agent 离线」 (agent offline) or 「实时不可用」 (live unavailable), and expanded details refresh in real time as Pods are added or removed.
- 📜Log viewer: live follow, keyword search / filter / step through matches, color highlighting for ERROR/WARN/INFO, preserved ANSI colors, and 「重启前日志」 (logs before restart); [2.0] 「日志」 (Logs) on a Deployment row aggregates all of its Pods (up to 60) with container switching; with a single Pod selected you can 「下载日志」 (download logs: the full log, gzipped).
- 🔁Scaling and restarts: 「立即执行」 (run now), 「定时执行」 (run once at a set time) and 「周期执行」 (run periodically), with optional 「临时扩容」 (temporary scale-out) and 「调度到指定节点」 (schedule to specified nodes); scheduled/periodic tasks are created by the agent as CronJobs in the `kubedoor` namespace of the workload cluster, and times in the Web UI are entered in Beijing time (UTC+8).
- 🧪Pod isolation: 「隔离」 (Isolate) changes the Pod's `app` label to `<original value>-ALERT`, taking it out of Service traffic while keeping it for investigation, optionally together with 「临时扩容1个Pod」 (temporarily add 1 Pod); deletion and bulk deletion are also supported.
- 🏷Image updates: the 「更新镜像」 (Update image) dialog automatically lists the 20 most recent tags in the image registry (Alibaba Cloud ACR, Huawei Cloud SWR, Harbor; registry credentials must be configured in `REGISTRY_SECRET_JSON`), or you can enter a tag manually; read-only accounts can be authorized by cluster, time window and account whitelist, and update progress is pushed to IM.
- 🔍Pod page: [2.0] filter by node and select multiple namespaces; jump straight to the Pod's 「事件」 (events) and 「告警」 (alerts).

<details close>
<summary>🔍Click to expand screenshots ...</summary>

|<img width="600" src="screenshot/update-image.png" alt="Update image" />|
|-|

</details>

📖Details: [K8S microservice image update configuration](help/K8S微服务镜像更新配置说明.md) (Chinese)

---

### 4. 🧬Alerts, Events and Silences

**Metric alerts, K8S event alerts and operation notifications are all pushed to WeCom, DingTalk, Feishu or Slack; silences only block notifications and never drop data.**

- 🚨Metric alerts: 44 built-in vmalert alerting rules (Pod status and resources, Deployments, nodes, JVM, etc.) and 2 recording rules; alerts go through Alertmanager to kubedoor-alarm, which pushes them to IM (Pod alerts whose name starts with `K8S_Pod` are also stored in PostgreSQL), and firing/resolved notifications show how long the alert lasted.
- 📋Alert analysis: 「告警面板」 (Alarm Statistics) aggregates alerts by name with daily totals; 「K8S告警详情」 (Alarm Detail) offers multi-criteria filtering, handled/unhandled marking and auto-refresh, and lets you isolate or delete a Pod or run Dump, Jstack, JFR or JVM on it directly, or 「屏蔽」 (silence) the alert in one click.
- 🔕Alert silences [2.0]: Alertmanager-style matchers (`=`, `!=`, `=~`, `!~`; regexes must match in full), quick durations and 「命中预览」 (match preview); the states are 生效中/待生效/已过期/已解除 (active/pending/expired/revoked), and rules can be revoked, extended, re-enabled and cloned; the 【屏蔽】 (Silence) link in IM notifications opens the silence page with the matchers pre-filled (requires `KUBEDOOR_EXTURL`). Silenced alerts are still stored and only their notifications are blocked; silences do not apply to K8S event alerts.
- 📺K8S event center: each cluster's agent collects all K8S events into PostgreSQL (one row per event); search them in 「K8S事件详情」 (K8S Event Details) by cluster, namespace, Kind, name, reason, message and source, and events that matched a rule are marked 「已告警」 (alerted).
- 🧾Event rule alerts: ignore rules and alert rules are configured in JSON (conditions are case-insensitive), with built-in rules for critical events, restarts, failed health checks, mount failures, scheduling failures, resource pressure, zombie processes and more; the same event is not notified again within `ALERT_DEDUP_WINDOW` (default 300 seconds). Rules come only from the ConfigMap `kubedoor-master-file-cfg` (source file `deploy/manifests/master/alert_rules.json`); after changing them, run `kubectl -n kubedoor rollout restart deploy/kubedoor-master`.
- 📮Operation notifications: isolation, deletion, JVM diagnostics, scaling, restarts, image update progress and admission results are all pushed to IM by the agent, and each cluster can use a different bot; external systems can also write alert records via `POST /api/custom_alert`.

<details close>
<summary>🔍Click to expand screenshots ...</summary>

|<img src="screenshot/1.0/4.jpg" alt="Alert dashboard" />|<img src="screenshot/alert1.png" alt="IM alert notification" />|
| ------------------------------------| ----------------------------------- |

</details>

📖Details: [Alert silences](docs/alert-silence.md) · [K8S event alert rule configuration](help/K8S事件告警规则配置说明.md) (both in Chinese)

---

### 5. 🚧Peak-Hour Resources and Admission Control

**Collect each microservice's real resource usage during the daily business peak, then use K8S admission control to keep its requests in line with that real peak usage: more accurate scheduling, less resource fragmentation.**

- 📊Peak-hour collection: in 「Agent管理」, turn on 「自动采集」 (auto collection) and set 「高峰时段」 (peak hours, default `10:00:00-11:30:00`); every day at 01:00 Beijing time (UTC+8) the previous day's peak data is collected. You can also click 「采集」 (Collect) to backfill historical data (1–90 days); collecting again never writes duplicates.
- 📐How it is measured: CPU and memory use the **P80** of the peak hours (columns 「P80PodCPU%」 and 「P80Pod内存%」 (P80 Pod memory %)), heap and G1 Eden usage use **P95**; the control table takes the data of **the day in the last 10 days on which the whole cluster's total CPU consumption was highest**, and uses its CPU/memory usage as the requests.
- 🗂「高峰资源管控」 (Peak Resource Control; English UI: "Resource Management"): one view of current-day and specified Pod counts, usage, requests, limits and JVM flags, with bulk scaling, bulk restarts, 「配置」 (Settings: save & scale / save & restart / save) and 「新增资源」 (Add Resource); 「每日高峰资源」 (Daily Peak Resources; English UI: "Resource Collection") shows the raw data collected each day, and 「高峰资源看板📊」 shows long-term trends and TOP 10 lists.
- 🚧Admission control: after you turn on 「准入控制」 (admission control) in 「Agent管理」 and select the namespaces to control, the agent creates a MutatingWebhook automatically. When a Deployment is created or rolled out, its replica count (「指定Pod」 (specified Pods) first, or 「当日Pod」 (current-day Pods) if not specified), the first container's requests/limits and the JVM flags are rewritten to the controlled values; when scaling via the scale subresource, only the replica count is rewritten.
- 🆕New services: for a service that is not in the control table, the deployment is rejected with a notification while 「新服务免确认」 (skip confirmation for new services) is off, and allowed through without control while it is on; the daily collection also adds newly appeared services to the control table automatically and sends a notification.
- ⏱Temporary scale-out exemption: for 5 minutes after a 「临时扩容」, replica changes to that Deployment are allowed through as-is.
- ⚡Faster admission [2.0]: the master uses a single SQL query (5-second timeout) cached for 60 seconds; changing a switch or the control table, or finishing a collection, takes effect immediately.

> ⚠️ Admission control is off by default and **fail-closed** once enabled: while the agent or master is unavailable, Deployment creation, rollouts and scaling are rejected in every namespace not labeled `kubedoor-ignore`; in an emergency, run `kubectl delete mutatingwebhookconfiguration kubedoor-admis-configuration`.
>
> 💡 When using 「新增资源」 (Add Resource), 「指定Pod」 (Specified Pods) is required: 0 means the service is published without starting any Pods, and -1 is not allowed. -1 (follow 「当日Pod」) only applies to services that already have collected peak-hour data.

<details close>
<summary>🔍Click to expand screenshots ...</summary>

Peak resource dashboard (Grafana): global peak-hour resource statistics with a TOP 10 for each resource, plus peak resource analysis and resource curves at namespace / microservice / Pod level.

|<img src="screenshot/kd1.jpg" alt="Peak resource dashboard - global statistics" />|<img src="screenshot/kd2.jpg" alt="Peak resource dashboard - microservice analysis" />|
|-|-|
|<img src="screenshot/kd3.jpg" alt="Peak resource dashboard - microservice resource curves" />|<img src="screenshot/kd4.jpg" alt="Peak resource dashboard - Pod resource curves" />|

</details>

📖Details: [K8S resource control guide](help/K8S资源管控功能说明.md) (Chinese)

---

### 6. ☕JVM Control and Diagnostics

**Startup flags, peak-hour heap usage and one-click diagnostics for Java microservices, all handled inside KubeDoor.**

- 🩺One-click diagnostics: run 「Dump」, 「Jstack」, 「JFR」 or 「JVM」 on a Pod from the Deployment, Pod and 「K8S告警详情」 pages; results are shown in a dialog and pushed to IM, and the Dump, Jstack and JFR files are uploaded to the object storage specified by `OSS_URL`.
- 🧩JVM startup flag control [2.1]: automatically collects `-Xms`, `-Xmx`, `-Xss` and `-XX:MaxMetaspaceSize` from the `args` of each Deployment's first container (where `args[0]` is `java`); view them in 「高峰资源管控」 and change them under 「配置」 (Xss in k, the others in m); on the next rollout or restart, admission control replaces them with the controlled values (admission control must be enabled).
- 📈Heap and G1 Eden [2.1]: the daily peak-hour P95 heap usage 「P95podHeap%」 and G1 Eden usage 「P95podG1E%」, for display only, to help tune the JVM flags.
- 🚨JVM alerts: 14 built-in JVM alerting rules (heap/non-heap memory, GC, threads, etc.); vmagent automatically scrapes Pods annotated with `prometheus.io/jvm: "true"`.

<details close>
<summary>🔍Click to expand screenshots ...</summary>

|<img width="800" src="screenshot/1.0/8.png" alt="JVM diagnostic result" />|
|-|

</details>

📖Details: [JVM flag control](docs/jvm-resource-control.md) (Chinese)

---

### 7. ⚖Node Balancing and Elastic Scheduling

**Generate a migration plan from live node CPU load and migrate in batches after manual confirmation; when scaling out, Pods can also be scheduled to specified nodes or to Huawei Cloud CCI.**

- ⚖「节点均衡」 (Node Balancing) [2.0]: 「分析负载」 (analyze load) iteratively builds a 「迁移计划」 (migration plan) from the range and standard deviation of node CPU usage; after confirming, use 「执行选中」 (execute selected) or 「执行全部」 (execute all). Migration is isolation-based and runs in batches, and the next batch starts only after the new Pods are ready; the old Pods stay under 「隔离Pod」 (isolated Pods) and are cleaned up once you have verified everything is fine.
- ⚙「负载均衡配置」 (load balancing settings): adjust the imbalance threshold, balancing target, minimum replicas, blacklist, excluded namespaces/nodes, source/target node selection strategies and more. Manual execution only, and it requires metrics-server; other nodes are temporarily cordoned during execution, so run it off-peak.
- ☁CCI elastic scaling [2.0]: on Huawei Cloud CCE clusters, tick 「CCI扩容」 (CCI scale-out) during a 「临时扩容」 and set 「本地Pod数」 (local Pod count); Pods beyond that number burst to CCI.
- 🎯Scheduling to specified nodes: when scaling, restarting or isolating, rank nodes by 「当前CPU」 (current CPU), 「当前内存」 (current memory), 「峰值CPU」 (peak CPU), 「峰值内存」 (peak memory) or 「Pod数」 (Pod count) and pick the target nodes.
- 💻「节点管理」 (Node Management): view node CPU, memory, disk and Pod usage against allocatable capacity, plus node status, with bulk cordon/uncordon (usage data requires metrics-server).

📖Details: [Node balancing algorithm and parameters](src/kubedoor-agent/load_balance/README.md) (Chinese; developer doc — its timed-check and auto-execute options are not implemented yet, execution is manual only)

---

### 8. 🌐Istio Route Management (Trial)

**Maintain the Istio VirtualServices shared by multiple clusters as templates in one place and push them to several clusters with one click; the UI still labels this feature as an internal trial.**

- 🗺「全局VS管理」 (Global VS Management; English UI: "Level1 VirtualService"): VirtualServices and HTTP routes (priority, match/rewrite rules, route/delegate forwarding) are stored in PostgreSQL, can be associated with multiple clusters, and are pushed to each cluster with 「下发」 (Push).
- 📥「采集VirtualService」 (Collect VirtualService): imports the existing VirtualServices from a cluster on first use; before importing, it clears the VSs already associated with that cluster (together with those VSs' associations with other clusters), so only use it during initial setup.
- 🚸The 「委托VS管理」 (Delegate VS Management; English UI: "Level2 VirtualService") page is still under development; if you want to use Istio route management, contact the author.

<details close>
<summary>🔍Click to expand screenshots ...</summary>

|<img src="screenshot/istio-vs.png" alt="Global VS management" />|
|-|

</details>

---

### 9. 🔐Platform and Permissions

**One entry point, two permission levels: the Web UI, Grafana, the AI assistant and MCP share one NodePort, and each account is either read-write or read-only.**

- 🚪Single entry point: nginx in kubedoor-web handles basic auth login and reverse-proxies the master, kubedoor-ai and Grafana (`/grafana/`).
- 👥Read-write and read-only: accounts listed in `WEB_RW_USERS` are read-write, all others are read-only; read-only accounts get a 403 when calling any API outside the query whitelist (the embedded Grafana runs as anonymous Admin and is not subject to this restriction); image updates can be opened to read-only accounts by cluster, time window and account via `UPDATE_IMAGE_JSON`.
- 🛰Multi-cluster onboarding: an agent registers itself on its first connection; 「Agent管理」 shows online status, heartbeat and version, 「更新」 (Update) upgrades the agent image with one click, and collection, admission control and 「AI Kubeconfig」 are all configured there.
- 📝Audit trail: nginx writes access logs in JSON format (including the logged-in user and the request body); operations are pushed to IM by the agent; AI sessions, approvals and tool calls are written to PostgreSQL.
- 🧹Automatic data cleanup [2.0]: K8S events are kept for 90 days, alert records and peak snapshots for 365 days; the master purges expired data in batches once a day, adjustable in the `kubedoor-config` ConfigMap (`0` means never purge).
- 🌗UI: Simplified Chinese / English (partially translated), light/dark/auto themes, mobile-friendly.

📖Details: [Deployment guide](deploy/README.md) (Chinese)

---

## 🚀Quick Start

**One config file + one bash script, three steps: start PostgreSQL → install the control plane → install an agent in each cluster. Helm is no longer needed.** See the [deployment guide](deploy/README.md) (Chinese) for full details.

#### Prerequisites

| Item | Requirement |
| --- | --- |
| Installer machine | Can reach the target cluster with `kubectl`; bash 4 or later |
| Kubernetes | ≥ 1.21; running daily collection and scheduled/periodic tasks in Beijing time (`timeZone: Asia/Shanghai`) requires ≥ 1.25 (older clusters run them in the time zone of kube-controller-manager) |
| metrics-server | Required: Pod/node CPU and memory usage and node balancing all depend on it |
| Time-series database | The bundled VictoriaMetrics (needs a working StorageClass; 100Gi PVC by default), or an existing Prometheus/VictoriaMetrics. The Deployment list, the K8S resource overview and peak-hour collection all depend on it; without monitoring data these pages are empty and nothing can be collected |
| Database | A machine with Docker Compose to run PostgreSQL 18 (K8S nodes must be able to reach its port 5432), or an existing PostgreSQL (14+ recommended; no extensions or superuser required) |
| CPU architecture | Official images are amd64 only |
| Network | Across clusters, the agent must be able to reach the control plane's Web NodePort (WebSocket), and vmagent must be able to reach the NodePort of the control plane's time-series database (remote write) |

#### Three-Step Install

```bash
git clone https://github.com/CassInfra/KubeDoor.git
cd KubeDoor
```

**① Start the database** (on a machine the K8S nodes can reach, starting from the repository root)

```bash
cd deploy/postgres
cp .env.example .env
vi .env                 # change at least PG_PASSWORD
docker compose up -d
docker compose logs -f postgres    # ready once you see "database system is ready"
```

The default image is amd64 only; outside mainland China or on ARM machines, set `PG_IMAGE=postgres:18-alpine` in `.env`. There is no need to create tables by hand: master and kubedoor-ai create them automatically at startup.

**② Install the control plane** (on a machine that can reach the cluster with kubectl, starting from the repository root)

```bash
cd deploy
cp kubedoor.conf.example kubedoor.conf   # copy the config template
vi kubedoor.conf        # set PG_HOST / PG_PASSWORD / MSG_TOKEN, etc.
./install.sh master
```

- Set `PG_HOST` to an address that Pods in K8S can reach (usually the private IP of the database host); `127.0.0.1` will not work.
- If you already have Prometheus/VictoriaMetrics, set `ENABLE_VICTORIA_METRICS="false"` and fill in `PROM_URL` and `PROM_TYPE`.
- To review the generated YAML first: `./install.sh render master -o /tmp/out`.

**③ Install the cluster side** (once in each workload cluster; with a single cluster, run it once more in the same cluster)

```bash
vi kubedoor.conf        # set K8S_NAME and MASTER_WS (on another machine, cp kubedoor.conf.example kubedoor.conf first)
./install.sh agent
```

- `K8S_NAME`: the unique identifier of the cluster; only letters, digits, `-` and `_` are allowed.
- `MASTER_WS`: in the same cluster use `ws://kubedoor-master.kubedoor`; across clusters use `ws://<read-write account>:<password>@<control plane node IP>:<Web NodePort>`, and also change `REMOTE_WRITE_URL` to the external write URL of the control plane's time-series database (printed by the script when you install the control plane).
- With multiple clusters, keep a separate config file for each: `./install.sh agent -c ./cluster-a.conf`.
- After a few dozen seconds, the cluster shows as 「在线」 (online) in 「Agent管理」.

#### Accessing the Web UI

- Open the `http://<node IP>:<NodePort>` printed by the install script in your browser; the default account is `kubedoor` / `kubedoor` (read-write).
- ⚠️ Change the default passwords as soon as possible (ideally before the installation in step ②): `WEB_AUTH_USERS` (htpasswd format) comes with two preset read-write accounts, one of which is used by cross-cluster agents to connect; change both. To change them after installation:
  - Control plane: re-run `./install.sh master`, then run `kubectl -n kubedoor rollout restart deploy/kubedoor-web` so that the new accounts take effect;
  - Cross-cluster agents: update the credentials in `MASTER_WS` accordingly, re-run `./install.sh agent`, then run `kubectl -n kubedoor rollout restart deploy/kubedoor-agent`.

#### First Steps

1. Open 「Agent管理」 (Agent Management; English UI: "Workbench") and check that the cluster's status is 「在线」 (online).
2. Turn on 「自动采集」 (auto collection) and set 「高峰时段」 (peak hours) in the dialog (default `10:00:00-11:30:00`).
3. Click 「采集」 (Collect) in the 「采集历史数据」 (collect historical data) column and choose 「采集天数」 (days to collect; default 10, range 1–90) to write historical peak data into the control table; from then on, collection runs automatically every day at 01:00 Beijing time (UTC+8). Newly installed monitoring only has data to collect after one full peak window has passed; after that, you can also run `kubectl -n kubedoor create job --from=cronjob/kubedoor-collect collect-now` to collect immediately.
4. After checking that the data in 「高峰资源管控」 (Peak Resource Control) is correct, turn on 「准入控制」 (admission control) as needed (off by default).
5. Click 「AI 助手」 (AI Assistant) → 「模型设置」 (Model Settings) in the top bar, fill in the Base URL, API Key and model name, click 「测试连接」 (Test Connection), and start chatting.

#### Production Tips

- 🔒The Web UI and MCP use HTTP + basic auth by default; in production, put HTTPS in front of the Web UI (the Ingress/LB must support WebSocket).
- 🚧Admission control is fail-closed: Deployment changes are rejected while the agent or master is unavailable; in an emergency, run `kubectl delete mutatingwebhookconfiguration kubedoor-admis-configuration`. Before uninstalling or reinstalling an agent, turn off 「准入控制」 in 「Agent管理」 first.
- 💾Back up the PostgreSQL data together with `kubedoor.conf` and `kubedoor.conf.ai-secrets` (its contents match the Secret `kubedoor-ai-security`); changing the AI encryption key makes saved kubeconfigs impossible to decrypt.
- 📌Keep `NAMESPACE` at its default value `kubedoor`; admission control, scheduled tasks and other features depend on this namespace.

---

## ♻Upgrade Notes

**2.x switches to PostgreSQL and script-based deployment, so upgrading from 1.x requires a fresh installation; between 2.x versions, just change `TAG_*` and re-run the install script.**

#### Upgrading from 1.x (Helm)

- ClickHouse/MySQL data from 1.x is not migrated; 2.x is designed as a fresh installation. The old Helm chart injects `CK_*` variables, so do not mix it with the new manifests.
- Recommended order:
  1. If admission control was ever enabled, first turn off 「准入控制」 in 「Agent管理」 of the old Web UI;
  2. Uninstall the old control plane and agents with `helm uninstall` (e.g. `helm uninstall kubedoor -n kubedoor` and `helm uninstall kubedoor-agent -n kubedoor`; use the release names of your actual installation);
  3. Do a fresh installation following [Quick Start](#quick-start); an existing Prometheus/VictoriaMetrics can still be used (`ENABLE_VICTORIA_METRICS="false"` + `PROM_URL`, `PROM_TYPE`).
- The 1.x MCP address (`/sse` on the NodePort of the standalone `kubedoor-mcp`) is deprecated; use `/mcp` on the Web entry point instead.

#### Upgrading from 2.0.x to 2.1

1. Update to the latest repository code; in `kubedoor.conf`, change `TAG_MASTER`, `TAG_AGENT` and `TAG_WEB` to `2.1.0` and add `TAG_AI="2.1.0"` (if omitted, it falls back to `latest`); the old `TAG_MCP` is no longer used and can be deleted.
2. The meaning of `ENABLE_MCP` has changed: it now only controls whether the Web entry point exposes kubedoor-ai's `/mcp`, `/sse` and `/messages`, and it defaults to `true`.
3. Run `./install.sh master`, then run `./install.sh agent` in each cluster (only the new agent supports the AI's generic tools).
4. The install script only runs `apply` and does not delete the standalone MCP service left over from 2.0.x; delete it manually:
   ```bash
   kubectl -n kubedoor delete deploy/kubedoor-mcp svc/kubedoor-mcp
   ```
5. Point external MCP clients to `/mcp` (`/sse` remains compatible); starting with 2.1, write operations from external MCP clients run only after they are approved in the Web UI.
6. The first 2.1 installation generates `kubedoor.conf.ai-secrets`; back it up together with the PostgreSQL data.

> Upgrades within 2.x: master and kubedoor-ai use the Recreate strategy, so the AI assistant and MCP are briefly unavailable during an upgrade, and in clusters with admission control enabled, Deployment changes are rejected while the master restarts; upgrade during off-peak hours. The nginx config is mounted via subPath, so if the Web image tag has not changed, run `kubectl -n kubedoor rollout restart deploy/kubedoor-web` for the new config to take effect.

---

## 📚Documentation

**Start with the deployment guide, then read the feature guides as needed.** All documents are in Chinese.

| Document | Contents |
| --- | --- |
| [Deployment guide](deploy/README.md) | Installing the control plane and the per-cluster agents with one script + one config file, standalone PostgreSQL, common scenarios, changing the configuration and troubleshooting |
| [AI assistant](docs/ai-assistant.md) | Model configuration, resource scope, multi-turn conversations, change approval, global memory, direct kubeconfig connections and external MCP |
| [Alert silences](docs/alert-silence.md) | Silence rule syntax, states, match preview and the IM 【屏蔽】 link |
| [JVM flag control](docs/jvm-resource-control.md) | Collecting and controlling Xms/Xmx/Xss/MaxMetaspaceSize; peak-hour heap and G1 Eden usage |
| [K8S resource control guide](help/K8S资源管控功能说明.md) | Controlling replica counts, requests and limits through admission control |
| [K8S event alert rule configuration](help/K8S事件告警规则配置说明.md) | Syntax of the event alert rule file, the default rules and how changes take effect |
| [K8S microservice image update configuration](help/K8S微服务镜像更新配置说明.md) | Authorizing image updates by cluster, time window and account, and configuring image registry credentials |
| [FAQ](help/FAQ.md) | Frequently asked questions and troubleshooting |

**Developer reference**
- [src/kubedoor-ai/README.md](src/kubedoor-ai/README.md): APIs, environment variables, build and testing of the AI service
- [src/kubedoor-tools/README.md](src/kubedoor-tools/README.md): the K8S execution library shared by the AI service and the agent, and its security boundaries
- [src/kubedoor-web/README.md](src/kubedoor-web/README.md): frontend development and build
- [src/kubedoor-agent/load_balance/README.md](src/kubedoor-agent/load_balance/README.md): node balancing algorithm and parameters

---

## 🔔KubeDoor Community and 🧧Donations

<div align="center">

#### If you like the project, please give it a ⭐️Star⭐️. If you have other ideas or needs, feel free to discuss them in the issues
<img width="600" alt="kubedoor-wechat" src="screenshot/wechat-qrcode.png" />

**Add the author on WeChat or follow the WeChat Official Account to join the community group**

</div>

## 🙇Contributors
<div align="center">
<table>
<tr>
    <td align="center">
        <a href="https://github.com/starsliao">
            <img src="https://avatars.githubusercontent.com/u/3349611?v=4" width="100;" alt="StarsL.cn"/>
            <br />
            <sub><b>StarsL.cn</b></sub>
        </a>
    </td>
    <td align="center">
        <a href="https://github.com/xiaofennie">
            <img src="https://avatars.githubusercontent.com/u/47970207?v=4" width="100;" alt="xiaofennie"/>
            <br />
            <sub><b>xiaofennie</b></sub>
        </a>
    </td>
    <td align="center">
        <a href="https://github.com/shidousanxia">
            <img src="https://avatars.githubusercontent.com/u/61586033?v=4" width="100;" alt="shidousanxia"/>
            <br />
            <sub><b>shidousanxia</b></sub>
        </a>
    </td>
    <td align="center">
        <a href="https://github.com/comqx">
            <img src="https://avatars.githubusercontent.com/u/30148386?v=4" width="100;" alt="comqx"/>
            <br />
            <sub><b>comqx</b></sub>
        </a>
    </td>
    <td align="center">
        <a href="https://github.com/seaworld008">
            <img src="https://avatars.githubusercontent.com/u/70318946?v=4" width="100;" alt="seaworld008"/>
            <br />
            <sub><b>seaworld008</b></sub>
        </a>
    </td>
  </tr>
</table>
</div>

## 🥰Acknowledgements

Thanks to the following excellent projects; without them, **KubeDoor** would not exist:
- [Python](https://www.python.org/) [AIOHTTP](https://github.com/aio-libs/aiohttp) [FastAPI](https://fastapi.tiangolo.com/) [Flask](https://flask.palletsprojects.com/) [VUE](https://cn.vuejs.org/) [Pure Admin](https://pure-admin.cn/) [Element Plus](https://element-plus.org) [ECharts](https://echarts.apache.org/) [Monaco Editor](https://microsoft.github.io/monaco-editor/) [Kubernetes](https://kubernetes.io/) [VictoriaMetrics](https://victoriametrics.com/) [Prometheus](https://prometheus.io/) [PostgreSQL](https://www.postgresql.org/) [Grafana](https://grafana.com/) [Nginx](https://nginx.org/) [LangChain](https://github.com/langchain-ai/langchain) [LangGraph](https://github.com/langchain-ai/langgraph) [DeepAgents](https://github.com/langchain-ai/deepagents) [FastMCP](https://gofastmcp.com/) ...

**Special thanks**
- [**CassTime**](https://www.casstime.com): **KubeDoor** would not have been possible without the support of 🦄**CassTime**.
