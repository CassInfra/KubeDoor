# Istio VirtualService 路由管理 API v3.0

这是一个集成在 kubedoor-master 中的 Istio VirtualService 和 HTTP 路由规则管理系统，支持 VS 级别和路由级别的 CRUD 操作，基于优先级的路由管理，以及 K8S 集群关联管理。

## 功能特性

- ✅ **完整的 CRUD 操作**: 支持 VirtualService 和 HTTP 路由的创建、读取、更新、删除
- ✅ **基于优先级的路由管理**: 使用 priority 字段控制路由顺序，支持自动分配和重复检查
- ✅ **智能优先级处理**: 不传 priority 时自动追加到最后，传入时检查重复值
- ✅ **YAML 生成**: 根据数据库数据生成完整的 VirtualService YAML 配置
- ✅ **K8S 集群关联管理**: 支持 VirtualService 与 K8S 集群的关联关系管理
- ✅ **PostgreSQL 数据库**: 使用 PostgreSQL 进行数据持久化(psycopg 3.x)
- ✅ **优先级重新整理**: 支持批量重新整理路由优先级
- ✅ **健康检查**: 提供系统健康状态检查接口

## 系统架构

```
├── kubedoor-master.py   # 主要 API 服务文件
├── istio_route/
│   ├── istio_route.py   # 路由管理核心逻辑
│   └── README.md        # 说明文档
└── utils.py             # 工具函数和配置
```

## 数据库设计

### 全局字段

| KEY       | VALUE  |
| --------- | ------ |
| name      | 字符串 |
| namespace | 字符串 |
| gateways  | 列表   |
| hosts     | 列表   |
| protocol  | 字符串 |

### HTTP 路由规则 [{...},{...}]

| KEY                 | 类型           | 描述         | 示例/说明                                                     |
| ------------------- | -------------- | ------------ | ------------------------------------------------------------- |
| **name**            | 字符串(可选)   | 虚拟服务名称 |                                                               |
| **match**           | **列表**(可选) | 匹配规则列表 | `[{uri: {...}, authority: {...}}, {...}]`                     |
| match[].uri         | 字典           | URI 匹配规则 | **{**`uri: {prefix/exact/regex: 字符串}`**,**                 |
| match[].authority   | 字典           | 权威匹配规则 | `authority: {exact: 字符串}`**}**                             |
| **rewrite**         | **字典**(可选) | 重写规则对象 | `{uri: "/"}`                                                  |
| rewrite.uri         | 字符串         | URI 重写路径 | `/`                                                           |
| forward_type        | 字符串         | 转发类型     | `route/delegate`                                              |
| **route**           | **列表**       | 路由规则列表 | `[{destination: {...}, headers: {...}}, {...}]`               |
| route[].destination | 字典           | 目标服务     | **{**`destination: {host: 字符串, port: {number: 数字}}`**,** |
| route[].headers     | 字典           | 请求头设置   | `headers: {request: {set: {X-Forwarded-Proto: https}}}`**}**  |
| **delegate**        | **字典**       | 委托配置对象 | `{name: "service-orders-service", namespace: "sos"}`          |
| delegate.name       | 字符串         | 委托服务名   | `service-orders-service`                                      |
| delegate.namespace  | 字符串         | 命名空间     | `sos`                                                         |
| **timeout**         | 字符串         | 超时时间     | `10s`                                                         |

### vs_global 表 (VirtualService 全局配置)

| 字段               | 类型         | 说明                                 |
| ------------------ | ------------ | ------------------------------------ |
| id                 | INT          | 主键，自增                           |
| name               | VARCHAR(255) | VirtualService 名称                  |
| namespace          | VARCHAR(255) | 命名空间，默认 'default'             |
| gateways           | TEXT         | 网关列表 (JSON)                      |
| hosts              | TEXT         | 主机列表 (JSON)                      |
| protocol           | VARCHAR(50)  | 协议类型，默认 'http'                |
| df_forward_type    | VARCHAR(20)  | 默认路由类型 ('route' 或 'delegate') |
| df_forward_detail  | TEXT         | 默认路由详情 (JSON)                  |
| df_forward_timeout | VARCHAR(50)  | 默认路由超时设置                     |
| created_at         | TIMESTAMP    | 创建时间                             |
| updated_at         | TIMESTAMP    | 更新时间                             |

