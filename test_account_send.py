"""账号绑定/发信回归；只使用临时数据库和假邮件连接，不发送邮件。"""
import imaplib
import base64
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import server


class AccountSendTests(unittest.TestCase):
    def test_missing_tls_file_is_not_network_failure_or_backoff(self):
        with patch.object(server, '_ProxyIMAP4SSL', side_effect=FileNotFoundError('missing TLS resource')):
            with self.assertRaisesRegex(RuntimeError, 'TLS.*资源缺失'):
                server.imap_open({'id': 88, 'imap_host': 'imap.qq.com'})
        self.assertNotIn(88, server._connect_fails)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_dir = server.DATA_DIR
        server.apply_data_dir(self.temp.name)
        server.init_db()
        server._connect_fails.clear()
        self.client = server.app.test_client()
        self.client.get('/')

    def tearDown(self):
        server.apply_data_dir(self.old_dir)
        self.temp.cleanup()

    def test_all_password_providers_can_bind_without_database_id(self):
        for provider in ('gmail', 'qq', '163', '126', 'icloud', 'custom'):
            with self.subTest(provider=provider):
                conn = MagicMock()
                with patch.object(server, '_ProxyIMAP4SSL', return_value=conn), \
                        patch.object(server, 'vault_upsert_account'), \
                        patch.object(server, 'sync_idle_threads'):
                    response = self.client.post('/api/accounts', json={
                        'email': provider + '@example.com', 'provider': provider,
                        'password': 'test-only', 'imap_host': 'imap.example.com',
                        'smtp_host': 'smtp.example.com',
                    })
                self.assertEqual(response.status_code, 200, response.get_json())
                conn.login.assert_called_once()
                conn.logout.assert_called_once()
        self.assertNotIn(None, server._connect_fails)

    def test_transient_connection_failure_is_not_shared_between_new_accounts(self):
        for _ in range(5):
            with patch.object(server, '_ProxyIMAP4SSL', side_effect=TimeoutError('timed out')):
                with self.assertRaises(TimeoutError):
                    server.imap_open({'imap_host': 'imap.gmail.com'})
        with patch.object(server, '_ProxyIMAP4SSL', return_value=MagicMock()) as factory:
            server.imap_open({'imap_host': 'imap.gmail.com'})
            factory.assert_called_once()
        self.assertEqual(server._connect_fails, {})

    def test_existing_account_still_uses_connection_backoff(self):
        for _ in range(server._CONNECT_BREAK_AFTER):
            server.note_connect(42, False)
        with patch.object(server, '_ProxyIMAP4SSL') as factory:
            with self.assertRaisesRegex(RuntimeError, '网络或代理'):
                server.imap_open({'id': 42, 'imap_host': 'imap.gmail.com'})
            factory.assert_not_called()

    def test_outlook_oauth_probe_without_id_uses_supplied_token(self):
        conn = MagicMock()
        with patch.object(server, '_ProxyIMAP4SSL', return_value=conn), \
                patch.object(server, 'outlook_refresh_access') as refresh:
            server.imap_open({'email': 'test@outlook.com', 'provider': 'outlook',
                              'imap_host': 'outlook.office365.com',
                              'oauth_refresh': 'fake-refresh', 'oauth_access': 'fake-access'})
        conn.authenticate.assert_called_once()
        refresh.assert_not_called()

    def test_auth_failure_closes_connection(self):
        conn = MagicMock()
        conn.login.side_effect = imaplib.IMAP4.error('AUTHENTICATIONFAILED')
        conn.authenticate.side_effect = imaplib.IMAP4.error('AUTHENTICATIONFAILED')
        with patch.object(server, '_ProxyIMAP4SSL', return_value=conn):
            with self.assertRaises(imaplib.IMAP4.error):
                server.imap_open({'imap_host': 'imap.gmail.com'})
        conn.shutdown.assert_called_once()

    def test_network_failure_is_not_reported_as_wrong_password(self):
        with patch.object(server, '_ProxyIMAP4SSL', side_effect=TimeoutError('timed out')):
            response = self.client.post('/api/accounts', json={
                'email': 'test@gmail.com', 'provider': 'gmail', 'password': 'test-only'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('网络', response.get_json()['error'])
        self.assertNotIn('检查地址和授权码', response.get_json()['error'])

    def _account(self):
        return {'id': 7, 'email': 'me@example.com', 'provider': 'custom',
                'smtp_host': 'smtp.example.com', 'smtp_port': 465, 'password': 'test-only'}

    def test_send_returns_before_sent_copy_finishes(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_save = server.save_sent_copy_background
        def tracked_save(*args):
            try:
                original_save(*args)
            finally:
                finished.set()
        def slow_imap(*args, **kwargs):
            started.set()
            release.wait(3)
            raise RuntimeError('fake IMAP unavailable')
        with patch.object(server, 'require_account', return_value=self._account()), \
                    patch.object(server, 'smtp_send') as smtp, \
                    patch.object(server, 'db_folder_label', return_value='Sent'), \
                    patch.object(server, 'imap_open', side_effect=slow_imap), \
                    patch.object(server, 'save_sent_copy_background', side_effect=tracked_save):
            try:
                import time
                before = time.monotonic()
                response = self.client.post('/api/send', json={
                    'acc': 7, 'to': 'you@example.com', 'body': 'hello',
                    'in_reply_to': '<original@example.com>'})
                elapsed = time.monotonic() - before
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertLess(elapsed, 1, 'SMTP 成功后不应等待 IMAP 保存')
                smtp.assert_called_once()
                self.assertEqual(smtp.call_args.args[2]['In-Reply-To'], '<original@example.com>')
                self.assertTrue(started.wait(1))
                rows = server.load_folder_mails(7, 'Sent')
                self.assertEqual(len(rows), 1, '成功返回前必须保存本地副本')
                self.assertEqual(rows[0]['body'], 'hello')
            finally:
                release.set()
                finished.wait(4)

    def test_local_sent_fallback_preserves_body_and_reply_headers(self):
        acc = self._account()
        data = {'to': 'you@example.com', 'subject': 'Re: Test', 'body': 'My reply',
                'in_reply_to': '<original@example.com>'}
        msg = server.build_msg(acc, ['you@example.com'], data['subject'], data['body'],
                               in_reply_to=data['in_reply_to'])
        with patch.object(server, 'db_folder_label', return_value='Sent'), \
                patch.object(server, 'imap_open', side_effect=TimeoutError('fake offline')), \
                patch.object(server, 'publish') as publish:
            server.save_sent_copy(acc, msg, data)
        rows = server.load_folder_mails(acc['id'], 'Sent')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['body'], 'My reply')
        self.assertEqual(rows[0]['message_id'], msg['Message-ID'])
        self.assertEqual(rows[0]['in_reply_to'], '<original@example.com>')
        publish.assert_called_once_with('mail', acc=acc['id'], folder='Sent')

    def test_local_sent_attachments_are_downloadable_without_imap(self):
        acc = self._account()
        data = {'to': 'you@example.com', 'body': 'Attachment', 'attachments': [{
            'name': 'note.txt', 'type': 'text/plain',
            'data': base64.b64encode(b'local attachment').decode()}]}
        msg = server.build_msg(acc, ['you@example.com'], 'Test', data['body'])
        with patch.object(server, 'db_folder_label', return_value='Sent'), \
                patch.object(server, 'imap_open', side_effect=TimeoutError('fake offline')):
            server.save_sent_copy(acc, msg, data)
        row = server.load_folder_mails(7, 'Sent')[0]
        att = server.row_to_message(row, acc)['attachments'][0]
        self.assertEqual(att['size'], 16)
        self.assertEqual(att['idx'], 0)
        self.assertNotIn('data', att)
        with patch.object(server, 'require_account', return_value=acc), \
                patch.object(server, 'with_imap') as imap:
            response = self.client.get('/api/attachment', query_string={
                'acc': 7, 'folder': 'Sent', 'uid': row['uid'], 'idx': 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'local attachment')
        imap.assert_not_called()

    def test_remote_sent_copy_replaces_local_snapshot_without_losing_reply(self):
        acc = self._account()
        data = {'to': 'you@example.com', 'body': 'My reply',
                'in_reply_to': '<original@example.com>', 'attachments': [{
                    'name': 'note.txt', 'data': base64.b64encode(b'still available').decode()}]}
        msg = server.build_msg(acc, ['you@example.com'], 'Re: Test', data['body'],
                               in_reply_to=data['in_reply_to'])
        conn = MagicMock()
        conn.uid.return_value = ('OK', [b'19'])
        with patch.object(server, 'db_folder_label', return_value='Sent'), \
                patch.object(server, 'imap_open', return_value=conn), \
                patch.object(server, 'append_to', return_value='Sent'), \
                patch.object(server, 'select_folder', return_value=True), \
                patch.object(server, 'fetch_header_chunk', return_value=[{
                    'uid': '19', 'subject': 'Re: Test', 'from_addr': acc['email']} ]):
            local_copy = server.save_local_sent(acc, msg, data)
            server.save_sent_copy(acc, msg, data, local_copy)
        rows = server.load_folder_mails(7, 'Sent')
        self.assertEqual([r['uid'] for r in rows], ['19'])
        self.assertEqual(rows[0]['body'], 'My reply')
        self.assertEqual(rows[0]['in_reply_to'], '<original@example.com>')
        conn.logout.assert_called_once()
        with patch.object(server, 'require_account', return_value=acc), \
                patch.object(server, 'with_imap') as imap:
            response = self.client.get('/api/attachment', query_string={
                'acc': 7, 'folder': 'Sent', 'uid': local_copy[1], 'idx': 0})
        self.assertEqual(response.status_code, 200, '已打开的本地副本链接在同步后仍应有效')
        self.assertEqual(response.data, b'still available')
        imap.assert_not_called()

    def test_smtp_failure_does_not_archive_or_claim_success(self):
        with patch.object(server, 'require_account', return_value=self._account()), \
                patch.object(server, 'smtp_send', side_effect=RuntimeError('SMTP rejected')), \
                patch.object(server, 'imap_open') as imap:
            response = self.client.post('/api/send', json={'acc': 7, 'to': 'you@example.com'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('SMTP rejected', response.get_json()['error'])
        imap.assert_not_called()

    def test_pending_sent_snapshot_cannot_be_moved_before_reconciliation(self):
        acc = self._account()
        data = {'to': 'you@example.com', 'body': 'Keep until copied'}
        msg = server.build_msg(acc, ['you@example.com'], 'Test', data['body'])
        with patch.object(server, 'db_folder_label', return_value='Sent'):
            folder, uid = server.save_local_sent(acc, msg, data)
        with patch.object(server, 'require_account', return_value=acc), \
                patch.object(server, 'background_sync'):
            response = self.client.post('/api/delete', json={'acc': 7, 'folder': folder, 'uid': uid})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(server.load_mail_row(7, folder, uid))


if __name__ == '__main__':
    unittest.main()
