"""集群内配置连 apiserver 的 TLS:证书缺 Authority Key Identifier 也要连得上,CA 照常校验

Python 3.13+ 上 urllib3(2.3 起)和 ssl.create_default_context() 一样默认开 X509 严格模式;2020 年建的华为云 CCE
集群,CA 没有 Subject Key Identifier、apiserver 证书没有 AKI,同步 SDK 的请求都报 "Missing Authority Key Identifier"。
测试证书照这个样子签,在本地起 HTTPS 的假 apiserver。
"""

import asyncio
import datetime
import ipaddress
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kubedoor_tools import KubernetesExecutor
from kubedoor_tools import executor as executor_module

pytest.importorskip("cryptography")
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

TOKEN = "test-serviceaccount-token"


def _strict_by_default():
    from urllib3.util.ssl_ import create_urllib3_context

    return bool(create_urllib3_context().verify_flags & ssl.VERIFY_X509_STRICT)


def _name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CCE Technologies"),
                      x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _sign(subject, issuer, public_key, signing_key, extensions):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key)
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(hours=1)).not_valid_after(now + datetime.timedelta(days=1)))
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical=critical)
    return builder.sign(signing_key, hashes.SHA256())


def make_ca():
    """同 CCE 的 CA:有 BasicConstraints、KeyUsage,没有 Subject Key Identifier"""
    key = ec.generate_private_key(ec.SECP256R1())
    usage = x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                          data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    cert = _sign(_name("CCE"), _name("CCE"), key.public_key(), key,
                 [(x509.BasicConstraints(ca=True, path_length=None), True), (usage, True)])
    return key, cert


def make_apiserver_cert(ca_key, ca_cert):
    """这个 CA 签的 apiserver 证书:只有 SAN,没有 Authority Key Identifier"""
    key = ec.generate_private_key(ec.SECP256R1())
    san = x509.SubjectAlternativeName([x509.DNSName("kubernetes.default.svc"),
                                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    return key, _sign(_name("system:kube-apiserver"), ca_cert.subject, key.public_key(), ca_key, [(san, False)])


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


class _ApiServer(BaseHTTPRequestHandler):
    def do_GET(self):
        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        authorized = scheme.lower() == "bearer" and token == TOKEN  # 旧版 SDK 发的是 "bearer <token>"
        body = json.dumps({"kind": "NamespaceList", "metadata": {}, "items": [{"metadata": {"name": "default"}}]}
                          if authorized else {"kind": "Status", "code": 401}).encode()
        self.send_response(200 if authorized else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def apiserver(tmp_path):
    """HTTPS 假 apiserver,返回 (CA、token 文件所在目录, 端口)"""
    ca_key, ca_cert = make_ca()
    key, cert = make_apiserver_cert(ca_key, ca_cert)
    (tmp_path / "ca.crt").write_bytes(pem(ca_cert))
    (tmp_path / "tls.crt").write_bytes(pem(cert))
    (tmp_path / "tls.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    (tmp_path / "token").write_text(TOKEN)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(tmp_path / "tls.crt", tmp_path / "tls.key")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiServer)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield tmp_path, server.server_address[1]
    server.shutdown()
    server.server_close()


def use_incluster(monkeypatch, directory, port, ca="ca.crt"):
    """executor 走集群内配置时读 directory 里的 token 和 CA"""
    from kubernetes import config
    from kubernetes.config.incluster_config import InClusterConfigLoader

    loader = InClusterConfigLoader(str(directory / "token"), str(directory / ca),
                                   environ={"KUBERNETES_SERVICE_HOST": "127.0.0.1", "KUBERNETES_SERVICE_PORT": str(port)})
    monkeypatch.setattr(config, "load_incluster_config",
                        lambda client_configuration: loader.load_and_set(client_configuration))
    return loader


def list_namespaces(api_client):
    from kubernetes import client

    return [ns.metadata.name for ns in client.CoreV1Api(api_client).list_namespace().items]


async def test_executor_accepts_apiserver_cert_without_authority_key_identifier(apiserver, monkeypatch):
    directory, port = apiserver
    use_incluster(monkeypatch, directory, port)
    executor = KubernetesExecutor()
    try:
        await executor._initialize()
        assert await asyncio.to_thread(list_namespaces, executor._client) == ["default"]
    finally:
        await executor.close()


@pytest.mark.skipif(not _strict_by_default(), reason="Python 3.13+ 上 urllib3 2.3 起才默认开 X509 严格模式")
async def test_sdk_default_pool_rejects_the_same_cert(apiserver, monkeypatch):
    """对照:SDK 默认的连接池就是线上的报错,说明测试证书确实缺扩展"""
    from kubernetes import client

    directory, port = apiserver
    configuration = client.Configuration()
    use_incluster(monkeypatch, directory, port).load_and_set(configuration)
    configuration.retries = False
    api_client = client.ApiClient(configuration)
    try:
        with pytest.raises(Exception, match="Key Identifier"):
            await asyncio.to_thread(list_namespaces, api_client)
    finally:
        api_client.close()


async def test_each_executor_still_verifies_its_own_ca(apiserver, monkeypatch):
    """去掉的只是严格模式:CA 不对照样连不上。SSLContext 每个客户端单独一个:urllib3 每次连接都往传入的
    SSLContext 里加载 CA,共用的话,先连上的客户端加载过的 CA 会让后面 CA 不对的客户端也连得上"""
    directory, port = apiserver
    _, other_ca = make_ca()
    (directory / "other-ca.crt").write_bytes(pem(other_ca))
    executors = []
    try:
        for ca in ("ca.crt", "other-ca.crt"):
            use_incluster(monkeypatch, directory, port, ca)
            executors.append(KubernetesExecutor())
            await executors[-1]._initialize()
        trusted, untrusted = executors
        assert await asyncio.to_thread(list_namespaces, trusted._client) == ["default"]
        with pytest.raises(Exception, match="certificate verify failed"):
            await asyncio.to_thread(list_namespaces, untrusted._client)
    finally:
        for executor in executors:
            await executor.close()


def fake_client(verify_ssl=True, **pool_kw):
    """_relax_x509_strict 只碰 configuration.verify_ssl 和连接池参数(各版本 SDK 默认的 ca_certs 不同,旧版是 certifi)"""
    return SimpleNamespace(configuration=SimpleNamespace(verify_ssl=verify_ssl),
                           rest_client=SimpleNamespace(pool_manager=SimpleNamespace(connection_pool_kw=pool_kw)))


def test_relax_x509_strict_keeps_sdk_tls_semantics(monkeypatch):
    """不校验证书的客户端不动(否则 urllib3 设 CERT_NONE 时报错);没配 CA 时和 urllib3 自己的默认一样用系统 CA"""
    loaded = []
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, *args, **kwargs: loaded.append(self))
    insecure = fake_client(verify_ssl=False, cert_reqs=ssl.CERT_NONE)
    executor_module._relax_x509_strict(insecure)
    assert "ssl_context" not in insecure.rest_client.pool_manager.connection_pool_kw

    with_ca = fake_client(ca_certs="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    executor_module._relax_x509_strict(with_ca)
    context = with_ca.rest_client.pool_manager.connection_pool_kw["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and not context.verify_flags & ssl.VERIFY_X509_STRICT
    assert loaded == []

    without_ca = fake_client(ca_certs=None, ca_cert_data=None)
    executor_module._relax_x509_strict(without_ca)
    assert loaded == [without_ca.rest_client.pool_manager.connection_pool_kw["ssl_context"]]