### vs_http_routes 表 (HTTP 路由规则)

| 字段           | 类型         | 说明                             |
| -------------- | ------------ | -------------------------------- |
| id             | INT          | 主键，自增                       |
| vs_global_id   | INT          | 关联的全局配置 ID (外键)         |
| name           | VARCHAR(255) | 路由规则名称                     |
| priority       | INT          | 优先级 (数字越小优先级越高)      |
| match_rules    | TEXT         | 匹配规则 (JSON)                  |
| rewrite_rules  | TEXT         | 重写规则 (JSON)                  |
| forward_type   | VARCHAR(20)  | 转发类型 ('route' 或 'delegate') |
| forward_detail | TEXT         | 转发详情 (JSON)                  |
| timeout        | VARCHAR(50)  | 路由级别超时设置                 |
| created_at     | TIMESTAMP    | 创建时间                         |
| updated_at     | TIMESTAMP    | 更新时间                         |

### k8s_cluster 表 (K8S集群关联)

| 字段       | 类型         | 说明                        |
| ---------- | ------------ | --------------------------- |
| id         | INT          | 主键，自增                  |
| k8s_name   | VARCHAR(255) | K8S集群名称                 |
| vs_id      | INT          | 关联的VirtualService ID (外键) |
| updated_at | TIMESTAMP    | 更新时间                    |

## 快速开始

### 1. 环境配置

与 kubedoor-master 共用 PostgreSQL 连接,通过环境变量设置(K8S 部署时在 `kubedoor-config` 这个 ConfigMap 里):

```env
PG_HOST=10.1.1.50
PG_PORT=5432
PG_USER=kubedoor
PG_PASSWORD=your_password
PG_DATABASE=kubedoor
```

### 2. 数据库初始化

不需要手动建表。kubedoor-master 启动时会执行 `db.sql`,Istio 的三张表(`vs_global` /
`vs_http_routes` / `k8s_cluster`)和其它表一起自动创建。

### 3. 启动服务

启动 kubedoor-master 主服务:

```bash
python kubedoor-master.py
```

服务监听 80 端口

### 4. API 访问

所有 Istio 路由管理接口都在 `/api/istio/` 路径下，集成在 kubedoor-master 服务中

## API 接口详解

### VS 级别接口

#### 1. 获取所有 VirtualService

```http
GET /api/istio/vs?namespace=default
```

**响应示例：**

```json
[
  {
    "id": 1,
    "name": "example-vs",
    "namespace": "default",
    "gateways": ["gateway1", "gateway2"],
    "hosts": ["example.com", "api.example.com"],
    "protocol": "http",
    "df_forward_type": "route",
    "df_forward_detail": [
      {
        "destination": {
          "host": "default-service",
          "port": { "number": 8080 }
        }
      }
    ],
    "df_forward_timeout": "30s",
    "created_at": "2024-01-15T10:30:00",
    "updated_at": "2024-01-15T10:30:00"
  }
]
```

#### 2. 获取指定 VirtualService

```http
GET /api/istio/vs?vs_id=1
```

**查询参数**:
- `vs_id`: VirtualService ID

#### 3. 创建 VirtualService

```http
POST /api/istio/vs
Content-Type: application/json

{
  "k8s_clusters": ["cluster1", "cluster2"],
  "name": "example-vs",
  "namespace": "default",
  "gateways": ["gateway1", "gateway2"],
  "hosts": ["example.com", "api.example.com"],
  "protocol": "http",
  "df_forward_type": "route",
  "df_forward_detail": [
    {
      "destination": {
        "host": "default-service",
        "port": {"number": 8080}
      }
    }
  ],
  "df_forward_timeout": "30s"
}
```

