"""Supervise the long-running tasks belonging to one master connection."""

import asyncio


async def run_connection_tasks(ws, monitor, process_request, heartbeat, health_check):
    tasks = []
    try:
        # start_monitoring launches its own workers and returns immediately.
        await monitor.start_monitoring()
        tasks = [asyncio.create_task(process_request(ws)), asyncio.create_task(heartbeat(ws)),
                 asyncio.create_task(health_check())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await monitor.stop_monitoring()
