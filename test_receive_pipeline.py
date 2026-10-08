"""收信链路的回归测试：不联网，用假 IMAP 连接和假同步函数。

直接运行：.venv\\Scripts\\python.exe -m unittest test_receive_pipeline
"""

import sqlite3
import tempfile
import threading
import time
import unittest

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

    def select(self, mailbox, readonly=False):
        return ('OK', [str(self.untagged_responses['EXISTS'][0], 'ascii').encode()])


class ReceivePipelineTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
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

    def tearDown(self):
        self.data_dir.cleanup()

    def account(self):
        return server.get_account(self.acc_id)

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
        before = server.event_head()
        server.announce_new_mail(self.account(), 'INBOX', [b'9'])
        events = server.events_since(before)
        self.assertTrue(any(e['type'] == 'mail' and e['last_uid'] == 9 for e in events))

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


if __name__ == '__main__':
    unittest.main()
