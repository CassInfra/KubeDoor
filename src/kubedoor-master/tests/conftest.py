"""master 测试共用的夹具。"""
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _cache_policy(policy, ttl=3600):
    from auth import permissions

    permissions._cache.update(policy={**policy, 'updated_by': 'test', 'updated_at': None},
                              expires=time.monotonic() + ttl)


@pytest.fixture(autouse=True)
def default_permission_policy():
    """权限与审批配置是进程内缓存:每个用例都从默认值开始(放进缓存,不查库),互不影响。
    要读真实数据库的用例(test_istio_api)自己 permissions.invalidate_cache()。"""
    from auth import permissions

    _cache_policy(permissions.default_policy())
    yield
    permissions.invalidate_cache()


@pytest.fixture
def set_policy():
    """改缓存里的权限表:set_policy({'pod.delete': {'rw': False}}, commands={'view': [...]})。返回生效的配置。"""
    from auth import permissions

    def apply(ops=None, commands=None):
        policy = permissions.default_policy()
        for key, values in (ops or {}).items():
            assert key in policy['ops'], key
            policy['ops'][key].update(values)
        policy['commands'].update(commands or {})
        _cache_policy(policy)
        return policy

    return apply
