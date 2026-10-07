import asyncio
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("ws_lifecycle", Path(__file__).parents[1] / "func_manager/ws_lifecycle.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Monitor:
    def __init__(self):
        self.running = False

    async def start_monitoring(self):
        self.running = True

    async def stop_monitoring(self):
        self.running = False


@pytest.mark.asyncio
async def test_monitor_launcher_return_does_not_disconnect_but_closed_ws_cleans_tasks():
    monitor, closed = Monitor(), asyncio.Event()
    finished = []

    async def process(_):
        await closed.wait()

    async def heartbeat(_):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append("heartbeat")

    async def health():
        try:
            await asyncio.Event().wait()
        finally:
            finished.append("health")

    task = asyncio.create_task(module.run_connection_tasks(None, monitor, process, heartbeat, health))
    await asyncio.sleep(0)
    assert monitor.running and not task.done()
    closed.set()
    await asyncio.wait_for(task, 1)
    assert set(finished) == {"heartbeat", "health"}
    assert not monitor.running


@pytest.mark.asyncio
async def test_cancelling_connection_supervisor_stops_monitor_and_children():
    monitor = Monitor()

    async def waiting(*args):
        await asyncio.Event().wait()

    task = asyncio.create_task(module.run_connection_tasks(None, monitor, waiting, waiting, waiting))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not monitor.running
