# Istio VirtualService 管理(全局VS管理)

页面:ISTIO管理 → 全局VS管理(`/istio/level1-vs`)。代码:

| 文件 | 内容 |
| --- | --- |
| `spec.py` | 纯逻辑:解析 / 渲染 spec、域名条件按集群拆分与展开、归一化哈希、规则对齐对比、从集群现状同步到组标准、规范检查、组的 VS 范围与跨组冲突、组矩阵 |
| `store.py` | 数据访问(asyncpg,经 `db.py` 连接池) |
| `handlers.py` | `/api/istio/*` 接口;调 agent 的列出 / 查看 / 下发 / 删除接口,改集群前备份、回退 |
| `../db.sql` | 第 6~8 节:`istio_*` 十张表(含早先「一个集群只属于一个组」结构的升级) |
| `../tests/test_istio_spec.py` | 纯逻辑单测 |
| `../tests/test_istio_api.py` | 接口端到端测试(真实 PG + 假 agent,设置 `KUBEDOOR_TEST_PG_DSN` 才运行) |

## 模型

- **Istio 组**:一个项目的集群 = 共用的测试集群 + 本项目的生产集群。**一个集群可以属于多个组**
  (多个项目共用的测试集群),组成员有类型(测试 / 生产)和备注,按列筛选、做覆盖检查用。
- **组的 VS 范围**:**同一个 VS 只能属于一个组**。组的包含 / 忽略规则决定哪些 VS 归本组,单个 VS 还可以手动忽略 /
  取消忽略,见下文「组的 VS 范围」。
- **组标准 VS**:花折保存的那一份,按「组 + 命名空间 + 名称」唯一。**规则、默认路由、gateways 全组共用**,用户修改的就是它。
- **部署关系**:每个 VS 自己选择部署到组内哪些集群(测试环境往往有全部 VS,各项目的生产集群只有一部分);
  **hosts 和规则里的域名条件按集群各自维护**。没有部署关系 = 这个 VS 不部署到该集群。
- **集群现状**:采集到的各集群 VirtualService 原样快照(只读,按集群存,各组共用),只用于对比、纳管和状态判断;
  采集状态(时间、采集人、VS 总数)在 `istio_cluster_state`。集群不在任何组里时,它的现状一并删除。

下发到集群 X 的内容 = 组标准(gateways + 规则 + 默认路由)+ X 的 hosts + X 在各规则里的域名,由 master 生成,
前端不传 spec。agent 用 Server-Side Apply(force)写入,`spec.http` 整段替换。

### 组的 VS 范围

- **包含 / 忽略规则**(`istio_group.include_patterns` / `ignore_patterns`):每条是通配符(`*` 任意多个字符,`?` 一个字符)
  或正则(全匹配);规则里有 `/` 时匹配「命名空间/名称」,否则只匹配名称。先看忽略规则(匹配就排除),
  包含规则为空表示全部,否则至少匹配一条。保存时校验正则;组管理里可以按已采集的现状预览规则的效果和冲突。
- **单个 VS 的手动决定**(`istio_group_vs_override`,优先于组规则):列表每行的「忽略」= `ignore`;
  「查看忽略」里取消忽略:组规则本来就纳入的删掉手动决定,被组规则排除的记为 `include`(单独纳入,不受规则影响)。
- 被忽略的(手动 + 规则排除)不显示、不能纳管和下发;已纳管的组标准保留,取消忽略后恢复。「查看忽略」列出全部,
  并给出组里的 VS 总数(组内各集群现状 + 组标准)、忽略数(手动 / 规则排除);手动忽略过、现状里已经没有的也列出来。
- **冲突**:本组的 VS,也在和本组共用集群的其他组里(那个组的组标准或任一集群现状里有,且没被它排除)。
  矩阵里整行标红,不能纳管和下发,要先在不该管它的组里忽略它。新建 VS 时同名的已在其他组里会拒绝;
  名称被本组规则排除的会提示,确认后创建并单独纳入。
- 采集照常拉取集群里的全部 VS(现状各组共用),按规则在展示和处理时过滤,所以改规则不用重新采集;
  刷新现状的结果里给出每个集群的总数、归本组的个数和被忽略的个数。

### 矩阵搜索

