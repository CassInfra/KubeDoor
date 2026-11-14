# K8S 事件告警规则(开发说明)

使用说明(页面操作、规则语法、默认规则、试运行、排查)见 [help/K8S事件告警规则配置说明.md](../../../help/K8S事件告警规则配置说明.md)。本文说明代码结构和约定。

## 规则从哪里来

- 规则在 Web「系统管理 → 事件告警规则」配置(界面配置 / JSON 两种模式),存 PostgreSQL 的 `auth_settings`,key 为 `event_alert_rules`,value 的结构与以前的 `alert_rules.json` 相同:`{"global_ignore_rules": [...], "alert_rules": [...]}`。
- 库里没有这一行时用内置默认规则 `default_alert_rules.json`(随版本更新),不写库;页面上显示「当前使用内置默认规则」。
- 保存后马上生效,不用重启 master。以前的 ConfigMap `kubedoor-master-file-cfg`(挂载到 `/k8s_event/rules/`)已不再使用,升级时由 `import_legacy_file` 导入一次。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `rules_schema.py` | 纯函数:字段、运算符、保存时的严格校验(`check` / `validate`,错误信息指明位置)、导入旧文件时的宽松结构检查、审计摘要 |
| `rules_store.py` | 内存快照:`current()` 返回只读的 `RuleSet`(规则冻结成 `MappingProxyType` / `tuple`);`refresh()` 从库加载(库里没有用默认规则,读库失败保留旧快照),`apply()` 保存后立即替换,`refresh_loop()` 每 10 秒刷新 |
| `alert_rule_matcher.py` | 匹配引擎:`first_match` / `rule_matches` / `match_field_condition` 是纯函数;`AlertRuleMatcher` 每次匹配读一次当前快照(也可以传固定的 `RuleSet`,试运行用) |
| `event_alert_processor.py` | 处理一个事件:DELETED 跳过 → `evaluate`(忽略规则 + 告警规则用同一份快照)→ 标记「已告警」→ 去重 → `notify`。去重窗口每次取 `general_settings.value('alert_dedup_window')`,检查和记录在同一把锁里 |
| `rules_api.py` | 接口、旧文件导入、启停(见下) |
| `default_alert_rules.json` | 内置默认规则(取自原来 setup 下的 `alert_rules.json`,逐字一致) |

- 页面保存后替换快照,**不重建** `EventAlertProcessor`:去重记录和统计在处理器上,保存规则后照常保留。
- `rules_api.on_startup` 必须排在建表(`init_db_and_schema`)之后:导入旧文件(失败只记 error)→ 加载快照 → 建好事件处理器 → 启动刷新任务。不再等第一条事件才加载规则。
- 快照还没加载成功之前(单测、启动时库不可用)匹配器用内置默认规则。

## 匹配语义

与以前读规则文件时完全一致(`tests/test_event_rules_matcher.py` 把旧实现原样拷贝过来随机对照):

- `eventStatus == "DELETED"` 的事件不进入规则。
- 忽略规则之间是 OR:任一启用的忽略规则命中即忽略;告警规则按数组顺序,第一条启用且命中的生效。不写 `enabled` 视为启用。
- 一条规则里各字段的条件是 AND;同一字段的多个值:`contains` / `starts_with` / `ends_with` 是 OR,`not_*` 是 AND。字符串比较不区分大小写,`contains` 是子串匹配。
- 12 个运算符:`contains`、`not_contains`、`starts_with`、`not_starts_with`、`ends_with`、`not_ends_with`、`equals`、`not_equals`、`greater_than`、`less_than`、`greater_equal`、`less_equal`(后 4 个只对 `count` 有效)。

下面这些宽松的情况在页面保存时会被严格校验拒绝,只可能出现在从旧文件原样导入、还没在页面上保存过的规则里,匹配时保持旧行为:

