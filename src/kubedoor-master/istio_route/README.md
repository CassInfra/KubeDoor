# Istio VirtualService 管理(全局VS管理)

页面:ISTIO管理 → 全局VS管理(`/istio/level1-vs`)。代码:

| 文件 | 内容 |
| --- | --- |
| `spec.py` | 纯逻辑:解析 / 渲染 spec、域名条件按集群拆分与展开、归一化哈希、规则对齐对比、规范检查、组矩阵 |
| `store.py` | 数据访问(asyncpg,经 `db.py` 连接池) |
| `handlers.py` | `/api/istio/*` 接口;调 agent 现有的列出 / 下发接口 |
| `../db.sql` | 第 6~8 节:`istio_*` 七张表 |
| `../tests/test_istio_spec.py` | 纯逻辑单测 |
| `../tests/test_istio_api.py` | 接口端到端测试(真实 PG + 假 agent,设置 `KUBEDOOR_TEST_PG_DSN` 才运行) |

## 模型

- **Istio 组**:一套业务的集群,如 `cassmall` = 共用的测试集群 + 各项目的生产集群。一个集群只属于一个组;
  组成员有类型(测试 / 生产)和备注,按列筛选、做覆盖检查用。
- **组标准 VS**:花折保存的那一份,按「组 + 命名空间 + 名称」唯一。**规则、默认路由、gateways 全组共用**,用户修改的就是它。
- **部署关系**:每个 VS 自己选择部署到组内哪些集群(测试环境往往有全部 VS,各项目的生产集群只有一部分);
  **hosts 和规则里的域名条件按集群各自维护**。没有部署关系 = 这个 VS 不部署到该集群。
- **集群现状**:采集到的各集群 VirtualService 原样快照(只读),只用于对比、纳管和状态判断,不直接修改。

下发到集群 X 的内容 = 组标准(gateways + 规则 + 默认路由)+ X 的 hosts + X 在各规则里的域名,由 master 生成,
前端不传 spec。agent 用 Server-Side Apply(force)写入,`spec.http` 整段替换。

### 域名条件按集群

规则里按域名区分的条件(`match` 里的 `authority`,以及写成 `headers.host` 的)取值都是该集群 hosts 里的域名,
各集群天然不同(本机房域名 `xxx-penglai` / `xxx-kunlun`、测试和生产的域名规律也不同),所以和 hosts 一样按集群保存:

- 组标准规则的 `match_rules` 是模板,域名条件的位置写成 `{"$domains": true}`(同一条目里的 uri / headers 等其他条件照常共用);
  相邻、除域名外条件相同的条目合成一个「域名块」。
- 各集群在每个域名块里要匹配的域名存在 `istio_vs_route_domain`(按域名块顺序,每块一组 `{field: authority|host, op, value}`),
  `field` / `op` 原样保留采集到的写法。下发到某集群时每个域名展开成一个 match 条目(条目之间是「或」)。
- 一律按集群存(各集群恰好相同也一样),纳入了该 VS 的集群都要设置,且至少匹配一个域名;
  没设置全的集群状态是「缺少域名」,下发预览直接报错、不能下发。
- 规则是否一致(版本 A / B)不算域名的值:只差域名的规则算同一版本;对比里标「只是域名不同」,不算规则差异。
- 纳管时各集群的域名取自己现状里对应的规则(按不算域名值的匹配条件对齐);对不上、集群里没有这个 VS 的,纳管后显示「缺少域名」。
- 以某集群现状更新组标准:该集群的域名取它的现状;其他集群保持原来对应规则的域名,新出现的规则取各自现状。
- 编辑:规则弹框里按已部署的集群从各自 hosts 里选域名,可以按关键字一次在各集群里选、从一个集群复制到另一个并替换机房标识;
  「编辑 hosts 和域名」弹框里改单个集群的 hosts 和它在各规则里的域名,可以从别的集群复制并替换(如 penglai → kunlun)。
- 规范检查:精确匹配的域名不在该集群 hosts 里的,标为规范问题(请求到不了这条规则)。

### 无损

采集只建模了 hosts、gateways、http 规则的 `name / match / rewrite / route|delegate / timeout` 和默认路由。
其他字段(`retries`、`corsPolicy`、`redirect`、`fault`、`mirror`、`tcp`、`tls`、`exportTo` 等)原样存进
`extra` / `df_extra` / `extra_spec`,渲染时原样带回,所以「采集 → 纳管 → 下发」不会丢配置。
页面上这些字段只读展示;规则编辑表单不认识的匹配条件、目标字段也原样保留。

- 默认路由 = 最后一条没有 match 的规则;中间没有 match 的规则保持原位,当普通规则处理。
- 归一化:省略和空值(`null` / `[]` / `{}`)视为相同,键顺序无关;列表顺序有意义(规则顺序决定命中)。

### 规则名称

用了重写就要有名称,没用重写就不要名称(名称用于访问日志里展示原始前缀)。新增 / 修改规则时后端强制校验,
启用重写时前端默认带出「uri 前缀去掉末尾 /」;采集来的不合规规则在矩阵和详情页标为「规范问题」。

### 状态

| 状态 | 判断 |
| --- | --- |
| 一致 `synced` | 集群现状 = 组标准 + 该集群的 hosts 和域名 |
| 待下发 `pending` | 不一致,但集群现状仍是上次下发(或纳管时)的内容:花折这边改了还没下发 |
| 不一致 `diff` | 集群现状不是上次下发的内容:有人直接改过集群,或纳管后从未下发且不同 |
| 集群中没有 `missing` | 有部署关系,但集群里没有这个 VS |
| 缺少域名 `incomplete` | 有规则的域名条件还没设置该集群的域名(或一个都没选),补上前不能下发 |
| 未采集 `unknown` | 该集群还没采集过现状 |
| 未纳入 `unbound` | 集群里有同名 VS,但组标准没有部署到该集群 |
| 未纳管 `unmanaged` | 还没有组标准,只是集群现状 |

