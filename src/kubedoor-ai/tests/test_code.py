"""代码缓存（kubedoor_ai/code.py）：下载、安全解压、缓存命中与清理、ripgrep 搜索、按行读文件"""
import asyncio
import gzip
import io
import json
import logging
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from kubedoor_ai import code
from kubedoor_ai.domain import redact

SECRET = "TOPSECRETSIG"
URL = f"https://gitlab.example.com/api/v4/projects/7/repository/archive.tar.gz?sha=abc&expires=1700000000&sig={SECRET}"
SAFE_URL = "https://gitlab.example.com/api/v4/projects/7/repository/archive.tar.gz"
INFO = {"env": "cluster-a", "namespace": "ops", "deployment": "order", "image": "registry.example.com/ops/order:1.0"}
JAVA = """package demo;

public class OrderService {
    // 下单入口
    public void createOrder(String id) {
        log.info("create order {}", id);
        throw new IllegalStateException("库存不足");
    }
}
"""
PROJECT = {
    "order-abc123/pom.xml": b"<project/>\n",
    "order-abc123/README.md": b"# Order\nCreateOrder docs\n",
    "order-abc123/src/main/java/demo/OrderService.java": JAVA.encode(),
    "order-abc123/src/main/resources/application.yml": b"server:\n  port: 8080\n",
}
BOM = chr(0xFEFF)
READ_FILES = {
    "proj/src/Main.java": "".join(f"line{i}\n" for i in range(1, 11)).encode(),
    "proj/bin.dat": b"PNG\0\0data",
    "proj/long.txt": ("a" * 2000 + "\n" + "b" * 10 + "\n").encode(),
    "proj/crlf.txt": b"one\r\ntwo\r\nthree",
    "proj/bom.txt": (BOM + "hello\nworld\n").encode(),
    "proj/empty.txt": b"",
    "proj/中文.md": "第一行\n第二行\n".encode(),
}
SEARCH_FILES = {
    "order-abc123/src/main/java/demo/OrderService.java": JAVA.encode(),
    "order-abc123/src/main/resources/application.yml": b"server:\n  port: 8080\norder:\n  timeout: 30\n",
    "order-abc123/README.md": b"# Order\nCreateOrder docs\n",
    "order-abc123/web/app.min.js": ("var a='" + "x" * 3000 + "';createOrder();\n").encode(),
    "order-abc123/.gitlab-ci.yml": b"stages: [build]\n",
}
JAVA_PATH = "src/main/java/demo/OrderService.java"