列表的搜索框不区分大小写,空格分开的多个关键字要全部命中;每个关键字命中 VirtualService 的「命名空间/名称」、
各集群的 hosts、规则内容中的任一处都算。规则内容是矩阵每行的 `search_lines`(`spec.search_lines`):组标准
(按当前用户能看的集群展开域名)和这些集群的现状里除 hosts 外的内容,一个字段一行「字段路径: 值」并去重。
http 规则从规则里的字段写起(`match.uri.prefix: /api/`、`route.destination.port.number: 8080`、`timeout: 30s`),
其余字段带上字段名(`gateways: ...`、`tcp.route.destination.host: ...`),数组不带下标,所以字段名
(retries、mirror、fault…)和值都能搜;单个值最多保留 300 个字符。规则里命中的行显示在名称下面
(命中关键字多的在前),前端的匹配和高亮在 `views/istio/level1-vs/search.ts`。

### 查看集群里的 VS

矩阵格子菜单的「查看现状」、详情页集群列表的「查看现状」:实时调 agent 的 `GET /api/agent/istio/vs/detail`
取集群里的完整对象(和 `kubectl get -o yaml` 一样,不是采集的快照),去掉 `metadata.managedFields` 和
`kubectl.kubernetes.io/last-applied-configuration` 注解后按 YAML 显示。要有该集群的读权限。
这个 agent 接口也要新版 agent;旧版 agent 退回用列表接口拼出来,只有 spec(页面提示升级)。

矩阵格子右上角的时钟图标:从这里下发过的,鼠标移上去显示最近一次下发的时间(北京时间)和下发人。

### 删除集群里的 VS

矩阵格子菜单的「删除」、详情页「更多 → 从集群删除」:调 agent 的 `DELETE /api/agent/istio/vs/delete` 立即删除。
删除前从集群取最新内容先备份(误删可以用下面的「回退」恢复),也记进审计 `istio.delete`(`detail.spec`);
本组标准部署到了该集群的,同时不再部署(删掉该集群的 hosts 和域名),免得下次下发又建回来。这个接口要新版 agent
(只换镜像),旧版 agent 会提示先升级。

### 下发前备份与回退

花折改集群里的 VirtualService 之前(下发、回退、删除),都先从集群取最新内容备份进 `istio_vs_backup`。详情页
「集群与 Hosts」每个集群后面的「回退」列出这个集群的备份(备份时间、执行人、是哪个操作之前的内容,新的在前),
选一份看过它和集群现状的差异后写回集群。

- 按集群里的对象(集群 + 命名空间 + 名称)保存,不挂在组标准上:组标准删了重建、换了组,备份还在;
  集群不在任何组里时一并删除。每个对象只留最近 5 份(`store.BACKUP_KEEP`),写入时清理。
- 存的是 spec(下发只改 spec,labels / annotations 不归花折管);当时集群里还没有的记为空,回退就是从集群删除。
- 下发:每个集群下发前取一次最新现状,取不到就不下发这个集群;内容不会变的项不备份,免得反复下发把有用的备份挤掉。
  agent 明确回了失败(集群没变)就删掉这份备份;等待超时、连接断开这类结果不明的保留。
- 回退:集群现状和那份备份一样时只提示不需要回退。回退前同样先备份当前内容,所以回退也能撤销;写回和下发一样用
  Server-Side Apply,备份是空的就调删除接口(要新版 agent)。结果不明时,要回退到的那份这次先不清理,万一没生效还能再回退。
- 回退只改集群,不改组标准:之后这个集群的状态是「不一致」,下次下发会再用组标准覆盖。最近一次下发之后又回退过、
  集群里也不是下发的内容时,详情页「最近下发」列显示谁、什么时候回退的。
- 删除后回退恢复的,本组标准已经不再部署到该集群,恢复后状态是「未纳入」。
- 审计 `istio.rollback`:`detail` 里有回退到的那份备份(`backup_id`、`backup_at`、`backup_by`、`backup_action`)
  和回退前集群里的内容 `spec`。

### 从集群现状同步到组标准

对比时一边是组标准(`standard` 或 `standard@集群`)、一边是集群现状(`cluster@集群`),管理员可以把现状里的内容
整条同步到组标准 —— 只能从现状到组标准(组标准只有一份,各集群的现状可能各不相同)。在要同步的规则后面点按钮,
可以选多处、可以撤销,选好后「保存组标准」一次写入(保存前确认改了什么、哪些集群会缺少域名);下发后才在集群里生效。

