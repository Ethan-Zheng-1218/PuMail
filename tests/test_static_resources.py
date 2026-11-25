"""Verify source and bundled pages still serve their local dependencies after relocation."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit

from backend import server


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ('src', 'href') and value:
                self.urls.append(value)


class StaticResourceTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def assert_pages_load(self):
        for page in ('/', '/index.html', '/guide.html', '/help.html', '/privacy.html'):
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                links = _Links()
                links.feed(response.get_data(as_text=True))
                for target in links.urls:
                    url = urlsplit(urljoin('http://127.0.0.1' + page, target))
                    if url.netloc != '127.0.0.1':
                        continue
                    resource = self.client.get(url.path)
                    self.assertEqual(resource.status_code, 200, (page, target))
                    resource.close()
                response.close()

    def test_source_pages_and_local_links(self):
        self.assert_pages_load()

    def test_guide_markdown_and_all_illustrations(self):
        response = self.client.get('/guides/邮箱绑定指南.md')
        self.assertEqual(response.status_code, 200)
        markdown = response.get_data(as_text=True)
        response.close()
        images = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', markdown)
        self.assertTrue(images)
        for image in images:
            with self.subTest(image=image):
                response = self.client.get('/guides/' + image)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.mimetype.startswith('image/'))
                response.close()
        guide = self.client.get('/guide.html').get_data(as_text=True)
        self.assertIn('window.__GUIDE_MD__=', guide)

    def test_notification_audio(self):
        response = self.client.get('/assets/notice14.mp3')
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.get_data()), 0)
        response.close()

    def test_backend_and_repository_files_are_not_web_resources(self):
        for path in ('/backend/server.py', '/server.py', '/packaging/PuMail.spec', '/.git/config'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_frozen_resource_layout(self):
        with tempfile.TemporaryDirectory() as bundle:
            shutil.copytree(server.BASE_DIR, Path(bundle) / 'frontend')
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, '_MEIPASS', bundle, create=True):
                with patch.object(server, 'BASE_DIR', server._resource_dir()):
                    self.assert_pages_load()


if __name__ == '__main__':
    unittest.main()