def tar_bytes(entries, mode="w:gz") -> bytes:
    """entries：{名字: 内容}，或按顺序的 [(名字, 内容) | TarInfo]"""
    items = entries.items() if isinstance(entries, dict) else entries
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for item in items:
            if isinstance(item, tarfile.TarInfo):
                tar.addfile(item)
                continue
            name, data = item
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def special(name, kind, linkname=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info


def zip_bytes(files: dict, links=()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


class Server:
    """MockTransport 的处理函数：返回预设的响应，记下每次请求的地址"""

    def __init__(self, body=b"", status=200, headers=None, delay=0.0):
        self.body, self.status, self.headers, self.delay = body, status, headers or {}, delay
        self.calls = []

    async def __call__(self, request):
        self.calls.append(str(request.url))
        if self.delay:
            await asyncio.sleep(self.delay)
        return httpx.Response(self.status, content=self.body, headers=self.headers)


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_cache(tmp_path, handler, clock=None):
    return code.CodeCache(tmp_path / "cache", transport=httpx.MockTransport(handler), **({"clock": clock} if clock else {}))


async def fetch(cache, url=URL, **override):
    return await cache.fetch(**{**INFO, **override}, download_url=url)


async def fetch_error(cache, url=URL) -> code.CodeError:
    with pytest.raises(code.CodeError) as caught:
        await fetch(cache, url)
    return caught.value


def all_files(path: Path) -> list[str]:
    return sorted(item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file())


def code_files(cache, code_id) -> list[str]:
    """代码根目录下的全部文件"""
    meta = json.loads((cache.root / code_id / "meta.json").read_text(encoding="utf-8"))
    return all_files(cache.root / code_id / meta["root"])


def assert_hidden(error, caplog, *extra):
    """错误信息和日志里不能出现下载地址的查询参数（签名）"""
    for secret in (SECRET, "expires=", "sig=", *extra):
        assert secret not in error.message and secret not in str(error), secret
        assert secret not in caplog.text, secret


async def readable(tmp_path, clock=None):
    cache = make_cache(tmp_path, Server(tar_bytes(READ_FILES)), clock)
    return cache, (await fetch(cache))["code_id"]


async def searchable(tmp_path):
    cache = make_cache(tmp_path, Server(tar_bytes(SEARCH_FILES)))
    return cache, (await fetch(cache))["code_id"]


@pytest.fixture
def rg(monkeypatch):
    """搜索用例要真的 ripgrep：KUBEDOOR_TEST_RG 指定路径，没有就找 PATH 里的 rg，都没有就跳过"""
    path = os.environ.get("KUBEDOOR_TEST_RG") or shutil.which("rg")
    if not path or not Path(path).is_file():
        pytest.skip("没有 ripgrep：设置 KUBEDOOR_TEST_RG 指向 rg 可执行文件后再跑搜索用例")
    monkeypatch.setattr(code, "RG", path)
    return path


# ---- 下载与解压 ----

@pytest.mark.asyncio
async def test_fetch_tar_gz_summary_and_cache_hit(tmp_path):
    server = Server(tar_bytes(PROJECT))
    cache = make_cache(tmp_path, server)
    first = await fetch(cache)
    assert first == {"code_id": code.code_key(*INFO.values()), "cached": False, "file_count": 4,
                     "bytes": sum(map(len, PROJECT.values())), "skipped_files": 0,
                     "top": ["src/", "pom.xml", "README.md"],
                     "languages": {"java": 1, "md": 1, "xml": 1, "yml": 1}, "markers": ["pom.xml"]}
    assert len(first["code_id"]) == 16 and int(first["code_id"], 16) >= 0
    # 只有一个顶层目录（GitLab archive 的 <项目>-<sha>/）：把它当代码根目录
    assert code_files(cache, first["code_id"]) == sorted(path.split("/", 1)[1] for path in PROJECT)
    assert await fetch(cache) == {**first, "cached": True}
    assert len(server.calls) == 1
    other = await fetch(cache, image="registry.example.com/ops/order:2.0")
    assert other["code_id"] != first["code_id"] and other["cached"] is False and len(server.calls) == 2


@pytest.mark.asyncio
async def test_fetch_zip_single_top_dir(tmp_path):
    cache = make_cache(tmp_path, Server(zip_bytes(PROJECT)))
    result = await fetch(cache, url="https://gitlab.example.com/archive.zip")
    assert (result["file_count"], result["top"], result["markers"]) == (4, ["src/", "pom.xml", "README.md"], ["pom.xml"])
    assert JAVA_PATH in code_files(cache, result["code_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["w:bz2", "w:xz", "w:"])
async def test_fetch_other_tar_formats_without_single_top(tmp_path, mode):
    files = {"package.json": b"{}\n", "go.mod": b"module demo\n", "src/index.ts": b"export {}\n", "cmd/main.go": b"package main\n"}
    cache = make_cache(tmp_path, Server(tar_bytes(files, mode)))
    result = await fetch(cache)
    assert result["top"] == ["cmd/", "src/", "go.mod", "package.json"]
    assert result["markers"] == ["package.json", "go.mod"]
    assert result["languages"] == {"go": 1, "json": 1, "mod": 1, "ts": 1}
    assert code_files(cache, result["code_id"]) == sorted(files)


@pytest.mark.asyncio
async def test_concurrent_fetch_downloads_once(tmp_path):
    server = Server(tar_bytes(PROJECT), delay=0.05)
    cache = make_cache(tmp_path, server)
    results = await asyncio.gather(*(fetch(cache) for _ in range(3)))
    assert len(server.calls) == 1
    assert sorted(result["cached"] for result in results) == [False, True, True]
    assert len({result["code_id"] for result in results}) == 1
    assert cache._locks == {}   # 锁用完就释放


@pytest.mark.asyncio
async def test_follows_redirect_to_object_storage(tmp_path):
    body = tar_bytes(PROJECT)

    def handler(request):
        if request.url.host == "gitlab.example.com":
            return httpx.Response(302, headers={"Location": "https://minio.example.com/code/o.tar.gz?X-Amz-Signature=RSIG"})
        return httpx.Response(200, content=body)

    assert (await fetch(make_cache(tmp_path, handler)))["file_count"] == 4


@pytest.mark.asyncio
async def test_unsafe_tar_members_are_skipped(tmp_path):
    absolute = f"/kubedoor-code-test-{uuid.uuid4().hex}.txt"
    entries = [("proj/ok.txt", b"ok\n"), ("../../../evil1.txt", b"x"), ("proj/../../../evil2.txt", b"x"),
               (absolute, b"x"), ("C:/evil3.txt", b"x"), ("proj\\..\\..\\evil4.txt", b"x"),
               special("proj/sym", tarfile.SYMTYPE, "/etc/passwd"), special("proj/hard", tarfile.LNKTYPE, "proj/ok.txt"),
               special("proj/fifo", tarfile.FIFOTYPE), special("proj/dev", tarfile.CHRTYPE)]
    cache = make_cache(tmp_path, Server(tar_bytes(entries)))
    result = await fetch(cache)
    assert (result["file_count"], result["skipped_files"], result["top"]) == (1, 9, ["ok.txt"])
    root = tmp_path / "cache"
    # 只写出了安全的文件：缓存根目录里只有这份代码，代码目录里只有 meta.json 和 tree/
    assert sorted(item.name for item in root.iterdir()) == [result["code_id"]]
    assert sorted(item.name for item in (root / result["code_id"]).iterdir()) == ["meta.json", "tree"]
    assert all_files(root / result["code_id"] / "tree") == ["proj/ok.txt"]
    assert all(path.startswith("cache/") for path in all_files(tmp_path))
    assert not list(tmp_path.parent.glob("evil*.txt")) and not Path(absolute).exists()


@pytest.mark.asyncio
async def test_unsafe_zip_members_are_skipped(tmp_path):
    absolute = f"/kubedoor-code-test-{uuid.uuid4().hex}.txt"
    files = {"proj/ok.txt": b"ok\n", "../../../evil1.txt": b"x", "proj/../../../evil2.txt": b"x", absolute: b"x"}
    cache = make_cache(tmp_path, Server(zip_bytes(files, links=[("proj/link", "/etc/passwd")])))
    result = await fetch(cache)
    assert (result["file_count"], result["skipped_files"]) == (1, 4)
    assert all_files(cache.root / result["code_id"] / "tree") == ["proj/ok.txt"]
    assert all(path.startswith("cache/") for path in all_files(tmp_path))
    assert not list(tmp_path.parent.glob("evil*.txt")) and not Path(absolute).exists()


@pytest.mark.asyncio
async def test_file_under_skipped_symlink_stays_inside(tmp_path):
    # 先来一个指向外面的符号链接，再来一个「穿过」它的文件：链接被跳过，文件写进代码目录里的普通目录
    entries = [special("proj/escape", tarfile.SYMTYPE, "../../.."), ("proj/escape/evil.txt", b"x")]
    cache = make_cache(tmp_path, Server(tar_bytes(entries)))
    result = await fetch(cache)
    tree = cache.root / result["code_id"] / "tree"
    assert all_files(tree) == ["proj/escape/evil.txt"] and result["skipped_files"] == 1
    assert not (tree / "proj" / "escape").is_symlink()
    assert all(path.startswith("cache/") for path in all_files(tmp_path))


def test_zip_names_without_utf8_flag():
    info = zipfile.ZipInfo("proj/" + "说明.txt".encode("gbk").decode("cp437"))
    assert code.zip_name(info) == "proj/说明.txt"
    info = zipfile.ZipInfo("proj/" + "说明.txt".encode().decode("cp437"))
    assert code.zip_name(info) == "proj/说明.txt"
    info = zipfile.ZipInfo("proj/a.txt")
    info.flag_bits |= 0x800
    assert code.zip_name(info) == "proj/a.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("pack", [tar_bytes, zip_bytes])
async def test_extract_bomb_is_stopped(tmp_path, monkeypatch, pack):
    monkeypatch.setattr(code, "MAX_EXTRACTED", 10_000)
    cache = make_cache(tmp_path, Server(pack({f"proj/zero{i}.txt": b"\0" * 4000 for i in range(4)})))
    error = await fetch_error(cache)
    assert error.code == "extract_limit"
    assert list((tmp_path / "cache").iterdir()) == []   # 临时目录清理干净，没有留下半份代码


@pytest.mark.asyncio
@pytest.mark.parametrize("pack, count, words", [(tar_bytes, 5, "文件"), (zip_bytes, 5, "文件"), (zip_bytes, 7, "条目")])
async def test_too_many_files(tmp_path, monkeypatch, pack, count, words):
    monkeypatch.setattr(code, "MAX_FILES", 3)
    cache = make_cache(tmp_path, Server(pack({f"proj/{i}.txt": b"x" for i in range(count)})))
    error = await fetch_error(cache)
    assert error.code == "extract_limit" and words in error.message
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pack", [tar_bytes, zip_bytes])
async def test_large_file_is_skipped(tmp_path, monkeypatch, pack):
    monkeypatch.setattr(code, "MAX_FILE", 100)
    cache = make_cache(tmp_path, Server(pack({"proj/big.bin": b"x" * 200, "proj/small.txt": b"y" * 50})))
    result = await fetch(cache)
    assert (result["file_count"], result["bytes"], result["skipped_files"]) == (1, 50, 1)
    assert all_files(cache.root / result["code_id"] / "tree") == ["proj/small.txt"]


@pytest.mark.asyncio
async def test_content_length_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(code, "MAX_ARCHIVE", 100)
    error = await fetch_error(make_cache(tmp_path, Server(b"x" * 200)))
    assert error.code == "archive_too_large"
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.asyncio
async def test_streamed_size_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(code, "MAX_ARCHIVE", 100)

    async def chunks():
        for _ in range(10):
            yield b"x" * 50

    def handler(request):
        response = httpx.Response(200, content=chunks())
        assert "content-length" not in response.headers
        return response

    error = await fetch_error(make_cache(tmp_path, handler))
    assert error.code == "archive_too_large"
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.asyncio
async def test_http_error_hides_query(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="kubedoor_ai.code")
    error = await fetch_error(make_cache(tmp_path, Server(b"not found", status=404)))
    assert error.code == "download_failed" and "HTTP 404" in error.message and SAFE_URL in error.message
    assert_hidden(error, caplog)
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.asyncio
async def test_redirect_error_hides_signatures(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="kubedoor_ai.code")

    def handler(request):
        if request.url.host == "gitlab.example.com":
            return httpx.Response(302, headers={"Location": "https://minio.example.com/code/o.tar.gz?X-Amz-Signature=RSIG"})
        return httpx.Response(403, content=b"<Error>expired</Error>")

    error = await fetch_error(make_cache(tmp_path, handler))
    assert "HTTP 403" in error.message and "https://minio.example.com/code/o.tar.gz" in error.message
    assert_hidden(error, caplog, "RSIG", "X-Amz")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, words", [("connect", "无法连接"), ("tls", "证书"), ("timeout", "超时"), ("redirects", "重定向")])
async def test_transport_errors_hide_query(tmp_path, caplog, failure, words):
    caplog.set_level(logging.DEBUG, logger="kubedoor_ai.code")

    def handler(request):
        # 异常信息里故意带上完整地址：日志里也得去掉签名
        if failure == "connect":
            raise httpx.ConnectError(f"connection refused: {request.url}", request=request)
        if failure == "tls":
            try:
                raise ssl.SSLCertVerificationError(1, "certificate verify failed")
            except ssl.SSLError as exc:
                raise httpx.ConnectError(f"[SSL: CERTIFICATE_VERIFY_FAILED] {request.url}", request=request) from exc
        if failure == "timeout":
            raise httpx.ReadTimeout(f"timed out: {request.url}", request=request)
        return httpx.Response(302, headers={"Location": str(request.url)})   # 自己跳自己

    error = await fetch_error(make_cache(tmp_path, handler))
    assert error.code == "download_failed" and words in error.message and SAFE_URL in error.message
    assert error.__cause__ is None and error.__suppress_context__   # 异常链里带完整地址的 httpx 异常不往上带
    assert_hidden(error, caplog)


@pytest.mark.asyncio
async def test_download_total_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(code, "DOWNLOAD_TIMEOUT", 0.05)
    error = await fetch_error(make_cache(tmp_path, Server(tar_bytes(PROJECT), delay=2)))
    assert error.code == "download_failed" and "超时" in error.message
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body, words", [
    (b"<!DOCTYPE html><html>login</html>", "网页"),
    (b"hello world, not an archive", "不是支持的压缩包"),
    (gzip.compress(b"just a gzip, not a tar " * 40), "压缩包"),
    (tar_bytes(PROJECT)[:60], "压缩包"),
    (b"", "空"),
])
async def test_not_an_archive(tmp_path, body, words):
    error = await fetch_error(make_cache(tmp_path, Server(body)))
    assert error.code == "archive_invalid" and words in error.message
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_fetch_leaves_nothing(tmp_path):
    cache = make_cache(tmp_path, Server(tar_bytes(PROJECT), delay=5))
    task = asyncio.create_task(fetch(cache))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "cache").iterdir()) == [] and cache._locks == {} and cache._active == set()


