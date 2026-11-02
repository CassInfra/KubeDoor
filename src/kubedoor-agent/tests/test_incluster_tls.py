"""集群内配置连 apiserver 的 TLS:证书缺 Authority Key Identifier 也要连得上,CA 照常校验

线上:Python 3.13 起 ssl.create_default_context() 默认开 X509 严格模式,2020 年建的华为云 CCE 集群,
CA 没有 Subject Key Identifier、apiserver 证书没有 AKI,agent 所有 K8S 调用都报 "Missing Authority Key Identifier"。
测试证书照这个样子签,在本地起 HTTPS 的假 apiserver。
"""

import datetime
import ipaddress
import ssl

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from kubernetes_asyncio import client, config
from kubernetes_asyncio.config import incluster_config
from kubernetes_asyncio.stream import WsApiClient

pytest.importorskip("cryptography")
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

STRICT_BY_DEFAULT = bool(ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT)
TOKEN = "test-serviceaccount-token"


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


@pytest_asyncio.fixture
async def apiserver(tmp_path, monkeypatch):
    """HTTPS 假 apiserver + 集群内配置要读的环境变量、token 和 CA 文件,返回这些文件所在目录"""
    ca_key, ca_cert = make_ca()
    key, cert = make_apiserver_cert(ca_key, ca_cert)
    (tmp_path / "ca.crt").write_bytes(pem(ca_cert))
    (tmp_path / "tls.crt").write_bytes(pem(cert))
    (tmp_path / "tls.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    (tmp_path / "token").write_text(TOKEN)

    async def list_namespaces(request):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"kind": "Status", "code": 401}, status=401)
        return web.json_response({"kind": "NamespaceList", "metadata": {}, "items": [{"metadata": {"name": "default"}}]})

    app = web.Application()
    app.router.add_get("/api/v1/namespaces", list_namespaces)
    server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ssl.load_cert_chain(tmp_path / "tls.crt", tmp_path / "tls.key")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
    await site.start()
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", str(site._server.sockets[0].getsockname()[1]))
    monkeypatch.setattr(incluster_config, "SERVICE_TOKEN_FILENAME", str(tmp_path / "token"))
    monkeypatch.setattr(incluster_config, "SERVICE_CERT_FILENAME", str(tmp_path / "ca.crt"))
    # 加载集群内配置会换掉全局默认配置,测试完还原
    monkeypatch.setattr(client.Configuration, "_default", None)
    yield tmp_path
    await runner.cleanup()


@pytest.mark.asyncio
async def test_incluster_clients_accept_apiserver_cert_without_authority_key_identifier(apiserver, client_manager_module):
    client_manager_module.load_incluster_config()
    assert client.Configuration.get_default_copy().disable_strict_ssl_verification is True
    # init_kubernetes 的 OffloadApiClient()、pod-mgr 的 client.CoreV1Api() 都从默认配置复制
    for api_client in (client_manager_module.OffloadApiClient(), client.ApiClient()):
        async with api_client:
            namespaces = await client.CoreV1Api(api_client).list_namespace()
            assert [ns.metadata.name for ns in namespaces.items] == ["default"]
    # Pod / 节点终端的 WsApiClient 也从默认配置复制,走同一套 TLS 设置
    async with WsApiClient() as ws_client:
        assert ws_client.configuration.disable_strict_ssl_verification is True


@pytest.mark.skipif(not STRICT_BY_DEFAULT, reason="Python 3.13 起 ssl.create_default_context() 才默认开 X509 严格模式")
@pytest.mark.asyncio
async def test_strict_mode_rejects_the_same_cert(apiserver):
    """对照:kubernetes_asyncio 默认(严格模式)就是线上的报错,说明测试证书确实缺扩展"""
    config.load_incluster_config()
    async with client.ApiClient() as api_client:
        with pytest.raises(aiohttp.ClientConnectorCertificateError, match="Key Identifier"):
            await client.CoreV1Api(api_client).list_namespace()


@pytest.mark.asyncio
async def test_incluster_clients_still_verify_ca(apiserver, client_manager_module, monkeypatch):
    """去掉的只是严格模式:换一个同名、同样没有 SKI 的 CA,apiserver 证书照样不认"""
    _, other_ca = make_ca()
    (apiserver / "other-ca.crt").write_bytes(pem(other_ca))
    monkeypatch.setattr(incluster_config, "SERVICE_CERT_FILENAME", str(apiserver / "other-ca.crt"))
    client_manager_module.load_incluster_config()
    async with client_manager_module.OffloadApiClient() as api_client:
        with pytest.raises(aiohttp.ClientConnectorCertificateError, match="certificate verify failed"):
            await client.CoreV1Api(api_client).list_namespace()
