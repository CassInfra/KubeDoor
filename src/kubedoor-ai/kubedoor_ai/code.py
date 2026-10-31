"""AI 排障时获取、搜索、阅读服务源码：下载压缩包、安全解压、本地缓存，ripgrep 搜索、按行读文件、universal-ctags 查找定义。

下载地址由网关从 master 的「获取代码接口」拿到后传进来，模型拿不到也传不进 URL。压缩包流式写到缓存根目录下的
临时目录，逐个成员检查后解压（不用 extractall：只写普通文件，跳过链接、设备和路径穿越，按实际写出的字节防压缩
炸弹），完成后原子改名成 <root>/<code_id>/。同一 (集群, 命名空间, Deployment, 镜像) 只下载一次；最近使用超过
CACHE_TTL 的和超出 CACHE_LIMIT 的（按最近使用时间从旧到新）由 cleanup 删掉。

    <root>/.tmp-<随机>/            下载、解压中；进程重启后残留的在第一次 fetch 或 cleanup 时清掉
    <root>/<code_id>/meta.json    概要；这个文件的 mtime 就是最近使用时间（fetch 命中、搜索、读文件都会刷新）
    <root>/<code_id>/tree/        解压结果；只有一个顶层目录时（GitLab archive 是 <项目>-<sha>/）把它当代码根目录

下载链接的查询参数常带签名（expires / sig），错误信息和日志里的地址只写 scheme + host + path。
单进程、单副本部署：每份代码的锁和正在用的临时目录只记在本进程内存里。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

LOGGER = logging.getLogger(__name__)

MAX_ARCHIVE = 300 * 1024 * 1024      # 压缩包大小
MAX_EXTRACTED = 1536 * 1024 * 1024   # 解压后总字节
MAX_FILES = 100_000                  # 压缩包里的文件数
MAX_FILE = 20 * 1024 * 1024          # 单个文件超过的跳过（计入 skipped_files），不报错
CACHE_LIMIT = 5 * 1024 ** 3          # 缓存总量，超出的按最近使用时间从旧到新删
CACHE_TTL = 24 * 3600                # 最近使用超过这么久的删掉
DOWNLOAD_TIMEOUT = 300               # 一次下载的总时长（秒）
SEARCH_TIMEOUT = 20                  # 一次搜索的时长（秒），超过就终止 rg，返回已收集的结果
SEARCH_DEFAULT_RESULTS, SEARCH_MAX_RESULTS = 50, 200
SEARCH_MAX_CHARS = 60_000            # 一次搜索结果的总字符数（matches 的 text + before + after，列文件时 paths），超过就停
READ_DEFAULT_LINES, READ_MAX_LINES = 200, 500
READ_MAX_CHARS = 60_000              # 一次读文件 content 的总字符数，超过就在那一行之前截止
LINE_MAX_CHARS = 500                 # 搜索结果和读文件时每行最多字符数
RG = shutil.which("rg") or "rg"
# universal-ctags：Debian 的包把它装成 ctags-universal，ctags 只是 update-alternatives 建的链接（可能指向别的实现）
CTAGS = shutil.which("ctags-universal") or shutil.which("ctags") or "ctags"
SYMBOL_DEFAULT_RESULTS, SYMBOL_MAX_RESULTS = 100, 200
SYMBOL_MAX_FILES = 300               # 只给名字时先用 rg 找含这个词的文件，最多交给 ctags 这么多个
SYMBOL_TIMEOUT = 30                  # 一次 ctags 的时长（秒），超过就终止，返回已解析的结果
SYMBOL_MAX_CHARS = 60_000            # 一次查找定义结果的总字符数（各字段字符串长度之和），超过就停

DEFAULT_ROOT = "/data/code-cache"    # 部署时挂 emptyDir；不存在或不可写时用临时目录
CONNECT_TIMEOUT = 30
MAX_REDIRECTS = 5
WRITE_BUFFER = 1024 * 1024           # 下载时攒够这么多再到线程里写盘
COPY_CHUNK = 1024 * 1024             # 解压时每次读写的块
# rg 的 --max-columns 对 --json 输出不起作用（实测 ripgrep 15），压缩过的 js、source map 会整行输出：
# 行内容在这边截断，单行 JSON 超过这个大小的整行丢掉（计入 omitted_long_lines），不占内存
JSON_LINE_LIMIT = 16 * 1024 * 1024
ZIP_DIRECTORY_LIMIT = 64 * 1024 * 1024   # zip 中央目录的大小上限：ZipFile 打开时会把全部条目读进内存
TOP_LIMIT, LANGUAGE_LIMIT = 50, 10
META_VERSION = 1
META_MAX = 1024 * 1024
TMP_PREFIX = ".tmp-"
TREE = "tree"
CUT = " …[截断]"
BOM = chr(0xFEFF)
CORRUPT = "压缩包已损坏，解压失败"
CODE_ID = re.compile(r"[0-9a-f]{16}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
DRIVE = re.compile(r"[A-Za-z]:")
WINDOWS = os.name == "nt"
# 本地开发在 Windows 上跑时写不出来、或会写到别处的名字（a:b 是备用数据流，con、nul 是设备）
WINDOWS_BAD = re.compile(r'[<>:"|?*]|[. ]$|^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$', re.IGNORECASE)
URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
SYMBOL_PART = re.compile(r"[\w$]+")          # 符号名的一段：字母、数字、_、$
SYMBOL_SPLIT = re.compile(r"::|[.#]")        # 限定名 Outer.Inner.method、Class::method、Class#method
SCOPE_SPLIT = re.compile(r"::|\.")           # ctags 输出的 scope：demo.OrderService、ns::Class
SYMBOL_HINT = "名字太常见，用 Class.method 或 path 缩小范围"
NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
# 代码根目录下认识的项目文件，按这个顺序列出；另外 .sln、.csproj 按扩展名认
MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "package.json",
           "tsconfig.json", "go.mod", "requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "Cargo.toml",
           "composer.json", "Gemfile", "build.sbt", "mix.exs", "pubspec.yaml", "CMakeLists.txt", "Makefile",
           "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Chart.yaml", "Jenkinsfile", ".gitlab-ci.yml")
MARKER_SUFFIXES = (".sln", ".csproj")
# 返回给网关的键名避开 files、token、password、stdin、kubeconfig 等：网关把结果交给模型前的 domain.redact
# 会把这些键的值换成 [redacted]。所以文件数叫 file_count，列出的文件叫 paths
META_FIELDS = ("code_id", "env", "namespace", "deployment", "image", "fetched_at", "file_count", "bytes")
SUMMARY_FIELDS = ("file_count", "bytes", "skipped_files", "top", "languages", "markers")


class CodeError(Exception):
    """失败原因直接给模型和用户看"""

    def __init__(self, message: str, code: str = "code_error"):
        super().__init__(message)
        self.message, self.code = message, code


class _Stopped(Exception):
    """fetch 被取消：解压线程尽快停下"""


# ---- 小工具 ----

def code_key(env: str, namespace: str, deployment: str, image: str) -> str:
    return hashlib.sha256(f"{env}\n{namespace}\n{deployment}\n{image}".encode()).hexdigest()[:16]


def normalize_id(value) -> str | None:
    """16 位十六进制才算 code_id（拼进路径之前先校验，防路径注入）"""
    if isinstance(value, str) and len(value) == 16:
        value = value.lower()
        if CODE_ID.fullmatch(value):
            return value
    return None


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{value:.1f}".rstrip("0").rstrip(".") + " " + unit


def safe_url(url) -> str:
    """只留 scheme + host + path：查询参数常带签名，账号密码更不能出现"""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}"
    except ValueError:
        return "[URL]"


def scrub(text: str, *urls) -> str:
    """日志里的异常信息：去掉其中 URL 的查询参数，再把已知下载地址的查询参数值换成 ***"""
    text = URL_IN_TEXT.sub(lambda match: safe_url(match.group()), text)
    for url in urls:
        try:
            values = [value for _, value in parse_qsl(urlsplit(str(url)).query) if len(value) >= 6]
        except ValueError:
            continue
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, "***")
    return text


def causes(exc):
    """异常本身和 cause / context 链"""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def valid_download_url(url) -> bool:
    if not isinstance(url, str) or len(url) > 8192 or CONTROL.search(url):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def download_failure(exc: Exception, url: str) -> str:
    """下载失败给用户看的原因；地址只写 scheme + host + path"""
    try:
        target = safe_url(exc.request.url)
    except (AttributeError, RuntimeError):
        target = safe_url(url)
    chain = list(causes(exc))
    if any(isinstance(error, ssl.SSLCertVerificationError) for error in chain):
        return f"下载代码失败：{target} 的 HTTPS 证书校验没通过（自签证书可以在「获取代码接口」的配置里关闭证书校验）"
    if any(isinstance(error, ssl.SSLError) for error in chain):
        return f"下载代码失败：和 {target} 的 TLS 握手失败"
    if isinstance(exc, httpx.TooManyRedirects):
        return f"下载代码失败：重定向超过 {MAX_REDIRECTS} 次（{target}）"
    if isinstance(exc, httpx.TimeoutException):
        return f"下载代码超时（{target}）"
    if isinstance(exc, httpx.ConnectError):
        return f"下载代码失败：无法连接 {target}（域名解析失败、连接被拒绝或网络不通）"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return f"下载代码失败：不支持的地址协议（{target}）"
    return f"下载代码失败：{type(exc).__name__}（{target}）"


def disk_error(exc: OSError) -> CodeError:
    if exc.errno in NO_SPACE:
        return CodeError("代码缓存目录的磁盘空间不足", "extract_limit")
    return CodeError(f"写代码缓存失败（{type(exc).__name__}）")


def too_large() -> CodeError:
    return CodeError(f"压缩包超过 {human(MAX_ARCHIVE)} 上限", "archive_too_large")


def touch(path: Path, now: float) -> None:
    with contextlib.suppress(OSError):
        os.utime(path, (now, now))


def remove_tree(directory: Path) -> None:
    """先改名成临时目录（从缓存里原子地拿掉）再删；删不干净的当残留临时目录，下次清理"""
    trash = directory.with_name(f"{TMP_PREFIX}{secrets.token_hex(8)}")
    try:
        os.rename(directory, trash)
    except OSError:
        trash = directory
    shutil.rmtree(trash, ignore_errors=True)


def usable(path: Path, create: bool) -> bool:
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return False
        fd, probe = tempfile.mkstemp(prefix=".probe-", dir=path)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def resolve_root(explicit) -> Path:
    """缓存根目录：参数 > AI_CODE_CACHE_DIR > /data/code-cache（要已存在）> 临时目录/kubedoor-code"""
    if explicit is not None:
        path = Path(explicit)
        if usable(path, create=True):
            return path
        raise CodeError(f"代码缓存目录 {path} 不可写")
    configured = os.environ.get("AI_CODE_CACHE_DIR", "").strip()
    if configured:
        if usable(Path(configured), create=True):
            return Path(configured)
        LOGGER.warning("AI_CODE_CACHE_DIR=%s 不可写，改用临时目录", configured)
    elif usable(Path(DEFAULT_ROOT), create=False):
        return Path(DEFAULT_ROOT)
    fallback = Path(tempfile.gettempdir()) / "kubedoor-code"
    if usable(fallback, create=True):
        return fallback
    raise CodeError("代码缓存目录不可写（/data/code-cache 和临时目录都不行）")


# ---- 参数校验 ----

def require_id(value) -> str:
    code_id = normalize_id(value)
    if code_id is None:
        raise CodeError("code_id 无效：应为获取代码时返回的 16 位十六进制", "bad_request")
    return code_id


def require_int(value, name: str, low: int | None = None, high: int | None = None) -> int:
    if type(value) is not int:
        raise CodeError(f"{name} 必须是整数", "bad_request")
    if (low is not None and value < low) or (high is not None and value > high):
        raise CodeError(f"{name} 应在 {low}–{high} 之间", "bad_request")
    return value


def require_text(value, name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CodeError(f"{name} 必须是字符串", "bad_request")
    if len(value) > limit:
        raise CodeError(f"{name} 最多 {limit} 个字符", "bad_request")
    if "\0" in value:
        raise CodeError(f"{name} 不能包含 NUL 字符", "bad_request")
    return value


def require_glob(value) -> str:
    glob = require_text(value, "glob", 300)
    bare = glob.lstrip("!")
    if bare.startswith(("/", "\\")) or DRIVE.match(bare) or ".." in glob:
        raise CodeError("glob 要写相对代码根目录的模式（例如 src/**/*.java），不能以 / 开头，也不能包含 ..", "bad_request")
    return glob


def require_path(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 4096 or CONTROL.search(text):
        raise CodeError("path 必须是相对代码根目录的文件路径", "bad_request")
    if "\\" in text or text.startswith("/") or DRIVE.match(text):
        raise CodeError("path 要写相对代码根目录的路径（例如 src/main/java/App.java），不能是绝对路径", "bad_request")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise CodeError("path 不能包含 ..", "bad_request")
    if not parts:
        raise CodeError("path 必须是相对代码根目录的文件路径", "bad_request")
    return "/".join(parts)


def parse_symbol(name: str) -> tuple[str, str | None]:
    """符号名 → (最后一段即名字, 倒数第二段即所属类型)；没给名字返回 ("", None)"""
    if not name:
        return "", None
    parts = SYMBOL_SPLIT.split(name)
    if not all(SYMBOL_PART.fullmatch(part) for part in parts):
        raise CodeError("name 只能由标识符组成（字母、数字、_、$），限定名用 . 或 :: 或 # 连接，例如 OrderService.createOrder",
                        "bad_request")
    return parts[-1], (parts[-2] if len(parts) > 1 else None)


# ---- meta.json ----

def valid_root(root) -> bool:
    if root == TREE:
        return True
    return isinstance(root, str) and root.startswith(TREE + "/") and root.count("/") == 1 and member_path(root) == root


def load_meta(directory: Path) -> tuple[dict | None, float]:
    """(概要, 最近使用时间)；不存在或格式不对返回 (None, 0)"""
    path = directory / "meta.json"
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > META_MAX:
            return None, 0.0
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None, 0.0
    if (not isinstance(data, dict) or data.get("version") != META_VERSION or data.get("code_id") != directory.name
            or not all(isinstance(data.get(key), str) for key in ("env", "namespace", "deployment", "image", "fetched_at"))
            or not all(type(data.get(key)) is int and data[key] >= 0 for key in ("file_count", "bytes", "skipped_files"))
            or not isinstance(data.get("top"), list) or not isinstance(data.get("languages"), dict)
            or not isinstance(data.get("markers"), list) or not valid_root(data.get("root"))):
        return None, 0.0
    return data, info.st_mtime


# ---- 解压 ----

def member_path(name) -> str | None:
    """压缩包成员名 → 相对目标目录的 POSIX 路径；绝对路径、含 ..、盘符、反斜杠、控制字符的返回 None"""
    if not isinstance(name, str) or not name or "\\" in name or CONTROL.search(name):
        return None
    if name.startswith("/") or DRIVE.match(name):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if WINDOWS and any(WINDOWS_BAD.search(part) for part in parts):
        return None
    return "/".join(parts)


class Extractor:
    """逐个成员检查后写出普通文件。计数按实际写出的字节，不信压缩包头里声明的大小"""

    def __init__(self, dest: Path, stop: threading.Event | None = None):
        self.dest, self.base, self.stop = dest, dest.resolve(), stop
        self.files: dict[str, int] = {}   # 写出的文件 → 字节数（同名成员后写的覆盖前面的）
        self.entries = 0                  # 处理过的成员（含目录、跳过的）
        self.regular = 0                  # 普通文件成员（含太大跳过的）
        self.inflated = 0                 # 解压出的字节（含太大被丢掉的部分）
        self.skipped = 0                  # 没写出的文件：太大、链接、设备、FIFO、路径不安全
        self.dirs: set[Path] = set()

    def check_stop(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise _Stopped()

    def entry(self) -> None:
        self.check_stop()
        self.entries += 1
        # 目录、链接也要限：tarfile / ZipFile 会把每个条目都留在内存里
        if self.entries > MAX_FILES * 2:
            raise CodeError(f"压缩包里的条目超过 {MAX_FILES * 2} 个", "extract_limit")

    def regular_file(self) -> None:
        self.regular += 1
        if self.regular > MAX_FILES:
            raise CodeError(f"压缩包里的文件超过 {MAX_FILES} 个上限", "extract_limit")

    def inflate(self, size: int) -> None:
        self.inflated += size
        if self.inflated > MAX_EXTRACTED:
            raise CodeError(f"解压后超过 {human(MAX_EXTRACTED)} 上限", "extract_limit")

    def target(self, name) -> tuple[str | None, Path | None]:
        rel = member_path(name)
        if rel is None:
            return None, None
        path = self.dest.joinpath(*rel.split("/"))
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return None, None
        if resolved == self.base or not resolved.is_relative_to(self.base):
            return None, None
        return rel, path

    def write(self, name: str, source, counted: bool) -> None:
        """写出一个普通文件成员；counted 表示解压字节已按成员大小计过（tar）"""
        rel, path = self.target(name)
        if path is None:
            self.skipped += 1
            return
        try:
            if path.parent not in self.dirs:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.dirs.add(path.parent)
            out = open(path, "wb")
        except OSError as exc:
            if exc.errno in NO_SPACE:
                raise disk_error(exc) from None
            self.skipped += 1   # 和已有的文件 / 目录同名冲突，或名字写不出来
            return
        size, too_big = 0, False
        with out:
            while True:
                self.check_stop()
                try:
                    chunk = source.read(COPY_CHUNK)
                except Exception as exc:   # 压缩数据损坏、CRC 不对
                    raise CodeError(CORRUPT, "archive_invalid") from exc
                if not chunk:
                    break
                size += len(chunk)
                if not counted:
                    self.inflate(len(chunk))
                if size > MAX_FILE:
                    too_big = True
                    break
                try:
                    out.write(chunk)
                except OSError as exc:
                    raise disk_error(exc) from None
        if too_big:
            path.unlink(missing_ok=True)
            self.files.pop(rel, None)
            self.skipped += 1
            return
        self.files[rel] = size


def archive_kind(head: bytes) -> str:
    """按文件头魔数判断格式，不看扩展名；认不出的当未压缩的 tar 试"""
    if head.startswith(b"\x1f\x8b"):
        return "r:gz"
    if head.startswith(b"BZh"):
        return "r:bz2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "r:xz"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    return "r:"


def not_archive(head: bytes) -> CodeError:
    if head.lstrip()[:1] in (b"<", b"{"):
        return CodeError("下载到的是网页或 JSON，不是压缩包：下载链接可能已过期或需要登录，请检查获取代码接口", "archive_invalid")
    return CodeError("下载的内容不是支持的压缩包（支持 .tar.gz、.zip、.tar、.tar.bz2、.tar.xz）", "archive_invalid")


def zip_directory(path: Path) -> tuple[int, int] | None:
    """读 zip 的中央目录结尾记录（含 zip64），返回 (条目数, 中央目录字节数)；找不到返回 None，交给 zipfile 判断"""
    with open(path, "rb") as file:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        tail_size = min(size, 22 + 65535)
        file.seek(size - tail_size)
        tail = file.read(tail_size)
        pos = len(tail)
        while True:
            pos = tail.rfind(b"PK\x05\x06", 0, pos)
            if pos < 0:
                return None
            # 结尾记录之后只能是注释：长度对得上才算，免得误认注释里的签名
            if len(tail) - pos >= 22 and pos + 22 + int.from_bytes(tail[pos + 20:pos + 22], "little") == len(tail):
                break
        entries = int.from_bytes(tail[pos + 10:pos + 12], "little")
        directory = int.from_bytes(tail[pos + 12:pos + 16], "little")
        locator_at = size - tail_size + pos - 20
        if (entries == 0xFFFF or directory == 0xFFFFFFFF) and locator_at >= 0:
            file.seek(locator_at)
            locator = file.read(20)
            if locator[:4] == b"PK\x06\x07":
                file.seek(int.from_bytes(locator[8:16], "little"))
                record = file.read(56)
                if len(record) == 56 and record[:4] == b"PK\x06\x06":
                    entries = int.from_bytes(record[32:40], "little")
                    directory = int.from_bytes(record[40:48], "little")
        return entries, directory


def zip_name(info: zipfile.ZipInfo) -> str:
    """没标 UTF-8 的成员名 zipfile 按 cp437 解码：还原成字节后依次试 UTF-8、GBK（Windows 中文环境打的包）"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def extract_tar(archive: Path, mode: str, head: bytes, extractor: Extractor) -> None:
    try:
        tar = tarfile.open(archive, mode, encoding="utf-8", errors="replace")
    except Exception:
        raise not_archive(head) from None
    with tar:
        members = iter(tar)
        while True:
            try:
                member = next(members, None)
            except Exception as exc:   # 头部损坏、压缩流被截断
                raise CodeError(CORRUPT, "archive_invalid") from exc
            if member is None:
                return
            extractor.entry()
            if member.isdir():
                continue
            if not member.isreg():   # 符号链接、硬链接、设备、FIFO
                extractor.skipped += 1
                continue
            extractor.regular_file()
            size = max(member.size, 0)
            # 太大跳过的也要计：压缩流得把这段数据解压一遍才读得到下一个成员
            extractor.inflate(size)
            if size > MAX_FILE:
                extractor.skipped += 1
                continue
            try:
                source = tar.extractfile(member)
            except Exception as exc:
                raise CodeError(CORRUPT, "archive_invalid") from exc
            if source is None:
                extractor.skipped += 1
                continue
            with source:
                extractor.write(member.name, source, counted=True)