@pytest.mark.asyncio
async def test_cancel_during_extract_stops_thread_and_cleans_up(tmp_path, monkeypatch):
    started = threading.Event()

    def endless_extract(archive, extractor):
        started.set()
        while True:   # 一直解压不完，直到收到取消通知
            extractor.check_stop()
            time.sleep(0.01)

    monkeypatch.setattr(code, "extract", endless_extract)
    cache = make_cache(tmp_path, Server(tar_bytes(PROJECT)))
    task = asyncio.create_task(fetch(cache))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "cache").iterdir()) == [] and cache._active == set()


@pytest.mark.asyncio
async def test_download_request_has_no_credentials(tmp_path):
    seen, body = [], tar_bytes(PROJECT)

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, content=body)

    await fetch(make_cache(tmp_path, handler))
    assert seen[0]["accept-encoding"] == "identity"
    assert "authorization" not in seen[0] and "cookie" not in seen[0]


@pytest.mark.asyncio
async def test_fetch_rejects_bad_arguments(tmp_path):
    server = Server(tar_bytes(PROJECT))
    cache = make_cache(tmp_path, server)
    for override, url in (({"env": ""}, URL), ({"namespace": None}, URL), ({"image": "a\nb"}, URL),
                          ({}, "ftp://example.com/a.tar.gz"), ({}, "not a url"), ({}, None)):
        with pytest.raises(code.CodeError) as caught:
            await cache.fetch(**{**INFO, **override}, download_url=url)
        assert caught.value.code == "bad_request"
    assert server.calls == []


