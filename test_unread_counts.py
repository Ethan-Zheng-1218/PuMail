import tempfile
import time
import unittest

import server


class UnreadCountTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        server.apply_data_dir(self.data_dir.name)
        server._mem_cache.clear()
        server.init_db()
        db = server.get_db()
        google_id = db.execute("INSERT INTO accounts(email, name, provider, sort_order, active) VALUES(?,?,?,?,?)",
                   ('google@example.com', 'Google', 'gmail', 1, 1))
        qq_id = db.execute("INSERT INTO accounts(email, name, provider, sort_order, active) VALUES(?,?,?,?,?)",
                   ('qq@example.com', 'QQ', 'qq', 2, 1))
        self.google_id = google_id.lastrowid
        self.qq_id = qq_id.lastrowid
        now = time.time()
        for uid in range(1, 10):
            db.execute(
                "INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,ts,unread) VALUES(?,?,?,?,?,?,?,1)",
                (self.google_id, 'INBOX', str(uid), f'Google unread {uid}', 'Sender', 'sender@example.com', now + uid),
            )
        for uid, subject in enumerate(('QQ conversation', 'QQ conversation', 'QQ conversation', 'QQ conversation',
                                       'QQ unread 2', 'QQ unread 3', 'QQ unread 4'), start=1):
            db.execute(
                "INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,ts,unread) VALUES(?,?,?,?,?,?,?,1)",
                (self.qq_id, 'INBOX', str(uid), subject, 'Sender', 'sender@example.com', now + uid),
            )
        db.commit()
        db.close()

    def tearDown(self):
        self.data_dir.cleanup()

    def test_badges_count_unread_conversations_not_messages(self):
        self.assertEqual(server.gaia_unread_per_account(), {self.google_id: 9, self.qq_id: 4})
        self.assertEqual(server.gaia_folder_unread('INBOX'), 13)

    def test_first_inbox_page_is_time_ordered_not_unread_first(self):
        db = server.get_db()
        account_id = db.execute(
            "INSERT INTO accounts(email, name, provider, sort_order, active) VALUES(?,?,?,?,?)",
            ('page@example.com', 'Paging', 'custom', 3, 1),
        ).lastrowid
        for uid in range(1, 32):
            db.execute(
                "INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,ts,unread) VALUES(?,?,?,?,?,?,?,0)",
                (account_id, 'INBOX', str(uid), f'Read mail {uid}', 'Sender', 'sender@example.com', 1_000 + uid),
            )
        db.execute(
            "INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,ts,unread) VALUES(?,?,?,?,?,?,?,1)",
            (account_id, 'INBOX', 'old-unread', 'Unread mail', 'Sender', 'sender@example.com', 1),
        )
        db.commit()
        db.close()
        server.db_save_folders(account_id, [{'id': 'INBOX', 'label': '收件箱'}])

        client = server.app.test_client()
        client.set_cookie('pupu_ok', server.APP_SECRET)
        response = client.get(f'/api/mails?acc={account_id}&folder=INBOX&page=1')

        self.assertEqual(response.status_code, 200)
        items = response.get_json()['items']
        # 未读不再置顶：第一页就是最新的 30 封，老未读排在它自己的时间位置上。
        self.assertEqual(len(items), 30)
        self.assertEqual(items[0]['uid'], '31')
        self.assertFalse(items[0]['unread'])
        self.assertFalse(any(item['uid'] == 'old-unread' for item in items))
        self.assertEqual(server.folder_unread_count(account_id, 'INBOX'), 1)

    def test_account_badge_counts_every_unread_conversation(self):
        db = server.get_db()
        account_id = db.execute(
            "INSERT INTO accounts(email, name, provider, sort_order, active) VALUES(?,?,?,?,?)",
            ('badge@example.com', 'Badge', 'custom', 4, 1),
        ).lastrowid
        for uid in range(1, 32):
            db.execute(
                "INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,ts,unread) VALUES(?,?,?,?,?,?,?,1)",
                (account_id, 'INBOX', str(uid), f'Unread {uid}', 'Sender', 'sender@example.com', uid),
            )
        db.commit()
        db.close()
        server.db_save_folders(account_id, [{'id': 'INBOX', 'label': '收件箱'}])

        self.assertEqual(server.folder_unread_count(account_id, 'INBOX'), 31)
        self.assertEqual(server.gaia_unread_per_account()[account_id], 31)


if __name__ == '__main__':
    unittest.main()