def extract_zip(archive: Path, head: bytes, extractor: Extractor) -> None:
    directory = zip_directory(archive)
    if directory and (directory[0] > MAX_FILES * 2 or directory[1] > ZIP_DIRECTORY_LIMIT):
        raise CodeError(f"压缩包里的条目超过 {MAX_FILES * 2} 个", "extract_limit")
    try:
        zf = zipfile.ZipFile(archive)
    except Exception:
        raise not_archive(head) from None
    with zf:
        for info in zf.infolist():
            extractor.entry()
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind == stat.S_IFDIR:
                continue
            if kind not in (0, stat.S_IFREG) or info.flag_bits & 0x1:   # 符号链接、设备、加密的成员
                extractor.skipped += 1
                continue
            extractor.regular_file()
            if info.file_size > MAX_FILE:
                extractor.skipped += 1
                continue
            try:
                source = zf.open(info)
            except NotImplementedError:   # 不支持的压缩算法
                extractor.skipped += 1
                continue
            except Exception as exc:
                raise CodeError(CORRUPT, "archive_invalid") from exc
            with source:
                extractor.write(zip_name(info), source, counted=False)


def extract(archive: Path, extractor: Extractor) -> None:
    with open(archive, "rb") as file:
        head = file.read(512)
    kind = archive_kind(head)
    if kind == "zip":
        extract_zip(archive, head, extractor)
    else:
        extract_tar(archive, kind, head, extractor)


