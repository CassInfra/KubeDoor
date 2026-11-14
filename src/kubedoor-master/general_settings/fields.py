"""通用配置项的定义、校验和地址工具（纯函数，不访问数据库）。

存储格式见 service.py：auth_settings 里 key=general_settings 这一行，value 是平铺的 JSON 对象，只存显式设置过的项，
没存的项用默认值。三个可能带「账号:密码@」的地址（查询地址、远程写地址、对象存储上传地址）加密后存字符串。

镜像相关默认值取 agent_install.bundle.BASE（bundle.py 只依赖标准库和 yaml，不 import master 的其他模块）。
这里也不能 import 会读通用配置的模块：auth.store 等在顶层 import general_settings，反过来会循环 import。
"""

import math
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from agent_install import bundle

# 页面回显时密码换成它；提交回来时换回已保存的密码（与安装向导一致）
PASSWORD_MASK = '******'
# 存库的值里记录「已经从旧环境变量导入过」的项（legacy.py 写，页面看不到、也提交不了）：导入过的项以后在页面上
# 删掉（恢复默认 / 清空），master 重启时不会再从还留着的环境变量（例如 Secret kubedoor-connection）导入回来
IMPORTED_MARK = '_legacy_imported'
MAX_URL = 2048
MAX_INPUT = 4096  # 字符串输入超过这个长度直接拒绝（具体上限按项再判断）

PROM_TYPES = {
    'Victoria-Metrics-Single': 'VictoriaMetrics 单机版',
    'Victoria-Metrics-Cluster': 'VictoriaMetrics 集群版',
    'Prometheus': 'Prometheus',
}
COOKIE_SECURE = ('auto', 'true', 'false')
GROUPS = ('时序数据库', '访问地址', '对象存储', 'Agent 安装', '节点终端', '数据保留', '登录与安全', '高级')

_TAG_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]*')
_INTEGER = re.compile(r'[+-]?\d{1,12}')
_NUMBER = re.compile(r'[+-]?(?:\d{1,12}(?:\.\d{0,12})?|\.\d{1,12})')
_IMAGE_FORBIDDEN = frozenset('\'"`\\')


@dataclass(frozen=True)
class Field:
    """一个配置项。kind:enum / url / tag_key / repo / image / int / number / bool"""
    key: str
    label: str  # 中文名称（错误说明、日志都用它）
    group: str  # 页面分组
    kind: str
    default: object
    env: str = ''  # 旧环境变量（迁移用）
    secret: bool = False  # 加密存储（可能带账号密码的地址）
    choices: tuple = ()
    minimum: float = None
    maximum: float = None
    above_minimum: bool = False  # 必须大于 minimum（不能等于）
    max_length: int = 0
    credentials: bool = False  # 地址可以带「账号:密码@」
    base: bool = False  # 地址只有协议、主机和端口
    query: bool = False  # 地址可以带查询参数
    negative_as_zero: bool = False  # 迁移：旧代码把负数当成 0（不清理 / 不限），按 0 导入

    @property
    def subject(self):
        """错误说明开头用的名称：英文结尾的名称后面补一个空格（「Cookie Secure 只能选…」）"""
        last = self.label[-1:]
        return f'{self.label} ' if last.isascii() and last.isalnum() else self.label


def _url(key, label, group, env, **options):
    return Field(key, label, group, 'url', '', env=env, max_length=MAX_URL, **options)


def _days(key, label, env, default):
    return Field(key, label, '数据保留', 'int', default, env=env, minimum=0, maximum=3650, negative_as_zero=True)


def _image(key, label, env, default, max_length=256):
    return Field(key, label, 'Agent 安装', 'image', default, env=env, max_length=max_length)


