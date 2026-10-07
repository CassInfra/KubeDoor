"""db.py 同步桥的单元测试:主 loop 线程里误用时必须报错,不能死锁。不需要真实 PG。"""
import asyncio
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def test_run_sync_on_loop_thread_raises_instead_of_deadlock(monkeypatch):
    outcome = {}

    async def main():
        monkeypatch.setattr(db, '_loop', asyncio.get_running_loop())
        try:
            db._run_sync(asyncio.sleep(0, result='x'))
        except RuntimeError as e:
            outcome['error'] = str(e)

    # 放在独立线程里跑:万一守卫失效会永久死锁,用 join 超时让测试失败而不是卡住
    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), '同步桥在主 loop 线程里调用时死锁了'
    assert '死锁' in outcome['error']


def test_run_sync_from_worker_thread_returns_result(monkeypatch):
    async def main():
        monkeypatch.setattr(db, '_loop', asyncio.get_running_loop())
        return await asyncio.to_thread(db._run_sync, asyncio.sleep(0, result='ok'))

    assert asyncio.run(main()) == 'ok'
