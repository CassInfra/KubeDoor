"""定时 / 周期任务到点回调 agent 自己时,页面和 AI 设的扩缩容开关要原样带上。"""

from conftest import AGENT_ROOT, load_module


def test_cron_callback_keeps_every_scale_switch():
    utils = load_module("agent_test_utils", AGENT_ROOT / "utils.py")
    query = {"env": "prod", "add_label": "true", "type": "cpu", "temp": "true", "scheduler": "true", "cci": "true"}
    # env 只用于 master 转发,agent 回调自己不需要;以前 temp/type/cci 到点全丢,add_label+scheduler 拼出两个 "?"
    assert utils.cron_callback_url("scale", query) == \
        "https://kubedoor-agent.kubedoor/api/scale?add_label=true&type=cpu&temp=true&scheduler=true&cci=true"
    assert utils.cron_callback_url("scale", {"temp": "true"}) == "https://kubedoor-agent.kubedoor/api/scale?temp=true"
    assert utils.cron_callback_url("restart", {"env": "prod"}) == "https://kubedoor-agent.kubedoor/api/restart"
