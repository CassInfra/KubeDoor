"""敏感值打码（约定 §8）：返回给 AI 的每个字符串值都过一遍（扫描结果的样本、预览同样）。

- 内置敏感名 = 链路追踪的 BUILTIN + sign、signature、ticket、credential 等
- 列名（不区分大小写）是敏感名、或以「.敏感名」「_敏感名」「-敏感名」结尾 → 整个值换成 ***；日志源的 mask_fields 同样
- 文本里：user:pass@ → user:***@；名称=值（名称前不是字母数字 _ . -，值到 & 空白 " ' , ; ) ] 为止）、名称: 值、
  JSON 风格 "名称": "值"（含转义过的 \\"名称\\":\\"值\\"）名称敏感时值换成 ***；Bearer xxx / Basic xxx → Bearer ***
- 日志源的 mask_patterns：有捕获组只替换第 1 组，否则替换整段
"""

import re

from tracing.mask import BUILTIN as TRACE_BUILTIN

MASK = '***'
EXTRA = ('sign', 'signature', 'ticket', 'credential', 'credentials', 'access_key', 'accesskey', 'secret_key',
         'secretkey', 'passphrase')
BUILTIN = tuple(dict.fromkeys((*TRACE_BUILTIN, *EXTRA)))
_USERINFO_URL = re.compile(r'(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^:/?#@\s]*):(?P<secret>[^@/?#\s]*)@')
_USERINFO = re.compile(r'(?<![A-Za-z0-9_.%+\-:/])(?P<head>[A-Za-z][A-Za-z0-9_.%+\-]{0,63}):'
                       r'(?P<secret>[^\s:@/\'"]{1,256})@(?=[A-Za-z0-9\[])')
_ASSIGN = re.compile(r'(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z0-9_.\-]{1,100})(?P<sep>=|:\s+)'
                     r'(?P<value>[^&\s"\',;)\]]+)')
_JSON_PAIR = re.compile(r'"(?P<name>[^"\\]{1,100})"(?P<sep>\s*:\s*)(?:"(?P<value>(?:[^"\\]|\\.)*)"'
                        r'|(?P<number>-?\d[\d.eE+\-]*))')
_JSON_ESCAPED = re.compile(r'\\"(?P<name>[^"\\]{1,100})\\"(?P<sep>\s*:\s*)\\"(?P<value>(?:[^"\\]|\\\\[^"])*?)\\"')
_BEARER = re.compile(r'\b(?P<kind>[Bb]earer|BEARER)\s+(?P<value>[A-Za-z0-9._~+/\-]{8,}=*)')
_BASIC = re.compile(r'\b(?P<kind>[Bb]asic|BASIC)\s+(?P<value>[A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=])')


def _basic(match):
    """Basic 后面像 base64 凭据（长度是 4 的倍数，含数字 / + / = 或大小写混合）才打码：「Basic auth failed」不动"""
    value = match.group('value')
    looks = len(value) % 4 == 0 and (any(ch.isdigit() or ch in '+/=' for ch in value) or
                                     (any(ch.isupper() for ch in value) and any(ch.islower() for ch in value)))
    return f'{match.group("kind")} {MASK}' if looks else match.group(0)


class Masker:
    """一个日志源的打码器：mask_fields 整列打码，mask_patterns 文本打码（配置保存时已校验能编译）"""

    def __init__(self, mask_fields=(), mask_patterns=()):
        self.names = frozenset(name.lower() for name in BUILTIN)
        self._suffixes = tuple(sep + name for name in self.names for sep in ('.', '_', '-'))
        self.fields = frozenset(str(item).lower() for item in mask_fields or ())
        self.patterns = []
        for pattern in mask_patterns or ():
            try:
                self.patterns.append(re.compile(pattern))
            except re.error:
                continue
        self._memo = {}

    def sensitive(self, name):
        """名称是不是敏感名（结果按名称缓存）"""
        result = self._memo.get(name)
        if result is None:
            lower = str(name).lower().lstrip('-')
            result = lower in self.names or lower.endswith(self._suffixes) or (
                lower.startswith('d') and lower[1:] in self.names)  # -Dpassword=
            if len(self._memo) < 10000:
                self._memo[name] = result
        return result

    def hidden(self, column):
        """整列打码：列名是敏感名，或在日志源的打码字段里"""
        if not column:
            return False
        return self.sensitive(column) or str(column).lower() in self.fields

    # ---- 文本 ----
    def _assign(self, match):
        if self.sensitive(match.group('name')):
            return f'{match.group("name")}{match.group("sep")}{MASK}'
        return match.group(0)

    def _json(self, match):
        if not self.sensitive(match.group('name')):
            return match.group(0)
        return f'"{match.group("name")}"{match.group("sep")}"{MASK}"'

    def _json_escaped(self, match):
        if not self.sensitive(match.group('name')):
            return match.group(0)
        return f'\\"{match.group("name")}\\"{match.group("sep")}\\"{MASK}\\"'

    def text(self, text):
        if not isinstance(text, str) or not text:
            return text
        if '@' in text:
            text = _USERINFO_URL.sub(lambda m: f'{m.group("head")}:{MASK}@', text)
            text = _USERINFO.sub(lambda m: f'{m.group("head")}:{MASK}@', text)
        # Bearer / Basic 先做：「Authorization: Bearer xxx」按名称=值只会打掉 Bearer 这个词
        if 'earer' in text or 'EARER' in text:
            text = _BEARER.sub(lambda m: f'{m.group("kind")} {MASK}', text)
        if 'asic' in text or 'ASIC' in text:
            text = _BASIC.sub(_basic, text)
        if '"' in text:
            text = _JSON_PAIR.sub(self._json, text)
            if '\\"' in text:
                text = _JSON_ESCAPED.sub(self._json_escaped, text)
        if '=' in text or ':' in text:
            text = _ASSIGN.sub(self._assign, text)
        for pattern in self.patterns:
            text = self._apply_pattern(pattern, text)
        return text

    @staticmethod
    def _apply_pattern(pattern, text):
        if not pattern.groups:
            return pattern.sub(MASK, text)

        def replace(match):
            if match.group(1) is None:
                return match.group(0)
            start, end = match.span(1)
            base = match.start(0)
            whole = match.group(0)
            return whole[:start - base] + MASK + whole[end - base:]

        return pattern.sub(replace, text)

    # ---- 值 ----
    def value(self, column, value):
        """某一列的值：整列打码或文本打码；数组、对象逐项（对象按「列名.键名」再判断）"""
        if value is None or value == '':
            return value
        if self.hidden(column):
            return MASK
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(column, item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                path = f'{column}.{key}' if column else str(key)
                result[key] = MASK if (self.hidden(key) or self.hidden(path)) and item not in (None, '') \
                    else self.value(path, item)
            return result
        return value

    def row(self, row):
        return {key: self.value(key, value) for key, value in row.items()}
