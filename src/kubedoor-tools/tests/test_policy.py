"""测试 kubedoor_tools.policy 模块"""
import pytest
from kubedoor_tools.policy import classify_operation


def test_pod_exec_safe_commands_are_read_only():
    """查看命令应该被标记为只读,允许只读用户执行"""
    safe_commands = [
        ["ls", "-la"],
        ["cat", "/app/config.yaml"],
        ["env"],
        ["ps", "aux"],
        ["grep", "error", "/var/log/app.log"],
        ["curl", "http://api/health"],
        ["wget", "-O-", "http://api/status"],
        ["tail", "-f", "/var/log/app.log"],
        ["head", "-n", "20", "/tmp/output"],
        ["find", "/app", "-name", "*.yaml"],
    ]
    for argv in safe_commands:
        result = classify_operation("pod_exec", {"argv": argv})
        assert result["read_only"] is True, f"查看命令 {' '.join(argv)} 应该是只读的"
        assert result["dangerous"] is False, f"查看命令 {' '.join(argv)} 不应该是高危的"


def test_pod_exec_dangerous_commands_need_write_permission():
    """高危命令应该需要写权限"""
    dangerous_commands = [
        ["rm", "-rf", "/tmp"],
        ["rm", "/etc/passwd"],
        ["kill", "-9", "1234"],
        ["pkill", "java"],
        ["killall", "nginx"],
        ["echo", "test", ">", "/tmp/file"],
        ["cat", "/etc/passwd", ">>", "/tmp/data"],
        ["ls", "|", "xargs", "rm"],
        ["find", "/tmp", "-delete"],
        ["find", "/app", "-exec", "rm", "{}", ";"],
    ]
    for argv in dangerous_commands:
        result = classify_operation("pod_exec", {"argv": argv})
        assert result["read_only"] is False, f"高危命令 {' '.join(argv)} 应该需要写权限"
        assert result["dangerous"] is True, f"高危命令 {' '.join(argv)} 应该被标记为高危"


def test_pod_exec_empty_argv():
    """空命令应该安全处理"""
    result = classify_operation("pod_exec", {"argv": []})
    assert result["read_only"] is True, "空命令应该被视为只读"
    assert result["dangerous"] is False, "空命令不应该被视为高危"


def test_pod_exec_invalid_argv():
    """非列表的 argv 应该安全处理"""
    result = classify_operation("pod_exec", {"argv": "not a list"})
    assert result["read_only"] is True, "无效 argv 应该被视为只读"
    assert result["dangerous"] is False, "无效 argv 不应该被视为高危"


def test_pod_exec_missing_argv():
    """缺少 argv 应该安全处理"""
    result = classify_operation("pod_exec", {})
    assert result["read_only"] is True, "缺少 argv 应该被视为只读"
    assert result["dangerous"] is False, "缺少 argv 不应该被视为高危"
