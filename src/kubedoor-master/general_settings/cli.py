"""`python3 -m general_settings migrate [--env-json -]`：把旧配置迁进数据库（升级控制端、旧脚本新装时调用）。

在 master 容器里执行（工作目录 /，继承容器的环境变量和挂载），自己建数据库连接池，依次执行（都可以重复执行）：
  6.1 环境变量 → 通用配置          6.2 MSG_* → 消息通知的全局配置
  6.3 UPDATE_IMAGE / REGISTRY_SECRET → 镜像设置（image_settings.import_legacy）
  6.4 事件告警规则文件 → 数据库（k8s_event.rules_api.import_legacy_file）
--env-json -：从 stdin 读一个 JSON 对象（键名同旧环境变量），代替进程环境变量作为 6.1–6.3 的来源。

stdout 最后一行是一行 JSON 汇总（不含任何配置值）。退出码：0 全部成功；3 有导入不了的旧配置（汇总里列出 key 名，
在页面上填好后库里有了这一项，下次就算 present）；1 其他错误（连不上库等）。错误详情只写日志（stderr）。
"""

import asyncio
import json
import os
import sys

from loguru import logger

import db

from . import legacy

USAGE = '用法: python3 -m general_settings migrate [--env-json -]'
MAX_STDIN = 2 * 1024 * 1024
EXIT_OK, EXIT_ERROR, EXIT_INVALID = 0, 1, 3


def env_from_json(text):
    """--env-json 的内容 → {旧环境变量名: 字符串}。值为 null 的键忽略；数字、布尔转成字符串，
    对象、数组转成 JSON 字符串（AGENT_INSTALL_DEFAULTS、UPDATE_IMAGE 等可以直接传对象）。格式不对抛 ValueError"""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError('--env-json 读到的不是合法的 JSON') from None
    if not isinstance(data, dict):
        raise ValueError('--env-json 读到的 JSON 必须是对象')
    env = {}
    for key, item in data.items():
        if item is None:
            continue
        if isinstance(item, str):
            env[key] = item
        elif isinstance(item, bool):
            env[key] = 'true' if item else 'false'
        elif isinstance(item, (int, float)):
            env[key] = str(item)
        else:
            env[key] = json.dumps(item, ensure_ascii=False)
    return env


def _image_settings_module():
    import image_settings  # 延迟 import:它在顶层 import auth.store(auth.store 又 import general_settings)

    return image_settings


def _rules_api_module():
    from k8s_event import rules_api

    return rules_api


async def _general(env):
    return await legacy.import_general(env)


async def _notification(env):
    return await legacy.import_notification(env, refresh=False)  # master 进程 10 秒内自己会刷新


async def _image_settings(env):
    return await _image_settings_module().import_legacy(env.get('UPDATE_IMAGE'), env.get('REGISTRY_SECRET'))


async def _event_rules(env):
    return await _rules_api_module().import_legacy_file()  # 读挂载的规则文件,与 env 无关


STEPS = (
    ('general_settings', _general),
    ('notification', _notification),
    ('image_settings', _image_settings),
    ('event_rules', _event_rules),
)


def has_invalid(summary):
    general = summary.get('general_settings')
    images = summary.get('image_settings')
    return bool((isinstance(general, dict) and general.get('invalid'))
                or summary.get('notification') == 'invalid' or summary.get('event_rules') == 'invalid'
                or (isinstance(images, dict) and 'invalid' in images.values()))


async def migrate(env):
    """建连接池，依次执行各步（某一步出错不影响后面的步骤），返回 (汇总, 退出码)"""
    summary = {'ok': False}
    try:
        await db.init_pool()
    except Exception as exc:
        logger.error(f'连接数据库失败: {type(exc).__name__}: {exc}')
        summary['error'] = '无法连接数据库，请检查 PG_* 配置和网络（详情见日志）'
        return summary, EXIT_ERROR
    failed = []
    try:
        for name, step in STEPS:
            try:
                summary[name] = await step(env)
            except Exception as exc:
                logger.error(f'导入旧配置 {name} 失败: {type(exc).__name__}: {exc}')
                summary[name] = 'error'
                failed.append(name)
    finally:
        try:
            await db.close_pool()
        except Exception as exc:
            logger.warning(f'关闭数据库连接池失败: {type(exc).__name__}: {exc}')
    if failed:
        summary['error'] = f'这几步出错了（详情见日志），可以重新执行：{"、".join(failed)}'
        return summary, EXIT_ERROR
    if has_invalid(summary):
        return summary, EXIT_INVALID
    summary['ok'] = True
    return summary, EXIT_OK


def _print(summary):
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main(argv=None, stdin=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (['migrate'], ['migrate', '--env-json', '-']):
        _print({'ok': False, 'error': USAGE})
        return EXIT_ERROR
    env = os.environ
    if len(args) == 3:
        stream = sys.stdin if stdin is None else stdin
        raw = getattr(stream, 'buffer', stream).read(MAX_STDIN + 1)
        try:
            if len(raw) > MAX_STDIN:
                raise ValueError(f'--env-json 的内容太大（最多 {MAX_STDIN} 字节）')
            env = env_from_json(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
        except UnicodeDecodeError:
            _print({'ok': False, 'error': '--env-json 读到的内容不是 UTF-8'})
            return EXIT_ERROR
        except ValueError as exc:
            _print({'ok': False, 'error': str(exc)})
            return EXIT_ERROR
    try:
        summary, code = asyncio.run(migrate(env))
    except Exception as exc:  # migrate 自己不抛,兜底
        logger.error(f'迁移旧配置失败: {type(exc).__name__}: {exc}')
        summary, code = {'ok': False, 'error': '迁移旧配置失败（详情见日志）'}, EXIT_ERROR
    _print(summary)
    return code