- 规则按对比的对齐结果整条处理:只有现状有的加入组标准(放在现状里的位置),两边都有但不同的用现状替换,
  只有组标准有的从组标准删除(其他集群下发时也会删掉),只是顺序不同的按现状调整顺序;没选的规则内容和顺序都不变。
- gateways、默认路由、其他字段(`extra_spec`)不同时也能同步,现状里没有的就是从组标准删掉;hosts 各集群独立,不同步。
- 同步来的规则里的域名:来源集群用现状里的;其他已纳入的集群先找组标准里原来对应的规则(匹配条件不算域名、转发目标都一样),
  找不到再取该集群的现状,都没有就留空,状态「缺少域名」。
- 「组标准 · 集群」和该集群的现状对比时,存的域名和现状不一样的规则显示「只是域名不同」,也可以同步(换成现状里的域名)。
- 选的是对比时的对齐结果:对比接口给两边返回 `version`(内容哈希),保存时有一边变了就拒绝(409),刷新对比后重新选。
- 对比里展开规则是左右逐行对照:两边按行对齐,不同的行标红、行内不同的字加深,一边没有的行显示占位;
  默认路由等较长的内容,离不同的行较远的相同行折叠起来。

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
- 纳管时各集群的域名取自己现状里对应的规则:匹配条件完全一样的直接对上;只是域名不同的,要不算域名值的匹配条件一样、
  转发目标(destination,不算权重)也一样才算同一条 —— 只有域名条件的几条规则去掉域名后一模一样,不看转发目标就只能按位置对,
  某个集群少一条时域名会对到别的规则上。对不上、集群里没有这个 VS 的,纳管后显示「缺少域名」。
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

### 域名重复检查

同一个集群里的域名不能重复:多个 VirtualService 的 hosts 不能重复,同一个 VirtualService 里不同规则的域名条件
(`authority`、`headers.host`)也不能重复。纯逻辑在 `spec.py`(`hosts_index`、`domain_issues`、`apply_domain_issues`)。

- hosts:同一个集群里的 VirtualService 都算 —— 本组的、其他组的、被忽略的、没纳管的。本组部署了的按组标准要下发的
  hosts 算(改了还没下发的也按改后的),其他的按集群现状(采集的快照)算;不区分大小写。
  gateway 不同的也报,提示里标「gateway 不同」(没写 gateways 的算 `mesh`,没写命名空间的 gateway 补上 VirtualService 的命名空间)。
- 规则域名:不分 `authority` / `headers.host` 写法和 exact / prefix / regex,值一样(不区分大小写)就算重复;
  默认路由和还没设置的域名不算,同一条规则里的几个 match 条目重复不算(条目之间是「或」)。
  后面的规则对这个域名不会生效的(它匹配该域名的每个条目,前面都有规则对这个域名不带其他条件,或其他条件和它一样)另外标出来;
  同一个域名按不同路径分到几条规则(如 `/a`、`/b`、兜底)也会报,但不标「不会生效」。
- 什么时候检查:
  - 矩阵:随矩阵接口一起算(几十个集群、上百个 VirtualService 也只要几十毫秒,不另开后台任务),名称旁标「域名重复」,
    鼠标移上去看明细;顶部汇总个数,筛选里有「域名重复」。部署了的按要下发的内容查,未纳管的按现状查。
  - 详情页:顶部列出部署到的各集群里重复的域名。
  - 下发预览:按「列表里的都下发之后」逐项检查,标出是这次下发带来的(集群里原来没有),还是集群里原有的;
    勾选的项里有这次下发带来的,确认下发时再列出来。**只提示、不拦截**:按路径分流是常见写法,
    把 host 从一个 VirtualService 挪到另一个、只先下发了新的那个时也会短暂重复。
- 提示里相同的内容合并成一条,前面列出集群(规则是组内共用的,各集群一样的问题只说一遍)。
- 局限:只比较写法相同的值,通配 host(`*.example.com`)和具体域名、prefix / regex 匹配范围的重叠不判断;
  其他组改了还没下发的 hosts 不算,只算集群现状;集群现状以最近一次采集为准,集群里刚改过的先刷新现状。

### 状态