**响应示例：**

```json
{
  "message": "VirtualService created successfully",
  "data": {
    "id": 1,
    "name": "example-vs",
    "namespace": "default"
  }
}
```

#### 4. 更新 VirtualService

```http
PUT /api/istio/vs?vs_id=1
Content-Type: application/json

{
  "name": "updated-vs-name",
  "namespace": "production",
  "hosts": ["new-example.com"],
  "df_forward_timeout": "60s"
}
```

**查询参数**:
- `vs_id`: VirtualService ID

#### 5. 删除 VirtualService

```http
DELETE /api/istio/vs?vs_id=1
```

**查询参数**:
- `vs_id`: VirtualService ID

### HTTP 路由级别接口

#### 1. 获取 HTTP 路由列表

```http
GET /api/istio/httproute?vs_id=1
```

**响应示例：**

```json
[
  {
    "id": 1,
    "vs_global_id": 1,
    "name": "api-route",
    "priority": 10,
    "match_rules": [
      {
        "uri": { "prefix": "/api" },
        "headers": {
          "x-version": { "exact": "v1" }
        }
      }
    ],
    "rewrite_rules": {
      "uri": "/v1"
    },
    "forward_type": "route",
    "forward_detail": [
      {
        "destination": {
          "host": "api-service",
          "port": { "number": 8080 }
        },
        "weight": 100
      }
    ],
    "timeout": "10s",
    "created_at": "2024-01-15T10:30:00",
    "updated_at": "2024-01-15T10:30:00"
  }
]
```

#### 2. 获取指定 HTTP 路由

```http
GET /api/istio/httproute?vs_id=1&route_id=1
```

#### 3. 创建 HTTP 路由

```http
POST /api/istio/httproute?vs_id=1
Content-Type: application/json

{
  "name": "api-route",
  "match_rules": [
    {
      "uri": {"prefix": "/api"},
      "headers": {
        "x-version": {"exact": "v1"}
      }
    }
  ],
  "rewrite_rules": {
    "uri": "/v1"
  },
  "forward_type": "route",
  "forward_detail": [
    {
      "destination": {
        "host": "api-service",
        "port": {"number": 8080}
      },
      "weight": 100
    }
  ],
  "timeout": "10s",
  "priority": 15
}
```

**注意：**

- `priority` 参数可选，不传则自动追加到最后
- 传入 `priority` 时会检查是否重复，重复则返回错误

**响应示例：**

```json
{
  "message": "HTTP route created successfully",
  "data": {
    "route_id": 1,
    "priority": 15
  }
}
```

#### 4. 更新 HTTP 路由

```http
PUT /api/istio/httproute?vs_id=1&route_id=1
Content-Type: application/json

{
  "name": "updated-api-route",
  "timeout": "20s"
}
```

**查询参数**:
- `vs_id`: VirtualService ID
- `route_id`: 路由 ID

#### 5. 删除 HTTP 路由

```http
DELETE /api/istio/httproute?vs_id=1&route_id=1
```

**查询参数**:
- `vs_id`: VirtualService ID
- `route_id`: 路由 ID

### 路由管理辅助接口

#### 重新整理路由优先级

```http
POST /api/istio/httproute/reorder?vs_id=1
```

这个接口会重新分配所有路由的优先级，确保连续性（使用 10 的倍数）。

**查询参数**:
- `vs_id`: VirtualService ID

**响应示例：**

```json
{
  "message": "Routes reordered successfully",
  "data": {
    "reordered_count": 5
  }
}
```

### 其他接口

#### 生成 YAML 配置

```http
GET /api/istio/vs/yaml?vs_id=1
```

**查询参数**:
- `vs_id`: VirtualService ID

**响应示例：**