def test_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CODE_CACHE_DIR", str(tmp_path / "from-env"))
    assert code.CodeCache().root == tmp_path / "from-env" and (tmp_path / "from-env").is_dir()


def test_safe_url_and_scrub():
    assert code.safe_url("https://user:pw@[::1]:8443/a/b.tar.gz?sig=x#frag") == "https://[::1]:8443/a/b.tar.gz"
    assert code.scrub(f"failed: {URL} (sig={SECRET})", URL) == f"failed: {SAFE_URL} (sig=***)"


# ---- meta ----

def test_meta_rejects_invalid_ids(tmp_path):
    cache = code.CodeCache(tmp_path / "cache")
    for value in (None, 123, "", "abc", "0" * 15, "0" * 17, "g" * 16, "../../etc/passwd", "..%2f..%2fpasswd", "/" * 16):
        assert cache.meta(value) is None
    assert cache.meta("0123456789abcdef") is None   # 格式对，但没有这份代码


@pytest.mark.asyncio
async def test_meta_fields_and_expiry(tmp_path):
    clock, server = Clock(), Server(tar_bytes(PROJECT))
    cache = make_cache(tmp_path, server, clock)
    result = await fetch(cache)
    meta = cache.meta(result["code_id"])
    assert meta == {"code_id": result["code_id"], **INFO, "file_count": 4, "bytes": result["bytes"],
                    "fetched_at": datetime.fromtimestamp(clock.now, timezone.utc).isoformat(timespec="seconds")}
    assert cache.meta(result["code_id"].upper()) == meta
    clock.now += code.CACHE_TTL + 1
    assert cache.meta(result["code_id"]) is None
    again = await fetch(cache)   # 过期的不算命中：重新下载
    assert again["cached"] is False and len(server.calls) == 2 and cache.meta(result["code_id"])


@pytest.mark.asyncio
async def test_meta_tampered_is_ignored(tmp_path):
    server = Server(tar_bytes(PROJECT))
    cache = make_cache(tmp_path, server)
    result = await fetch(cache)
    path = cache.root / result["code_id"] / "meta.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for broken in ({**data, "root": "../.."}, {**data, "file_count": "4"}, {**data, "code_id": "0" * 16}, ["x"]):
        path.write_text(json.dumps(broken), encoding="utf-8")
        assert cache.meta(result["code_id"]) is None
    path.write_text("{not json", encoding="utf-8")
    assert cache.meta(result["code_id"]) is None
    again = await fetch(cache)   # 损坏的不算命中：重新下载
    assert again["cached"] is False and len(server.calls) == 2 and cache.meta(result["code_id"])


# ---- 读文件 ----

@pytest.mark.asyncio
async def test_read_line_range(tmp_path):
    cache, code_id = await readable(tmp_path)
    result = await cache.read(code_id, "src/Main.java", start_line=3, lines=2)
    assert result == {"code_id": code_id, "path": "src/Main.java", "start_line": 3, "end_line": 4, "total_lines": 10,
                      "content": "3\tline3\n4\tline4", "truncated": True}
    whole = await cache.read(code_id, "./src//Main.java")
    assert (whole["path"], whole["end_line"], whole["total_lines"], whole["truncated"]) == ("src/Main.java", 10, 10, False)
    assert whole["content"].split("\n")[0] == "1\tline1" and whole["content"].split("\n")[-1] == "10\tline10"
    assert (await cache.read(code_id, "中文.md"))["content"] == "1\t第一行\n2\t第二行"


@pytest.mark.asyncio
async def test_read_past_end_and_limits(tmp_path, monkeypatch):
    cache, code_id = await readable(tmp_path)
    past = await cache.read(code_id, "src/Main.java", start_line=20)
    assert (past["content"], past["total_lines"], past["end_line"], past["truncated"]) == ("", 10, 19, False)
    empty = await cache.read(code_id, "empty.txt")
    assert (empty["content"], empty["total_lines"], empty["end_line"], empty["truncated"]) == ("", 0, 0, False)
    monkeypatch.setattr(code, "READ_MAX_LINES", 3)
    clamped = await cache.read(code_id, "src/Main.java", start_line=0, lines=1000)
    assert (clamped["start_line"], clamped["end_line"], clamped["total_lines"], clamped["truncated"]) == (1, 3, 10, True)
    assert (await cache.read(code_id, "src/Main.java", lines=0))["end_line"] == 1
    with pytest.raises(code.CodeError) as caught:
        await cache.read(code_id, "src/Main.java", lines="10")
    assert caught.value.code == "bad_request"


@pytest.mark.asyncio
async def test_read_total_chars_limit(tmp_path, monkeypatch):
    cache, code_id = await readable(tmp_path)
    monkeypatch.setattr(code, "READ_MAX_CHARS", 30)
    result = await cache.read(code_id, "src/Main.java")
    # 每行 "N\tlineN" 7 个字符加换行：前 3 行共 23 个字符，第 4 行放不下，在它之前截止
    assert result["content"] == "1\tline1\n2\tline2\n3\tline3"
    assert (result["end_line"], result["total_lines"], result["truncated"]) == (3, 10, True)


