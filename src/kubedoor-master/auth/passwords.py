"""本地账号口令:scrypt 哈希 + pepper + 口令策略。

存储格式(自描述,参数变了也能校验老哈希,登录成功后按新参数重新哈希):
    scrypt-p1$<N>$<r>$<p>$<salt base64>$<hash base64>   加了 pepper(默认)
    scrypt$<N>$<r>$<p>$<salt base64>$<hash base64>      未加 pepper(没配密钥的开发环境)

参数取 OWASP Password Storage Cheat Sheet 的 scrypt 等强度档位之一:
N=2^14(16 MiB)、r=8、p=5。单次约 0.3 秒,调用方要放进线程池,不能卡主 loop。

pepper 由 AI_ENCRYPTION_KEY 派生(不在数据库里):数据库被读走(备份泄露、Grafana 的 SQL 数据源)
也没法离线跑字典破解。这个密钥丢了本地账号的口令就都对不上了,需要用 reset_password 重置。
"""

import base64
import hashlib
import hmac
import os
import secrets

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 5
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024
_PREFIX = 'scrypt'
_PEPPERED = 'scrypt-p1'

MIN_LENGTH = 8
MAX_LENGTH = 128

# 太常见的口令直接拒绝(NIST 800-63B:不做复杂度规则,但要挡掉常见/已泄露口令)
_COMMON = {
    'kubedoor', 'password', 'password1', 'passw0rd', 'p@ssw0rd', 'admin123', 'admin@123',
    'administrator', '12345678', '123456789', '1234567890', '11111111', '88888888',
    '00000000', 'qwertyui', 'qwerty123', '1q2w3e4r', 'abc12345', 'abcd1234', 'iloveyou',
    'changeme', 'letmein1', 'welcome1', 'root1234', 'kubernetes', 'k8s12345',
}


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def _pepper():
    """从 AI_ENCRYPTION_KEY 派生独立的 pepper 密钥(和加密用途做域分离);没配置返回 None。"""
    raw = os.environ.get('AI_ENCRYPTION_KEY', '').strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(key) != 32:
        return None
    return hmac.new(key, b'kubedoor-password-pepper-v1', hashlib.sha256).digest()


def _material(password, pepper):
    data = password.encode('utf-8')
    return hmac.new(pepper, data, hashlib.sha256).digest() if pepper else data


def _derive(material, salt, n, r, p):
    return hashlib.scrypt(material, salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_DKLEN)


def hash_password(password):
    pepper = _pepper()
    salt = secrets.token_bytes(16)
    digest = _derive(_material(password, pepper), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    prefix = _PEPPERED if pepper else _PREFIX
    return f'{prefix}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}'


def _parse(encoded):
    parts = (encoded or '').split('$')
    if len(parts) != 6 or parts[0] not in (_PREFIX, _PEPPERED):
        raise ValueError('不支持的口令哈希格式')
    n, r, p = (int(x) for x in parts[1:4])
    return parts[0] == _PEPPERED, n, r, p, base64.b64decode(parts[4]), base64.b64decode(parts[5])


def verify_password(password, encoded):
    """常量时间比较;哈希为空、格式不对、需要 pepper 但没配密钥,一律返回 False。"""
    if not isinstance(password, str) or not encoded:
        return False
    try:
        peppered, n, r, p, salt, expected = _parse(encoded)
        pepper = _pepper() if peppered else None
        if peppered and pepper is None:
            return False
        actual = _derive(_material(password, pepper), salt, n, r, p)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(encoded):
    """参数变了,或者现在有 pepper 而老哈希没加,登录成功后重新哈希。"""
    try:
        peppered, n, r, p, _, _ = _parse(encoded)
    except (ValueError, TypeError):
        return True
    if not peppered and _pepper() is not None:
        return True
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


_DUMMY_HASH = None


def burn_time(password):
    """用户不存在时也做一次同等开销的校验,避免按响应时间枚举用户名。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password or '', _DUMMY_HASH)
    return False


def validate_new_password(password, username=''):
    """返回错误提示;合格返回 None。"""
    if not isinstance(password, str):
        return '口令格式不正确'
    if len(password) < MIN_LENGTH:
        return f'口令至少 {MIN_LENGTH} 位'
    if len(password) > MAX_LENGTH:
        return f'口令最多 {MAX_LENGTH} 位'
    if password != password.strip():
        return '口令首尾不能有空格'
    lowered = password.lower()
    if username and lowered == username.lower():
        return '口令不能和用户名相同'
    if lowered in _COMMON or len(set(password)) < 3:
        return '口令太简单,请换一个'
    return None
