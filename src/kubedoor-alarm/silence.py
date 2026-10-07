#!/usr/bin/python3
"""告警屏蔽引擎(Alertmanager Silence 风格)

职责:
  1. 周期性从 PostgreSQL 的 alert_silences 表拉取"未解除且未过期"的规则并缓存在内存,
     避免每条告警都打一次库(告警风暴时 QPS 可能很高)。
  2. 对单条告警做匹配判断:规则内多个 matcher 为 AND,规则之间为 OR,任一命中即屏蔽。
  3. 累计每条规则的命中次数,随缓存刷新批量回写,避免逐条 UPDATE。

匹配语义与 Alertmanager 保持一致:
  - 操作符 '=' '!=' '=~' '!~',其中正则为**全匹配**(等价于两端加 ^...$)。
  - 告警上不存在的 label,其值视为空字符串 ""。因此 `foo!=bar` 会命中没有 foo 标签的告警,
    这与 Alertmanager 的行为相同。
  - 规则的时间窗每次判断都按当前时间实时计算,缓存 TTL 只影响新建/解除规则的生效延迟。
"""

import re
import threading
import time
from datetime import datetime, timezone

import logging

import utils

# 支持的操作符
OP_EQ = '='
OP_NE = '!='
OP_RE = '=~'
OP_NRE = '!~'
VALID_OPS = (OP_EQ, OP_NE, OP_RE, OP_NRE)

# 规范化 label 别名:把 KubeDoor 各处不统一的标签名归一到一个规范 key。
# 值为候选原始 label 名,按顺序取第一个非空的。
LABEL_ALIASES = {
    'namespace': ('namespace', 'k8s_ns'),
    'pod': ('pod', 'k8s_pod'),
    'container': ('container', 'k8s_app'),
    'alertname': ('alertname',),
    'severity': ('severity',),
    'alertgroup': ('alertgroup',),
}
# 规范 key 的等价写法(前端/存量数据里可能写成下划线形式)
KEY_SYNONYMS = {
    'alert_name': 'alertname',
    'alert_group': 'alertgroup',
    'k8s': 'env',
    'cluster': 'env',
}

# 正则编译缓存,避免每条告警重复编译
_regex_cache = {}
_regex_lock = threading.Lock()


def _compile(pattern):
    """编译并缓存正则。编译失败返回 None(调用方按"不匹配"处理)。"""
    with _regex_lock:
        if pattern in _regex_cache:
            return _regex_cache[pattern]
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        logging.warning(f'【silence】正则编译失败,已忽略该条件: {pattern!r} ({exc})')
        compiled = None
    with _regex_lock:
        _regex_cache[pattern] = compiled
    return compiled


def build_match_labels(labels, annotations=None):
    """把告警的原始 labels 展开成可供 matcher 查询的字典。

    原始 label 全部保留(支持屏蔽任意自定义标签),再叠加规范化别名:
      env       ← labels[PROM_K8S_TAG_KEY]
      namespace ← namespace / k8s_ns
      pod       ← pod / k8s_pod
      container ← container / k8s_app
      description ← annotations.description(仅取最后一段,与入库逻辑一致)
    """
    result = {}
    for key, value in (labels or {}).items():
        result[key] = '' if value is None else str(value)

    for canonical, candidates in LABEL_ALIASES.items():
        if result.get(canonical):
            continue
        for candidate in candidates:
            if result.get(candidate):
                result[canonical] = result[candidate]
                break

    # env 取自可配置的集群标签 key
    tag_key = utils.PROM_K8S_TAG_KEY
    if not result.get('env') and tag_key:
        result['env'] = result.get(tag_key, '')

    if annotations:
        description = annotations.get('description', '')
        if description:
            result.setdefault('description', str(description).split('\n- ')[-1])

    return result


def build_match_labels_from_record(alert_data):
    """从已解析的 alert_data(见 kubedoor-alarm.process_single_alert)构造匹配字典。

    用于 /api/custom_alert 这类没有原始 Alertmanager labels 的入口。
    """
    keys = ('env', 'namespace', 'pod', 'container', 'severity', 'description')
    result = {k: str(alert_data.get(k) or '') for k in keys}
    result['alertname'] = str(alert_data.get('alert_name') or '')
    result['alertgroup'] = str(alert_data.get('alert_group') or '')
    return result


def _normalize_key(key):
    """把 matcher 的 key 归一到规范写法。"""
    key = (key or '').strip()
    return KEY_SYNONYMS.get(key, key)


def _match_one(matcher, labels):
    """单个 matcher 是否匹配。非法 matcher 一律判为不匹配(fail-safe:宁可发通知)。"""
    if not isinstance(matcher, dict):
        return False
    key = _normalize_key(matcher.get('key'))
    op = (matcher.get('op') or OP_EQ).strip()
    expected = matcher.get('value')
    expected = '' if expected is None else str(expected)
    if not key or op not in VALID_OPS:
        return False

    actual = labels.get(key, '')

    if op == OP_EQ:
        return actual == expected
    if op == OP_NE:
        return actual != expected

    compiled = _compile(expected)
    if compiled is None:
        return False
    matched = compiled.fullmatch(actual) is not None
    return matched if op == OP_RE else not matched


def match_rule(rule, labels):
    """规则内所有 matcher 都满足才算命中(AND)。matchers 为空视为不命中。"""
    matchers = rule.get('matchers') or []
    if not matchers:
        return False
    return all(_match_one(m, labels) for m in matchers)


