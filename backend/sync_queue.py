"""PuMail 账号级 IMAP 任务队列。

同一账号的 IMAP 操作严格串行执行：每个账号一个工作线程，
任务按优先级出队；账号之间最多同时 2 个在运行。

为什么要它：以前多个线程直接抢同一把账号锁，等 8 秒拿不到就抛
"imap busy"，前端会弹出"同步失败"。改成队列之后，等待就是正常排队，
不再产生错误；用户正在等待的操作（点开文件夹、手动刷新）还能插队。
"""

import heapq
import itertools
import threading

# 数值越小越优先
PRIORITY_USER = 0         # 用户正在等待的操作：点开文件夹、手动刷新
PRIORITY_CURRENT = 1      # 当前账号的收件箱
PRIORITY_OTHER = 2        # 其它账号的收件箱
PRIORITY_BACKGROUND = 3   # 后台补齐类任务

# 同时最多几个账号在联网（避免一次开出一堆连接）
MAX_PARALLEL_ACCOUNTS = 2


class _Task(object):
    __slots__ = ('fn', 'done', 'result', 'error')

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error = None


class AccountWorker(object):
    """单个账号的串行任务队列。"""

    def __init__(self, acc_id, gate):
        self.acc_id = acc_id
        self._gate = gate
        self._heap = []
        self._counter = itertools.count()
        self._cv = threading.Condition()
        self._thread = None
        self._thread_id = None

    # ---------------- 对外接口 ----------------

    def submit(self, fn, priority=PRIORITY_USER, timeout=None):
        # 已经在自己的工作线程里（任务嵌套提交）：直接执行，避免自我死锁
        if self._thread_id == threading.get_ident():
            return fn()

        task = _Task(fn)
        with self._cv:
            heapq.heappush(self._heap, (int(priority), next(self._counter), task))
            self._ensure_thread_locked()
            self._cv.notify()

        if not task.done.wait(timeout):
            raise TimeoutError('IMAP 任务排队等待超时')
        if task.error is not None:
            raise task.error
        return task.result

    def pending(self, max_priority=None):
        with self._cv:
            if max_priority is None:
                return len(self._heap)
            return sum(1 for p, _s, _t in self._heap if p <= max_priority)

    # ---------------- 内部实现 ----------------

    def _ensure_thread_locked(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop,
                name='imap-acc-%s' % self.acc_id,
                daemon=True,
            )
            self._thread.start()

    def _loop(self):
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                _priority, _seq, task = heapq.heappop(self._heap)

            self._gate.acquire()
            try:
                self._thread_id = threading.get_ident()
                task.result = task.fn()
            except BaseException as exc:      # 把异常带回去给提交方
                task.error = exc
            finally:
                self._thread_id = None
                self._gate.release()
                task.done.set()


_workers = {}
_workers_guard = threading.Lock()
_gate = threading.BoundedSemaphore(MAX_PARALLEL_ACCOUNTS)


def worker_for(acc_id):
    """取（必要时创建）某个账号的工作队列。"""
    key = int(acc_id)
    with _workers_guard:
        worker = _workers.get(key)
        if worker is None:
            worker = AccountWorker(key, _gate)
            _workers[key] = worker
        return worker


def run(acc_id, fn, priority=PRIORITY_USER, timeout=None):
    """把 fn 排进该账号的队列并等它执行完；异常原样抛出。"""
    return worker_for(acc_id).submit(fn, priority=priority, timeout=timeout)


def stats():
    """当前各账号的排队数量（便于排查）。"""
    with _workers_guard:
        return dict((aid, worker.pending()) for aid, worker in _workers.items())
