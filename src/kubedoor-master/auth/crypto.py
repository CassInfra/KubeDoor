"""敏感配置(LDAP 绑定密码)入库前加密。

密钥复用安装脚本生成并持久化的 AI_ENCRYPTION_KEY(64 位十六进制,Secret kubedoor-ai-security),
AES-256-GCM。没配密钥(本地开发)时退化为明文存储并打警告,方便调试。
"""

import base64
import os

from loguru import logger

_AAD = b'kubedoor-auth-secret'


def _key():
    raw = os.environ.get('AI_ENCRYPTION_KEY', '').strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def encrypt_secret(plain):
    if not plain:
        return ''
    key = _key()
    if key is None:
        logger.warning('未配置 AI_ENCRYPTION_KEY,LDAP 绑定密码将以明文存储')
        return 'plain:' + plain
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plain.encode('utf-8'), _AAD)
    return 'v1:' + base64.b64encode(nonce + sealed).decode('ascii')


def decrypt_secret(stored):
    """解不开(密钥被换过)抛 ValueError。"""
    if not stored:
        return ''
    if stored.startswith('plain:'):
        return stored[len('plain:'):]
    if not stored.startswith('v1:'):
        raise ValueError('无法识别的密文格式')
    key = _key()
    if key is None:
        raise ValueError('未配置 AI_ENCRYPTION_KEY,无法解密')
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(stored[3:])
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], _AAD).decode('utf-8')
    except InvalidTag as exc:
        raise ValueError('密钥与加密时不一致,无法解密') from exc