@pytest.mark.asyncio
async def test_read_rejects_traversal(tmp_path):
    cache, code_id = await readable(tmp_path)
    for path in ("../meta.json", "../../meta.json", "src/../../meta.json", "/etc/passwd", "C:/Windows/win.ini",
                 "src\\Main.java", "", ".", "a\0b", None):
        with pytest.raises(code.CodeError) as caught:
            await cache.read(code_id, path)
        assert caught.value.code == "bad_request", path


@pytest.mark.asyncio
async def test_read_refuses_symlink_out_of_tree(tmp_path):
    cache, code_id = await readable(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    try:
        os.symlink(outside, cache.root / code_id / "tree" / "proj" / "escape.txt")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不能创建符号链接")
    with pytest.raises(code.CodeError) as caught:
        await cache.read(code_id, "escape.txt")
    assert caught.value.code == "bad_request"


@pytest.mark.asyncio
async def test_read_binary_directory_and_missing(tmp_path):
    cache, code_id = await readable(tmp_path)
    for path, (kind, words) in {"bin.dat": ("bad_request", "二进制"), "src": ("not_found", "目录"),
                                "nope.txt": ("not_found", "不存在")}.items():
        with pytest.raises(code.CodeError) as caught:
            await cache.read(code_id, path)
        assert caught.value.code == kind and words in caught.value.message, path
    for bad_id, kind in (("0123456789abcdef", "not_found"), ("../etc", "bad_request"), (None, "bad_request")):
        with pytest.raises(code.CodeError) as caught:
            await cache.read(bad_id, "src/Main.java")
        assert caught.value.code == kind


@pytest.mark.asyncio
async def test_read_long_lines_crlf_and_bom(tmp_path):
    cache, code_id = await readable(tmp_path)
    long = (await cache.read(code_id, "long.txt"))["content"].split("\n")
    assert long == ["1\t" + "a" * code.LINE_MAX_CHARS + code.CUT, "2\t" + "b" * 10]
    crlf = await cache.read(code_id, "crlf.txt")
    assert (crlf["content"], crlf["total_lines"]) == ("1\tone\n2\ttwo\n3\tthree", 3)
    assert (await cache.read(code_id, "bom.txt"))["content"] == "1\thello\n2\tworld"


@pytest.mark.asyncio
async def test_results_survive_gateway_redaction(tmp_path):
    # 网关把结果交给模型前会调 domain.redact：键名不能撞上 files、token 这类会被整个替换掉的
    cache, code_id = await readable(tmp_path)
    for result in (await fetch(cache), cache.meta(code_id), await cache.read(code_id, "src/Main.java")):
        assert redact(result) == result


# ---- 清理 ----

@pytest.mark.asyncio
async def test_cleanup_removes_expired(tmp_path):
    clock = Clock()
    cache = make_cache(tmp_path, Server(tar_bytes(PROJECT)), clock)
    first = await fetch(cache)
    clock.now += 100
    second = await fetch(cache, image="registry.example.com/ops/order:2.0")
    clock.now += code.CACHE_TTL - 50   # first 过期了，second 还没有
    await cache.cleanup()
    assert not (cache.root / first["code_id"]).exists()
    assert cache.meta(second["code_id"]) is not None


@pytest.mark.asyncio
async def test_cleanup_total_limit_removes_least_recently_used(tmp_path, monkeypatch):
    clock = Clock()
    cache = make_cache(tmp_path, Server(tar_bytes({"proj/a.txt": b"x" * 1000})), clock)
    monkeypatch.setattr(code, "CACHE_LIMIT", 2500)   # 放得下两份
    ids = {}
    for tag in ("a", "b", "c"):
        clock.now += 10
        ids[tag] = (await fetch(cache, image=f"img:{tag}"))["code_id"]
    assert cache.meta(ids["a"]) is None and cache.meta(ids["b"]) and cache.meta(ids["c"])
    clock.now += 10
    assert (await fetch(cache, image="img:b"))["cached"] is True   # b 成了最近用过的
    clock.now += 10
    ids["d"] = (await fetch(cache, image="img:d"))["code_id"]
    assert sorted(item.name for item in cache.root.iterdir()) == sorted([ids["b"], ids["d"]])


@pytest.mark.asyncio
async def test_reading_keeps_code_alive(tmp_path):
    clock = Clock()
    cache, code_id = await readable(tmp_path, clock)
    clock.now += code.CACHE_TTL - 10
    await cache.read(code_id, "src/Main.java", lines=1)
    clock.now += code.CACHE_TTL - 10
    assert cache.meta(code_id) is not None
    clock.now += 20
    assert cache.meta(code_id) is None
    with pytest.raises(code.CodeError) as caught:
        await cache.read(code_id, "src/Main.java")
    assert caught.value.code == "not_found"


@pytest.mark.asyncio
async def test_stale_temp_dirs_are_removed(tmp_path):
    root = tmp_path / "cache"
    (root / ".tmp-leftover" / "tree").mkdir(parents=True)
    (root / ".tmp-leftover" / "archive").write_bytes(b"x")
    (root / "keep.txt").write_text("not ours")
    cache = make_cache(tmp_path, Server(tar_bytes(PROJECT)))
    result = await fetch(cache)
    assert sorted(item.name for item in root.iterdir()) == sorted([result["code_id"], "keep.txt"])
    (root / ".tmp-again").mkdir()
    await cache.cleanup()
    assert sorted(item.name for item in root.iterdir()) == sorted([result["code_id"], "keep.txt"])


# ---- 搜索 ----

@pytest.mark.asyncio
async def test_line_reader_drops_overlong_lines():
    stream = asyncio.StreamReader()
    stream.feed_data(b"short\n" + b"x" * 100 + b"\nnext\n" + b"y" * 100)
    stream.feed_eof()
    reader, lines = code.LineReader(stream, 10), []
    while (line := await reader.readline()) is not None:
        lines.append(line)
    assert lines == [b"short", b"next"] and reader.dropped == 2


@pytest.mark.asyncio
async def test_search_rejects_bad_arguments(tmp_path):
    cache = code.CodeCache(tmp_path / "cache")
    good = "0123456789abcdef"
    cases = [{"code_id": "../x"}, {"pattern": "x" * 1001}, {"pattern": b"x"}, {"glob": "x" * 301}, {"glob": "/etc/*"},
             {"glob": "../*.java"}, {"glob": "src/../../*"}, {"glob": "!/abs"}, {"glob": "C:/x"}, {"context": 6},
             {"context": -1}, {"max_results": 0}, {"max_results": code.SEARCH_MAX_RESULTS + 1}, {"max_results": "10"}]
    for case in cases:
        args = {"code_id": good, **case}
        with pytest.raises(code.CodeError) as caught:
            await cache.search(args.pop("code_id"), **args)
        assert caught.value.code == "bad_request", case
    with pytest.raises(code.CodeError) as caught:
        await cache.search(good, pattern="x")
    assert caught.value.code == "not_found"


@pytest.mark.asyncio
async def test_search_without_rg(tmp_path, monkeypatch):
    cache, code_id = await readable(tmp_path)
    monkeypatch.setattr(code, "RG", str(tmp_path / "no-such-rg"))
    with pytest.raises(code.CodeError) as caught:
        await cache.search(code_id, pattern="line")
    assert caught.value.code == "rg_missing"


@pytest.mark.asyncio
async def test_search_regex_with_context(tmp_path, rg):
    cache, code_id = await searchable(tmp_path)
    result = await cache.search(code_id, pattern=r"create\w+\(", context=1, glob="*.java")
    assert result == {"code_id": code_id, "pattern": r"create\w+\(", "glob": "*.java", "count": 1, "truncated": False,
                      "matches": [{"path": JAVA_PATH, "line": 5, "text": "    public void createOrder(String id) {",
                                   "before": ["    // 下单入口"], "after": ['        log.info("create order {}", id);']}]}
    # 相邻的匹配：前后文按行号紧挨着取，别的匹配行也算
    near = await cache.search(code_id, pattern="order", ignore_case=True, context=2, glob="*.java")
    assert [(match["line"], match["before"], match["after"]) for match in near["matches"]] == [
        (3, ["package demo;", ""], ["    // 下单入口", "    public void createOrder(String id) {"]),
        (5, ["public class OrderService {", "    // 下单入口"],
         ['        log.info("create order {}", id);', '        throw new IllegalStateException("库存不足");']),
        (6, ["    // 下单入口", "    public void createOrder(String id) {"],
         ['        throw new IllegalStateException("库存不足");', "    }"]),
    ]
    plain = await cache.search(code_id, pattern="IllegalState")
    assert plain["matches"] == [{"path": JAVA_PATH, "line": 7, "text": '        throw new IllegalStateException("库存不足");'}]


@pytest.mark.asyncio
async def test_search_fixed_strings_and_bad_regex(tmp_path, rg):
    cache, code_id = await searchable(tmp_path)
    fixed = await cache.search(code_id, pattern="(String", fixed_strings=True)
    assert [(match["path"], match["line"]) for match in fixed["matches"]] == [(JAVA_PATH, 5)]
    with pytest.raises(code.CodeError) as caught:
        await cache.search(code_id, pattern="(String")
    assert caught.value.code == "search_failed" and "regex" in caught.value.message


@pytest.mark.asyncio
async def test_search_ignore_case_and_glob(tmp_path, rg):
    cache, code_id = await searchable(tmp_path)
    sensitive = await cache.search(code_id, pattern="createorder")
    assert (sensitive["matches"], sensitive["count"], sensitive["truncated"]) == ([], 0, False)
    insensitive = await cache.search(code_id, pattern="createorder", ignore_case=True)
    assert [(match["path"], match["line"]) for match in insensitive["matches"]] == [
        ("README.md", 2), (JAVA_PATH, 5), ("web/app.min.js", 1)]
    only_java = await cache.search(code_id, pattern="createorder", ignore_case=True, glob="**/*.java")
    assert [match["path"] for match in only_java["matches"]] == [JAVA_PATH]
    no_js = await cache.search(code_id, pattern="createorder", ignore_case=True, glob="!*.js")
    assert [match["path"] for match in no_js["matches"]] == ["README.md", JAVA_PATH]


@pytest.mark.asyncio
async def test_search_lists_files(tmp_path, rg):
    cache, code_id = await searchable(tmp_path)
    java = await cache.search(code_id, glob="*.java")
    assert java == {"code_id": code_id, "pattern": "", "glob": "*.java", "paths": [JAVA_PATH], "count": 1, "truncated": False}
    everything = await cache.search(code_id)
    assert everything["paths"] == [".gitlab-ci.yml", "README.md", JAVA_PATH, "src/main/resources/application.yml",
                                   "web/app.min.js"]
    assert "files" not in everything and "matches" not in everything


@pytest.mark.asyncio
async def test_search_result_limits(tmp_path, rg, monkeypatch):
    cache, code_id = await searchable(tmp_path)
    limited = await cache.search(code_id, pattern="o", max_results=2)
    assert (limited["count"], limited["truncated"]) == (2, True)
    listed = await cache.search(code_id, max_results=2)
    assert (listed["paths"], listed["truncated"]) == ([".gitlab-ci.yml", "README.md"], True)
    monkeypatch.setattr(code, "SEARCH_MAX_CHARS", 60)
    capped = await cache.search(code_id, pattern="order", ignore_case=True, context=2, glob="*.java")
    used = sum(len(match["text"]) + sum(map(len, match["before"] + match["after"])) for match in capped["matches"])
    assert capped["truncated"] is True and capped["count"] == 1 and used <= 60
    monkeypatch.setattr(code, "SEARCH_MAX_CHARS", 20)
    paths = await cache.search(code_id)
    assert (paths["paths"], paths["truncated"]) == ([".gitlab-ci.yml"], True)


@pytest.mark.asyncio
async def test_search_long_lines(tmp_path, rg, monkeypatch):
    cache, code_id = await searchable(tmp_path)
    # 压缩过的 js：匹配在三千列之后，截取匹配附近的一段
    text = (await cache.search(code_id, pattern="createOrder", glob="*.js"))["matches"][0]["text"]
    assert "createOrder" in text and text.startswith("…") and len(text) <= code.LINE_MAX_CHARS + 1 + len(code.CUT)
    monkeypatch.setattr(code, "JSON_LINE_LIMIT", 1000)
    dropped = await cache.search(code_id, pattern="createOrder")
    assert dropped["omitted_long_lines"] == 1 and [match["path"] for match in dropped["matches"]] == [JAVA_PATH]


@pytest.mark.asyncio
async def test_search_timeout_returns_partial(tmp_path, rg, monkeypatch):
    cache, code_id = await searchable(tmp_path)
    monkeypatch.setattr(code, "SEARCH_TIMEOUT", 0)
    result = await cache.search(code_id, pattern="order")
    assert result["timed_out"] is True and result["truncated"] is True


@pytest.mark.asyncio
async def test_search_results_survive_gateway_redaction(tmp_path, rg):
    cache, code_id = await searchable(tmp_path)
    for result in (await cache.search(code_id, pattern="order", ignore_case=True, context=1), await cache.search(code_id)):
        assert result["count"] and redact(result) == result


# ---- 查找定义 ----

ORDER_SERVICE = """package demo;

public class OrderService {
    public void createOrder(String id) {
        log.info("create order {}", id);
    }

    public void createOrder(String id, int count) {
        createOrder(id);
    }

    static class Inner {
        void createOrder() {}
    }
}
"""
ORDER_CONTROLLER = """package demo;

public class OrderController {
    private OrderService service;

    public void submit(String id) {
        service.createOrder(id);
    }
}
"""
WORKER_PY = """class Worker:
    def run(self, job):
        return job


def run(task):
    return Worker().run(task)
"""
SERVER_GO = """package main

type Server struct{}

func (s *Server) Run(addr string) error { return nil }

func Run() {}
"""
# 代码目录里的 ctags 选项文件：要是被读了，.java / .py / .go 都会按这个假语言解析，每行出一个 kind 为 pwned 的标签
EVIL_CTAGS = ("--langdef=Evil\n--map-Evil=+.java\n--map-Evil=+.py\n--map-Evil=+.go\n"
              "--kinddef-Evil=x,pwned,pwned\n--regex-Evil=/^(.+)$/" + chr(92) + "1/x/\n")
SYMBOL_FILES = {
    "proj/src/main/java/demo/OrderService.java": ORDER_SERVICE.encode(),
    "proj/src/main/java/demo/OrderController.java": ORDER_CONTROLLER.encode(),
    "proj/app/worker.py": WORKER_PY.encode(),
    "proj/cmd/server.go": SERVER_GO.encode(),
    "proj/.ctags.d/evil.ctags": EVIL_CTAGS.encode(),
    # 文件名以 -- 开头：ctags -L 读进来的行会被当成选项，路径不带 ./ 前缀的话后面的文件就都不解析了
    "proj/--languages=-all": b"createOrder Run run\n",
}
SERVICE_PATH = "src/main/java/demo/OrderService.java"


@pytest.fixture
def ctags(monkeypatch):
    """查找定义的用例要真的 universal-ctags：KUBEDOOR_TEST_CTAGS 指定路径，没有就找 PATH 里的 ctags，都没有就跳过"""
    path = os.environ.get("KUBEDOOR_TEST_CTAGS") or shutil.which("ctags")
    if not path or not Path(path).is_file():
        pytest.skip("没有 universal-ctags：设置 KUBEDOOR_TEST_CTAGS 指向 ctags 可执行文件后再跑查找定义的用例")
    version = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout
    if "Universal Ctags" not in version:
        pytest.skip(f"{path} 不是 universal-ctags")
    monkeypatch.setattr(code, "CTAGS", path)
    return path


async def symbol_cache(tmp_path):
    cache = make_cache(tmp_path, Server(tar_bytes(SYMBOL_FILES)))
    return cache, (await fetch(cache))["code_id"]


def brief(result):
    return [(item["path"], item["line"], item["kind"], item["scope"]) for item in result["symbols"]]


@pytest.mark.asyncio
async def test_symbols_java_overloads_not_calls(tmp_path, rg, ctags):
    cache, code_id = await symbol_cache(tmp_path)
    result = await cache.symbols(code_id, name="createOrder")
    # 重载的两个和内部类的都返回；OrderService 第 9 行、OrderController 里的调用不返回
    assert brief(result) == [(SERVICE_PATH, 4, "method", "OrderService"), (SERVICE_PATH, 8, "method", "OrderService"),
                             (SERVICE_PATH, 13, "method", "OrderService.Inner")]
    assert result["symbols"][0] == {"name": "createOrder", "kind": "method", "path": SERVICE_PATH, "line": 4, "end": 6,
                                    "scope": "OrderService", "scope_kind": "class", "signature": "(String id)"}
    # 候选文件：OrderService、OrderController（只有调用）、-- 开头的那个文件（排在最前面，没被当成选项）
    assert (result["code_id"], result["name"], result["path"], result["kind"]) == (code_id, "createOrder", "", "")
    assert (result["count"], result["truncated"], result["scanned"]) == (3, False, 3)
    assert "hint" not in result and redact(result) == result


@pytest.mark.asyncio
async def test_symbols_qualified_names(tmp_path, rg, ctags):
    cache, code_id = await symbol_cache(tmp_path)

    async def lines(name):
        return [item["line"] for item in (await cache.symbols(code_id, name=name))["symbols"]]

    assert await lines("OrderService.createOrder") == [4, 8]
    assert await lines("OrderService::createOrder") == [4, 8]
    assert await lines("Inner#createOrder") == [13]
    assert await lines("OrderService.Inner.createOrder") == [13]
    assert await lines("Nope.createOrder") == []


@pytest.mark.asyncio
async def test_symbols_python_and_go(tmp_path, rg, ctags):
    cache, code_id = await symbol_cache(tmp_path)
    assert brief(await cache.symbols(code_id, name="run")) == [("app/worker.py", 2, "member", "Worker"),
                                                               ("app/worker.py", 6, "function", None)]
    assert brief(await cache.symbols(code_id, name="Worker.run")) == [("app/worker.py", 2, "member", "Worker")]
    go = await cache.symbols(code_id, name="Run")
    assert brief(go) == [("cmd/server.go", 5, "func", "main.Server"), ("cmd/server.go", 7, "func", "main")]
    assert (go["symbols"][0]["signature"], go["symbols"][0]["scope_kind"]) == ("(addr string)", "struct")
    assert brief(await cache.symbols(code_id, name="Server.Run")) == [("cmd/server.go", 5, "func", "main.Server")]


@pytest.mark.asyncio
async def test_symbols_outline_kind_and_option_files(tmp_path, ctags):
    cache, code_id = await symbol_cache(tmp_path)
    assert (cache.root / code_id / "tree" / "proj" / ".ctags.d" / "evil.ctags").is_file()
    outline = await cache.symbols(code_id, path=SERVICE_PATH)
    # 只给 path：这个文件的全部定义按行号排；代码目录里的 .ctags.d/evil.ctags 没有生效（否则全是 pwned）
    assert [(item["line"], item["name"], item["kind"]) for item in outline["symbols"]] == [
        (1, "demo", "package"), (3, "OrderService", "class"), (4, "createOrder", "method"),
        (8, "createOrder", "method"), (12, "Inner", "class"), (13, "createOrder", "method")]
    assert (outline["name"], outline["path"], outline["scanned"], outline["truncated"]) == ("", SERVICE_PATH, 1, False)
    methods = await cache.symbols(code_id, path=SERVICE_PATH, kind="method")
    assert [item["line"] for item in methods["symbols"]] == [4, 8, 13]
    assert [item["line"] for item in (await cache.symbols(code_id, path=SERVICE_PATH, kind="class"))["symbols"]] == [3, 12]
    in_file = await cache.symbols(code_id, path=SERVICE_PATH, name="Inner")
    assert [(item["line"], item["kind"]) for item in in_file["symbols"]] == [(12, "class")]
    assert [item["name"] for item in (await cache.symbols(code_id, path="app/worker.py"))["symbols"]] == ["Worker", "run", "run"]


@pytest.mark.asyncio
async def test_symbols_limits(tmp_path, rg, ctags, monkeypatch):
    cache, code_id = await symbol_cache(tmp_path)
    limited = await cache.symbols(code_id, path=SERVICE_PATH, max_results=2)
    assert ([item["line"] for item in limited["symbols"]], limited["truncated"]) == ([1, 3], True)
    monkeypatch.setattr(code, "SYMBOL_MAX_CHARS", 100)
    capped = await cache.symbols(code_id, path=SERVICE_PATH)
    assert capped["truncated"] is True and 0 < capped["count"] < 6
    monkeypatch.setattr(code, "SYMBOL_MAX_FILES", 1)
    common = await cache.symbols(code_id, name="createOrder")
    assert (common["truncated"], common["scanned"], common["hint"]) == (True, 1, code.SYMBOL_HINT)


@pytest.mark.asyncio
async def test_symbols_timeout(tmp_path, ctags, monkeypatch):
    cache, code_id = await symbol_cache(tmp_path)
    monkeypatch.setattr(code, "SYMBOL_TIMEOUT", 0)
    assert (await cache.symbols(code_id, path=SERVICE_PATH))["truncated"] is True


@pytest.mark.asyncio
async def test_symbols_no_candidate_files(tmp_path, rg, monkeypatch):
    cache, code_id = await symbol_cache(tmp_path)
    monkeypatch.setattr(code, "CTAGS", str(tmp_path / "no-such-ctags"))   # 没有候选文件就不跑 ctags
    result = await cache.symbols(code_id, name="NoSuchSymbolAnywhere")
    assert (result["symbols"], result["count"], result["truncated"], result["scanned"]) == ([], 0, False, 0)


@pytest.mark.asyncio
async def test_symbols_rejects_bad_arguments(tmp_path):
    cache, code_id = await readable(tmp_path)
    cases = [{"name": "a b"}, {"name": "a..b"}, {"name": "a/b"}, {"name": "::a"}, {"name": "a;rm"}, {"name": "-e"},
             {"name": "x" * 201}, {"name": 123}, {}, {"name": "", "path": ""}, {"path": "../x"}, {"path": "/etc/passwd"},
             {"name": "a", "kind": "k" * 51}, {"name": "a", "max_results": 0},
             {"name": "a", "max_results": code.SYMBOL_MAX_RESULTS + 1}]
    for case in cases:
        with pytest.raises(code.CodeError) as caught:
            await cache.symbols(code_id, **case)
        assert caught.value.code == "bad_request", case
    for bad_id, args, kind in ((code_id, {"path": "nope.java"}, "not_found"), (code_id, {"path": "src"}, "not_found"),
                               ("0123456789abcdef", {"name": "a"}, "not_found"), ("../x", {"name": "a"}, "bad_request")):
        with pytest.raises(code.CodeError) as caught:
            await cache.symbols(bad_id, **args)
        assert caught.value.code == kind, args


@pytest.mark.asyncio
async def test_symbols_without_ctags(tmp_path, monkeypatch):
    cache, code_id = await readable(tmp_path)
    monkeypatch.setattr(code, "CTAGS", str(tmp_path / "no-such-ctags"))
    with pytest.raises(code.CodeError) as caught:
        await cache.symbols(code_id, path="src/Main.java")
    assert caught.value.code == "ctags_missing"


@pytest.mark.asyncio
async def test_symbols_failed_when_ctags_errors(tmp_path, monkeypatch):
    # 拿 Python 冒充 ctags：不认识 --options=NONE，非 0 退出，stdout 没有任何输出
    cache, code_id = await readable(tmp_path)
    monkeypatch.setattr(code, "CTAGS", sys.executable)
    with pytest.raises(code.CodeError) as caught:
        await cache.symbols(code_id, path="src/Main.java")
    assert caught.value.code == "symbols_failed" and "Notice" not in caught.value.message