_FIELDS = (
    # ---- 时序数据库 ----
    Field('prom_type', '时序库类型', '时序数据库', 'enum', 'Victoria-Metrics-Single', env='PROM_TYPE',
          choices=tuple(PROM_TYPES)),
    # 后面要拼 /api/v1/query 等路径，带查询参数的地址拼出来不能用，所以不允许查询参数
    _url('prom_url', '查询地址', '时序数据库', 'PROM_URL', secret=True, credentials=True),
    # vmagent 的远程写地址可以带查询参数（例如 extra_label），Agent 安装会把它放进 Secret
    _url('metrics_write_url', '远程写地址', '时序数据库', 'METRICS_WRITE_URL', secret=True, credentials=True,
         query=True),
    Field('prom_k8s_tag_key', '集群指标标签 key', '时序数据库', 'tag_key', 'origin_prometheus',
          env='PROM_K8S_TAG_KEY', max_length=128),
    # ---- 访问地址 ----
    _url('kubedoor_exturl', 'KubeDoor 访问地址', '访问地址', 'KUBEDOOR_EXTURL', base=True),
    # kubedoor-alarm 拼成 <地址>/#/alerts?...，不能带查询参数和锚点
    _url('alertmanager_exturl', 'Alertmanager 外部地址', '访问地址', 'ALERTMANAGER_EXTURL'),
    # ---- 对象存储：pod-mgr 拼成 <地址>/<集群>/dump/<文件> 上传 ----
    _url('oss_url', '对象存储上传地址', '对象存储', 'OSS_URL', secret=True, credentials=True),
    # ---- Agent 安装（旧值来自 AGENT_INSTALL_DEFAULTS；旧脚本 kubedoor.conf 里的同名变量也认）----
    Field('image_repo', '镜像仓库', 'Agent 安装', 'repo', bundle.BASE['image_repo'], env='IMAGE_REPO', max_length=256),
    _image('image_vmagent', 'vmagent 镜像', 'IMAGE_VMAGENT', bundle.BASE['image_vmagent']),
    _image('image_kube_state_metrics', 'kube-state-metrics 镜像', 'IMAGE_KUBE_STATE_METRICS',
           bundle.BASE['image_kube_state_metrics']),
    _image('image_node_exporter', 'node-exporter 镜像', 'IMAGE_NODE_EXPORTER', bundle.BASE['image_node_exporter']),
    # ---- 节点终端：空 = 用 Agent 内置镜像；长度上限与 agent func_manager/pod_exec.py 的 MAX_IMAGE_LEN 一致 ----
    Field('node_shell_image', '节点终端镜像', '节点终端', 'image', '', env='NODE_SHELL_IMAGE', max_length=512),
    # ---- 数据保留：0 = 不清理 ----
    _days('retention_events_days', 'K8S 事件保留天数', 'RETENTION_EVENTS_DAYS', 90),
    _days('retention_alerts_days', '指标告警保留天数', 'RETENTION_ALERTS_DAYS', 365),
    _days('retention_resources_days', '高峰期资源保留天数', 'RETENTION_RESOURCES_DAYS', 365),
    _days('retention_audit_days', '审计日志保留天数', 'RETENTION_AUDIT_DAYS', 180),
    _days('retention_ai_checkpoint_days', 'AI 对话检查点保留天数', 'RETENTION_AI_CHECKPOINT_DAYS', 7),
    # ---- 登录与安全 ----
    Field('auth_session_hours', '登录会话有效期（小时）', '登录与安全', 'number', 12.0, env='AUTH_SESSION_HOURS',
          minimum=0, maximum=720, above_minimum=True),
    # 0 = 不限空闲时间（auth/store.py 一直是这个语义：不大于 0 不做空闲超时）
    Field('auth_idle_hours', '空闲超时（小时）', '登录与安全', 'number', 4.0, env='AUTH_IDLE_HOURS',
          minimum=0, maximum=720, negative_as_zero=True),
    Field('auth_remember_days', '「记住我」有效天数', '登录与安全', 'number', 7.0, env='AUTH_REMEMBER_DAYS',
          minimum=0, maximum=365, above_minimum=True),
    Field('auth_login_max_failures', '登录失败锁定次数', '登录与安全', 'int', 5, env='AUTH_LOGIN_MAX_FAILURES',
          minimum=1, maximum=100),
    Field('auth_lock_minutes', '锁定时长（分钟）', '登录与安全', 'int', 15, env='AUTH_LOCK_MINUTES',
          minimum=1, maximum=1440),
    Field('auth_cookie_secure', 'Cookie Secure', '登录与安全', 'enum', 'auto', env='AUTH_COOKIE_SECURE',
          choices=COOKIE_SECURE),
    Field('agent_auth_required', 'Agent 连接必须带账号', '登录与安全', 'bool', True, env='AGENT_AUTH_REQUIRED'),
    # ---- 高级 ----
    Field('alert_dedup_window', '事件告警去重窗口（秒）', '高级', 'int', 300, env='ALERT_DEDUP_WINDOW',
          minimum=0, maximum=86400),
    Field('silence_cache_ttl', '告警屏蔽规则缓存（秒）', '高级', 'int', 15, env='SILENCE_CACHE_TTL',
          minimum=1, maximum=3600),
    Field('admis_cache_ttl', '准入查询缓存（秒）', '高级', 'int', 60, env='ADMIS_CACHE_TTL', minimum=0, maximum=3600),
)

FIELDS = {field.key: field for field in _FIELDS}
DEFAULTS = {field.key: field.default for field in _FIELDS}
LABELS = {field.key: field.label for field in _FIELDS}
SECRET_KEYS = tuple(field.key for field in _FIELDS if field.secret)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def is_blank(raw):
    """None、空串或只有空白：页面提交时表示「删掉这一项（恢复默认 / 未配置）」"""
    return raw is None or (isinstance(raw, str) and not raw.strip())


