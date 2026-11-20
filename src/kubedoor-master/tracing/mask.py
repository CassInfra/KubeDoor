"""敏感值打码（约定 §6.6）：Span 详情、字段示例值、识别字段、搜索结果里的路径和查询参数。

- 属性名（不区分大小写）等于某个敏感名，或以「.敏感名」「_敏感名」「-敏感名」结尾 → 值换成 ***
  （例如 http.request.header.authorization、db.password）；内置敏感名之外，管理员可以在配置里加（mask_keys）
- URL 类的值（url.full、http.url、url.query、http.target、db.connection_string，以及加速表的 url_query）：
  参数名是敏感名的，值换成 ***；user:pass@ 换成 user:***@
- 进程命令行（process.command_line、process.command_args）里 名称=值 形式的敏感参数同样打码
  （例如 -Dspring.datasource.password=xxx）
"""

import re

MASK = '***'
BUILTIN = (
    'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'x-api-key', 'api-key', 'apikey', 'api_key',
    'x-auth-token', 'auth-token', 'access_token', 'refresh_token', 'id_token', 'token', 'password', 'passwd', 'pwd',
    'secret', 'client_secret', 'private_key', 'session', 'sessionid', 'jsessionid',
)
URL_KEYS = frozenset({'url.full', 'http.url', 'url.query', 'http.target', 'db.connection_string'})
COMMAND_KEYS = frozenset({'process.command_line', 'process.command_args'})
_USERINFO = re.compile(r'(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^:/?#@\s]*):(?P<password>[^@/?#\s]*)@')
# 查询参数 / 连接串参数：? & ; 分隔的 名称=值
_PARAM = re.compile(r'(?P<sep>^|[?&;])(?P<name>[^=&;#?]+)=(?P<value>[^&;#]*)')
# 命令行里的 名称=值（名称前面可以有 - / -- / -D）
_ARG = re.compile(r'(?P<name>[A-Za-z0-9_.\-]+)=(?P<value>[^\s"\']+|"[^"]*"|\'[^\']*\')')


class Masker:
    def __init__(self, extra=()):
        names = {name.lower() for name in BUILTIN}
        names.update(str(item).strip().lower() for item in extra or () if str(item).strip())
        self.names = frozenset(names)
        self._suffixes = tuple(sep + name for name in self.names for sep in ('.', '_', '-'))
        self._memo = {}

    def sensitive(self, key):
        """属性名是不是敏感名（结果按属性名缓存）"""
        result = self._memo.get(key)
        if result is None:
            lower = str(key).lower()
            result = lower in self.names or lower.endswith(self._suffixes)
            if len(self._memo) < 10000:
                self._memo[key] = result
        return result

    def url(self, text):
        """URL / 查询串 / 连接串：敏感参数的值和 user:pass@ 里的密码换成 ***"""
        if not isinstance(text, str) or not text:
            return text
        text = _USERINFO.sub(lambda m: f'{m.group("head")}:{MASK}@', text)
        if '=' not in text:
            return text

        def replace(match):
            name = match.group('name').strip().lstrip('-')
            if self.sensitive(name):
                return f'{match.group("sep")}{match.group("name")}={MASK}'
            return match.group(0)

        if '?' in text:
            head, _, rest = text.partition('?')
            query, hash_mark, fragment = rest.partition('#')
            return f'{head}?{_PARAM.sub(replace, query)}{hash_mark}{fragment}'
        return _PARAM.sub(replace, text)

    def command(self, text):
        """命令行里的 名称=值：名称（去掉 -D、- 前缀后的最后一段）是敏感名的打码"""
        if not isinstance(text, str) or '=' not in text:
            return text

        def replace(match):
            name = match.group('name').lstrip('-')
            hit = self.sensitive(name) or (name.startswith('D') and self.sensitive(name[1:]))  # -Dxxx=
            return f'{match.group("name")}={MASK}' if hit else match.group(0)

        return _ARG.sub(replace, text)

    def value(self, key, value):
        """单个属性值打码；嵌套的 kvlist 按内层的名称再判断"""
        if self.sensitive(key):
            return MASK
        lower = str(key).lower()
        if lower in URL_KEYS:
            return self.url(value) if isinstance(value, str) else value
        if lower in COMMAND_KEYS:
            if isinstance(value, list):
                return [self.command(item) for item in value]
            return self.command(value)
        if isinstance(value, dict):
            return self.attrs(value)
        return value

    def attrs(self, attrs):
        return {key: self.value(key, value) for key, value in (attrs or {}).items()}