def code_root_of(tree: Path) -> str:
    """只有一个顶层目录时把它当代码根目录；返回相对 <code_id>/ 的路径"""
    with os.scandir(tree) as entries:
        items = list(entries)
    if len(items) == 1 and items[0].is_dir(follow_symlinks=False):
        return f"{TREE}/{items[0].name}"
    return TREE


def summarize(code_root: Path, files: dict[str, int]) -> dict:
    with os.scandir(code_root) as entries:
        items = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    items.sort(key=lambda item: (not item[1], item[0].lower(), item[0]))
    names = {name for name, is_dir in items if not is_dir}
    counts = Counter()
    for path in files:
        name = path.rsplit("/", 1)[-1]
        dot = name.rfind(".")
        ext = name[dot + 1:].lower() if dot > 0 else ""
        if ext and len(ext) <= 10 and ext.isascii() and ext.isalnum():
            counts[ext] += 1
    return {"file_count": len(files), "bytes": sum(files.values()),
            "top": [name + "/" if is_dir else name for name, is_dir in items[:TOP_LIMIT]],
            "languages": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:LANGUAGE_LIMIT]),
            "markers": [name for name in MARKERS if name in names] + sorted(name for name in names if name.endswith(MARKER_SUFFIXES))}


# ---- 搜索 ----

