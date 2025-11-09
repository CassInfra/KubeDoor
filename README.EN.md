<div align="center">

English | [简体中文](README.md)

[![StarsL.cn](https://img.shields.io/badge/website-StarsL.cn-orange)](https://starsl.cn)
[![Commits](https://img.shields.io/github/commit-activity/m/CassInfra/KubeDoor?color=ffff00)](https://github.com/CassInfra/KubeDoor/commits/main)
[![open issues](http://isitmaintained.com/badge/open/CassInfra/KubeDoor.svg)](https://github.com/CassInfra/KubeDoor/issues)
[![Python](https://img.shields.io/badge/python-v3.11-3776ab)](https://nodejs.org)
[![Node.js](https://img.shields.io/badge/node.js-v22-229954)](https://nodejs.org)
[![GitHub license](https://img.shields.io/badge/license-MIT-blueviolet)](https://github.com/CassInfra/KubeDoor/blob/main/LICENSE)
[![OSCS Status](https://www.oscs1024.com/platform/badge/CassInfra/KubeDoor.svg?size=small)](https://www.murphysec.com/dr/Zoyt5g0huRavAtItj2)

<img src="https://github.com/user-attachments/assets/3dc6a022-cacf-4b89-9e26-24909102552c" width="80;" alt="kubedoor"/>

# 花折 - KubeDoor

Seize the moment when flowers bloom🌻Don't wait until there are no flowers to pick

</div>

---
**For users in China experiencing image loading issues, please visit the Gitee mirror site: <a target="_blank" href="https://gitee.com/starsl/KubeDoor">https://gitee.com/starsl/KubeDoor</a>**


## 🏷Table of Contents
* [💠New Architecture](#new-architecture)
* [💎Feature Description](#feature-description)
  * [💥New Version Highlights](#0-new-version-highlights)
  * [📡Microservice Monitoring & Analysis: Multi-K8S Cluster Unified Monitoring Best Practices](#1-microservice-monitoring--analysis-multi-k8s-cluster-unified-monitoring-best-practices)
  * [🎛K8S Resource Maintenance Management: Enhanced Management Features](#2-k8s-resource-maintenance-management-enhanced-management-features)
  * [🧬Exception Alert Notification & Intelligent Aggregation](#3-exception-alert-notification--intelligent-aggregation)
  * [💠Peak Period P95 Resource Collection & Analysis](#4-peak-period-p95-resource-collection--analysis)
  * [🚧K8S Admission Control Based: Ensuring Strong Consistency Between Microservice Requirements and Peak Resources](#5-k8s-admission-control-based-ensuring-strong-consistency-between-microservice-requirements-and-peak-resources)
  * [✨Others](#6-others)
* [🚀Deployment Instructions](#kubedoor-new-architecture-new-deployment)
* [🔔KubeDoor Community](#kubedoor-community--sponsorship)
* [🙇Contributors](#contributors)
* [🥰Acknowledgments](#acknowledgments)

---

## 🌈Overview

🌼**花折 - KubeDoor** is a microservice resource management platform developed with Python + Vue, based on K8S admission control mechanisms. It supports unified remote storage, monitoring, alerting, and display for multiple K8S clusters. Focusing on the resource perspective of microservices during daily peak periods, it implements resource analysis, statistics, and strong governance for microservices, ensuring consistency between microservice resource request rates and actual usage rates.

## 💠New Architecture
<div align="center">

#### 🎉New K8S Event Monitoring, Pod Real-time Logs, K8S MCP Support! Unified K8S Management, Multi-K8S Monitoring, Alerting, and Display Best Practices🎉
![KubeDoor1.5.2](screenshot/KubeDoor1.3.3.png)
</div>

## 💎Feature Description
### 0. 💥New Version Highlights
##### **🎥KubeDoor K8S Event Collection, Analysis & Monitoring Alerts! Multi-K8S event collection and query analysis with flexible K8S event alert rule configuration.**
<details close>
<summary>🔍Click to expand ...</summary>
<a target="_blank" href="docs/K8S事件告警规则配置说明.md">【K8S Event Alert Rule Configuration Guide】</a>
 
|<img src="https://github.com/user-attachments/assets/916b77dd-5747-47f3-94cc-f8ef3027193a" />|<img src="https://github.com/user-attachments/assets/44b83c51-dee2-4d99-89d6-0230cd8e06e2" />|
| ------------------------------------| ----------------------------------- |
</details>

##### **📜KubeDoor WEB adds Pod real-time log tracking with automatic multi-color marking for various exception levels, supporting keyword search, positioning, and filtering. Supports native log color display.**
<details close>
<summary>🔍Click to expand ...</summary>

|<img width="2418" height="1278" alt="Image" src="https://github.com/user-attachments/assets/e563c36a-2c1c-4cee-9b35-21a20976856b" />|
| ------------------------------------|
</details>


##### **💽K8S Microservice Version Update Feature: Supports designated account and time period authorization operations, plus automatic tag retrieval from image repositories.**
<details close>
<summary>🔍Click to expand ...</summary>
<a target="_blank" href="docs/K8S微服务镜像更新配置说明.md">【K8S Microservice Image Update Configuration Guide】</a>

 
|<img width="600" src="https://github.com/user-attachments/assets/0c7d1891-3df1-4413-a7c2-1b2288c35a25" /> |
|-|
</details>

##### **💠KubeDoor supports management and synchronization of multi-K8S ISTIO VirtualService rules.**
<details close>
<summary>🔍Click to expand ...</summary>

**🚸Internal trial phase, using MySQL data source. Please contact the author for debugging assistance if needed.**
|<img width="2463" height="1310" alt="1" src="https://github.com/user-attachments/assets/695bc0d1-929f-4326-8ab6-7e8590319cf5" /> | <img width="893" height="897" alt="3" src="https://github.com/user-attachments/assets/0a74c56c-6a76-4c85-9d2e-e498aaa0b2aa" /> |
| ------------------------------------| ----------------------------------- |
| <img width="2460" height="1311" alt="2" src="https://github.com/user-attachments/assets/4e512717-c3d9-4131-824e-c02de89c59fe" />|<img width="1332" height="1182" alt="4" src="https://github.com/user-attachments/assets/2095e0bd-3077-48c6-bcef-25ee258495cc" /> |

</details>

##### **🧱KubeDoor MCP Preview Version is here! Connect to any MCP client for LLM conversational operations on all your K8S clusters and resource exception troubleshooting.**
<details close>
<summary>🔍Click to expand ...</summary>

- Use any MCP client, add MCP server, select SSE type, and enter the address: `http://{nodeIP}:{kubedoor-mcp-NodePort}/sse` to connect to KubeDoor MCP.
- Based on KubeDoor's multi-K8S management monitoring architecture and existing API interfaces, we can quickly generate numerous MCP tools for K8S operations and Grafana data analysis. Stay tuned!
- **Connected Tool List & Operation Demo**

  | <img src="https://github.com/user-attachments/assets/19f50de7-248d-429d-9c19-c3a6a2282716"/> | <img src="https://github.com/user-attachments/assets/26e03c8e-4038-4094-affe-1d4de85d4675"/> |
  | ------------------------------------| ----------------------------------- |
  
  >Due to the uncertainty of large language models, please try to use MCP clients with tool confirmation (Cline, Cursor).
  >
  >This is currently a preview version. MCP web client and tool call authentication are not yet implemented. Please do not expose the MCP server address to the public internet.
</details>

---

### 1. 📡Microservice Monitoring & Analysis: Multi-K8S Cluster Unified Monitoring Best Practices
<div align="center">
<img src="./screenshot/1.0/vm-arch.png" width="650;" />
</div>

  - 🌊Based on the VictoriaMetrics suite, implementing a one-stop K8S monitoring solution for **multi-K8S cluster** unified remote storage, monitoring, alerting, and display.
  - 🎨Integrated K8S node monitoring dashboard and K8S resource monitoring dashboard, both supporting viewing of various K8S cluster resources in a single dashboard.
  - 📐Built-in alert rules for K8S resources, JVM resources, and K8S nodes, supporting unified alert rule management, integration with various IM alerts, and flexible @ mechanisms.
<div align="center">
   
| <img width="550" src="https://github.com/user-attachments/assets/5a1ba8db-ac3d-4852-b913-000b78c5d0f5" />| <img width="700" src="./screenshot/1.0/2.jpg"/> |<img width="700" src="./screenshot/1.0/3.png"/> | 
| ------------------------------------| ----------------------------------- | ---------------------------------- |

</div>

---

### 2. 🎛K8S Resource Maintenance Management: Enhanced Management Features
- 🎭K8S workload real-time monitoring management page: Real-time viewing of microservice status and **Pod log tracking** with automatic **multi-color marking** for various exception levels, supporting keyword filtering.
- 📀Microservice version updates support **designated accounts**, **specified time periods** for authorized operations, and **automatic tag retrieval from image repositories**. <a target="_blank" href="docs/K8S微服务镜像更新配置说明.md">【K8S Microservice Image Update Configuration Guide】</a>
- ⏱️Supports **immediate, scheduled, and periodic** execution of microservice **isolation, scaling, and restart** operations.
- ♨Provides extensive one-click JVM performance analysis operation support for **JAVA microservices**.
- 🌐Supports management and synchronization of multi-K8S **ISTIO VirtualService** rules.
<div align="center">

| <img  width="850" src="./screenshot/1.0/1.png"/> |<img width="550" src="https://github.com/user-attachments/assets/0c7d1891-3df1-4413-a7c2-1b2288c35a25" /> |<img width="550" src="https://github.com/user-attachments/assets/d25f67b0-25df-4a43-af8d-49b9fc385c85" />| 
| ------------------------------------| ----------------------------------- | ---------------------------------- |

</div>

---

### 3. 🧬Exception Alert Notification & Intelligent Aggregation

- 🦄K8S microservice unified alert analysis and processing page with **daily intelligent aggregation** display, processing markers, daily cumulative counting for identical alerts, providing clear daily alert overview.
- 🕹️Supports operations on PODs including **isolation, deletion, Java dump, jstack, jfr, JVM** data collection and analysis, with IM notifications.
- 📺New K8S event collection, analysis & monitoring alerts! Multi-K8S event collection and query analysis with flexible K8S event alert rule configuration. <a target="_blank" href="docs/K8S事件告警规则配置说明.md">【K8S Event Alert Rule Configuration Guide】</a>
<div align="center">

| <img src="./screenshot/1.0/4.jpg"/> | <img src="./screenshot/1.0/5.png"/> | <img src="./screenshot/1.0/15.jpg"/> |
| ------------------------------------| ----------------------------------- | ----------------------------------- |
| <img src="./screenshot/1.0/6.png"/> | <img src="./screenshot/1.0/7.jpg"/> | <img src="./screenshot/1.0/8.png"/> |
</div>

---

### 4. 💠Peak Period P95 Resource Collection & Analysis

#### 📊Collects P95 CPU and memory consumption during daily business peak periods for K8S microservices, along with request values, limit values, and Pod counts. Implements visualization analysis based on collected data.
  - 🎨**Daily dimension-based collection of P95 resource data during peak periods**, enabling excellent observation of long-term resource changes for various microservices, with smooth performance even when viewing 1 year of data.
  - 🏅Peak period global resource statistics and various **resource TOP10**, namespace-level peak period P95 resource usage and **resource consumption ratio relative to overall resources**
  - 🧿**Microservice-level** peak period overall resource and utilization analysis, microservice and **Pod-level** resource curve charts (request values, limit values, usage values)
<div align="center">
  
|<img src="./screenshot/kd1.jpg"/>|<img src="./screenshot/kd2.jpg"/>|<img src="https://github.com/user-attachments/assets/0b74f5cf-b3f5-4dae-a44e-5382e4977cf4"/>|
|-|-|-|
|<img src="./screenshot/kd3.jpg"/>|<img src="./screenshot/kd4.jpg"/>|<img src="./screenshot/1.0/9.png"/>|
</div>

#### 🎡Daily retrieval of resource information for various microservices from the last 10 days of collected data, obtaining P95 resources from the day with maximum resource consumption as microservice request values written to the database.
  - ♻**After enabling admission control**: Implements microservice **automatic request value management** mechanism, supporting unified strong governance page for manual adjustment of microservice **limit values and Pod counts**.
  - ✨**Based on admission control mechanism**, achieving **consistency between actual usage rates and resource request values** for K8S microservice resources has very important significance:
    - 🌊**K8S scheduler** can more precisely schedule Pods to appropriate nodes through real resource request values, **avoiding resource fragmentation and achieving node resource balance**.
    - ♻**K8S auto-scaling** also relies on resource request values for judgment, **real request values can more accurately trigger scaling operations**.
    - 🛡**K8S Quality of Service** (QoS mechanism) combined with request values, Pods with real request values will be prioritized for retention, **ensuring normal operation of critical services**.

---

### 5. 🚧K8S Admission Control Based: Ensuring Strong Consistency Between Microservice Requirements and Peak Resources

- #### <a target="_blank" href="docs/K8S资源管控功能说明.md">👑Detailed Explanation of Admission Control-Based Governance Capabilities</a>

---

### 6. ✨Others
  - ❤️Agent management page: Update and maintain Agent status, configure collection and governance.
  - 🔒Based on **NGINX basic authentication**, supports LDAP, interface-level permission control, and all **operation audit** logs with notifications.
  - 📊All dashboards are created based on Grafana and integrated into the frontend UI, enabling data analysis to quickly achieve more elegant displays.
<div align="center">
   
| <img src="./screenshot/1.0/11.jpg" width="800;" />| 
| ------------------------------------|
</div>

---

## 📀KubeDoor New Architecture, New Deployment
#### 🛠Quick Installation <a target="_blank" href="docs/灵活部署方案.md">【View Flexible Deployment Options】</a>
<details close>
<summary>🔍Click to expand ...</summary>

```
### 【Download helm package】
wget https://StarsL.cn/kubedoor/kubedoor-1.5.2.tgz
tar -zxvf kubedoor-1.5.2.tgz
cd kubedoor
### 【Master installation】
# Edit values-master.yaml file, please read comments carefully and modify configuration content according to descriptions.
# try
helm upgrade -i kubedoor . --namespace kubedoor --create-namespace --values values-master.yaml --dry-run --debug
# install
helm upgrade -i kubedoor . --namespace kubedoor --create-namespace --values values-master.yaml
### 【Agent installation】
# Edit values-agent.yaml file, please read comments carefully and modify configuration content according to descriptions.
helm upgrade -i kubedoor-agent . --namespace kubedoor --create-namespace --values values-agent.yaml --set tsdb.external_labels_value=xxxxxxxx
```
</details>

#### ♻Update Guide <a target="_blank" href="https://github.com/CassInfra/KubeDoor/releases/tag/1.5.2">【Version Change Log】</a>

<details close>
<summary>🔍Click to expand ...</summary>

```
# Download installation package
wget https://StarsL.cn/kubedoor/kubedoor-1.5.2.tgz
tar -zxvf kubedoor-1.5.2.tgz
```
```
# Updating from older versions to 1.3.0 and above requires adding 2 new database fields
ALTER TABLE kubedoor.k8s_agent_status ADD COLUMN nms_not_confirm Bool DEFAULT false AFTER admission_namespace;
ALTER TABLE kubedoor.k8s_agent_status ADD COLUMN scheduler Bool DEFAULT false AFTER nms_not_confirm;
```
**Note:**
- Please refer to the already deployed configmap: `kubedoor-info` for `VictoriaMetrics`, `ClickHouse` and other configuration items to modify the corresponding configurations in `values-master.yaml` and `values-agent.yaml`, ensuring the configurations used are consistent with the old version. (Direct file replacement is not possible due to yaml configuration adjustments.)
- Or use the following commands to view the values configuration information used during deployment, and modify the corresponding configurations in `values-master.yaml` and `values-agent.yaml`.
```
helm get values kubedoor -n kubedoor
helm get values kubedoor-agent -n kubedoor
```
# 【Master update】
helm upgrade -i kubedoor . --namespace kubedoor --create-namespace --values values-master.yaml
# 【Agent update】
helm upgrade -i kubedoor-agent . --namespace kubedoor --create-namespace --values values-agent.yaml --set tsdb.external_labels_value=xxxxxxxx

</details>

#### 🌐Usage Instructions
<details close>
<summary>🔍Click to expand ...</summary>

- **Access WebUI and Initialize Data:**
  1. Access using K8S node IP + kubedoor-web NodePort, default username and password are both **`kubedoor`**
  2. Click `Agent Management`, first enable `Auto Collection`, set the `Peak Period`, then execute collection: input the `Historical Data Duration` to collect, click `Collect` to collect historical data and update peak period data to the governance table.
- **Note:**
  - After enabling auto collection, data from the previous day's peak period will be collected daily at 1 AM, and data from the day with maximum resource consumption within 10 days will be written to the governance table.
  - Repeatedly executing `Collection` will not cause duplicate data writes, please use with confidence; after each collection, data from the day with maximum resource consumption within 10 days will automatically be written to the governance table. If it takes a long time, please wait for collection completion or shorten the collection duration.
  - If you have a newly installed monitoring system and the current day's peak period has passed, no data will be collected; you need to wait until after the next day's peak period to collect data.
</details>

---

## 🔔KubeDoor Community & 🧧Sponsorship

<div align="center">

#### If you think the project is good, please give it a ⭐️Star⭐️ If you have other ideas or requirements, welcome to discuss in issues
<img width="600" alt="kubedoor-wechat" src="https://github.com/user-attachments/assets/91babc64-2473-4dd2-bc39-eaa8c3232156" />

**Add author's WeChat or follow the official account to join the discussion group**

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
  </tr>
</table>
</div>

## 🥰Acknowledgments

Thanks to the following excellent projects, without which **KubeDoor** would not be possible:
- [TRAE](www.trae.ai) [Python](https://www.python.org/) [AIOHTTP](https://github.com/aio-libs/aiohttp) [VUE](https://cn.vuejs.org/) [Pure Admin](https://pure-admin.cn/) [Element Plus](https://element-plus.org) [Kubernetes](https://kubernetes.io/) [VictoriaMetrics](https://victoriametrics.com/) [ClickHouse](https://clickhouse.com/) [Grafana](https://grafana.com/) [Nginx](https://nginx.org/) ...

**Special Thanks**
- [**CassTime**](https://www.casstime.com): The birth of **KubeDoor** is inseparable from the support of 🦄**CassTime**.