比较用 `spec_hash`(归一化后整份 spec);各集群规则是否一致用 `rule_digest`(去掉 hosts,规则里域名的值也不算),
未纳管的 VS 按它把各集群分成版本 A / B / C(集群多的版本在前)。纳管时把各集群当时的现状哈希记为「上次下发」,
所以和组标准不同但集群没被动过的,显示「待下发」。

## 典型流程

1. **初始化整理**:建组并加入集群 → 刷新现状(并行采集各集群,只写各自的快照) → 一致的 VS 一键纳管
   (只差 hosts 和域名的也算一致,各集群的域名取自己的现状) →
   不一致的先对比,选基准集群纳管(或以某集群现状更新组标准) → 确认差异后下发拉齐。
2. **日常修改**:改组标准 → 相关集群变成「待下发」 → 先下发测试集群,验证后再下发生产集群。
   下发前预览的是集群最新现状 → 将要下发的差异。新增按域名区分的规则时,在规则弹框里给每个集群选域名。
3. **发现漂移**:刷新现状后出现「不一致」 → 对比 → 下发组标准覆盖回去,或以该集群现状更新组标准。
4. **部署到新集群**:「部署到此集群」填 hosts 和各规则的域名(可以从别的集群复制并替换机房标识)→ 下发。

删除组标准、不再部署到某集群,都只删花折里的数据,集群里的 VirtualService 不受影响。

## 接口

查看(GET)要登录,看不到的集群不返回(组里一个都看不到就 403);**所有修改只允许管理员**(`auth/policy.py`)。
浏览器不能经通用转发直接调 `/api/agent/istio/*`(master 的 `http_handler` 直接拒绝)。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/istio/groups` | 组列表;管理员额外返回所有集群及所属组 |
| POST / PUT / DELETE | `/api/istio/groups`(`?id=`) | 新建 / 修改 / 删除组。body:`{name, description, clusters:[{k8s_name, env_type, remark}]}`,顺序即显示顺序;移出组的集群,它的部署关系和现状一并删除 |
| GET | `/api/istio/matrix?group_id=` | 组矩阵:每行一个 VS,`cells` 按集群给出 hosts、状态、版本 |
| POST | `/api/istio/collect` | 刷新现状:`{group_id, k8s_names?}`,逐个集群返回成功 / 失败 |
| GET | `/api/istio/compare?group_id=&namespace=&name=&left=&right=` | 对比。一方是 `standard`(组标准,不含 hosts,域名显示 `<按集群>`)、`standard@集群`、`cluster@集群`(现状);`diff.rules_same` = 只差 hosts 和域名,规则行 `op=domains` = 只是域名不同 |
| POST | `/api/istio/import` | 纳管:`{group_id, items:[{namespace, name, base, clusters}]}`,以 base 集群现状生成组标准,各集群 hosts 和域名取自己的现状 |
| POST | `/api/istio/vs/reset` | 以某集群现状更新组标准的共用部分:`{vs_id, k8s_name}`,hosts 不变;域名见上文 |
| GET | `/api/istio/vs?vs_id=` | 组标准详情:共用部分、规则(`domains`:各集群的域名)、各集群的 hosts 和状态(`missing_domains` 缺哪些规则的域名,`live_domains` 现状里对应规则的域名)、规范检查 |
| POST | `/api/istio/vs` | 新建:`{group_id, namespace, name, gateways, df_*, clusters:[{k8s_name, hosts}]}`;集群里已有同名 VS 时要求用纳管 |
| PUT / DELETE | `/api/istio/vs?vs_id=` | 修改 gateways 和默认路由 / 删除组标准(名称、命名空间不可改) |
| PUT / DELETE | `/api/istio/vs/cluster` | 部署到某集群并设置 hosts:`{vs_id, k8s_name, hosts, domains?: {规则 id: [[域名]]}}` / 不再部署(`?vs_id=&k8s_name=`,该集群的域名一并删除) |
| POST | `/api/istio/httproute?vs_id=` | 新增规则;`priority` 不传追加到最后,传了检查是否占用;有域名块时带 `domains: {集群: [[域名]]}`,已纳入的集群都要给 |
| PUT / DELETE | `/api/istio/httproute?route_id=` | 修改(`priority` 不传保持原位置,未建模字段保留,各集群域名整体替换)/ 删除(删除后重排优先级) |
| POST | `/api/istio/httproute/reorder?vs_id=` | 优先级重排为 10、20、30…… |
| POST | `/api/istio/apply/preview` | `{items:[{vs_id, k8s_name}]}`:从集群取最新现状(顺便刷新快照),逐项给出现状、将要下发的 spec 和结构化差异;缺少域名的项给出 error |
| POST | `/api/istio/apply` | 下发:同一集群依次、不同集群并行;有缺少域名的项整个请求拒绝;成功的记下发人和内容哈希,完成后重新采集该集群现状;每项记审计 `istio.apply` |

## 测试

```bash
cd src/kubedoor-master
python -m pytest tests/test_istio_spec.py -q
# 接口端到端:需要一个可清空的测试库(会执行 db.sql 并清空 istio_* 表)
KUBEDOOR_TEST_PG_DSN=postgresql://postgres@127.0.0.1:5432/kubedoor_test python -m pytest tests/test_istio_api.py -q
```
