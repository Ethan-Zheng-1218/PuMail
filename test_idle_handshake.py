"""验证 IMAP 待机（IDLE）握手：不连真实邮箱，用一个假的 IMAP 服务器来测。

直接运行：.venv\\Scripts\\python.exe test_idle_handshake.py

测的是 server.py 里的三个函数：imap_idle_begin / imap_idle_wait / imap_idle_end。
它们用 ast 从源码里取出来单独执行，不会启动整个应用、也不碰数据库。
"""

import ast
import imaplib
import select
import socket
import threading
import time


def load_functions(names):
    """把 server.py 里指定的函数取出来，放进一个独立的命名空间。"""
    with open('server.py', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename='server.py')
    # 注意：待机函数里用到了 imaplib 和 select，两者都要放进执行命名空间，
    # 少一个就会走进异常分支、表现为"收不到推送"。
    ns = {'imaplib': imaplib, 'select': select}
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = names - {n.name for n in picked}
    if missing:
        raise AssertionError('server.py 里找不到这些函数：%s' % ', '.join(sorted(missing)))
    module = ast.Module(body=picked, type_ignores=[])
    exec(compile(module, 'server.py', 'exec'), ns)
    return ns


class FakeImapServer(object):
    """只实现测试需要的那几条命令。第一条 IDLE 会主动推一条 EXISTS。"""

    def __init__(self, push_on_first_idle=True):
        self.push = push_on_first_idle
        self.idle_count = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.error = None

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _serve(self):
        try:
            conn, _addr = self.sock.accept()
            conn.settimeout(5)
            f = conn.makefile('rwb')
            f.write(b'* OK fake imap ready\r\n')
            f.flush()
            pending_idle_tag = None
            while True:
                line = f.readline()
                if not line:
                    break
                text = line.decode('utf-8', 'replace').strip()
                if not text:
                    continue
                if text.upper() == 'DONE':
                    f.write(pending_idle_tag + b' OK IDLE terminated\r\n')
                    f.flush()
                    pending_idle_tag = None
                    continue
                parts = text.split(' ', 2)
                tag = parts[0].encode()
                cmd = parts[1].upper() if len(parts) > 1 else ''
                if cmd == 'CAPABILITY':
                    f.write(b'* CAPABILITY IMAP4rev1 IDLE\r\n' + tag + b' OK CAPABILITY completed\r\n')
                elif cmd == 'LOGIN':
                    f.write(tag + b' OK LOGIN completed\r\n')
                elif cmd in ('SELECT', 'EXAMINE'):
                    f.write(b'* 3 EXISTS\r\n* 3 RECENT\r\n* FLAGS ()\r\n'
                            + tag + b' OK [READ-ONLY] ' + cmd.encode() + b' completed\r\n')
                elif cmd == 'NOOP':
                    f.write(tag + b' OK NOOP completed\r\n')
                elif cmd == 'IDLE':
                    self.idle_count += 1
                    pending_idle_tag = tag
                    f.write(b'+ idling\r\n')
                    f.flush()
                    if self.push and self.idle_count == 1:
                        time.sleep(0.3)
                        f.write(b'* 4 EXISTS\r\n')
                    f.flush()
                    continue
                elif cmd == 'LOGOUT':
                    f.write(b'* BYE\r\n' + tag + b' OK LOGOUT completed\r\n')
                    f.flush()
                    break
                else:
                    f.write(tag + b' BAD unknown command\r\n')
                f.flush()
        except Exception as exc:            # 测试辅助线程里的异常记下来，主线程再看
            self.error = exc


def _connect(port):
    conn = imaplib.IMAP4('127.0.0.1', port)
    conn.login('someone@example.com', 'secret')
    conn.select('INBOX', readonly=True)
    return conn


def test_handshake_receives_push():
    ns = load_functions({'imap_idle_begin', 'imap_idle_wait', 'imap_idle_end'})
    server = FakeImapServer(push_on_first_idle=True).start()
    try:
        conn = _connect(server.port)
        tag = ns['imap_idle_begin'](conn)
        assert tag, '待机请求应该返回一个标签'
        assert ns['imap_idle_wait'](conn, 5) is True, '服务器推了 EXISTS，应该报告收到推送'
        assert ns['imap_idle_end'](conn, tag), '退出待机应该拿到服务器回应'
        conn.logout()
    finally:
        server.stop()
    assert server.error is None, '假服务器出错：%r' % (server.error,)


def test_idle_waits_and_times_out_quietly():
    ns = load_functions({'imap_idle_begin', 'imap_idle_wait', 'imap_idle_end'})
    server = FakeImapServer(push_on_first_idle=False).start()
    try:
        conn = _connect(server.port)
        tag = ns['imap_idle_begin'](conn)
        started = time.time()
        assert ns['imap_idle_wait'](conn, 1) is False, '没有推送时应该安静地超时'
        waited = time.time() - started
        assert 0.8 <= waited <= 3.0, '等待时间不合理：%.2f 秒' % waited
        assert ns['imap_idle_end'](conn, tag), '超时后也应该能正常退出待机'
        conn.logout()
    finally:
        server.stop()
    assert server.error is None, '假服务器出错：%r' % (server.error,)


def test_can_rearm_on_same_connection():
    ns = load_functions({'imap_idle_begin', 'imap_idle_wait', 'imap_idle_end'})
    server = FakeImapServer(push_on_first_idle=True).start()
    try:
        conn = _connect(server.port)
        tag = ns['imap_idle_begin'](conn)
        ns['imap_idle_wait'](conn, 5)
        ns['imap_idle_end'](conn, tag)
        conn.noop()
        tag = ns['imap_idle_begin'](conn)          # 同一条连接上再次待机
        assert ns['imap_idle_wait'](conn, 1) is False
        assert ns['imap_idle_end'](conn, tag)
        conn.logout()
    finally:
        server.stop()
    assert server.idle_count >= 2, '应该在同一条连接上多次进入待机'
    assert server.error is None, '假服务器出错：%r' % (server.error,)


if __name__ == '__main__':
    test_handshake_receives_push()
    print('ok: 待机能收到服务器推送')
    test_idle_waits_and_times_out_quietly()
    print('ok: 没推送时安静超时')
    test_can_rearm_on_same_connection()
    print('ok: 同一条连接上可以反复待机（不重连）')
    print('ALL OK')