def field_text(value) -> str:
    """rg --json 的 path / lines：{"text": ...}，不是合法 UTF-8 时是 {"bytes": base64}"""
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("bytes"), str):
            try:
                return base64.b64decode(value["bytes"]).decode("utf-8", "replace")
            except ValueError:
                return ""
    return ""


def rg_path(text: str) -> str:
    """rg 输出的路径 → 相对代码根目录的 POSIX 路径（--json 里的路径不受 --path-separator 影响，Windows 上是 .\\src\\…）"""
    if os.sep != "/":
        text = text.replace(os.sep, "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def clip(text: str, start: int | None = None) -> str:
    """去掉行尾换行，超过 LINE_MAX_CHARS 的截断；给了匹配的字节偏移时截匹配附近的一段（压缩过的 js 匹配常在几千列之后）"""
    text = text.rstrip("\r\n")
    if len(text) <= LINE_MAX_CHARS:
        return text
    begin = 0
    if start:
        offset = len(text[:start].encode("utf-8")[:start].decode("utf-8", "ignore"))
        begin = max(0, min(offset - LINE_MAX_CHARS // 5, len(text) - LINE_MAX_CHARS))
    end = begin + LINE_MAX_CHARS
    return ("…" if begin else "") + text[begin:end] + (CUT if end < len(text) else "")


class LineReader:
    """按 \\n 切分子进程输出；单行超过 limit 的整行丢掉（计入 dropped），不占内存"""

    def __init__(self, stream, limit: int):
        self.stream, self.limit = stream, limit
        self.buffer, self.scanned = bytearray(), 0
        self.dropping, self.dropped, self.eof = False, 0, False

    async def readline(self) -> bytes | None:
        """下一行（不带换行）；读完返回 None"""
        while True:
            index = self.buffer.find(b"\n", self.scanned)
            last = index < 0 and self.eof   # 输出结束：剩下的是没有换行的最后一行
            if last and not self.buffer and not self.dropping:
                return None
            if index >= 0 or last:
                end = index if index >= 0 else len(self.buffer)
                drop = self.dropping or end > self.limit
                line = None if drop else bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                self.scanned, self.dropping = 0, False
                if drop:
                    self.dropped += 1
                    continue
                return line
            if self.dropping or len(self.buffer) > self.limit:
                self.buffer.clear()   # 超长行：中间部分直接扔掉，只等它的换行
                self.dropping = True
            self.scanned = len(self.buffer)
            chunk = await self.stream.read(65536)
            if chunk:
                self.buffer += chunk
            else:
                self.eof = True


class MatchCollector:
    """把 rg --json 的事件整理成匹配列表；有上下文时每个匹配带上紧挨着的前后各 context 行（相邻匹配的行也算）。
    条数到 limit、或 text + before + after 的总字符数放不下了就标记截断（停止收集，rg 随即被终止）"""

    def __init__(self, context: int, limit: int):
        self.context, self.limit = context, limit
        self.items: list[dict] = []
        self.truncated = False
        self.chars = 0
        self.recent: deque = deque(maxlen=max(context, 1))   # 最近的 (行号, 文本)，给下一个匹配当 before
        self.pending: list[dict] = []                        # after 还没收满的匹配

    def _take(self, size: int) -> bool:
        """总字符数还放得下 size 就记上；放不下就标记截断"""
        if self.truncated or self.chars + size > SEARCH_MAX_CHARS:
            self.truncated = True
            return False
        self.chars += size
        return True

    async def feed(self, line: bytes) -> None:
        try:
            # 超长行的 JSON 能有几 MB：放线程里解析，不阻塞 event loop
            event = await asyncio.to_thread(json.loads, line) if len(line) > 1024 * 1024 else json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            return
        kind, data = event.get("type"), event["data"]
        if kind == "begin":
            self.recent.clear()
            self.pending = []
            return
        number = data.get("line_number")
        if kind not in ("match", "context") or type(number) is not int:
            return
        start = None
        if kind == "match" and isinstance(data.get("submatches"), list) and data["submatches"]:
            first = data["submatches"][0]
            start = first.get("start") if isinstance(first, dict) and type(first.get("start")) is int else None
        text = clip(field_text(data.get("lines")), start)
        if self.context:
            self.pending = [match for match in self.pending if self._extend(match, number, text)]
        if self.truncated:
            return
        if kind == "match":
            if len(self.items) >= self.limit:
                self.truncated = True
                return
            before = self._before(number) if self.context else []
            if not self._take(len(text) + sum(map(len, before))):
                return
            item = {"path": rg_path(field_text(data.get("path"))), "line": number, "text": text}
            if self.context:
                item["before"], item["after"] = before, []
                self.pending.append(item)
            self.items.append(item)
        if self.context:
            self.recent.append((number, text))

    def _before(self, number: int) -> list[str]:
        lines, expect = [], number - 1
        for line_number, text in reversed(self.recent):
            if line_number != expect:
                break
            lines.append(text)
            expect -= 1
        return lines[::-1]

    def _extend(self, match: dict, number: int, text: str) -> bool:
        """把紧接着的一行补进前面匹配的 after；返回这个匹配还要不要继续收"""
        expect = match["line"] + len(match["after"]) + 1
        if number > expect:   # 中间断开了
            return False
        if number == expect:
            if not self._take(len(text)):
                return False
            match["after"].append(text)
        return len(match["after"]) < self.context


class FileCollector:
    """rg --files / rg -l 的输出；条数到 limit 或路径总字符数放不下了（count_chars 时）就标记截断"""

    def __init__(self, limit: int, count_chars: bool = True):
        self.limit, self.count_chars = limit, count_chars
        self.items, self.truncated, self.chars = [], False, 0

    async def feed(self, line: bytes) -> None:
        path = rg_path(line.decode("utf-8", "replace").rstrip("\r"))
        if not path:
            return
        if len(self.items) >= self.limit or (self.count_chars and self.chars + len(path) > SEARCH_MAX_CHARS):
            self.truncated = True
            return
        self.chars += len(path)
        self.items.append(path)


async def read_head(stream, limit: int) -> bytes:
    """读完子进程的 stderr（免得写满管道卡住 rg），只留开头 limit 字节"""
    head = bytearray()
    while chunk := await stream.read(65536):
        if len(head) < limit:
            head += chunk[:limit - len(head)]
    return bytes(head)


async def run_rg(args: list[str], cwd: Path, collector) -> tuple[bool, int]:
    """跑 rg（参数数组，不经过 shell），边读边交给 collector；收满或超时就终止。返回 (是否超时, 丢掉的超长行数)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            RG, *args, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise CodeError("服务器上没有 ripgrep（rg），不能搜索代码", "rg_missing") from None
    except OSError as exc:
        raise CodeError(f"启动 ripgrep 失败（{type(exc).__name__}）", "search_failed") from None
    reader = LineReader(proc.stdout, JSON_LINE_LIMIT)
    errors = asyncio.create_task(read_head(proc.stderr, 4000))
    timed_out = False
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT):
            while not collector.truncated:
                line = await reader.readline()
                if line is None:
                    break
                await collector.feed(line)
    except TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        try:
            await proc.wait()
            stderr = await errors
        finally:
            errors.cancel()   # 被取消时不留下读 stderr 的任务
    # 0 有匹配、1 没匹配；2 出错（正则写错等）。有结果时的出错（个别文件读不了）只记日志
    if not timed_out and not collector.truncated and proc.returncode not in (0, 1):
        detail = stderr.decode("utf-8", "replace").strip()
        if not collector.items:
            raise CodeError(f"搜索失败：{detail[:500] or f'rg 退出码 {proc.returncode}'}", "search_failed")
        LOGGER.warning("ripgrep 退出码 %s：%s", proc.returncode, detail[:300])
    return timed_out, reader.dropped


# ---- 查找定义 ----

def ctags_env() -> dict:
    """ctags 只拿到 PATH 和 LANG；HOME 指向不存在的目录，读不到用户级选项文件（--options=NONE 之外再保险一层）"""
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "HOME": "/nonexistent/kubedoor-ctags"}
    if WINDOWS and "SYSTEMROOT" in os.environ:   # 本地开发：Windows 进程缺了 SYSTEMROOT 可能起不来
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


async def feed_stdin(stream, data: bytes) -> None:
    """写进子进程的 stdin 后关掉；进程提前退出（管道断开）不算错"""
    try:
        stream.write(data)
        await stream.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def text_or_none(value) -> str | None:
    return value if isinstance(value, str) and value else None


class SymbolCollector:
    """ctags --output-format=json 的输出：只留名字相等（kind、所属类型给了也要对上）的定义，没给名字时留全部（文件大纲）。
    条数到 limit、或各字段的总字符数放不下了就标记截断（停止收集，ctags 随即被终止）"""

    def __init__(self, name: str, owner: str | None, kind: str, limit: int):
        self.name, self.owner, self.kind, self.limit = name, owner, kind, limit
        self.items: list[dict] = []
        self.truncated, self.chars, self.lines = False, 0, 0

    def feed(self, line: bytes) -> None:
        self.lines += 1
        try:
            tag = json.loads(line)
        except ValueError:
            return
        if not isinstance(tag, dict) or tag.get("_type") != "tag" or not isinstance(tag.get("name"), str):
            return
        name, kind, scope = tag["name"], text_or_none(tag.get("kind")), text_or_none(tag.get("scope"))
        if (self.name and name != self.name) or (self.kind and kind != self.kind):
            return
        if self.owner and (scope is None or SCOPE_SPLIT.split(scope)[-1] != self.owner):
            return
        signature = text_or_none(tag.get("signature"))
        item = {"name": clip(name), "kind": kind, "path": rg_path(str(tag.get("path") or "")),
                "line": tag.get("line") if type(tag.get("line")) is int else None,
                "end": tag.get("end") if type(tag.get("end")) is int else None,
                "scope": clip(scope) if scope else None, "scope_kind": text_or_none(tag.get("scopeKind")),
                "signature": clip(signature) if signature else None}
        size = sum(len(value) for value in item.values() if isinstance(value, str))
        if len(self.items) >= self.limit or self.chars + size > SYMBOL_MAX_CHARS:
            self.truncated = True
            return
        self.chars += size
        self.items.append(item)


async def run_ctags(cwd: Path, paths: list[str], collector: SymbolCollector) -> bool:
    """对 paths 跑 universal-ctags（文件列表从 stdin 传），边读边交给 collector；收满或超时就终止。返回是否超时"""
    try:
        proc = await asyncio.create_subprocess_exec(
            # --options=NONE 必须是第一个参数：压缩包里的文件不可信，不能读代码目录里的 .ctags.d/*.ctags
            # （选项文件能改输出位置、加正则解析器）。字段：n 行号、K kind 全名、S 签名、e 结束行、p scope 的 kind；去掉 P pattern
            CTAGS, "--options=NONE", "-f", "-", "--output-format=json", "--sort=no", "--fields=+nKSep", "--fields=-P",
            "-L", "-", cwd=str(cwd), env=ctags_env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise CodeError("服务器上没有 universal-ctags（ctags），不能查找定义", "ctags_missing") from None
    except OSError as exc:
        raise CodeError(f"启动 ctags 失败（{type(exc).__name__}）", "symbols_failed") from None
    # -L 读进来的行以 -- 开头会被当成选项（实测 ctags 6.2）：路径一律带 ./ 前缀
    writer = asyncio.create_task(feed_stdin(proc.stdin, "".join(f"./{path}\n" for path in paths).encode()))
    reader = LineReader(proc.stdout, JSON_LINE_LIMIT)
    errors = asyncio.create_task(read_head(proc.stderr, 4000))
    timed_out = False
    try:
        async with asyncio.timeout(SYMBOL_TIMEOUT):
            while not collector.truncated:
                line = await reader.readline()
                if line is None:
                    break
                collector.feed(line)
    except TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        try:
            await proc.wait()
            stderr = await errors
        finally:
            errors.cancel()
            writer.cancel()
    if not timed_out and not collector.truncated and proc.returncode != 0 and not collector.lines:
        # 去掉 --options=NONE 固定打印的「Notice: No options will be read …」
        detail = "\n".join(line for line in scrub(stderr.decode("utf-8", "replace")).splitlines()
                           if line.strip() and ": Notice: " not in line)
        raise CodeError(f"查找定义失败：{detail[:500] or f'ctags 退出码 {proc.returncode}'}", "symbols_failed")
    if timed_out:
        LOGGER.info("ctags 超过 %s 秒，返回已解析的 %d 个定义", SYMBOL_TIMEOUT, len(collector.items))
    return timed_out


# ---- 读文件 ----

def read_line(file, keep: int) -> tuple[bytes, bool] | None:
    """按 \\n 读一行，最多保留 keep 字节、其余跳过不进内存；返回 (内容, 是否截断)，文件结束返回 None"""
    head = file.readline(keep)
    if not head:
        return None
    if head.endswith(b"\n"):
        return head[:-1], False
    cut = False
    while more := file.readline(65536):
        if more.endswith(b"\n"):
            cut = cut or bool(more.rstrip(b"\r\n"))
            break
        cut = cut or bool(more.rstrip(b"\r"))
    return head, cut


def count_rest(file) -> int:
    """从行首开始数剩下的行数（最后一行没有换行也算一行）"""
    lines, last = 0, b"\n"
    while chunk := file.read(1024 * 1024):
        lines += chunk.count(b"\n")
        last = chunk[-1:]
    return lines + (last != b"\n")


def line_text(raw: bytes, cut: bool, first: bool) -> str:
    text = raw.decode("utf-8", "replace").rstrip("\r")
    if first:
        text = text.removeprefix(BOM)
    if cut or len(text) > LINE_MAX_CHARS:
        return text[:LINE_MAX_CHARS] + CUT
    return text


class CodeCache:
    def __init__(self, root: str | Path | None = None, *, transport: httpx.AsyncBaseTransport | None = None, clock=time.time):
        self._root_arg, self._root = root, None
        self.transport, self.clock = transport, clock
        self._locks: dict[str, list] = {}    # code_id → [asyncio.Lock, 使用者数]；cleanup 不动正在获取的代码
        self._active: set[str] = set()       # 本进程正在下载、解压的临时目录名；清理残留时跳过
        self._swept = False
        self._cleanup_lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = resolve_root(self._root_arg)
        return self._root

    @contextlib.asynccontextmanager
    async def _locked(self, code_id: str):
        entry = self._locks.get(code_id)
        if entry is None:
            entry = self._locks[code_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(code_id, None)

    # ---- 获取 ----

    async def fetch(self, *, env: str, namespace: str, deployment: str, image: str,
                    download_url: str, tls_verify: bool = True) -> dict:
        """下载并解压。同一 (env, namespace, deployment, image) 只下载一次：缓存命中直接返回并更新最近使用时间；
        同一份代码并发调用只下载一次。返回 code_id、cached、file_count、bytes、skipped_files、top、languages、markers"""
        info = {"env": env, "namespace": namespace, "deployment": deployment, "image": image}
        for name, value in info.items():
            if not isinstance(value, str) or not value or len(value) > 1024 or CONTROL.search(value):
                raise CodeError(f"{name} 无效", "bad_request")
        if not valid_download_url(download_url):
            raise CodeError("下载地址无效：只支持 http / https", "bad_request")
        code_id = code_key(env, namespace, deployment, image)
        await asyncio.to_thread(self._prepare)
        async with self._locked(code_id):
            summary = await asyncio.to_thread(self._hit, code_id)
            cached = summary is not None
            if summary is None:
                summary = await self._obtain(code_id, info, download_url, bool(tls_verify))
        await self._cleanup(keep=code_id)
        return {"code_id": code_id, "cached": cached, **summary}

    def _prepare(self) -> None:
        root = self.root
        if not self._swept:
            self._swept = True
            self._sweep(root)

    def _hit(self, code_id: str) -> dict | None:
        directory = self.root / code_id
        data, last_used = load_meta(directory)
        now = self.clock()
        if data is None or now - last_used > CACHE_TTL or not (directory / data["root"]).is_dir():
            if directory.exists():   # 过期、损坏或没写完的
                remove_tree(directory)
            return None
        touch(directory / "meta.json", now)
        return {key: data[key] for key in SUMMARY_FIELDS}

    async def _obtain(self, code_id: str, info: dict, url: str, verify: bool) -> dict:
        tmp = self.root / f"{TMP_PREFIX}{secrets.token_hex(8)}"
        self._active.add(tmp.name)   # 先登记再建目录：并发的清理看到目录时一定知道它在用
        started = time.monotonic()
        LOGGER.info("下载代码 %s（%s/%s/%s，%s）：%s", code_id, info["env"], info["namespace"], info["deployment"],
                    info["image"], safe_url(url))
        try:
            try:
                await asyncio.to_thread(tmp.mkdir)
                size = await self._download(url, tmp / "archive", verify)
            except BaseException as exc:
                # 临时目录里只有下载了一部分的压缩包
                with contextlib.suppress(BaseException):
                    await asyncio.to_thread(functools.partial(shutil.rmtree, tmp, ignore_errors=True))
                if isinstance(exc, OSError):
                    raise disk_error(exc) from None
                raise
            summary = await self._extract(tmp, code_id, info)
        except CodeError as exc:
            LOGGER.warning("获取代码 %s 失败：%s", code_id, exc.message)
            raise
        finally:
            self._active.discard(tmp.name)
        LOGGER.info("代码 %s 就绪：压缩包 %s，%d 个文件共 %s，跳过 %d 个，用时 %.1f 秒", code_id, human(size),
                    summary["file_count"], human(summary["bytes"]), summary["skipped_files"], time.monotonic() - started)
        return summary

    async def _download(self, url: str, archive: Path, verify: bool) -> int:
        """流式写到 archive；Content-Length 超限直接拒绝，流式累计超限就中止。返回字节数"""
        out = open(archive, "wb")   # 不放线程：在等线程时被取消会漏掉文件句柄（Windows 上临时目录就删不掉了）
        total = 0
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                async with httpx.AsyncClient(follow_redirects=True, max_redirects=MAX_REDIRECTS, verify=verify,
                                             timeout=httpx.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT),
                                             transport=self.transport) as client:
                    # 压缩包再做传输压缩没有收益，还得在 event loop 里解压：要原样的字节
                    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                        if not response.is_success:
                            raise CodeError(f"下载代码失败：HTTP {response.status_code}（{safe_url(response.url)}）",
                                            "download_failed")
                        declared = response.headers.get("content-length", "")
                        if declared.isdigit() and int(declared) > MAX_ARCHIVE:
                            raise too_large()
                        pending, buffered = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_ARCHIVE:
                                raise too_large()
                            pending.append(chunk)
                            buffered += len(chunk)
                            if buffered >= WRITE_BUFFER:
                                await asyncio.to_thread(out.write, b"".join(pending))
                                pending, buffered = [], 0
                        if pending:
                            await asyncio.to_thread(out.write, b"".join(pending))
        except CodeError:
            raise
        except TimeoutError:
            raise CodeError(f"下载代码超时（超过 {DOWNLOAD_TIMEOUT} 秒）", "download_failed") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            message = download_failure(exc, url)
            LOGGER.info("%s：%s %s", message, type(exc).__name__, scrub(str(exc), url)[:300])
            # from None：异常链里的 httpx 异常带完整地址（含签名），不能被上层打进日志
            raise CodeError(message, "download_failed") from None
        finally:
            out.close()
        if not total:
            raise CodeError("下载到的内容是空的", "archive_invalid")
        return total

    async def _extract(self, tmp: Path, code_id: str, info: dict) -> dict:
        """在线程里解压；被取消时通知线程尽快停下，等它删掉临时目录再把取消往上抛"""
        stop = threading.Event()
        future = asyncio.get_running_loop().run_in_executor(None, functools.partial(self._install, tmp, code_id, info, stop))
        future.add_done_callback(lambda done: done.cancelled() or done.exception())   # 取消后才结束的线程不报「异常没取回」
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            stop.set()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(future), 60)
            raise

    def _install(self, tmp: Path, code_id: str, info: dict, stop: threading.Event) -> dict:
        try:
            archive, tree = tmp / "archive", tmp / TREE
            tree.mkdir()
            extractor = Extractor(tree, stop)
            extract(archive, extractor)
            archive.unlink()
            root = code_root_of(tree)
            summary = {**summarize(tmp / root, extractor.files), "skipped_files": extractor.skipped}
            now = self.clock()
            meta = {"version": META_VERSION, "code_id": code_id, **info,
                    "fetched_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                    "root": root, **summary}
            path = tmp / "meta.json"
            path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            touch(path, now)
            final = self.root / code_id
            if final.exists():
                remove_tree(final)
            os.rename(tmp, final)
            return {key: summary[key] for key in SUMMARY_FIELDS}
        except OSError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise disk_error(exc) from exc
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def meta(self, code_id: str) -> dict | None:
        """{"code_id","env","namespace","deployment","image","fetched_at","file_count","bytes"}；不存在、过期、格式不对返回 None。
        code_id 不是 16 位十六进制时直接返回 None（防路径注入）。同步方法，只读一个小 JSON"""
        code_id = normalize_id(code_id)
        if code_id is None:
            return None
        try:
            directory = self.root / code_id
        except CodeError:
            return None
        data, last_used = load_meta(directory)
        if data is None or self.clock() - last_used > CACHE_TTL:
            return None
        return {key: data[key] for key in META_FIELDS}

    def _locate(self, code_id: str) -> Path:
        """代码根目录；顺便刷新最近使用时间（在用的代码不过期）"""
        directory = self.root / code_id
        data, last_used = load_meta(directory)
        now = self.clock()
        base = directory / data["root"] if data else None
        if data is None or now - last_used > CACHE_TTL or not base.is_dir():
            raise CodeError("代码不存在或已过期，请重新获取代码", "not_found")
        touch(directory / "meta.json", now)
        return base

    # ---- 搜索、读文件、查找定义 ----

    async def search(self, code_id: str, *, pattern: str = "", glob: str = "", ignore_case: bool = False,
                     fixed_strings: bool = False, context: int = 0, max_results: int = SEARCH_DEFAULT_RESULTS) -> dict:
        """用 ripgrep 搜索；pattern 为空时按 glob 列出文件（相当于 rg --files -g）。
        返回 code_id、pattern、glob、matches（搜索时）或 paths（列文件时）、count、truncated（条数或总字符数到上限、
        超时都算）；超时另带 timed_out，有整行太长没列出的另带 omitted_long_lines"""
        code_id = require_id(code_id)
        pattern = require_text(pattern, "pattern", 1000)
        glob = require_glob(glob)
        context = require_int(context, "context", 0, 5)
        max_results = require_int(max_results, "max_results", 1, SEARCH_MAX_RESULTS)
        base = await asyncio.to_thread(self._locate, code_id)
        # 压缩包里的就是全部代码：搜隐藏文件，不读 .gitignore / .ignore；按路径排序，截断时结果稳定
        args = ["--no-config", "--hidden", "--no-ignore", "--sort", "path", "--path-separator", "/"]
        if pattern:
            args += ["--json", "--max-columns", str(LINE_MAX_CHARS * 4), "-C", str(context)]
            args += (["-i"] if ignore_case else []) + (["-F"] if fixed_strings else [])
        else:
            args.append("--files")
        if glob:
            args += ["-g", glob]
        if pattern:
            args += ["-e", pattern]
        args += ["--", "."]
        collector = MatchCollector(context, max_results) if pattern else FileCollector(max_results)
        timed_out, dropped = await run_rg(args, base, collector)
        if timed_out:
            LOGGER.info("搜索代码 %s 超过 %s 秒，返回已找到的 %d 条", code_id, SEARCH_TIMEOUT, len(collector.items))
        result = {"code_id": code_id, "pattern": pattern, "glob": glob,
                  "matches" if pattern else "paths": collector.items,
                  "count": len(collector.items), "truncated": collector.truncated or timed_out}
        if timed_out:
            result["timed_out"] = True
        if dropped:
            result["omitted_long_lines"] = dropped
        return result

    async def read(self, code_id: str, path: str, *, start_line: int = 1, lines: int = READ_DEFAULT_LINES) -> dict:
        """按行读文本文件。返回 code_id、path、start_line、end_line、total_lines、content（每行前面是行号 + Tab）、
        truncated（end_line 之后还有内容：行数或总字符数到上限）；超出文件末尾时 content 为空，靠 total_lines 说明"""
        code_id = require_id(code_id)
        rel = require_path(path)
        start = max(1, require_int(start_line, "start_line"))
        count = min(max(1, require_int(lines, "lines")), READ_MAX_LINES)
        return await asyncio.to_thread(self._read, code_id, rel, start, count)

    def _source(self, base: Path, rel: str) -> Path:
        """代码根目录下 rel 对应的普通文件；resolve 后必须还在代码根目录内"""
        try:
            resolved, inside = base.joinpath(*rel.split("/")).resolve(), base.resolve()
        except (OSError, RuntimeError):
            raise CodeError(f"文件不存在：{rel}", "not_found") from None
        if not resolved.is_relative_to(inside):
            raise CodeError("path 必须在代码目录内", "bad_request")
        if resolved.is_dir():
            raise CodeError(f"{rel} 是目录，不是文件；可以用搜索按 glob 列出文件", "not_found")
        if not resolved.is_file():
            raise CodeError(f"文件不存在：{rel}", "not_found")
        return resolved

    def _read(self, code_id: str, rel: str, start: int, count: int) -> dict:
        resolved = self._source(self._locate(code_id), rel)
        keep, out, number, chars = LINE_MAX_CHARS * 4, [], 0, 0
        try:
            with open(resolved, "rb") as file:
                if b"\0" in file.read(8192):
                    raise CodeError("二进制文件，不能按文本读取", "bad_request")
                file.seek(0)
                while number < start + count - 1:
                    line = read_line(file, keep)
                    if line is None:
                        break
                    number += 1
                    if number >= start:
                        entry = f"{number}\t{line_text(*line, first=number == 1)}"
                        chars += len(entry) + (1 if out else 0)
                        if chars > READ_MAX_CHARS:   # 这一行放不下了：在它之前截止
                            number += count_rest(file)
                            break
                        out.append(entry)
                else:   # 要的行读满了：剩下的只数行数
                    number += count_rest(file)
        except FileNotFoundError:
            raise CodeError(f"文件不存在：{rel}", "not_found") from None
        except OSError as exc:
            raise CodeError(f"读取 {rel} 失败（{type(exc).__name__}）") from None
        end = start + len(out) - 1
        return {"code_id": code_id, "path": rel, "start_line": start, "end_line": end, "total_lines": number,
                "content": "\n".join(out), "truncated": end < number}

    async def symbols(self, code_id: str, *, name: str = "", path: str = "", kind: str = "",
                      max_results: int = SYMBOL_DEFAULT_RESULTS) -> dict:
        """用 universal-ctags 找定义。name 区分大小写精确匹配，可写成限定名 Outer.Inner.method、Class::method、
        Class#method（倒数第二段和 ctags 输出的 scope 最后一段比较）；只给 path 返回这个文件的大纲，两个都给只在这个
        文件里找；kind 按 ctags 的 kind 全名过滤。只给 name 时先用 rg 找出含这个词的文件，最多 SYMBOL_MAX_FILES 个。
        返回 code_id、name、path、kind、symbols（name、kind、path、line、end、scope、scope_kind、signature，按 path、
        line 排序，缺的字段为 None）、count、truncated、scanned（跑 ctags 的文件数）；候选文件太多时另带 hint"""
        code_id = require_id(code_id)
        name = require_text(name, "name", 200)
        last, owner = parse_symbol(name)
        rel = require_path(path) if path else ""
        kind = require_text(kind, "kind", 50)
        max_results = require_int(max_results, "max_results", 1, SYMBOL_MAX_RESULTS)
        if not last and not rel:
            raise CodeError("name 和 path 至少要给一个", "bad_request")
        base = await asyncio.to_thread(self._locate, code_id)
        truncated, hint = False, None
        if rel:
            await asyncio.to_thread(self._source, base, rel)
            candidates = [rel]
        else:
            finder = FileCollector(SYMBOL_MAX_FILES, count_chars=False)
            timed_out, _ = await run_rg(["--no-config", "-l", "-w", "-F", "--hidden", "--no-ignore", "--sort", "path",
                                         "--path-separator", "/", "-e", last, "--", "."], base, finder)
            candidates = finder.items
            if finder.truncated:
                truncated, hint = True, SYMBOL_HINT
            truncated = truncated or timed_out
        found = SymbolCollector(last, owner, kind, max_results)
        if candidates:
            timed_out = await run_ctags(base, candidates, found)
            truncated = truncated or found.truncated or timed_out
        items = sorted(found.items, key=lambda item: (item["path"], item["line"] or 0))
        result = {"code_id": code_id, "name": name, "path": rel, "kind": kind, "symbols": items, "count": len(items),
                  "truncated": truncated, "scanned": len(candidates)}
        if hint:
            result["hint"] = hint
        return result

    # ---- 清理 ----

    async def cleanup(self) -> None:
        """删除过期（最近使用超过 CACHE_TTL）的代码、超出总量的代码（按最近使用时间从旧到新删）、残留的临时目录"""
        await self._cleanup()

    async def _cleanup(self, keep: str | None = None) -> None:
        async with self._cleanup_lock:
            try:
                await asyncio.to_thread(self._clean, keep)
            except (CodeError, OSError) as exc:
                LOGGER.warning("清理代码缓存失败：%s", exc.message if isinstance(exc, CodeError) else type(exc).__name__)

    def _clean(self, keep: str | None) -> None:
        root, now = self.root, self.clock()
        self._swept = True
        self._sweep(root)
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries if CODE_ID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)]
        live, removed = [], []
        for name in names:
            # 刚获取的、正在获取的不删，但大小照样算进总量
            protected = name == keep or name in self._locks
            data, last_used = load_meta(root / name)
            if data is None or now - last_used > CACHE_TTL:
                if not protected:
                    remove_tree(root / name)
                    removed.append(name)
                continue
            live.append((last_used, name, data["bytes"], protected))
        total = sum(item[2] for item in live)
        for _, name, size, protected in sorted(live):
            if total <= CACHE_LIMIT:
                break
            if not protected:
                remove_tree(root / name)
                removed.append(name)
                total -= size
        if removed:
            LOGGER.info("清理代码缓存：删除 %d 份（%s）", len(removed), "、".join(removed))

    def _sweep(self, root: Path) -> None:
        """删掉残留的临时目录：上次进程没做完的下载、解压，和没删干净的旧代码"""
        with os.scandir(root) as entries:
            stale = [Path(entry.path) for entry in entries
                     if entry.name.startswith(TMP_PREFIX) and entry.name not in self._active]
        for path in stale:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()