| 状态 | 判断 |
| --- | --- |
| 一致 `synced` | 集群现状 = 组标准 + 该集群的 hosts 和域名 |
| 待下发 `pending` | 不一致,但集群现状仍是上次下发(或纳管时)的内容:花折这边改了还没下发 |
| 不一致 `diff` | 集群现状不是上次下发的内容:有人直接改过集群、回退过,或纳管后从未下发且不同 |
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
   下发后发现有问题:详情页该集群的「回退」→ 选「下发前」那份回退,改好组标准再下发。
3. **发现漂移**:刷新现状后出现「不一致」 → 对比 → 下发组标准覆盖回去,或把现状里的规则挑着同步到组标准(全部要就以该集群现状更新组标准)。
4. **部署到新集群**:「部署到此集群」填 hosts 和各规则的域名(可以从别的集群复制并替换机房标识)→ 下发。

5. **多个项目共用测试集群**:每个项目一个组,都加入共用的测试集群,用包含 / 忽略规则划分 VS(如 b2c 组包含 `b2c*`,
   另一个组忽略 `b2c*`)→ 刷新现状,红色的冲突行在不该管它的组里忽略 → 各组分别纳管、下发。

删除组标准、不再部署到某集群、忽略,都只动花折里的数据,集群里的 VirtualService 不受影响;
要删集群里的 VS 用格子菜单的「删除」。

## 接口