```json
{
  "yaml": "apiVersion: networking.istio.io/v1beta1\nkind: VirtualService\nmetadata:\n  name: example-vs\n  namespace: default\nspec:\n  hosts:\n  - example.com\n  - api.example.com\n  gateways:\n  - gateway1\n  - gateway2\n  http:\n  - name: api-route\n    match:\n    - uri:\n        prefix: /api\n      headers:\n        x-version:\n          exact: v1\n    rewrite:\n      uri: /v1\n    route:\n    - destination:\n        host: api-service\n        port:\n          number: 8080\n      weight: 100\n    timeout: 10s\n  - route:\n    - destination:\n        host: default-service\n        port:\n          number: 8080\n    timeout: 30s"
}
```

#### 2. 健康检查

```http
GET /api/istio/health
```

**响应示例：**

```json
{
  "status": "healthy",
  "timestamp": "2024-01-15T10:30:00Z",
  "database": "connected"
}
```

### K8S 集群关联管理接口

#### 更新 VirtualService 的 K8S 集群关联关系

**接口**: `POST /api/istio/vs/k8s`

**描述**: 更新指定 VirtualService 与 K8S 集群的关联关系

**请求体**:
```json
{
  "vs_id": 1,
  "k8s_clusters": ["cluster-1", "cluster-2", "cluster-3"]
}
```

**响应**:
```json
{
  "message": "K8S cluster relations updated successfully",
  "vs_id": 1,
  "updated_clusters": ["cluster-1", "cluster-2", "cluster-3"]
}
```

**说明**:
- 该接口会先删除指定 VirtualService 的所有现有 K8S 集群关联
- 然后添加新的关联关系
- 操作在数据库事务中进行，确保数据一致性
- 如果 `k8s_clusters` 为空数组，则只删除现有关联，不添加新关联

## 优先级管理详解

### 1. 优先级规则

- **数字越小，优先级越高**
- 系统使用 10 的倍数作为基础优先级（10, 20, 30...）
- 便于在中间插入新路由

### 2. 自动优先级分配

当创建路由时不传 `priority` 参数：

```json
{
  "name": "auto-priority-route",
  "forward_type": "route",
  "forward_detail": [...]
  // 不传 priority，系统自动分配
}
```

系统会自动分配一个比当前最大优先级大 10 的值。

### 3. 手动指定优先级

```json
{
  "name": "manual-priority-route",
  "forward_type": "route",
  "forward_detail": [...],
  "priority": 25  // 手动指定优先级
}
```

如果指定的优先级已存在，会返回错误。

### 4. 优先级重新整理

当优先级变得不连续或需要重新排序时，调用重新整理接口：

```http
POST /api/istio/httproute/reorder?vs_name=example-vs&namespace=default
```

## 使用示例

### Python 示例

