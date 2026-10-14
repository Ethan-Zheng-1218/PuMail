"""收信链路的回归测试：不联网，用假 IMAP 连接和假同步函数。

直接运行：.venv\\Scripts\\python.exe -m unittest test_receive_pipeline
"""

import sqlite3
import tempfile
import threading
import time
import unittest
import re

import server


class _FakeConn(object):
    """只实现测试用到的那部分 IMAP 接口。"""

    def __init__(self, fetch_payload=None, fail=False):
        self.fetch_payload = fetch_payload or []
        self.fail = fail

    def uid(self, command, *args):
        if self.fail:
            raise RuntimeError('imap boom')
        if str(command).lower() == 'fetch':
            return ('OK', self.fetch_payload)
        return ('OK', [b''])


class _FakeSelectConn(object):
    """模拟 SELECT 之后 imaplib 里留下的未登记响应。"""

    def __init__(self, uidvalidity=4242, uidnext=77, exists=3):
        self.untagged_responses = {
            'EXISTS': [str(exists).encode()],
            'UIDVALIDITY': [str(uidvalidity).encode()],
            'UIDNEXT': [str(uidnext).encode()],
        }
        self._pumail_mailbox = None
        self._pumail_readonly = None
        self.selects = []

    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        return ('OK', [str(self.untagged_responses['EXISTS'][0], 'ascii').encode()])


RAW_SMALL_MAIL = (b'From: Sender <s@example.com>\r\nTo: me@example.com\r\n'
                  b'Subject: Hello\r\nDate: Mon, 06 Oct 2026 10:00:00 +0800\r\n'
                  b'MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n'
                  b'\r\nHello body line\r\n')


# 一封带 5MB PDF 附件的典型邮件：正文 1=纯文本、2=HTML，附件 3
MIXED_STRUCTURE = (
    '(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 13 1)'
    ' ("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 22 1)'
    ' ("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 5242880 NIL'
    ' ("ATTACHMENT" ("FILENAME" "report.pdf")))'
    ' "MIXED" ("BOUNDARY" "b1"))'
)

PLAIN_PART = b'Hello plain\r\n'
HTML_PART = b'<p>Hi=20there</p>'          # quoted-printable：=20 是空格
HEADER_PART = b'Message-ID: <abc@example.com>\r\nCc: cc@example.com\r\n\r\n'


class _FakeStructureConn(object):
    """够用的假 IMAP：能回答 BODYSTRUCTURE 和指定段落，并记录取过哪些段落。"""

    def __init__(self, structure=MIXED_STRUCTURE, plain=PLAIN_PART, html=HTML_PART,
                 header=HEADER_PART, big_attachment=b'x' * 64, flags=b''):
        self.structure = structure
        self.flags = flags
        self.parts = {
            'HEADER.FIELDS (MESSAGE-ID CC REPLY-TO)': header,
            '1': plain,
            '2': html,
            '3': big_attachment,
        }
        self.fetched_sections = []
        self._pumail_mailbox = None
        self.untagged_responses = {}

    def select(self, mailbox, readonly=False):
        return ('OK', [b'3'])

    def uid(self, command, *args):
        if str(command).lower() != 'fetch':
            return ('OK', [b''])
        spec = args[1] if len(args) > 1 else b''
        spec_text = spec.decode('ascii', 'replace') if isinstance(spec, (bytes, bytearray)) else str(spec)
        if 'BODYSTRUCTURE' in spec_text:
            meta = (b'* 5 FETCH (UID 5 FLAGS (' + self.flags + b') BODYSTRUCTURE '
                    + self.structure.encode('ascii') + b')')
            return ('OK', [meta])
        sections = re.findall(r'BODY\.PEEK\[([^\]]*)\]', spec_text)
        self.fetched_sections.extend(sections)
        out = []
        for i, sec in enumerate(sections):
            data = self.parts.get(sec, b'')
            head = b'* 5 FETCH (UID 5 ' if i == 0 else b' '
            out.append((head + b'BODY[%s] {%d}' % (sec.encode('ascii'), len(data)), data))
        out.append((b' FLAGS ())', b''))
        return ('OK', out)


