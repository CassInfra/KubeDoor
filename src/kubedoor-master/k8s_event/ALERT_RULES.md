# K8S 事件告警规则配置指南

## 概述

K8S 事件告警系统是一个基于规则驱动的智能告警解决方案，能够根据配置的规则自动检测和处理 Kubernetes 事件，并发送相应的告警通知。

## 功能特性

- 🎯 **规则驱动**: 基于 JSON 配置文件的灵活告警规则
- 🔍 **多条件匹配**: 支持字段包含/不包含、开头/结尾匹配、数值比较等多种匹配条件
- 🚫 **智能过滤**: 忽略 DELETED 事件和配置的忽略规则
- 🔄 **修改生效**: 规则在 master 启动时加载，修改 ConfigMap 后需重启 master
- 📱 **多渠道通知**: 支持企业微信、钉钉、飞书、Slack 等多种告警通知方式

## 快速开始

告警系统已集成到事件处理流程中，当事件匹配告警规则时会自动发送告警通知。

告警消息会通过 kubedoor 的统一消息发送接口自动发送到配置的通知渠道（企业微信、钉钉、飞书、Slack）。

## DELETED 事件处理

系统对 `eventStatus=DELETED` 的事件采用**硬编码忽略**策略：

- **优先级最高**: 在所有规则检查之前，直接忽略 DELETED 事件
- **不影响入库**: 事件仍会正常存储到 PostgreSQL，只是跳过告警处理
- **统计计入**: DELETED 事件会被计入忽略事件统计
- **日志记录**: 会记录调试日志 "DELETED 事件被直接忽略"

这种设计确保了 DELETED 事件不会触发任何告警，同时保持数据完整性。

## 规则匹配逻辑

### 规则组之间的关系

- **global_ignore_rules**: 多条忽略规则之间是 **OR** 关系，任意一条规则匹配即忽略事件
- **alert_rules**: 多条告警规则之间是 **OR** 关系，按数组顺序匹配，第一个匹配的规则生效

### 单条规则内的条件关系

每条规则的 `conditions` 内的多个字段条件之间是 **AND** 关系，必须所有条件都满足才算匹配：

```json
{
  "conditions": {
    "reason": { "contains": ["OOMKilling"] }, // 条件1
    "level": { "equals": "Warning" }, // 条件2
    "namespace": { "not_contains": ["test"] } // 条件3
  }
  // 只有当 reason包含OOMKilling AND level等于Warning AND namespace不包含test 时才匹配
}
```

### 字段条件内的值关系

对于同一字段的多个值，关系取决于条件类型：

- **contains**: 多个值之间是 **OR** 关系（包含任一值即匹配）
- **not_contains**: 多个值之间是 **AND** 关系（不包含任何值才匹配）
- **starts_with/ends_with**: 多个值之间是 **OR** 关系
- **not_starts_with/not_ends_with**: 多个值之间是 **AND** 关系

## 全局忽略规则

全局忽略规则用于过滤不需要处理的事件，支持多条规则按顺序匹配：

- **顺序匹配**: 按数组顺序依次检查规则，前面的规则先匹配
- **启用状态**: 可以通过 `enabled` 字段控制规则是否生效
- **OR 关系**: 多条忽略规则之间是 OR 关系，任一规则匹配即忽略事件
- **AND 关系**: 每条规则内的 conditions 字段之间是 AND 关系，必须全部满足
- **灵活配置**: 可以根据不同场景添加多条专门的忽略规则

## 告警规则配置

告警规则配置文件 `deploy/manifests/master/alert_rules.json`，安装时做成 ConfigMap `kubedoor-master-file-cfg`，挂载到 master 的 `/k8s_event/rules/`（镜像内不自带规则文件），格式示例如下（集群名、服务名均为示意）:

```json
{
  "global_ignore_rules": [
    {
      "name": "环境过滤规则(排除非生产环境的K8S)",
      "enabled": true,
      "conditions": {
        "k8s": {
          "contains": ["test-a", "test-b"]
        }
      }
    },
    {
      "name": "常见忽略事件",
      "enabled": true,
      "conditions": {
        "reason": {
          "contains": ["BackOffStart", "SuccessfulCreate", "SuccessfulMountVolume"]
        }
      }
    }
  ],
  "alert_rules": [
    {
      "name": "关键事件告警",
      "enabled": true,
      "severity": "critical",
      "conditions": {
        "reason": {
          "contains": [
            "LoadBalancerFailed",
            "OOMKilling",
            "NodeNotReady",
            "SystemOOM",
            "Unreachable",
            "BackOffPullImage"
          ]
        },
        "level": {
          "contains": ["Warning"]
        }
      }
    },
    {
      "name": "重启事件告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "message": {
          "contains": ["restart"]
        }
      }
    },
    {
      "name": "健康检查失败告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "reason": {
          "contains": ["Unhealthy"]
        },
        "name": {
          "not_starts_with": ["deploy-demo-"]
        },
        "count": {
          "greater_than": 1
        }
      }
    },
    {
      "name": "挂载失败告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "reason": {
          "contains": ["FailedMount", "FailedAttachVolume"]
        }
      }
    },
    {
      "name": "调度失败告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "reason": {
          "contains": ["FailedScheduling", "Insufficient"]
        }
      }
    },
    {
      "name": "资源压力告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "reason": {
          "contains": ["Pressure", "OutOf", "Evict"]
        }
      }
    },
    {
      "name": "僵尸进程告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "reason": {
          "contains": ["ProcessZ"]
        }
      }
    }
  ]
}
```

## 支持的条件类型