```python
import requests
import json

base_url = "http://localhost:5001"

# 1. 创建 VirtualService
vs_data = {
    "name": "my-app",
    "namespace": "production",
    "hosts": ["myapp.com"],
    "gateways": ["istio-system/gateway"]
}

response = requests.post(f"{base_url}/api/istio/vs", json=vs_data)
print(f"创建 VS: {response.json()}")

# 2. 创建 HTTP 路由（自动优先级）
route_data = {
    "name": "api-v1",
    "match_rules": [
        {
            "uri": {"prefix": "/api/v1"},
            "headers": {
                "x-api-version": {"exact": "v1"}
            }
        }
    ],
    "forward_type": "route",
    "forward_detail": [
        {
            "destination": {
                "host": "api-v1-service",
                "port": {"number": 8080}
            }
        }
    ],
    "timeout": "30s"
}

response = requests.post(
    f"{base_url}/api/istio/httproute?vs_id=1",
    json=route_data
)
print(f"创建路由: {response.json()}")

# 3. 创建另一个路由（指定优先级）
route_data2 = {
    "name": "api-v2",
    "match_rules": [
        {
            "uri": {"prefix": "/api/v2"}
        }
    ],
    "forward_type": "route",
    "forward_detail": [
        {
            "destination": {
                "host": "api-v2-service",
                "port": {"number": 8080}
            }
        }
    ],
    "priority": 5  # 更高优先级
}

response = requests.post(
    f"{base_url}/api/istio/httproute?vs_id=1",
    json=route_data2
)
print(f"创建高优先级路由: {response.json()}")

# 4. 获取路由列表
response = requests.get(
    f"{base_url}/api/istio/httproute?vs_id=1"
)
routes = response.json()
print(f"路由列表: {json.dumps(routes, indent=2)}")

# 5. 生成 YAML
response = requests.get(
    f"{base_url}/api/istio/vs/yaml?vs_id=1"
)
yaml_content = response.json()["yaml"]
print(f"生成的 YAML:\n{yaml_content}")

# 6. 重新整理优先级
response = requests.post(
    f"{base_url}/api/istio/httproute/reorder?vs_id=1"
)
print(f"重新整理结果: {response.json()}")

# 7. 更新 K8S 集群关联关系
k8s_data = {
    "vs_id": 1,  # 使用创建的 VirtualService ID
    "k8s_clusters": ["prod-cluster-1", "prod-cluster-2", "backup-cluster"]
}

response = requests.post(
    f"{base_url}/api/istio/vs/k8s",
    json=k8s_data
)
print(f"K8S集群关联更新结果: {response.json()}")
```

### curl 示例

```bash
# 创建 VirtualService
curl -X POST "http://localhost:5001/api/istio/vs" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-vs",
    "hosts": ["test.com"],
    "gateways": ["istio-system/gateway"]
  }'

# 创建 HTTP 路由
curl -X POST "http://localhost:5001/api/istio/httproute?vs_id=1" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-route",
    "match_rules": [
      {
        "uri": {"prefix": "/test"}
      }
    ],
    "forward_type": "route",
    "forward_detail": [
      {
        "destination": {
          "host": "test-service",
          "port": {"number": 8080}
        }
      }
    ]
  }'

# 获取路由列表
curl "http://localhost:5001/api/istio/httproute?vs_id=1"

# 生成 YAML
curl "http://localhost:5001/api/istio/vs/yaml?vs_id=1"

# 重新整理路由优先级
curl -X POST "http://localhost:5001/api/istio/httproute/reorder?vs_id=1"

# 更新 K8S 集群关联关系
curl -X POST "http://localhost:5001/api/istio/vs/k8s" \
  -H "Content-Type: application/json" \
  -d '{
    "vs_id": 1,
    "k8s_clusters": ["cluster-1", "cluster-2"]
  }'
```

## 错误处理

### 常见错误码

- **400 Bad Request**: 请求参数错误
- **404 Not Found**: 资源不存在
- **409 Conflict**: 资源冲突（如名称重复、优先级重复）
- **500 Internal Server Error**: 服务器内部错误

### 错误响应格式

```json
{
  "detail": "Priority 10 already exists"
}
```

## 故障排除

### 1. 数据库连接问题

检查 master 的 `PG_*` 环境变量(K8S 部署时看 `kubedoor-config` 这个 ConfigMap),
确认 `PG_HOST` 是 Pod 能访问到的地址。

### 2. 优先级冲突

如果遇到优先级冲突，可以：

1. 使用自动优先级分配（不传 priority 参数）
2. 调用重新整理接口重新分配优先级
3. 手动指定一个未使用的优先级值

### 3. 服务启动失败

检查：

- 端口 80 是否被占用
- PostgreSQL 服务是否运行
- 数据库权限是否正确

## 扩展开发

### 添加新字段

1. 修改数据库表结构
2. 更新 Pydantic 模型
3. 修改相应的 CRUD 操作
4. 更新 YAML 生成逻辑

### 添加新接口

1. 定义新的路由处理函数
2. 添加相应的 Pydantic 模型
3. 更新 API 文档

## 许可证

本项目采用 MIT 许可证。
