"""sync_queue 的单元测试：串行、优先级、异常传递、同线程重入。

直接运行：.venv\\Scripts\\python.exe test_sync_queue.py
也可以用 pytest。
"""

import threading
import time

import sync_queue


def test_serialized_no_overlap():
    """同一账号的任务不允许同时在跑。"""
    acc = 9001
    state = {'running': 0, 'max': 0}
    lock = threading.Lock()

    def job():
        with lock:
            state['running'] += 1
            state['max'] = max(state['max'], state['running'])
        time.sleep(0.05)
        with lock:
            state['running'] -= 1

    threads = [threading.Thread(target=lambda: sync_queue.run(acc, job)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state['max'] == 1, '同一账号出现了并发执行：%s' % state['max']


def test_priority_order():
    """先到的后台任务要给后来的高优先级任务让位。"""
    acc = 9002
    order = []
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(3)
        order.append('blocker')

    tb = threading.Thread(target=lambda: sync_queue.run(acc, blocker))
    tb.start()
    assert started.wait(2), '阻塞任务没有启动'

    tl = threading.Thread(target=lambda: sync_queue.run(
        acc, lambda: order.append('background'), priority=sync_queue.PRIORITY_BACKGROUND))
    tl.start()
    time.sleep(0.1)

    th = threading.Thread(target=lambda: sync_queue.run(
        acc, lambda: order.append('user'), priority=sync_queue.PRIORITY_USER))
    th.start()
    time.sleep(0.1)

    release.set()
    tb.join(3)
    tl.join(3)
    th.join(3)
    assert order == ['blocker', 'user', 'background'], order


def test_exception_propagates():
    """任务里的异常要原样传回给提交方。"""
    acc = 9003

    def boom():
        raise ValueError('boom')

    try:
        sync_queue.run(acc, boom)
    except ValueError as exc:
        assert str(exc) == 'boom'
    else:
        raise AssertionError('异常没有被抛出')


def test_reentrant_same_thread():
    """任务内部再提交同一账号的任务，不能死锁。"""
    acc = 9004
    out = []

    def inner():
        out.append('inner')
        return 7

    def outer():
        out.append('outer')
        return sync_queue.run(acc, inner)

    assert sync_queue.run(acc, outer) == 7
    assert out == ['outer', 'inner'], out


def test_different_accounts_can_run_parallel():
    """不同账号之间可以并行（最多同时 2 个）。"""
    state = {'running': 0, 'max': 0}
    lock = threading.Lock()
    started = threading.Barrier(2, timeout=3)

    def job():
        started.wait()
        with lock:
            state['running'] += 1
            state['max'] = max(state['max'], state['running'])
        time.sleep(0.15)
        with lock:
            state['running'] -= 1

    threads = [threading.Thread(target=lambda a=a: sync_queue.run(a, job)) for a in (9101, 9102)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state['max'] >= 1


if __name__ == '__main__':
    test_serialized_no_overlap()
    print('ok: 同一账号串行')
    test_priority_order()
    print('ok: 优先级让位')
    test_exception_propagates()
    print('ok: 异常传递')
    test_reentrant_same_thread()
    print('ok: 同线程重入不死锁')
    test_different_accounts_can_run_parallel()
    print('ok: 不同账号可并行')
    print('ALL OK')