class _LiteralStructureConn(_FakeStructureConn):
    """文件名用 IMAP literal 给的服务器（QQ 就是这种），必须也能解析。"""

    def __init__(self, literal_name='巴斯夫声明.pdf'):
        super().__init__()
        self.literal = literal_name.encode('utf-8')

    def uid(self, command, *args):
        spec = args[1] if len(args) > 1 else b''
        spec_text = spec.decode('ascii', 'replace') if isinstance(spec, (bytes, bytearray)) else str(spec)
        if str(command).lower() == 'fetch' and 'BODYSTRUCTURE' in spec_text:
            head = (b'* 5 FETCH (UID 5 FLAGS () BODYSTRUCTURE '
                    b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 13 1)'
                    b' ("APPLICATION" "PDF" ("NAME" {%d}' % len(self.literal))
            tail = (b') NIL NIL "BASE64" 1000 NIL'
                    b' ("ATTACHMENT" ("FILENAME" "report.pdf"))) "MIXED" ("BOUNDARY" "b1"))')
            return ('OK', [(head, self.literal), (tail, b''), (b' FLAGS ())', b'')])
        return super().uid(command, *args)


def _wait_prefetch_quiet(timeout=3.0):
    """等后台预热线程收工，否则临时目录会被它占着删不掉。"""
    deadline = time.time() + timeout
    while server._prefetch_running and time.time() < deadline:
        time.sleep(0.05)


class _PipelineBase(unittest.TestCase):
    """一份隔离的数据目录 + 一个账号 + 5 封未读邮件。"""

    def setUp(self):
        # 后台同步线程可能还在收尾，收不掉临时目录不该让测试失败
        self.data_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        server.apply_data_dir(self.data_dir.name)
        server._mem_cache.clear()
        server._stats_cache.clear()
        server.init_db()
        db = server.get_db()
        self.acc_id = db.execute(
            "INSERT INTO accounts(email, name, provider, sort_order, active) VALUES(?,?,?,?,?)",
            ('pipe@example.com', 'Pipe', 'custom', 1, 1)).lastrowid
        now = time.time()
        for uid in range(1, 6):
            db.execute(
                "INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,ts,unread,starred,pending_sync)"
                " VALUES(?,?,?,?,?,?,?,1,0,'')",
                (self.acc_id, 'INBOX', str(uid), 'Mail %d' % uid, 'S', 's@example.com', now + uid))
        db.execute("INSERT INTO folder_state(acc_id,folder,last_uid,last_sync,oldest_uid,caught_up)"
                   " VALUES(?,?,?,?,?,0)", (self.acc_id, 'INBOX', 5, now, 1))
        db.commit()
        db.close()
        server.db_save_folders(self.acc_id, [{'id': 'INBOX', 'label': '收件箱'}])
        self._patched = {}

    def tearDown(self):
        for name, value in self._patched.items():
            setattr(server, name, value)
        _wait_prefetch_quiet()
        self.data_dir.cleanup()

    def patch(self, name, value):
        """临时替换 server 里的某个函数（tearDown 自动还原）。"""
        if name not in self._patched:
            self._patched[name] = getattr(server, name)
        setattr(server, name, value)

    def account(self):
        return server.get_account(self.acc_id)

    def _mail_row(self, uid='5'):
        db = server.get_db()
        row = db.execute('SELECT unread, has_body, pending_sync, body FROM mails'
                         ' WHERE acc_id=? AND folder=? AND uid=?',
                         (self.acc_id, 'INBOX', uid)).fetchone()
        db.close()
        return dict(row) if row else None

    def _seen_jobs(self):
        db = server.get_db()
        rows = db.execute("SELECT uid FROM sync_jobs WHERE acc_id=? AND kind='seen'",
                          (self.acc_id,)).fetchall()
        db.close()
        return [r['uid'] for r in rows]