- 一个字段写了多个运算符时只认第一个(按上面的顺序);不认识的运算符视为成立;数值运算符写在 `count` 以外的字段上视为成立。
- 列表型运算符的值写成单个值时等同于只有一个元素的数组。
- 事件里没有这个字段(字段名写错)时,`not_contains` / `not_starts_with` / `not_ends_with` 视为成立,其它不成立。
- 数值运算符的值不是数字时不成立(日志「无法将count字段转换为数值」)。
- 快照里不是对象的规则、不是数组的分组在匹配时跳过;条件不是对象时不成立(以前会抛异常)。

## 保存时的校验

`rules_schema.check` 与 Web 的 `views/system/event-alert-rules/rules-utils.ts` 的 `checkRules` 逐字一致,两边共用用例 `tests/fixtures/event_rules_cases.json`(改校验时用 `check` 重新生成 `errors` / `normalized` 并逐条核对)。规则:

- 顶层只能有两个键,两个都是数组,每组最多 200 条;每条规则是对象,只能有 `name`、`enabled`、`severity`(只有告警规则)、`conditions`。
- `name` 必填(去掉首尾空白后非空,最长 100);`enabled` 是布尔(不写补 `true`);`severity` ∈ critical / warning / info(不写补 `warning`)。
- `conditions` 至少一个;字段在 14 个事件字段内;每个字段恰好一个已知运算符;数值运算符只能用于 `count` 且值是数字(不能是布尔、NaN、Infinity)。
- 列表型运算符的值是非空字符串数组,不含空串和只有空格的值,最多 200 个、每个最长 1000 字符;`equals` / `not_equals` 的值是字符串(可以为空)或数字。
- 错误信息带位置,例如:`告警规则第 3 条「重启事件告警」的条件 count：greater_than 的值必须是数字`;每处问题还带 `path`(在规则 JSON 里的位置),页面用它把问题标在卡片或 JSON 的行上。

## 接口(都在 `/api/admin/` 下,系统管理员)

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/event-alert-rules` | `{"rules", "source": "database"\|"default", "updated_by", "updated_at"}`;库里的规则原样返回(可能不符合严格校验) |
| `PUT /api/admin/event-alert-rules` | 请求 `{"rules": {...}}`,严格校验后保存并立即替换快照,响应同 GET;不合法 422 `invalid_rules`(`message` 是第一处问题,`errors` 是全部问题);审计 `event_rules.update`,detail 只记每组条数和名称 |
| `GET /api/admin/event-alert-rules/default` | `{"rules": 内置默认规则}`;页面「恢复默认」把它载入编辑区,保存后才生效(不删库里的这一行) |
| `POST /api/admin/event-alert-rules/preview` | 试运行:请求 `{"rules", "hours": 1~168(默认 24), "env": 可选}`,规则先过严格校验;按 `lasttimestamp` 倒序取最近的事件(最多 10000 条),匹配在线程里跑,不发通知、不改事件 |

试运行把库里的行还原成实时处理时的事件:时间换成北京时间(不带时区);库里只有最新状态,`DELETED` 按删除前的状态算(次数大于 1 当成 MODIFIED,否则 ADDED),级别「已告警」按 Warning 算,两种情况的条数分别在 `deleted`、`alerted_level` 里返回。每条忽略规则的 `hits` 是按顺序第一条命中、由它忽略的事件数;每条告警规则的 `hits` 是第一条命中的事件数,`shadowed` 是也能命中但被前面的规则先命中的事件数。

## 旧规则文件导入

`rules_api.import_legacy_file(path='/k8s_event/rules/alert_rules.json') -> str`,master 启动时和 `python3 -m general_settings migrate` 都会调用,幂等:

- 库里已有 `event_alert_rules` → `'present'`(不读文件);文件不存在 → `'absent'`;
- 文件读不了、不是 JSON(含 NaN / Infinity)、超过 2MB、顶层不是对象、两个键存在但不是数组 → `'invalid'`(不导入);
- 否则原样写库(`INSERT ... ON CONFLICT (key) DO NOTHING`,`updated_by='legacy-import'`)→ `'imported'`;并发时别人先写了 → `'present'`;
- 不做严格校验(保持用户原来的规则行为);每次调用都现读文件;读写数据库失败时抛异常。
- 接口没有删除这一行的操作,所以导入过一次(或页面保存过一次)之后,旧文件还挂着也不会再导回来。
