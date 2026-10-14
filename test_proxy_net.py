"""自动代理的测试：解析、SOCKS5/HTTP 隧道、连不上时的快速失败。不碰真实网络。"""

import socket
import tempfile
import threading
import time
import unittest

import server


class _FakeSocks5(socket.socket):
    """最小的 SOCKS5 服务端：只做握手，然后回显数据。"""


def _start_fake_proxy(kind='socks5'):
    """起一个假代理，返回 (port, 收到的请求列表, 关闭函数)。"""
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    seen = []
    stop = threading.Event()

    def handle(conn):
        try:
            if kind == 'socks5':
                head = conn.recv(3)
                seen.append(('greeting', head))
                conn.sendall(b'\x05\x00')
                req = conn.recv(4)
                ln = req[3] == 3 and conn.recv(1)[0] or 0
                host = conn.recv(ln).decode() if ln else ''
                p = int.from_bytes(conn.recv(2), 'big')
                seen.append(('connect', host, p))
                conn.sendall(b'\x05\x00\x00\x01' + bytes([127, 0, 0, 1]) + (0).to_bytes(2, 'big'))
            else:
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = conn.recv(512)
                    if not chunk:
                        break
                    data += chunk
                first = data.split(b'\r\n', 1)[0].decode('latin-1', 'replace')
                seen.append(('http', first))
                conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            conn.sendall(b'PONG')
            # 把客户端后面发的数据读掉再关，否则 Windows 上 close 会回 RST
            try:
                conn.settimeout(0.4)
                conn.recv(64)
            except Exception:
                pass
            time.sleep(0.05)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def loop():
        while not stop.is_set():
            try:
                srv.settimeout(0.3)
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()

    def close():
        stop.set()
        try:
            srv.close()
        except Exception:
            pass

    return port, seen, close


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        server.apply_data_dir(self.data_dir.name)
        server._mem_cache.clear()
        server.init_db()
        server._proxy_cache['value'] = ''
        server._proxy_cache['ts'] = time.time()
        server._host_route.clear()
        server._connect_fails.clear()
        server.setting_set(server.PROXY_SETTING_KEY, '')

    def tearDown(self):
        server._host_route.clear()
        server._connect_fails.clear()
        self.data_dir.cleanup()

    def test_parse_proxy_forms(self):
        self.assertEqual(server._parse_proxy('127.0.0.1:7897'),
                         ('socks5', '127.0.0.1', 7897, '', ''))
        self.assertEqual(server._parse_proxy('socks5://127.0.0.1:7897'),
                         ('socks5', '127.0.0.1', 7897, '', ''))
        self.assertEqual(server._parse_proxy('http://u:p@10.0.0.1:8080'),
                         ('http', '10.0.0.1', 8080, 'u', 'p'))
        self.assertEqual(server._parse_proxy('[::1]:1080'),
                         ('socks5', '::1', 1080, '', ''))
        self.assertIsNone(server._parse_proxy(''))
        self.assertIsNone(server._parse_proxy('没有端口'))
        self.assertIsNone(server._parse_proxy('ftp://x:1'))

    def test_windows_proxy_value_variants(self):
        self.assertEqual(server._pick_proxy_server('127.0.0.1:7897'), '127.0.0.1:7897')
        self.assertEqual(server._pick_proxy_server('http=a:1;https=b:2'), 'b:2')
        self.assertEqual(server._pick_proxy_server('http=a:1'), 'a:1')
        self.assertEqual(server._pick_proxy_server('  '), '')

    def test_manual_proxy_setting_wins(self):
        server.setting_set(server.PROXY_SETTING_KEY, '10.1.2.3:9999')
        self.assertEqual(server.system_proxy(), '10.1.2.3:9999')

    def _connect_through(self, kind):
        port, seen, close = _start_fake_proxy(kind)
        try:
            server.setting_set(server.PROXY_SETTING_KEY, '%s://127.0.0.1:%d' % (kind, port))
            server._host_route['mail.example.com'] = ('proxy', time.time())
            sock = server.open_tcp('mail.example.com', 993, timeout=5)
            sock.sendall(b'PING')
            self.assertEqual(sock.recv(4), b'PONG')
            sock.close()
        finally:
            close()
            server.setting_set(server.PROXY_SETTING_KEY, '')
        return seen

    def test_socks5_tunnel(self):
        seen = self._connect_through('socks5')
        self.assertEqual(seen[0][0], 'greeting')
        self.assertEqual(seen[1], ('connect', 'mail.example.com', 993))

    def test_http_connect_tunnel(self):
        seen = self._connect_through('http')
        self.assertEqual(seen[0][0], 'http')
        self.assertIn('CONNECT mail.example.com:993', seen[0][1])

    def test_no_proxy_goes_direct(self):
        port, seen, close = _start_fake_proxy('socks5')
        try:
            server.setting_set(server.PROXY_SETTING_KEY, '')
            server._proxy_cache['value'] = ''
            sock = server.open_tcp('127.0.0.1', port, timeout=5)
            sock.settimeout(2)
            sock.close()
        finally:
            close()
        # 直连这个"目标"本身就会让假代理收到一个空读，关键是不能出现 SOCKS5 的 CONNECT
        self.assertFalse(any(entry[0] == 'connect' for entry in seen),
                         '没有代理时不应该把连接送进代理')

    def test_fail_fast_after_repeated_failures(self):
        acc = {'id': 4242, 'imap_host': 'imap.example.com', 'imap_port': 993}
        server.note_connect(4242, False)
        self.assertTrue(server.connect_allowed(4242))
        server.note_connect(4242, False)
        self.assertFalse(server.connect_allowed(4242), '连续失败后要先歇一会儿')
        t0 = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            server.imap_open(acc)
        self.assertLess(time.time() - t0, 1.0, '被熔断时要立刻报错，不能再去等超时')
        self.assertIn('网络或代理不通', str(ctx.exception))
        server.note_connect(4242, True)
        self.assertTrue(server.connect_allowed(4242))

    def test_host_route_memo(self):
        server._host_route.clear()
        self.assertEqual(server.host_route('a.example.com'), '')
        server._host_route['a.example.com'] = ('proxy', time.time())
        self.assertEqual(server.host_route('a.example.com'), 'proxy')

    def test_falls_back_to_direct_when_proxy_dies(self):
        """记着'走代理'的服务器，代理关掉后要能自己改回直连。"""
        port, _seen, close = _start_fake_proxy('socks5')
        try:
            # 代理指向一个没人监听的端口，同时这台"服务器"直连是通的
            dead = socket.socket()
            dead.bind(('127.0.0.1', 0))
            dead_port = dead.getsockname()[1]
            dead.close()
            server.setting_set(server.PROXY_SETTING_KEY, 'socks5://127.0.0.1:%d' % dead_port)
            server._proxy_cache['value'] = ''
            server._host_route['127.0.0.1'] = ('proxy', time.time())
            sock = server.open_tcp('127.0.0.1', port, timeout=5)
            sock.close()
            self.assertEqual(server.host_route('127.0.0.1'), 'direct',
                             '代理不通时应该自动退回直连，并把结论改过来')
        finally:
            close()
            server.setting_set(server.PROXY_SETTING_KEY, '')

    def test_domestic_mail_never_uses_proxy(self):
        """国内邮箱（QQ/163…）永远直连，绝不绕代理。"""
        port, seen, close = _start_fake_proxy('socks5')
        try:
            server.setting_set(server.PROXY_SETTING_KEY, 'socks5://127.0.0.1:%d' % port)
            server._proxy_cache['value'] = ''
            for host in ('imap.qq.com', 'imap.163.com', 'smtp.163.com', 'imap.126.com',
                         'imap.exmail.qq.com', 'imap.vip.163.com'):
                self.assertTrue(server.is_domestic_host(host))
            self.assertFalse(server.is_domestic_host('imap.gmail.com'))
            self.assertFalse(server.is_domestic_host('outlook.office365.com'))
            self.assertFalse(server.is_domestic_host('imap.mail.me.com'))
            # 就算记忆里写着"走代理"，国内邮箱也要直连
            server._host_route['imap.163.com'] = ('proxy', time.time())
            asked = []
            real_create = socket.create_connection

            def spy(addr, timeout=None, **kw):
                asked.append(addr)
                raise ConnectionRefusedError('测试里不真的联网')

            socket.create_connection = spy
            try:
                with self.assertRaises(Exception):
                    server.open_tcp('imap.163.com', 993, timeout=2)
            finally:
                socket.create_connection = real_create
            self.assertEqual(asked, [('imap.163.com', 993)],
                             '国内邮箱只应该直连目标服务器，不去碰代理')
            self.assertEqual(seen, [], '国内邮箱一个包都不该发给代理')
            self.assertEqual(server.host_route('imap.163.com'), '', '记忆里的代理结论要被清掉')
        finally:
            close()
            server.setting_set(server.PROXY_SETTING_KEY, '')

    def test_foreign_mail_goes_to_proxy_first(self):
        """国外邮箱（Gmail 等）有代理就直接走代理，省掉试直连的几秒。"""
        port, seen, close = _start_fake_proxy('socks5')
        try:
            server.setting_set(server.PROXY_SETTING_KEY, 'socks5://127.0.0.1:%d' % port)
            server._proxy_cache['value'] = ''
            asked = []
            real_create = socket.create_connection

            def spy(addr, timeout=None, **kw):
                asked.append(addr)
                return real_create(addr, timeout)

            socket.create_connection = spy
            try:
                sock = server.open_tcp('imap.gmail.com', 993, timeout=5)
                sock.close()
            finally:
                socket.create_connection = real_create
            self.assertEqual(asked[0], ('127.0.0.1', port), '第一跳就该是代理')
            self.assertEqual(server.host_route('imap.gmail.com'), 'proxy')
            self.assertTrue(any(e[0] == 'connect' for e in seen))
            self.assertTrue(server.is_foreign_mail_host('imap.gmail.com'))
            self.assertTrue(server.is_foreign_mail_host('outlook.office365.com'))
            self.assertTrue(server.is_foreign_mail_host('imap.mail.me.com'))
            self.assertFalse(server.is_foreign_mail_host('imap.qq.com'))
        finally:
            close()
            server.setting_set(server.PROXY_SETTING_KEY, '')

    def test_body_error_text_is_human_readable(self):
        err = socket.timeout('timed out')
        self.assertEqual(server.body_error_text(err), '连不上邮件服务器（网络或代理不通）')
        self.assertIn('连不上', server.body_error_text(RuntimeError('连不上 x：网络或代理不通，稍后自动重试')))


if __name__ == '__main__':
    unittest.main()