class ReceivePipelineTests(_PipelineBase):

    # ---------- 打开文件夹不再等网络 ----------
    def test_opening_inbox_does_not_wait_for_network(self):
        calls = []

        def slow_sync(acc, folder, limit=None, priority=None):
            calls.append((folder, time.time()))
            time.sleep(3)

        original = server.sync_folder_headers
        server.sync_folder_headers = slow_sync
        try:
            client = server.app.test_client()
            client.set_cookie('pupu_ok', server.APP_SECRET)
            t0 = time.time()
            resp = client.get('/api/mails?acc=%d&folder=INBOX&page=1' % self.acc_id)
            elapsed = time.time() - t0
        finally:
            server.sync_folder_headers = original

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['items']), 5)
        # 本地有内容时必须立刻返回（联网同步交给后台线程）
        self.assertLess(elapsed, 1.0, '打开收件箱不应该等 IMAP（耗时 %.2fs）' % elapsed)

    # ---------- 变更事件 ----------
    def test_events_are_published_and_can_be_read_since_a_point(self):
        before = server.event_head()
        server.publish('mail', acc=self.acc_id, folder='INBOX', count=2)
        later = server.events_since(before)
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]['type'], 'mail')
        self.assertEqual(later[0]['count'], 2)
        self.assertGreater(later[0]['id'], before)
        # 读过的位置不会重复拿到同一条
        self.assertEqual(server.events_since(later[0]['id']), [])

    def test_events_endpoint_is_an_sse_stream(self):
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        resp = client.get('/api/events', buffered=False)
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, 'text/event-stream')
        finally:
            resp.close()

    def test_new_mail_publishes_a_mail_event(self):
        self.patch('queue_body_prefetch', lambda *a, **k: None)
        before = server.event_head()
        server.announce_new_mail(self.account(), 'INBOX', [b'9'])
        events = server.events_since(before)
        self.assertTrue(any(e['type'] == 'mail' and e['last_uid'] == 9 for e in events))

    def test_new_mail_starts_a_body_prefetch(self):
        """到达即预热：announce_new_mail 必须把正文预热带起来。"""
        calls = []
        self.patch('queue_body_prefetch',
                   lambda acc, folder, uids, **kw: calls.append((folder, list(uids), kw)))
        server.announce_new_mail(self.account(), 'INBOX', [b'7', b'8'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'INBOX')
        self.assertEqual(calls[0][1], [b'7', b'8'])

    def test_notification_carries_clean_uids(self):
        """提示里带的 uid 要能直接拿去打开邮件（IMAP 回来的是 bytes）。"""
        self.patch('queue_body_prefetch', lambda *a, **k: None)
        before = server.event_head()
        server.notify_new_mail(self.account(), 'INBOX', [b'2601', b'2602'], baseline=2600)
        events = server.events_since(before)
        newmail = [e for e in events if e['type'] == 'newmail']
        self.assertTrue(newmail)
        self.assertEqual(newmail[0]['uids'], ['2601', '2602'])
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        notes = client.get('/api/notifications').get_json()['notifications']
        self.assertEqual(notes[-1]['uids'], ['2601', '2602'])

    def test_sse_stream_delivers_a_published_event(self):
        """端到端：长连接真的会把后到的变化推出来。"""
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        resp = client.get('/api/events', buffered=False)
        self.assertEqual(resp.status_code, 200)

        def fire():
            time.sleep(0.4)
            server.publish('mail', acc=self.acc_id, folder='INBOX', count=3)

        def watchdog():
            time.sleep(6)
            try:
                resp.close()
            except Exception:
                pass

        threading.Thread(target=fire, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        body = b''
        try:
            for chunk in resp.iter_encoded():
                body += chunk
                if b'"type": "mail"' in body:
                    break
        except Exception:
            pass
        finally:
            resp.close()
        self.assertIn(b'"type": "mail"', body)
        self.assertIn(b'"count": 3', body)

    # ---------- 已读状态校准（手机上读了/删了） ----------
    def test_flag_calibration_applies_phone_side_changes(self):
        before = server.event_head()
        conn = _FakeConn([
            (b'1 (UID 5 FLAGS (\\Seen))', b''),
            (b'2 (UID 4 FLAGS (\\Seen \\Flagged))', b''),
            (b'3 (UID 3 FLAGS ())', b''),
            # UID 2 没有返回 → 相当于被手机删掉/移走了
        ])
        changed = server.sync_recent_flags(conn, self.account(), 'INBOX', span=10)
        self.assertGreaterEqual(changed, 3)
        db = server.get_db()
        rows = {r['uid']: dict(r) for r in db.execute(
            'SELECT uid,unread,starred,local_deleted FROM mails WHERE acc_id=? AND folder=?',
            (self.acc_id, 'INBOX')).fetchall()}
        db.close()
        self.assertEqual(rows['5']['unread'], 0)
        self.assertEqual(rows['4']['starred'], 1)
        self.assertEqual(rows['4']['unread'], 0)
        self.assertEqual(rows['2']['local_deleted'], 1)
        self.assertTrue(any(e['type'] == 'flags' for e in server.events_since(before)))

    def test_flag_calibration_keeps_local_pending_changes(self):
        db = server.get_db()
        db.execute("UPDATE mails SET pending_sync='seen' WHERE acc_id=? AND folder=? AND uid=?",
                   (self.acc_id, 'INBOX', '5'))
        db.commit()
        db.close()
        conn = _FakeConn([(b'1 (UID 5 FLAGS (\\Seen))', b'')])
        server.sync_recent_flags(conn, self.account(), 'INBOX', span=10)
        db = server.get_db()
        row = db.execute('SELECT unread, pending_sync FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                         (self.acc_id, 'INBOX', '5')).fetchone()
        db.close()
        self.assertEqual(row['unread'], 1, '本地还没推上去的改动不能被服务器状态覆盖')

    # ---------- UIDVALIDITY ----------
    def test_uidvalidity_change_drops_stale_cache_and_resets_cursor(self):
        acc = self.account()
        # 第一次记录不下手清理
        self.assertFalse(server.check_uidvalidity(acc, 'INBOX', 111, 6))
        self.assertTrue(server.check_uidvalidity(acc, 'INBOX', 222, 7))
        db = server.get_db()
        left = db.execute('SELECT COUNT(*) FROM mails WHERE acc_id=? AND folder=?',
                          (self.acc_id, 'INBOX')).fetchone()[0]
        state = server.get_folder_state(self.acc_id, 'INBOX')
        db.close()
        self.assertEqual(left, 0)
        self.assertEqual(int(state['last_uid']), 0)
        self.assertEqual(int(state['uidvalidity']), 222)
        self.assertEqual(int(state['uidnext']), 7)
        # 相同编号再来一次不会重复清理
        self.assertFalse(server.check_uidvalidity(acc, 'INBOX', 222, 8))

    def test_uidvalidity_unknown_value_is_ignored(self):
        acc = self.account()
        server.check_uidvalidity(acc, 'INBOX', 111, 6)
        self.assertFalse(server.check_uidvalidity(acc, 'INBOX', 0, 6))
        db = server.get_db()
        left = db.execute('SELECT COUNT(*) FROM mails WHERE acc_id=? AND folder=?',
                          (self.acc_id, 'INBOX')).fetchone()[0]
        db.close()
        self.assertEqual(left, 5)

    def test_select_folder_records_uidvalidity_from_imap_response(self):
        """SELECT 里拿到的编号要落到 INBOX 这一行上（不能用工信名做 key）。"""
        conn = _FakeSelectConn(uidvalidity=4242, uidnext=77)
        self.assertTrue(server.select_folder(conn, 'INBOX', readonly=True, acc=self.account()))
        state = server.get_folder_state(self.acc_id, 'INBOX')
        self.assertEqual(int(state['uidvalidity']), 4242)
        self.assertEqual(int(state['uidnext']), 77)
        self.assertEqual(int(state['last_uid']), 5, '只是记录编号，不能动游标')
        db = server.get_db()
        stale = db.execute("SELECT COUNT(*) FROM folder_state WHERE acc_id=? AND folder LIKE '\"%'",
                           (self.acc_id,)).fetchone()[0]
        db.close()
        self.assertEqual(stale, 0, '不应该再写出带引号的文件夹状态')

    def test_readonly_select_is_upgraded_before_writing_flags(self):
        """读信只读选择过之后，推"已读/星标"必须重新切成读写选择。"""
        conn = _FakeSelectConn()
        acc = self.account()
        self.assertTrue(server.select_folder(conn, 'INBOX', readonly=True, acc=acc))
        self.assertTrue(server.select_folder(conn, 'INBOX', readonly=True, acc=acc))
        self.assertEqual(len(conn.selects), 1, '重复的只读需求可以直接复用连接')

        self.assertTrue(server.select_folder(conn, 'INBOX', readonly=False, acc=acc))
        self.assertEqual(len(conn.selects), 2, '要写操作就必须切成读写选择')
        self.assertFalse(conn.selects[-1][1])

        # 读写选择能满足后续任何需求，不必来回切
        self.assertTrue(server.select_folder(conn, 'INBOX', readonly=True, acc=acc))
        self.assertEqual(len(conn.selects), 2)

    # ---------- 未读统计缓存 ----------
    def test_unread_stats_are_cached_until_something_changes(self):
        calls = []
        original = server.threads_from_db

        def counting(acc, folder, q=''):
            calls.append(folder)
            return original(acc, folder, q)

        server.threads_from_db = counting
        try:
            first = server.folder_unread_count(self.acc_id, 'INBOX')
            second = server.folder_unread_count(self.acc_id, 'INBOX')
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1, '5 秒内不应该反复重算')
            server.stats_bump()
            server.folder_unread_count(self.acc_id, 'INBOX')
            self.assertEqual(len(calls), 2, '状态变化后必须重算')
        finally:
            server.threads_from_db = original

    # ---------- 收信模式 ----------
    def test_sync_mode_round_trip_and_poll_interval(self):
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        self.assertEqual(client.get('/api/general').get_json()['sync_mode'], 'realtime')

        resp = client.post('/api/general', json={'sync_mode': 'eco'})
        self.assertEqual(resp.get_json()['sync_mode'], 'eco')
        self.assertEqual(server.sync_mode(), 'eco')

        acc = self.account()
        provider = (acc.get('provider') or '').lower()
        base = server._POLL_INTERVAL.get(provider, server._POLL_DEFAULT)
        self.assertEqual(server.poll_interval_for(acc), max(30, base * 3))

        server.setting_set('poll_interval:custom', '45')
        self.assertEqual(server.poll_interval_for(acc), 135)
        server.setting_set('poll_interval:custom', '')

        self.assertEqual(client.post('/api/general', json={'sync_mode': 'nonsense'})
                         .get_json().get('sync_mode'), None)
        self.assertEqual(server.sync_mode(), 'eco')

    def test_sync_status_reports_each_account(self):
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        data = client.get('/api/sync-status').get_json()
        self.assertEqual(data['mode'], 'realtime')
        self.assertEqual(len(data['accounts']), 1)
        row = data['accounts'][0]
        self.assertEqual(row['email'], 'pipe@example.com')
        self.assertEqual(row['last_uid'], 5)
        self.assertIn(row['mode'], ('idle', 'poll', 'manual', 'off'))


class ReadAndPrefetchTests(_PipelineBase):
    """1.1.2：点开即已读 + 到达即预热正文。"""

    def test_thread_marks_read_even_when_body_fetch_fails(self):
        self.patch('background_sync', lambda *a, **k: None)

        def boom(*a, **k):
            raise RuntimeError('imap timeout')

        self.patch('fetch_one_body', boom)
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        resp = client.get('/api/thread?acc=%d&folder=INBOX&uids=5' % self.acc_id)

        self.assertEqual(resp.status_code, 200)
        row = self._mail_row('5')
        self.assertEqual(row['unread'], 0, '正文取不到也必须算已读')
        self.assertEqual(row['pending_sync'], 'seen')
        self.assertIn('5', self._seen_jobs())
        messages = resp.get_json()['messages']
        self.assertEqual(len(messages), 1, '正文失败也要把这封邮件给界面，不能凭空消失')
        self.assertEqual(messages[0]['uid'], '5')

    def test_thread_uses_cached_body_without_touching_network(self):
        db = server.get_db()
        db.execute("UPDATE mails SET has_body=1, body='cached', html=? WHERE acc_id=? AND uid='5'",
                   (server.sanitize_html('<p>cached</p>'), self.acc_id))
        db.commit()
        db.close()
        called = []
        self.patch('fetch_one_body', lambda *a, **k: called.append(a))
        self.patch('background_sync', lambda *a, **k: None)
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        resp = client.get('/api/thread?acc=%d&folder=INBOX&uids=5' % self.acc_id)
        self.assertEqual(resp.get_json()['messages'][0]['body'], 'cached')
        self.assertEqual(called, [], '本地已有正文就不该再联网')

    def test_text_only_fetch_never_downloads_attachments(self):
        """核心：带 5MB 附件的邮件，只下正文，附件字节一个都不碰。"""
        conn = _FakeStructureConn()
        data = server.fetch_body_parts(self.account(), 'INBOX', '5', conn)
        self.assertIsNotNone(data)
        self.assertIn('Hello plain', data['body'])
        self.assertIn('Hi there', data['html'], 'quoted-printable 要正确解码')
        self.assertEqual(data['attachments'],
                         [{'part': '3', 'name': 'report.pdf', 'size': int(5242880 * 3 / 4)}],
                         '附件只记名字/大小/段号（base64 的按真实大小换算）')
        self.assertNotIn('3', conn.fetched_sections, '绝不允许把附件内容下下来')
        self.assertEqual(data['message_id'], '<abc@example.com>')
        self.assertEqual(data['cc'], 'cc@example.com')

    def test_prefetch_stores_body_without_attachments(self):
        conn = _FakeStructureConn()
        self.patch('with_imap', lambda acc, fn, **kw: fn(conn))
        self.assertTrue(server.prefetch_body(self.account(), 'INBOX', '5'))
        row = self._mail_row('5')
        self.assertEqual(row['has_body'], 1)
        self.assertIn('Hello plain', row['body'])
        self.assertEqual(row['unread'], 1, '预热不能改变未读状态')
        self.assertNotIn('3', conn.fetched_sections)
        db = server.get_db()
        saved = db.execute("SELECT attachments FROM mails WHERE acc_id=? AND uid='5'",
                           (self.acc_id,)).fetchone()['attachments']
        db.close()
        self.assertIn('report.pdf', saved)

    def test_prefetch_skips_oversized_text(self):
        huge = ('(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" %d 1)'
                ' "MIXED" ("BOUNDARY" "b1"))') % (server.PREFETCH_SECTION_MAX_BYTES + 1)
        conn = _FakeStructureConn(structure=huge)
        self.patch('with_imap', lambda acc, fn, **kw: fn(conn))
        self.assertFalse(server.prefetch_body(self.account(), 'INBOX', '5'))
        self.assertEqual(self._mail_row('5')['has_body'], 0, '正文太大就不预读，留给点开时再取')

    def test_prefetch_is_idempotent_for_cached_mail(self):
        conn = _FakeStructureConn()
        self.patch('with_imap', lambda acc, fn, **kw: fn(conn))
        server.prefetch_body(self.account(), 'INBOX', '5')
        self.assertFalse(server.prefetch_body(self.account(), 'INBOX', '5'),
                         '已经有正文了就别再看服务器')

    def test_keeps_existing_headers_when_storing_body(self):
        """只取正文也要带上原来的主题/发件人，别把本地邮件头冲掉。"""
        conn = _FakeStructureConn()
        self.patch('with_imap', lambda acc, fn, **kw: fn(conn))
        server.prefetch_body(self.account(), 'INBOX', '5')
        db = server.get_db()
        row = db.execute("SELECT subject, sender FROM mails WHERE acc_id=? AND uid='5'",
                         (self.acc_id,)).fetchone()
        db.close()
        self.assertEqual(row['subject'], 'Mail 5')
        self.assertEqual(row['sender'], 'S')

    def test_attachment_name_from_imap_literal(self):
        """文件名是 IMAP literal 给的（QQ 常见）也要能正确解析出来。"""
        conn = _LiteralStructureConn()
        data = server.fetch_body_parts(self.account(), 'INBOX', '5', conn)
        self.assertIsNotNone(data)
        self.assertEqual(data['attachments'][0]['name'], '巴斯夫声明.pdf')
        self.assertEqual(data['attachments'][0]['part'], '2')
        self.assertIn('Hello plain', data['body'])

    def test_thread_part_newest_returns_only_newest(self):
        """会话很长时，先只取最新一封（首屏秒出）。"""
        self.patch('background_sync', lambda *a, **k: None)
        self.patch('fetch_one_body', lambda acc, fol, uid, priority=None: {
            'uid': str(uid), 'folder': fol, 'subject': 'S', 'from': 'F', 'from_addr': 'f@x',
            'to': '', 'cc': '', 'date': 'Mon, 06 Oct 2026 10:00:00 +0800', 'body': 'b',
            'html': '', 'attachments': [], 'starred': False, 'unread': False,
            'message_id': '', 'in_reply_to': '', 'mine': False, 'ts': float(uid),
        })
        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        newest = client.get('/api/thread?acc=%d&folder=INBOX&uids=2,3,5&part=newest'
                            % self.acc_id).get_json()['messages']
        self.assertEqual([m['uid'] for m in newest], ['5'])
        older = client.get('/api/thread?acc=%d&folder=INBOX&uids=2,3,5&part=older'
                           % self.acc_id).get_json()['messages']
        self.assertEqual(sorted(m['uid'] for m in older), ['2', '3'])
        db = server.get_db()
        left = db.execute('SELECT COUNT(*) FROM mails WHERE acc_id=? AND unread=1'
                          ' AND folder=\'INBOX\'', (self.acc_id,)).fetchone()[0]
        db.close()
        self.assertEqual(left, 2, '点开任意一封都要把整个会话记成已读')

    def test_new_mail_prefetch_takes_newest_first(self):
        calls = []
        self.patch('prefetch_body', lambda acc, folder, uid, priority=None: calls.append(uid))
        server.queue_body_prefetch(self.account(), 'INBOX', [b'1', b'5', b'4'])
        deadline = time.time() + 3
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(calls, ['5', '4', '1'], '按最新优先预热')

    def test_stored_html_is_not_refetched_again(self):
        self.assertFalse(server.html_needs_refetch(server.sanitize_html('<p>hi</p>')))
        self.assertFalse(server.html_needs_refetch(
            server.sanitize_html('<p>x</p><img src="cid:logo@example.com">')))
        self.assertTrue(server.html_needs_refetch('<p>旧版本存的正文，没有版本标记</p>'))
        once = server.sanitize_html('<p>hi</p>')
        self.assertEqual(server.sanitize_html(once).count(server.HTML_RENDER_TAG), 1,
                         '重复清洗不能叠加多个标记')


if __name__ == '__main__':
    unittest.main()
