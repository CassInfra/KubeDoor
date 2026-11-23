"""日志模式归类（约定 §7，纯函数）：把正文归并成模式（数字、ID 换成占位符）并计数。

1. 取正文第一行（去掉首尾空白），最多 1000 字；去掉行首的时间戳（每行都不同，又不提供信息）
2. 依次替换：UUID → <UUID>；IPv4(:端口) → <IP>；≥ 16 位的十六进制串 → <HEX>；含数字且 ≥ 12 位的字母数字串 → <ID>；
   数字（含小数、负号）→ <NUM>；引号里的长串（≥ 30）→ <STR>；连续空白合并
3. 按结果分组；再合并相似的组（简化版 Drain）：词数相同、开头两个「有信息的词」相同、有信息的位置至少 70% 一样，
   不同的位置换成 <*>。「有信息的词」指不全是占位符和标点的词；模板里已经是 <*> 的位置不算一样——
   否则合并出的 <*> 越来越多，最后同样长度的行全并成一类
4. 按条数降序；estimated = round(count / 样本数 * 总数)；example 用该组最早的一条原文（调用方已打码），截断 600
"""

import re

from .common import clip_text, iso_ms

FIRST_LINE = 1000
EXAMPLE_CHARS = 600
SIMILARITY = 0.7
LEAD = 2  # 开头几个有信息的词要一样
WILDCARD = '<*>'
_PREFIX_TIME = re.compile(r'^\[?\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,9})?'
                          r'(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?\]?[\s|:,-]*')
_UUID = re.compile(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
                   r'(?![0-9A-Fa-f])')
_IP = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])')
_HEX = re.compile(r'(?<![0-9A-Za-z])[0-9A-Fa-f]{16,}(?![0-9A-Za-z])')
_ID = re.compile(r'(?<![0-9A-Za-z])(?=[A-Za-z]*\d)[0-9A-Za-z]{12,}(?![0-9A-Za-z])')
_NUM = re.compile(r'(?<![A-Za-z0-9_<])-?\d+(?:\.\d+)?')
_QUOTED = re.compile(r'"[^"\n]{30,}"|\'[^\'\n]{30,}\'')
_SPACE = re.compile(r'\s+')
_PLACEHOLDERS = re.compile(r'<(?:NUM|HEX|ID|IP|UUID|STR)>|<\*>')
_NO_WORD = re.compile(r'[\W_]*')


def normalize(text):
    """一条正文 → 模式字符串"""
    line = (text or '').strip().split('\n', 1)[0].strip()[:FIRST_LINE] if isinstance(text, str) else ''
    line = _PREFIX_TIME.sub('', line)
    line = _UUID.sub('<UUID>', line)
    line = _IP.sub('<IP>', line)
    line = _HEX.sub('<HEX>', line)
    line = _ID.sub('<ID>', line)
    line = _NUM.sub('<NUM>', line)
    line = _QUOTED.sub('<STR>', line)
    return _SPACE.sub(' ', line).strip()


def informative(token):
    """有信息的词：去掉占位符后还有字母、数字或汉字（「<NUM>]」「-」这类不算）"""
    return not _NO_WORD.fullmatch(_PLACEHOLDERS.sub('', token))


def _lead(tokens):
    return [token for token in tokens if informative(token)][:LEAD]


class _Cluster:
    def __init__(self, tokens, members):
        self.tokens = list(tokens)
        self.lead = _lead(tokens)  # 第一个成员的开头几个有信息的词（之后合并出 <*> 也不变）
        self.members = list(members)

    def similar(self, tokens):
        if len(tokens) != len(self.tokens) or _lead(tokens) != self.lead:
            return False
        keys = [index for index, (a, b) in enumerate(zip(tokens, self.tokens))
                if informative(a) or informative(b) or b == WILDCARD]
        if not keys:
            return True
        same = sum(1 for index in keys if tokens[index] == self.tokens[index] != WILDCARD)
        return same / len(keys) >= SIMILARITY

    def merge(self, tokens, members):
        self.tokens = [a if a == b else WILDCARD for a, b in zip(self.tokens, tokens)]
        self.members.extend(members)


def group(samples, total, top):
    """samples: [(毫秒, 正文（已打码）, 级别)]（从新到旧）；total: 窗口内总条数 → 模式列表（按条数降序，最多 top 个）"""
    size = len(samples)
    if not size:
        return []
    groups = {}
    for index, (_, message, _) in enumerate(samples):
        groups.setdefault(normalize(message), []).append(index)
    clusters = []
    for key, members in sorted(groups.items(), key=lambda item: -len(item[1])):
        tokens = key.split(' ') if key else []
        for cluster in clusters:
            if cluster.similar(tokens):
                cluster.merge(tokens, members)
                break
        else:
            clusters.append(_Cluster(tokens, members))
    clusters.sort(key=lambda cluster: -len(cluster.members))
    result = []
    for cluster in clusters[:top]:
        members = cluster.members
        times = [samples[index][0] for index in members if samples[index][0] is not None]
        earliest = min(members, key=lambda index: (samples[index][0] is None, samples[index][0] or 0))
        levels = {}
        for index in members:
            level = samples[index][2]
            if level not in (None, ''):
                levels[str(level)] = levels.get(str(level), 0) + 1
        count = len(members)
        result.append({
            'pattern': ' '.join(cluster.tokens) or '（空正文）',
            'count': count,
            'ratio': round(count / size, 4),
            'estimated': round(count / size * total) if total is not None else None,
            'example': clip_text(samples[earliest][1] or '', EXAMPLE_CHARS, EXAMPLE_CHARS),
            'first': iso_ms(min(times)) if times else None,
            'last': iso_ms(max(times)) if times else None,
            'levels': dict(sorted(levels.items(), key=lambda item: -item[1])),
        })
    return result