def clean(key, raw, previous=None):
    """校验并规范化一项的值（调用方先用 is_blank 判断「删除」），返回存库前的明文。

    不合法抛 ValueError：中文说明是哪一项、什么问题，不带提交的值（地址里可能有密码）。
    previous：加密地址已保存的明文，用来把提交里的密码 ****** 换回已保存的密码；None 表示不替换（旧配置迁移）。
    数字项接受数字或数字字符串，布尔项只接受布尔。未知 key 抛 KeyError。
    """
    field = FIELDS[key]
    if field.kind == 'bool':
        if not isinstance(raw, bool):
            raise ValueError(f'{field.subject}只能是 true 或 false')
        return raw
    if field.kind in ('int', 'number'):
        return _check_range(field, _parse_number(field, raw))
    if not isinstance(raw, str):
        raise ValueError(f'{field.subject}必须是字符串')
    text = raw.strip()
    if len(text) > MAX_INPUT:
        raise ValueError(f'{field.subject}太长')
    if field.kind == 'enum':
        return _clean_choice(field, text)
    if field.kind == 'url':
        return _clean_url(field, text, previous)
    if field.kind == 'tag_key':
        if len(text) > field.max_length or not _TAG_KEY.fullmatch(text):
            raise ValueError(f'{field.subject}只能包含字母、数字和下划线，不能以数字开头，'
                             f'最长 {field.max_length} 个字符')
        return text
    if field.kind == 'repo':
        value = text.rstrip('/')
        if not _REPO.fullmatch(text) or not value or len(value) > field.max_length:
            raise ValueError(f'{field.subject}只能包含字母、数字和 . _ : / -，以字母或数字开头，'
                             f'最长 {field.max_length} 个字符')
        return value
    if field.kind == 'image':
        if not text:
            raise ValueError(f'请填写{field.label}')
        if len(text) > field.max_length or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                                               or ch in _IMAGE_FORBIDDEN for ch in text):
            raise ValueError(f'{field.subject}不能包含空白、引号、反斜杠或控制字符，最长 {field.max_length} 个字符')
        return text
    raise ValueError(f'{field.subject}的类型未知')  # pragma: no cover - 定义表里没有别的 kind


def _clean_choice(field, text):
    for choice in field.choices:
        if text.lower() == choice.lower():
            return choice
    if field.key == 'prom_type':
        options = '、'.join(f'{label}（{name}）' if label != name else name for name, label in PROM_TYPES.items())
    else:
        options = '、'.join(field.choices)
    raise ValueError(f'{field.subject}只能选 {options}')


def _parse_number(field, raw):
    """数字或数字字符串 → int（整数项）/ float（数字项），不检查范围"""
    integer = field.kind == 'int'
    word = '整数' if integer else '数字'
    if isinstance(raw, bool):
        raise ValueError(f'{field.subject}必须是{word}')
    if isinstance(raw, str):
        text = raw.strip()
        if not (_INTEGER if integer else _NUMBER).fullmatch(text):
            raise ValueError(f'{field.subject}必须是{word}')
        number = int(text) if integer else float(text)
    elif isinstance(raw, (int, float)):
        number = raw
    else:
        raise ValueError(f'{field.subject}必须是{word}')
    if isinstance(number, float) and not math.isfinite(number):
        raise ValueError(f'{field.subject}必须是{word}')
    if integer:
        if isinstance(number, float):
            if not number.is_integer():
                raise ValueError(f'{field.subject}必须是整数')
            number = int(number)
        return number
    try:
        return float(number)
    except OverflowError:
        raise ValueError(_range_text(field)) from None


def _check_range(field, number):
    low, high = field.minimum, field.maximum
    if (number <= low if field.above_minimum else number < low) or number > high:
        raise ValueError(_range_text(field))
    return number


def _range_text(field):
    if field.kind == 'int':
        return f'{field.subject}必须是 {field.minimum:g} 到 {field.maximum:g} 之间的整数'
    if field.above_minimum:
        return f'{field.subject}必须大于 {field.minimum:g}，最大 {field.maximum:g}'
    return f'{field.subject}必须在 {field.minimum:g} 到 {field.maximum:g} 之间'