| 条件类型          | 说明                      | 示例                                               |
| ----------------- | ------------------------- | -------------------------------------------------- |
| `contains`        | 字段包含任一指定值        | `{"reason": {"contains": ["OOM", "Failed"]}}`      |
| `not_contains`    | 字段不包含任何指定值      | `{"namespace": {"not_contains": ["test", "dev"]}}` |
| `equals`          | 字段等于指定值            | `{"level": {"equals": "Warning"}}`                 |
| `not_equals`      | 字段不等于指定值          | `{"eventStatus": {"not_equals": "DELETED"}}`       |
| `starts_with`     | 字段以指定值开头          | `{"name": {"starts_with": ["deploy-"]}}`           |
| `not_starts_with` | 字段不以指定值开头        | `{"name": {"not_starts_with": ["test-"]}}`         |
| `ends_with`       | 字段以指定值结尾          | `{"name": {"ends_with": ["-service", "-pod"]}}`    |
| `not_ends_with`   | 字段不以指定值结尾        | `{"name": {"not_ends_with": ["-test", "-debug"]}}` |
| `greater_than`    | 数值大于（仅 count 字段） | `{"count": {"greater_than": 3}}`                   |
| `less_than`       | 数值小于（仅 count 字段） | `{"count": {"less_than": 10}}`                     |

## 配置示例

### 基础告警规则

```json
{
  "alert_rules": [
    {
      "name": "重启事件告警",
      "enabled": true,
      "conditions": {
        "message": {
          "contains": ["restart"]
        },
        "k8s": {
          "contains": ["prod"]
        },
        "namespace": {
          "not_contains": ["apo"]
        }
      },
      "severity": "warning"
    }
  ]
}
```

**规则匹配逻辑说明**：

- `message` 包含 "restart" **AND**
- `k8s` 包含 "prod" **AND**
- `namespace` 不包含 "apo"
- 三个条件必须同时满足才会触发告警

### 高级过滤规则

```json
{
  "global_ignore_rules": [
    {
      "name": "环境过滤规则",
      "enabled": true,
      "conditions": {
        "k8s": {
          "not_contains": ["prod", "prd", "online"]
        }
      }
    },
    {
      "name": "系统组件过滤",
      "enabled": true,
      "conditions": {
        "namespace": {
          "contains": ["kube-system", "kube-public"]
        },
        "name": {
          "starts_with": ["system-"]
        }
      }
    }
  ]
}
```

**忽略规则匹配逻辑说明**：

- **规则 1**: `k8s` 字段不包含任何生产环境标识
- **规则 2**: `namespace` 包含系统命名空间 **AND** `name` 以 "system-" 开头
- 两条规则之间是 **OR** 关系，任一规则匹配即忽略事件

### 复杂条件组合

```json
{
  "alert_rules": [
    {
      "name": "健康检查失败告警",
      "enabled": true,
      "conditions": {
        "reason": {
          "contains": ["Unhealthy"]
        },
        "k8s": {
          "not_contains": ["test", "dev"]
        },
        "name": {
          "not_starts_with": ["deploy-demo-", "deploy-batch-", "deploy-temp-"]
        },
        "count": {
          "greater_than": 1
        },
        "namespace": {
          "not_contains": ["apo"]
        }
      },
      "severity": "warning"
    }
  ]
}
```

**复杂条件匹配逻辑说明**：

- `reason` 包含 "Unhealthy" **AND**
- `k8s` 不包含测试环境标识 **AND**
- `name` 不以特定服务前缀开头（多个前缀之间是 AND 关系） **AND**
- `count` 大于 1 **AND**
- `namespace` 不包含 "apo"
- 所有 5 个条件必须同时满足才会触发告警

## 规则匹配流程总结

### 完整的事件处理流程

1. **硬编码检查**: 首先检查 `eventStatus=DELETED`，如果是则直接忽略（最高优先级）
2. **全局忽略规则**: 按顺序检查 `global_ignore_rules`，任一规则匹配即忽略事件
3. **告警规则匹配**: 按数组顺序检查 `alert_rules`，第一个匹配的规则生效
4. **告警发送**: 如果匹配到告警规则，则生成告警消息并发送

### 关键逻辑要点

| 层级           | 关系类型       | 说明                                                           |
| -------------- | -------------- | -------------------------------------------------------------- |
| **规则组间**   | OR             | `global_ignore_rules` 中任一规则匹配即忽略                     |
| **规则组间**   | OR             | `alert_rules` 中按数组顺序匹配，第一个匹配的生效               |
| **规则内条件** | AND            | 单条规则的 `conditions` 内所有字段条件必须都满足               |
| **字段内值**   | 取决于条件类型 | `contains`/`starts_with`/`ends_with` 是 OR，`not_*` 系列是 AND |

### 实际匹配示例

假设有事件：`{"reason": "Unhealthy", "k8s": "prod", "namespace": "default", "count": 3}`

对于规则：

```json
{
  "conditions": {
    "reason": { "contains": ["Unhealthy", "Failed"] }, // ✅ 匹配（包含Unhealthy）
    "k8s": { "contains": ["prod"] }, // ✅ 匹配（包含prod）
    "count": { "greater_than": 1 } // ✅ 匹配（3>1）
  }
}
```

**结果**: 所有条件都满足（AND 关系），规则匹配成功 ✅

## 最佳实践

### 1. 规则顺序设计

- **数组前部**: 严重故障，如 OOM、节点不可用（优先匹配）
- **数组中部**: 一般告警，如 Pod 重启、镜像拉取失败
- **数组后部**: 信息性告警，如配置更新、扩缩容

### 2. 忽略规则配置

- 优先配置环境过滤，避免测试环境干扰
- 忽略正常的系统事件，如 SuccessfulCreate
- 根据业务需求配置特定的忽略条件

### 3. 性能优化

- 合理设置忽略规则，减少不必要的处理
- 在高并发场景下使用异步处理
- 定期检查和优化规则配置
