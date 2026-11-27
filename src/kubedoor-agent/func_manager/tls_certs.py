"""Dynamic per-cluster webhook TLS certificate loading and generation."""

from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path

DEFAULT_WEBHOOK_CERT_DIR = "/etc/kubedoor/certs"
DEFAULT_RUNTIME_CERT_DIR = "/run/kubedoor/certs"
_NS_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _resolve_namespace(namespace: str | None = None) -> str:
    ns = (namespace or os.environ.get("POD_NAMESPACE") or "kubedoor").strip() or "kubedoor"
    if not _NS_RE.fullmatch(ns):
        raise ValueError(f"Invalid Kubernetes namespace for webhook TLS SANs: {ns!r}")
    return ns


def _has_cert_triplet(directory: Path) -> tuple[Path, Path, Path] | None:
    tls_crt = directory / "tls.crt"
    tls_key = directory / "tls.key"
    ca_crt = directory / "ca.crt"
    if all(p.is_file() and p.stat().st_size > 0 for p in (tls_crt, tls_key, ca_crt)):
        return tls_crt, tls_key, ca_crt
    return None


def _run_openssl(args: list[str]) -> None:
    subprocess.run(
        ["openssl", *args],
        shell=False,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ensure_webhook_certs(namespace: str | None = None) -> tuple[str, str, str]:
    """Return (tls.crt, tls.key, ca.crt) paths from mounted Secret or per-instance generated certs."""
    mounted_dir = Path((os.environ.get("WEBHOOK_CERT_DIR") or "").strip() or DEFAULT_WEBHOOK_CERT_DIR)
    mounted = _has_cert_triplet(mounted_dir)
    if mounted is not None:
        return tuple(str(p) for p in mounted)  # type: ignore[return-value]

    runtime_dir = Path((os.environ.get("WEBHOOK_RUNTIME_CERT_DIR") or "").strip() or DEFAULT_RUNTIME_CERT_DIR)
    existing = _has_cert_triplet(runtime_dir)
    if existing is not None:
        return tuple(str(p) for p in existing)  # type: ignore[return-value]

    ns = _resolve_namespace(namespace)
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    os.chmod(runtime_dir, 0o700)

    ca_key = runtime_dir / "ca.key"
    ca_crt = runtime_dir / "ca.crt"
    tls_key = runtime_dir / "tls.key"
    tls_crt = runtime_dir / "tls.crt"
    csr_path = runtime_dir / "tls.csr"
    srl_path = runtime_dir / "ca.srl"
    ca_cnf = runtime_dir / "ca.cnf"
    leaf_cnf = runtime_dir / "leaf.cnf"

    ca_cnf_text = (
        "[req]\n"
        "distinguished_name = req_dn\n"
        "x509_extensions = v3_ca\n"
        "prompt = no\n\n"
        "[req_dn]\n"
        "CN = kubedoor-agent-ca\n\n"
        "[v3_ca]\n"
        "subjectKeyIdentifier = hash\n"
        "authorityKeyIdentifier = keyid:always,issuer\n"
        "basicConstraints = critical, CA:TRUE\n"
        "keyUsage = critical, digitalSignature, cRLSign, keyCertSign\n"
    )
    leaf_cnf_text = (
        "[req]\n"
        "distinguished_name = req_dn\n"
        "req_extensions = v3_csr\n"
        "prompt = no\n\n"
        "[req_dn]\n"
        f"CN = kubedoor-agent.{ns}.svc\n\n"
        "[v3_csr]\n"
        "subjectAltName = @alt_names\n\n"
        "[v3_cert]\n"
        "basicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectKeyIdentifier = hash\n"
        "authorityKeyIdentifier = keyid,issuer\n"
        "subjectAltName = @alt_names\n\n"
        "[alt_names]\n"
        "DNS.1 = kubedoor-agent\n"
        f"DNS.2 = kubedoor-agent.{ns}\n"
        f"DNS.3 = kubedoor-agent.{ns}.svc\n"
        f"DNS.4 = kubedoor-agent.{ns}.svc.cluster.local\n"
        "DNS.5 = localhost\n"
        "IP.1 = 127.0.0.1\n"
    )

    try:
        ca_cnf.write_text(ca_cnf_text, encoding="utf-8")
        leaf_cnf.write_text(leaf_cnf_text, encoding="utf-8")

        _run_openssl(["genrsa", "-out", str(ca_key), "2048"])
        os.chmod(ca_key, 0o600)
        _run_openssl([
            "req", "-x509", "-new", "-nodes", "-key", str(ca_key),
            "-sha256", "-days", "365", "-out", str(ca_crt), "-config", str(ca_cnf),
        ])

        _run_openssl(["genrsa", "-out", str(tls_key), "2048"])
        os.chmod(tls_key, 0o600)
        _run_openssl(["req", "-new", "-key", str(tls_key), "-out", str(csr_path), "-config", str(leaf_cnf)])
        _run_openssl([
            "x509", "-req", "-in", str(csr_path), "-CA", str(ca_crt), "-CAkey", str(ca_key),
            "-CAcreateserial", "-CAserial", str(srl_path),
            "-out", str(tls_crt), "-days", "365", "-sha256",
            "-extfile", str(leaf_cnf), "-extensions", "v3_cert",
        ])
    finally:
        for tmp_file in (csr_path, srl_path, ca_cnf, leaf_cnf):
            try:
                tmp_file.unlink()
            except OSError:
                pass

    return str(tls_crt), str(tls_key), str(ca_crt)


def load_ca_bundle_b64(ca_path: str | None = None, namespace: str | None = None) -> str:
    """Return base64-encoded CA certificate PEM for MutatingWebhookConfiguration clientConfig.caBundle."""
    if ca_path is None:
        _, _, ca_path = ensure_webhook_certs(namespace=namespace)
    return base64.b64encode(Path(ca_path).read_bytes()).decode("ascii")