def _clean_url(field, text, previous):
    """完整的 http(s) 地址：去掉末尾的 /，账号密码统一百分号编码；规则参照 agent_install/bundle.py 的 clean_url"""
    label = field.label
    if len(text) > MAX_URL:
        raise ValueError(f'{label}太长（最多 {MAX_URL} 个字符）')
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text) or '\\' in text:
        raise ValueError(f'{label}不能包含空白、反斜杠或控制字符')
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError(f'{label}格式不正确，请检查端口和 IPv6 地址') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError(f'{label}必须是完整的 http:// 或 https:// 地址')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f'{label}的端口无效')
    if '#' in text:
        raise ValueError(f'{label}不能包含锚点（#）')
    if '?' in text and (field.base or not field.query):
        raise ValueError(f'{label}不能包含查询参数（?）')
    userinfo, at, address = parts.netloc.rpartition('@')
    if at and not field.credentials:
        raise ValueError(f'{label}不需要填写账号或密码')
    netloc = address
    if at:
        name, colon, secret = userinfo.partition(':')
        name, secret = unquote(name), unquote(secret)
        if not name:
            raise ValueError(f'{label}的 @ 前面缺少账号；不需要认证时去掉「账号:密码@」')
        if colon and secret == PASSWORD_MASK and previous is not None:
            secret = _saved_password(field, previous, name, parts.scheme, address)
        netloc = quote(name, safe='') + (':' + quote(secret, safe='') if secret else '') + '@' + address
    if field.base:
        if parts.path not in ('', '/'):
            raise ValueError(f'{label}只填写协议、主机和端口，不包含路径')
        return urlunsplit((parts.scheme, netloc, '', '', ''))
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip('/'), parts.query, ''))


def _saved_password(field, previous, name, scheme, address):
    """提交的密码是 ******：沿用已保存地址的密码。要求已保存的地址有密码，账号、协议、主机和端口都没变
    （否则改个主机再点「测试」就能把已保存的密码发到别的地方）"""
    try:
        old = urlsplit(previous) if previous else None
        if old is not None:
            _, old_name, old_secret = url_credentials(previous)
            if (old_secret and old_name == name and old.scheme == scheme
                    and old.netloc.rpartition('@')[2].lower() == address.lower()):
                return old_secret
    except ValueError:
        pass
    raise ValueError(f'请重新填写{field.label}的密码（已保存的地址没有密码，或者账号、协议、主机、端口变了）')


def legacy(key, text):
    """旧环境变量的值（字符串）→ 规范化后的值；不合法抛 ValueError。取值语义与原来读环境变量的代码一致：
    - PROM_TYPE 不在枚举里时按是否含 victoria / cluster 归类（ai_api 原来也是按含不含 victoria 判断）
    - AUTH_COOKIE_SECURE：true/1/yes → true，false/0/no → false，其他按 auto（auth/handlers.py 原来的判断）
    - AGENT_AUTH_REQUIRED：false/0/no/off 为假，其余为真（auth/middleware.py 原来的判断）
    - 保留天数、空闲超时：负数原来等于 0（不清理 / 不限），按 0 导入
    """
    field = FIELDS[key]
    text = text.strip()
    lowered = text.lower()
    if key == 'prom_type':
        for name in PROM_TYPES:
            if lowered == name.lower():
                return name
        if 'victoria' in lowered:
            return 'Victoria-Metrics-Cluster' if 'cluster' in lowered else 'Victoria-Metrics-Single'
        return 'Prometheus'
    if key == 'auth_cookie_secure':
        if lowered in ('true', '1', 'yes'):
            return 'true'
        return 'false' if lowered in ('false', '0', 'no') else 'auto'
    if field.kind == 'bool':
        return lowered not in ('false', '0', 'no', 'off')
    if field.kind in ('int', 'number'):
        number = _parse_number(field, text)
        if field.negative_as_zero and number < 0:
            number = type(number)(0)
        return _check_range(field, number)
    return clean(key, text)


# ---------------------------------------------------------------------------
# 地址工具
# ---------------------------------------------------------------------------
def masked(url):
    """把地址里的密码换成 ******；没有密码原样返回，空串（或不是字符串）返回空串。
    不依赖 urlsplit（格式不对的地址也不会把密码原样带出去）"""
    if not isinstance(url, str) or not url:
        return ''
    scheme, sep, rest = url.partition('://')
    if not sep:
        scheme, rest = '', url
    end = len(rest)
    for mark in '/?#':
        index = rest.find(mark)
        if index != -1:
            end = min(end, index)
    netloc, tail = rest[:end], rest[end:]
    userinfo, at, address = netloc.rpartition('@')
    name, colon, secret = userinfo.partition(':')
    if not at or not colon or not secret:
        return url
    prefix = f'{scheme}://' if sep else ''
    return f'{prefix}{name}:{PASSWORD_MASK}@{address}{tail}'


def url_credentials(url):
    """拆出地址里的「账号:密码@」，返回 (不含账号密码的地址, 账号, 密码)，账号密码已解码"""
    parts = urlsplit(url)
    userinfo, at, address = parts.netloc.rpartition('@')
    if not at:
        return url, '', ''
    name, _, secret = userinfo.partition(':')
    return (urlunsplit((parts.scheme, address, parts.path, parts.query, parts.fragment)),
            unquote(name), unquote(secret))