class SilenceEngine:
    """屏蔽规则缓存 + 匹配 + 命中计数回写。线程安全(Flask 多线程下共享一个实例)。"""

    def __init__(self, pool, ttl=None):
        self._pool = pool
        self._ttl = ttl if ttl is not None else utils.SILENCE_CACHE_TTL
        self._rules = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()
        # {silence_id: [命中次数增量, 最后命中时间]}
        self._pending = {}

    # -- 缓存 ---------------------------------------------------------------
    def _load_rules(self):
        """拉取未解除且未过期的规则(含尚未开始的 pending 规则,时间窗在匹配时判断)。"""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, matchers, starts_at, ends_at FROM alert_silences "
                "WHERE revoked_at IS NULL AND (ends_at IS NULL OR ends_at > now())"
            ).fetchall()
        rules = []
        for row in rows:
            matchers = row[1]
            # psycopg 对 jsonb 已自动反序列化;兼容返回字符串的情况
            if isinstance(matchers, str):
                import json

                try:
                    matchers = json.loads(matchers)
                except ValueError:
                    logging.warning(f'【silence】规则 {row[0]} 的 matchers 无法解析,已跳过')
                    continue
            rules.append({'id': row[0], 'matchers': matchers, 'starts_at': row[2], 'ends_at': row[3]})
        return rules

    def _refresh_if_stale(self):
        """TTL 到期则刷新缓存,并顺带把累计的命中计数回写数据库。"""
        if time.monotonic() - self._loaded_at < self._ttl:
            return
        with self._lock:
            # 双重检查:并发请求下只让一个线程真正查库
            if time.monotonic() - self._loaded_at < self._ttl:
                return
            self._loaded_at = time.monotonic()
        self._flush_counts()
        try:
            rules = self._load_rules()
        except Exception as exc:
            # 查库失败时沿用旧缓存,避免屏蔽功能故障导致告警链路中断
            logging.error(f'【silence】加载屏蔽规则失败,沿用上一次缓存: {exc}')
            return
        with self._lock:
            self._rules = rules
        logging.debug(f'【silence】已加载 {len(rules)} 条有效屏蔽规则')

    def _flush_counts(self):
        """把内存中累计的命中次数批量写回。失败则把增量放回,下次重试。"""
        with self._lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return
        try:
            with self._pool.connection() as conn:
                for silence_id, (delta, last_ts) in pending.items():
                    conn.execute(
                        "UPDATE alert_silences SET match_count = match_count + %s, "
                        "last_match_at = GREATEST(COALESCE(last_match_at, %s), %s) WHERE id = %s",
                        (delta, last_ts, last_ts, silence_id),
                    )
        except Exception as exc:
            logging.error(f'【silence】回写屏蔽命中次数失败,将在下次刷新重试: {exc}')
            with self._lock:
                for silence_id, (delta, last_ts) in pending.items():
                    cur = self._pending.get(silence_id)
                    if cur:
                        cur[0] += delta
                        cur[1] = max(cur[1], last_ts)
                    else:
                        self._pending[silence_id] = [delta, last_ts]

    def _record_hit(self, silence_id):
        now = datetime.now(timezone.utc)
        with self._lock:
            cur = self._pending.get(silence_id)
            if cur:
                cur[0] += 1
                cur[1] = now
            else:
                self._pending[silence_id] = [1, now]

    # -- 匹配 ---------------------------------------------------------------
    def match(self, labels, annotations=None, count_hit=True):
        """判断一条告警是否应被屏蔽。

        Args:
            labels: 告警的原始 labels,或 build_match_labels_from_record 的结果。
            annotations: 告警 annotations(可选,用于 description 匹配)。
            count_hit: 命中时是否计入规则的 match_count。同一条告警在入库和通知
                两个环节都会调用本方法,只在通知环节计数,避免重复累加。

        Returns:
            命中的规则 id;未命中返回 None。
        """
        try:
            self._refresh_if_stale()
            with self._lock:
                rules = self._rules
            if not rules:
                return None

            match_labels = build_match_labels(labels, annotations)
            now = datetime.now(timezone.utc)
            for rule in rules:
                starts_at = rule.get('starts_at')
                ends_at = rule.get('ends_at')
                if starts_at and now < starts_at:
                    continue  # 尚未生效
                if ends_at and now >= ends_at:
                    continue  # 已过期(缓存期内到期)
                if match_rule(rule, match_labels):
                    if count_hit:
                        self._record_hit(rule['id'])
                    return rule['id']
            return None
        except Exception as exc:
            # 屏蔽功能任何异常都不能影响告警链路,一律按"不屏蔽"处理
            logging.error(f'【silence】屏蔽匹配异常,本条告警按不屏蔽处理: {exc}', exc_info=True)
            return None

    def invalidate(self):
        """强制下次匹配时重新拉取规则(供规则变更后主动失效)。"""
        with self._lock:
            self._loaded_at = 0.0


_engine = None


def init_engine(pool, ttl=None):
    """初始化全局屏蔽引擎。在 alarm 启动时调用一次。"""
    global _engine
    _engine = SilenceEngine(pool, ttl)
    return _engine


def get_engine():
    return _engine


def match_alert(labels, annotations=None, count_hit=True):
    """模块级快捷方式:引擎未初始化时按"不屏蔽"处理。"""
    if _engine is None:
        return None
    return _engine.match(labels, annotations, count_hit)