查看(GET)要登录,看不到的集群不返回(组里一个都看不到就 403);**所有修改只允许管理员**(`auth/policy.py`)。
浏览器不能经通用转发直接调 `/api/agent/istio/*`(master 的 `http_handler` 直接拒绝)。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/istio/groups` | 组列表(含包含 / 忽略规则);管理员额外返回所有集群,以及各自已在哪些组里 |
| POST / PUT / DELETE | `/api/istio/groups`(`?id=`) | 新建 / 修改 / 删除组。body:`{name, description, clusters:[{k8s_name, env_type, remark}], include_patterns, ignore_patterns}`,规则每条 `{type: glob\|regex, pattern}`,集群顺序即显示顺序;移出组的集群,它在本组的部署关系删除,不再属于任何组的集群,现状和备份也删除 |
| GET | `/api/istio/groups/preview?group_id=&clusters=&include=&ignore=` | 组管理里预览规则(`include` / `ignore` 是 JSON 数组):按所选集群已采集的现状,每个 VS 归本组还是被忽略、和哪些组冲突;不保存 |
| GET | `/api/istio/matrix?group_id=` | 组矩阵:只列本组范围内的 VS,`cells` 按集群给出 hosts、状态、版本、最近一次下发的时间和下发人,`conflicts` 冲突的组,`search_lines` 搜索用的规则内容(见「矩阵搜索」),`domain_issues` 重复的域名(见「域名重复检查」,每条前面是集群);另给 `ignored`(被忽略的 VS 和原因)和 `stats`(总数、忽略数) |
| POST | `/api/istio/collect` | 刷新现状:`{group_id, k8s_names?}`,逐个集群返回成功 / 失败,以及 VS 总数、归本组的个数、被忽略的个数 |
| POST | `/api/istio/ignore` / `/api/istio/unignore` | `{group_id, items:[{namespace, name}]}`:在本组忽略 / 取消忽略(被组规则排除的取消忽略后单独纳入) |
| GET | `/api/istio/live?group_id=&k8s_name=&namespace=&name=` | 查看集群里 VS 的现状(实时取完整对象,见上文):`{object, full}`,`full=false` 是旧版 agent,只有 spec |
| POST | `/api/istio/live/delete` | `{group_id, k8s_name, namespace, name}`:删除集群里的 VS(删除前先备份),见上文;审计 `istio.delete` |
| GET | `/api/istio/live/backups?group_id=&k8s_name=&namespace=&name=` | 集群里这个 VirtualService 的备份,新的在前:`{keep, live, live_error, backups:[{id, spec, exists, resource_version, action, created_by, created_at, same}]}`。`action` 是备份之后做的操作(`apply` / `rollback` / `delete`),`spec` 为空表示当时集群里还没有;`live` 是实时取的集群现状,取不到时为空、`live_error` 说明原因,`same` = 和现状一样。要有该集群的读权限 |
| POST | `/api/istio/live/rollback` | `{group_id, k8s_name, namespace, name, backup_id}`:把集群里的 VirtualService 回退到这份备份(见上文),只改集群;审计 `istio.rollback` |
| GET | `/api/istio/compare?group_id=&namespace=&name=&left=&right=` | 对比。一方是 `standard`(组标准,不含 hosts,域名显示 `<按集群>`)、`standard@集群`、`cluster@集群`(现状);`diff.rules_same` = 只差 hosts 和域名,规则行 `op=domains` = 只是域名不同、`target` = 转发目标;两边的 `version` 是内容哈希,同步到组标准时用 |
| POST | `/api/istio/import` | 纳管:`{group_id, items:[{namespace, name, base, clusters}]}`,以 base 集群现状生成组标准,各集群 hosts 和域名取自己的现状 |
| POST | `/api/istio/vs/sync/preview`、`/api/istio/vs/sync` | 从集群现状同步到组标准(见上文):`{group_id, namespace, name, standard: standard\|standard@集群, k8s_name, rules:[{standard_index, live_index}], gateways, default, extra, standard_version, live_version}`。`rules` 是对比里要同步的规则行(两边的序号,从 0 开始),`*_version` 是对比接口两边的 `version`;preview 不保存,返回保存后的组标准(和对比里组标准一侧一样显示)、改动统计、会缺少域名的集群 |
| POST | `/api/istio/vs/reset` | 以某集群现状更新组标准的共用部分:`{vs_id, k8s_name}`,hosts 不变;域名见上文 |
| GET | `/api/istio/vs?vs_id=` | 组标准详情:共用部分、规则(`domains`:各集群的域名)、各集群的 hosts 和状态(`missing_domains` 缺哪些规则的域名,`live_domains` 现状里对应规则的域名,`backup_count` 回退用的备份份数,`rolled_back` 下发之后又回退过时是谁、什么时候)、规范检查、`domain_issues` 部署到的集群里重复的域名(按要下发的内容查) |
| GET | `/api/istio/vs/check?group_id=&namespace=&name=` | 新建前检查:本组是否已有组标准、集群里是否已有同名的、其他组里是否已有、名称是否被本组排除 |
| POST | `/api/istio/vs` | 新建:`{group_id, namespace, name, gateways, df_*, clusters:[{k8s_name, hosts}], force_include?}`;集群里已有同名 VS 时要求用纳管,其他组里已有的拒绝;名称被本组排除的要 `force_include`(单独纳入) |
| PUT / DELETE | `/api/istio/vs?vs_id=` | 修改 gateways 和默认路由 / 删除组标准(名称、命名空间不可改) |
| PUT / DELETE | `/api/istio/vs/cluster` | 部署到某集群并设置 hosts:`{vs_id, k8s_name, hosts, domains?: {规则 id: [[域名]]}}` / 不再部署(`?vs_id=&k8s_name=`,该集群的域名一并删除) |
| POST | `/api/istio/httproute?vs_id=` | 新增规则;`priority` 不传追加到最后,传了检查是否占用;有域名块时带 `domains: {集群: [[域名]]}`,已纳入的集群都要给 |
| PUT / DELETE | `/api/istio/httproute?route_id=` | 修改(`priority` 不传保持原位置,未建模字段保留,各集群域名整体替换)/ 删除(删除后重排优先级) |
| POST | `/api/istio/httproute/reorder?vs_id=` | 优先级重排为 10、20、30…… |
| POST | `/api/istio/apply/preview` | `{items:[{vs_id, k8s_name}]}`:从集群取最新现状(顺便刷新快照),逐项给出现状、将要下发的 spec 和结构化差异;不能下发的项(在本组被忽略、和其他组冲突、缺少域名)给出 error;`domain_issues: [{text, new}]` 是列表里的都下发之后这一项重复的域名,`new` = 集群里原来没有、是这次下发带来的 |
| POST | `/api/istio/apply` | 下发:同一集群依次、不同集群并行;有不能下发的项整个请求拒绝;每个集群先取最新现状备份(取不到就不下发这个集群,内容不会变的项不备份);成功的记下发人和内容哈希,完成后重新采集该集群现状;每项记审计 `istio.apply` |

## 测试

```bash
cd src/kubedoor-master
python -m pytest tests/test_istio_spec.py -q
# 接口端到端:需要一个可清空的测试库(会执行 db.sql 并清空 istio_* 表)
KUBEDOOR_TEST_PG_DSN=postgresql://postgres@127.0.0.1:5432/kubedoor_test python -m pytest tests/test_istio_api.py -q
```
