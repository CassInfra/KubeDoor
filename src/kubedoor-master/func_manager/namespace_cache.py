import time
from loguru import logger

# 缓存数据结构: { "env": {"timestamp": 1678886400, "namespaces": ["ns1", "ns2"]} }
_namespace_cache = {}
_CACHE_EXPIRATION_SECONDS = 3600  # 1 hour


def get_namespaces_from_cache(env):
    """
    从缓存中获取指定环境的命名空间列表。
    如果缓存不存在或已过期，则返回 None。
    """
    if env in _namespace_cache:
        cache_entry = _namespace_cache[env]
        if time.time() - cache_entry["timestamp"] < _CACHE_EXPIRATION_SECONDS:
            logger.info(f"♻从缓存中获取到 {env} 的命名空间数据")
            return cache_entry["namespaces"]
        else:
            logger.info(f"♻{env} 的命名空间缓存已过期")
    else:
        logger.info(f"♻未在缓存中找到 {env} 的命名空间数据")
    return None


def update_namespace_cache(env, namespaces):
    """
    更新指定环境的命名空间缓存。
    """
    _namespace_cache[env] = {
        "timestamp": time.time(),
        "namespaces": namespaces,
    }
    logger.info(f"♻已从agent更新 {env} 的命名空间缓存")
