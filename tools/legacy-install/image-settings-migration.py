#!/usr/bin/env python3
"""Temporarily preserve legacy image settings while upgrading the master.

The installer sends old ConfigMap data and already-loaded shell values via stdin.
Secrets never appear in command arguments or successful output. This helper does
not execute the user's configuration and never connects to Kubernetes or a DB.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile


SETTINGS = {"UPDATE_IMAGE_JSON": "update_image", "REGISTRY_SECRET_JSON": "registry_secret"}
ASSIGNMENT = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(UPDATE_IMAGE_JSON|REGISTRY_SECRET_JSON)[ \t]*=")
SUFFIX = ".image-settings-migration.json"


def object_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except json.JSONDecodeError:
            raise ValueError("旧镜像配置不是合法 JSON，请检查原配置后重试") from None
    if not isinstance(value, dict):
        raise ValueError("旧镜像配置必须为 JSON 对象，请检查原配置后重试")
    return value


def snapshot_path(config):
    return Path(str(config) + SUFFIX)


def read_snapshot(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("无法读取待迁移的镜像配置，已保留原文件，请检查后重试") from None
    if not isinstance(payload, dict) or set(payload) != set(SETTINGS.values()):
        raise ValueError("待迁移的镜像配置格式不正确，已保留原文件")
    return {key: object_value(value) for key, value in payload.items()}


def atomic_write(path, content, mode=0o600):
    """Write beside the destination, never following a destination symlink."""
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(content)
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(config, fields):
    if len(fields) != 5:
        raise ValueError("镜像配置迁移输入不完整")
    previous_raw, update_present, update_raw, registry_present, registry_raw = fields
    pending = snapshot_path(config)
    payload = read_snapshot(pending) if pending.exists() else dict.fromkeys(SETTINGS.values())
    try:
        previous = json.loads(previous_raw) if previous_raw.strip() else {}
        previous_data = previous.get("data", {})
        if not isinstance(previous_data, dict):
            raise ValueError
    except (AttributeError, ValueError):
        raise ValueError("无法解析旧 ConfigMap，尚未修改集群配置") from None
    for old_name, key in (("UPDATE_IMAGE", "update_image"), ("REGISTRY_SECRET", "registry_secret")):
        if old_name in previous_data and payload[key] is None:
            payload[key] = object_value(previous_data[old_name])
    for present, value, key in ((update_present, update_raw, "update_image"),
                                (registry_present, registry_raw, "registry_secret")):
        if present:
            payload[key] = object_value(value)
    if all(value is None for value in payload.values()):
        return False
    atomic_write(pending, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    return True


def assignment_end(text, start):
    """Find the end of a complete quoted shell assignment without running it."""
    quote = None
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "\n":
            return pos + 1
    if quote or escaped:
        raise ValueError("旧配置的引号未闭合，已保留本地配置和迁移快照")
    return len(text)


def clean_text(text, payload):
    blocks = {key: [] for key in SETTINGS}
    skip_until = 0
    for match in ASSIGNMENT.finditer(text):
        if match.start() < skip_until:
            continue
        end = assignment_end(text, match.start())
        skip_until = end
        try:
            tokens = shlex.split(text[match.start():end], comments=True, posix=True)
            if tokens and tokens[0] == "export":
                tokens = tokens[1:]
            if len(tokens) != 1 or not tokens[0].startswith(match.group(1) + "="):
                raise ValueError
            value = object_value(tokens[0].split("=", 1)[1])
        except ValueError:
            raise ValueError("旧配置不是独立的 JSON 赋值，已保留本地配置和迁移快照") from None
        blocks[match.group(1)].append((match.start(), end, value))
    removals = []
    for name, items in blocks.items():
        if not items:
            continue
        # Bash uses the last assignment. Never erase a value edited during rollout.
        if items[-1][2] != payload[SETTINGS[name]]:
            raise ValueError("迁移期间本地镜像配置有变化，已保留原配置和迁移快照，请重试")
        removals.extend((start, end) for start, end, _ in items)
    for start, end in sorted(removals, reverse=True):
        text = text[:start] + text[end:]
    return text


def cleanup(config):
    pending = snapshot_path(config)
    payload = read_snapshot(pending)
    # Preserve a user's config symlink, permissions and all unrelated text.
    target = config.resolve(strict=True)
    original = target.read_bytes()
    cleaned = clean_text(original.decode("utf-8"), payload).encode("utf-8")
    if cleaned != original:
        current = target.stat()
        if target.read_bytes() != original:
            raise ValueError("本地配置正在修改，已保留原配置和迁移快照，请重试")
        atomic_write(target, cleaned, stat.S_IMODE(current.st_mode))
    pending.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            raw = sys.stdin.buffer.read().decode("utf-8")
            fields = raw.split("\0")
            if fields and not fields[-1]:
                fields.pop()
            if prepare(args.config, fields):
                print("已暂存旧镜像配置，将在新 master 就绪后迁入数据库")
        else:
            cleanup(args.config)
            print("镜像配置已迁入数据库，本地旧配置与迁移快照已清理")
    except (OSError, UnicodeError):
        print("镜像配置迁移文件处理失败，已保留现有数据，请检查原配置后重试", file=sys.stderr)
        return 1
    except ValueError as exc:
        # All parser errors are replaced above with messages containing no payload.
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
