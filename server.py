# -*- coding: utf-8 -*-
"""
PuMail 后端 API 服务器（多账号真实邮箱接入）
功能：多账号管理 / 文件夹列表 / 收信列表 / 读信 / 发信 / 草稿 / 星标 / 归档 / 删除 / 搜索
依赖：pip install flask
运行：python server.py  → 浏览器打开 http://127.0.0.1:5000
"""

import base64
import hashlib
import hmac
import html as html_mod
import imaplib
import sync_queue
import io
import json
import logging
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from email import message_from_bytes
from email.header import Header, decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from email.utils import (formataddr, formatdate, getaddresses, make_msgid,
                         parseaddr, parsedate_to_datetime)

STARRED_ID = '__STARRED__'
DEMO_EMAIL = 'shipupuo@pumail.com'
DEMO_NAME = 'PuMail'
DEMO_PROVIDER = 'demo'
DEMO_DISMISSED_KEY = 'demo_account_dismissed'
HEADER_CHUNK = 30
FIRST_BATCH = 30
PER_PAGE = 30
BOOT_MAIL_LIMIT = 20
RECENT_DAYS = 30
PAGE_SCAN = 50
PAIR_SCAN = 160
_IMAP_MON = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()

from flask import (Flask, abort, jsonify, make_response, request,
                   send_file, send_from_directory)

def _resource_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _install_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resource_dir()
INSTALL_DIR = _install_dir()
app = Flask(__name__, static_folder=None)
HOME_DIR = os.path.expanduser('~')
DEFAULT_DATA_DIR = os.path.join(HOME_DIR, '.PuMail')
LEGACY_DATA_DIR = os.path.join(HOME_DIR, '.pumail')
LOCATION_FILE = os.path.join(DEFAULT_DATA_DIR, 'location.json')
DATA_FILES = ('pumail.db', 'pumail.db-wal', 'pumail.db-shm', 'imap-debug.log', 'pumail.vault', 'pupumail.secret')
DATA_DIR = DEFAULT_DATA_DIR
DB_PATH = os.path.join(DATA_DIR, 'pumail.db')
OLD_DB_PATH = os.path.join(INSTALL_DIR, 'pupumail.db')
SECRET_PATH = os.path.join(DATA_DIR, 'pupumail.secret')
CACHE_TTL = 600
_mem_cache = {}
_storage_lock = threading.Lock()


def _norm_dir(path):
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path or '').strip())))


def _read_saved_data_dir():
    try:
        with open(LOCATION_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        path = raw.get('data_dir')
        if path and os.path.isdir(path):
            return _norm_dir(path)
    except Exception:
        pass
    return ''


def _write_saved_data_dir(path):
    os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)
    with open(LOCATION_FILE, 'w', encoding='utf-8') as f:
        json.dump({'data_dir': path}, f, ensure_ascii=False)


def apply_data_dir(path):
    global DATA_DIR, DB_PATH, SECRET_PATH
    DATA_DIR = _norm_dir(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(DATA_DIR, 'pumail.db')
    SECRET_PATH = os.path.join(DATA_DIR, 'pupumail.secret')


def resolve_data_dir():
    saved = _read_saved_data_dir()
    if saved:
        return saved
    if os.path.exists(os.path.join(DEFAULT_DATA_DIR, 'pumail.db')):
        return DEFAULT_DATA_DIR
    if os.path.exists(os.path.join(LEGACY_DATA_DIR, 'pumail.db')):
        return LEGACY_DATA_DIR
    return DEFAULT_DATA_DIR


def storage_info(**extra):
    info = {
        'install_dir': INSTALL_DIR,
        'data_dir': DATA_DIR,
        'default_dir': DEFAULT_DATA_DIR,
        'is_default': os.path.normcase(_norm_dir(DATA_DIR)) == os.path.normcase(_norm_dir(DEFAULT_DATA_DIR)),
    }
    info.update(extra)
    return info


def _checkpoint_db():
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _copy_data_files(src, dst):
    os.makedirs(dst, exist_ok=True)
    copied = []
    for name in DATA_FILES:
        s = os.path.join(src, name)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst, name))
            copied.append(name)
    return copied


def migrate_data_dir(new_dir):
    new_dir = _norm_dir(new_dir)
    if not new_dir:
        raise ValueError('请选择有效的文件夹')
    if os.path.isfile(new_dir):
        raise ValueError('存储位置必须是文件夹')
    old = DATA_DIR
    if os.path.normcase(new_dir) == os.path.normcase(old):
        _write_saved_data_dir(new_dir)
        return storage_info(migrated=False)
    dest_db = os.path.join(new_dir, 'pumail.db')
    if os.path.isfile(dest_db):
        raise ValueError('目标文件夹已有 PuMail 数据，请另选位置')
    os.makedirs(new_dir, exist_ok=True)
    _checkpoint_db()
    _copy_data_files(old, new_dir)
    _write_saved_data_dir(new_dir)
    apply_data_dir(new_dir)
    _mem_cache.clear()
    if os.path.normcase(old) != os.path.normcase(new_dir):
        for name in DATA_FILES:
            leftover = os.path.join(old, name)
            if os.path.isfile(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
    return storage_info(migrated=True)


def _pick_folder(initial=''):
    if os.name != 'nt':
        return ''
    start = initial or DATA_DIR
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '选择 PuMail 数据存储位置';"
        "$d.ShowNewFolderButton = $true;"
        "$d.SelectedPath = $env:PUMAIL_PICK_PATH;"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
    )
    env = os.environ.copy()
    env['PUMAIL_PICK_PATH'] = start
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-STA', '-Command', script],
            capture_output=True, text=True, timeout=300, env=env
        )
        return (proc.stdout or '').strip()
    except Exception:
        return ''


apply_data_dir(resolve_data_dir())


def load_secret():
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, 'r', encoding='utf-8') as f:
            val = f.read().strip()
            if val:
                return val
    val = secrets.token_hex(16)
    with open(SECRET_PATH, 'w', encoding='utf-8') as f:
        f.write(val)
    return val


APP_SECRET = load_secret()

PROVIDERS = {
    'qq':      {'label': 'QQ邮箱',  'imap': 'imap.qq.com',           'imap_port': 993, 'smtp': 'smtp.qq.com',        'smtp_port': 465, 'starttls': False},
    '163':     {'label': '163邮箱', 'imap': 'imap.163.com',          'imap_port': 993, 'smtp': 'smtp.163.com',       'smtp_port': 465, 'starttls': False},
    '126':     {'label': '126邮箱', 'imap': 'imap.126.com',          'imap_port': 993, 'smtp': 'smtp.126.com',       'smtp_port': 465, 'starttls': False},
    'gmail':   {'label': 'Gmail',   'imap': 'imap.gmail.com',        'imap_port': 993, 'smtp': 'smtp.gmail.com',     'smtp_port': 465, 'starttls': False},
    'outlook': {'label': 'Outlook', 'imap': 'outlook.office365.com', 'imap_port': 993, 'smtp': 'smtp.office365.com', 'smtp_port': 587, 'starttls': True},
    'icloud':  {'label': 'iCloud',  'imap': 'imap.mail.me.com',      'imap_port': 993, 'smtp': 'smtp.mail.me.com',   'smtp_port': 587, 'starttls': True},
    'custom':  {'label': '自定义',  'imap': '', 'imap_port': 993, 'smtp': '', 'smtp_port': 465, 'starttls': False},
}

# Outlook.com 个人邮箱已关闭授权码 / 密码 IMAP，必须用 OAuth2。
OUTLOOK_OAUTH_CLIENT = '9e5f94bc-e8a4-4e73-b8be-63364c29d753'
OUTLOOK_OAUTH_SCOPE = 'offline_access openid email https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send'
OUTLOOK_OAUTH_TENANT = 'consumers'
_outlook_token_cache = {}
_outlook_pending = {}
_outlook_lock = threading.Lock()


# ---------- 本机保护 / 密码 ----------
def _dpapi_protect(data: bytes) -> str:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError('CryptProtectData failed')
    try:
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return base64.b64encode(encrypted).decode('ascii')


def _dpapi_unprotect(token: str) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    raw = base64.b64decode(token.encode('ascii'))
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError('CryptUnprotectData failed')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def pw_enc(s):
    raw = (s or '').encode('utf-8')
    try:
        return 'dpapi:' + _dpapi_protect(raw)
    except Exception as exc:
        raise RuntimeError('密码加密失败：当前系统不支持 DPAPI 保护') from exc


def pw_dec(s):
    s = s or ''
    if s.startswith('dpapi:'):
        return _dpapi_unprotect(s[6:]).decode('utf-8')
    if s.startswith('b64:'):
        return base64.b64decode(s[4:].encode('ascii')).decode('utf-8')
    return base64.b64decode(s.encode('ascii')).decode('utf-8')


def _migrate_b64_passwords():
    """将旧的 b64: 密码迁移为 dpapi: 加密"""
    if os.name != 'nt':
        return
    try:
        db = get_db()
        rows = db.execute(
            "SELECT id, password FROM accounts WHERE password LIKE 'b64:%' OR (password IS NOT NULL AND password != '' AND password NOT LIKE 'dpapi:%')"
        ).fetchall()
        migrated = 0
        for r in rows:
            try:
                plain = pw_dec(r['password'])
                if plain:
                    encrypted = pw_enc(plain)
                    db.execute('UPDATE accounts SET password=? WHERE id=?', (encrypted, r['id']))
                    migrated += 1
            except Exception:
                pass
        if migrated:
            db.commit()
            logging.info('已迁移 %d 个旧密码加密方式 (b64 → dpapi)', migrated)
        db.close()
    except Exception:
        pass


# ---------- 主密码保险库 ----------
VAULT_KDF_ITERS = 150000
VAULT_AUTH_TYPES = {
    'qq': '授权码',
    '163': '授权码',
    '126': '授权码',
    'gmail': '应用专用密码',
    'icloud': '应用专用密码',
    'outlook': 'Microsoft 登录',
    'custom': '授权码 / 密码',
}
_vault_lock = threading.Lock()
_vault_pass = None


def vault_path():
    return os.path.join(DATA_DIR, 'pumail.vault')


def vault_configured():
    return os.path.isfile(vault_path())


def vault_unlocked():
    return bool(_vault_pass)


def vault_lock():
    global _vault_pass
    with _vault_lock:
        _vault_pass = None


def vault_auth_type(provider, oauth=False):
    if oauth or provider == 'outlook':
        return 'Microsoft 登录'
    return VAULT_AUTH_TYPES.get(provider or 'custom', '授权码 / 密码')


def _vault_derive(password, salt, iterations):
    raw = hashlib.pbkdf2_hmac(
        'sha256', (password or '').encode('utf-8'), salt, int(iterations), dklen=64
    )
    return raw[:32], raw[32:]


def _vault_xor(key, nonce, data):
    out = bytearray()
    counter = 0
    need = len(data)
    while len(out) < need:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, 'big'), hashlib.sha256).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[:need]))


def _vault_empty():
    return {'accounts': [], 'api_keys': []}


def vault_write(password, payload):
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = _vault_derive(password, salt, VAULT_KDF_ITERS)
    raw = json.dumps(payload or _vault_empty(), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ct = _vault_xor(enc_key, nonce, raw)
    mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    blob = {
        'version': 1,
        'kdf': 'pbkdf2_sha256',
        'iterations': VAULT_KDF_ITERS,
        'salt': base64.b64encode(salt).decode('ascii'),
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ciphertext': base64.b64encode(ct).decode('ascii'),
        'mac': base64.b64encode(mac).decode('ascii'),
    }
    path = vault_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(blob, f, ensure_ascii=False)
    os.replace(tmp, path)


def vault_read(password):
    path = vault_path()
    if not os.path.isfile(path):
        raise RuntimeError('尚未设置主密码')
    with open(path, 'r', encoding='utf-8') as f:
        blob = json.load(f)
    try:
        salt = base64.b64decode(blob['salt'])
        nonce = base64.b64decode(blob['nonce'])
        ct = base64.b64decode(blob['ciphertext'])
        mac = base64.b64decode(blob['mac'])
        iters = int(blob.get('iterations') or VAULT_KDF_ITERS)
    except Exception:
        raise RuntimeError('密码文件已损坏')
    enc_key, mac_key = _vault_derive(password, salt, iters)
    expect = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expect):
        raise RuntimeError('主密码不正确')
    try:
        data = json.loads(_vault_xor(enc_key, nonce, ct).decode('utf-8'))
    except Exception:
        raise RuntimeError('主密码不正确')
    if not isinstance(data, dict):
        return _vault_empty()
    data.setdefault('accounts', [])
    data.setdefault('api_keys', [])
    return data


def _vault_live_emails():
    db = get_db()
    rows = db.execute(
        "SELECT email FROM accounts WHERE COALESCE(active,1)=1"
    ).fetchall()
    db.close()
    return {(r['email'] or '').strip().lower() for r in rows if r['email']}


def vault_prune(payload):
    live = _vault_live_emails()
    accs = []
    seen = set()
    for item in payload.get('accounts') or []:
        email = (item.get('email') or '').strip()
        key = email.lower()
        if not email or key not in live or key in seen:
            continue
        seen.add(key)
        accs.append(item)
    payload['accounts'] = accs
    keys = []
    seen_k = set()
    for item in payload.get('api_keys') or []:
        kind = (item.get('kind') or 'translate').strip()
        engine = (item.get('engine') or '').strip()
        mark = kind + ':' + engine
        if mark in seen_k:
            continue
        seen_k.add(mark)
        keys.append(item)
    payload['api_keys'] = keys
    return payload


def vault_public(payload):
    accounts = []
    for item in payload.get('accounts') or []:
        accounts.append({
            'email': item.get('email') or '',
            'provider': item.get('provider') or 'custom',
            'auth_type': item.get('auth_type') or vault_auth_type(item.get('provider')),
            'secret': item.get('secret') or '',
        })
    api_keys = []
    for item in payload.get('api_keys') or []:
        api_keys.append({
            'name': item.get('name') or 'API 密钥',
            'kind': item.get('kind') or 'translate',
            'engine': item.get('engine') or '',
            'secret': item.get('secret') or '',
        })
    return {'accounts': accounts, 'api_keys': api_keys}


def _decode_secret(raw):
    raw = raw or ''
    if not raw:
        return ''
    try:
        return pw_dec(raw)
    except Exception:
        return ''


def vault_collect_current():
    payload = _vault_empty()
    db = get_db()
    rows = db.execute(
        'SELECT email,provider,password,oauth_refresh FROM accounts WHERE COALESCE(active,1)=1 ORDER BY sort_order, id'
    ).fetchall()
    db.close()
    for r in rows:
        email = (r['email'] or '').strip()
        if not email:
            continue
        provider = r['provider'] or 'custom'
        password = _decode_secret(r['password'])
        oauth = _decode_secret(r['oauth_refresh'] if 'oauth_refresh' in r.keys() else '')
        oauth_on = bool(oauth) or provider == 'outlook'
        payload['accounts'].append({
            'email': email,
            'provider': provider,
            'auth_type': vault_auth_type(provider, oauth_on),
            'secret': password if password else ('已通过 Microsoft 授权' if oauth_on else ''),
        })
    key = _decode_secret(setting_get('translate_key', ''))
    if key:
        engine = setting_get('translate_engine', 'deepseek') or 'deepseek'
        label = (TRANSLATE_ENGINES.get(engine) or {}).get('label') or engine
        payload['api_keys'].append({
            'name': label + ' 翻译',
            'kind': 'translate',
            'engine': engine,
            'secret': key,
        })
    return payload


def vault_setup(password):
    global _vault_pass
    if vault_configured():
        raise RuntimeError('主密码已设置')
    pw = (password or '').strip()
    if len(pw) < 6:
        raise RuntimeError('主密码至少 6 位')
    payload = vault_collect_current() if setting_get('vault_wiped', '') != '1' else _vault_empty()
    with _vault_lock:
        vault_write(pw, payload)
        _vault_pass = pw
    return vault_prune(payload)


def vault_unlock(password):
    global _vault_pass
    payload = vault_read((password or '').strip())
    payload = vault_prune(payload)
    with _vault_lock:
        _vault_pass = password
        try:
            vault_write(password, payload)
        except Exception:
            pass
    return payload


def vault_payload():
    if not _vault_pass:
        raise RuntimeError('请先输入主密码')
    return vault_prune(vault_read(_vault_pass))


def vault_save(payload):
    if not _vault_pass:
        return False
    with _vault_lock:
        if not _vault_pass:
            return False
        vault_write(_vault_pass, vault_prune(payload))
    return True


def vault_upsert_account(email, provider, secret='', oauth=False):
    email = (email or '').strip()
    if not email or not _vault_pass:
        return
    try:
        payload = vault_payload()
    except Exception:
        return
    key = email.lower()
    item = {
        'email': email,
        'provider': provider or 'custom',
        'auth_type': vault_auth_type(provider, oauth),
        'secret': secret if secret else ('已通过 Microsoft 授权' if oauth else ''),
    }
    accs = [a for a in payload.get('accounts') or [] if (a.get('email') or '').strip().lower() != key]
    accs.append(item)
    payload['accounts'] = accs
    vault_save(payload)


def vault_drop_account(email):
    email = (email or '').strip().lower()
    if not email or not _vault_pass:
        return
    try:
        payload = vault_payload()
    except Exception:
        return
    payload['accounts'] = [
        a for a in payload.get('accounts') or [] if (a.get('email') or '').strip().lower() != email
    ]
    vault_save(payload)


def vault_upsert_api_key(engine, secret, name=''):
    if not _vault_pass:
        return
    secret = (secret or '').strip()
    try:
        payload = vault_payload()
    except Exception:
        return
    keys = [k for k in payload.get('api_keys') or [] if (k.get('kind') or 'translate') != 'translate']
    if secret:
        label = name or ((TRANSLATE_ENGINES.get(engine) or {}).get('label') or engine or '翻译')
        keys.append({
            'name': label + (' 翻译' if '翻译' not in label else ''),
            'kind': 'translate',
            'engine': engine or 'deepseek',
            'secret': secret,
        })
    payload['api_keys'] = keys
    vault_save(payload)


def vault_reset():
    global _vault_pass
    path = vault_path()
    with _vault_lock:
        _vault_pass = None
        for name in (path, path + '.tmp'):
            if os.path.isfile(name):
                try:
                    os.remove(name)
                except OSError:
                    pass
    setting_set('vault_wiped', '1')


# ---------- 数据库 ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    db = get_db()
    db.execute("CREATE TABLE IF NOT EXISTS accounts("
               "id INTEGER PRIMARY KEY AUTOINCREMENT,"
               "email TEXT NOT NULL, name TEXT DEFAULT '',"
               "provider TEXT DEFAULT 'custom',"
               "imap_host TEXT, imap_port INTEGER,"
               "smtp_host TEXT, smtp_port INTEGER,"
               "starttls INTEGER DEFAULT 0,"
               "password TEXT,"
               "sort_order INTEGER DEFAULT 0)")
    db.execute("""CREATE TABLE IF NOT EXISTS mails(
        acc_id INTEGER NOT NULL,
        folder TEXT NOT NULL,
        uid TEXT NOT NULL,
        subject TEXT DEFAULT '',
        sender TEXT DEFAULT '',
        from_addr TEXT DEFAULT '',
        recipients TEXT DEFAULT '',
        cc TEXT DEFAULT '',
        date_raw TEXT DEFAULT '',
        ts REAL DEFAULT 0,
        unread INTEGER DEFAULT 1,
        starred INTEGER DEFAULT 0,
        mine INTEGER DEFAULT 0,
        message_id TEXT DEFAULT '',
        thread_key TEXT DEFAULT '',
        body TEXT DEFAULT '',
        html TEXT DEFAULT '',
        attachments TEXT DEFAULT '[]',
        has_body INTEGER DEFAULT 0,
        local_deleted INTEGER DEFAULT 0,
        pending_sync TEXT DEFAULT '',
        PRIMARY KEY (acc_id, folder, uid))""")
    try:
        db.execute('ALTER TABLE mails ADD COLUMN pending_sync TEXT DEFAULT ""')
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE accounts ADD COLUMN sort_order INTEGER DEFAULT 0')
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE accounts ADD COLUMN active INTEGER DEFAULT 1')
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE accounts ADD COLUMN oauth_refresh TEXT DEFAULT ""')
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE mails ADD COLUMN in_reply_to TEXT DEFAULT ""')
    except Exception:
        pass
    db.execute('UPDATE accounts SET sort_order=id WHERE sort_order IS NULL OR sort_order=0')
    db.execute('UPDATE accounts SET active=1 WHERE active IS NULL')
    db.execute("CREATE INDEX IF NOT EXISTS idx_mails_ts ON mails(acc_id, folder, ts DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mails_thread ON mails(acc_id, thread_key)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mails_star ON mails(acc_id, starred)")
    db.execute("""CREATE TABLE IF NOT EXISTS folder_cache(
        acc_id INTEGER NOT NULL,
        folder_id TEXT NOT NULL,
        label TEXT DEFAULT '',
        PRIMARY KEY (acc_id, folder_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS folder_state(
        acc_id INTEGER NOT NULL,
        folder TEXT NOT NULL,
        last_uid INTEGER DEFAULT 0,
        last_sync REAL DEFAULT 0,
        oldest_uid INTEGER DEFAULT 0,
        caught_up INTEGER DEFAULT 0,
        PRIMARY KEY (acc_id, folder))""")
    for col, spec in (('oldest_uid', 'INTEGER DEFAULT 0'), ('caught_up', 'INTEGER DEFAULT 0')):
        try:
            db.execute(f'ALTER TABLE folder_state ADD COLUMN {col} {spec}')
        except Exception:
            pass
    db.execute("""CREATE TABLE IF NOT EXISTS tombstones(
        acc_id INTEGER NOT NULL,
        folder TEXT NOT NULL,
        uid TEXT NOT NULL,
        created REAL DEFAULT 0,
        PRIMARY KEY (acc_id, folder, uid))""")
    db.execute("""CREATE TABLE IF NOT EXISTS app_settings(
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS sync_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        acc_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        folder TEXT DEFAULT '',
        uid TEXT DEFAULT '',
        extra TEXT DEFAULT '',
        tries INTEGER DEFAULT 0,
        error TEXT DEFAULT '',
        created REAL DEFAULT 0)""")
    try:
        db.execute('UPDATE folder_state SET caught_up=0')
    except Exception:
        pass
    db.commit()
    db.close()
    _migrate_old_accounts()
    _migrate_b64_passwords()
    try:
        ensure_demo_account()
    except Exception:
        pass


def _migrate_old_accounts():
    if getattr(sys, 'frozen', False):
        return
    if not os.path.exists(OLD_DB_PATH):
        return
    db = get_db()
    n = db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
    mails = db.execute('SELECT COUNT(*) FROM mails').fetchone()[0]
    if n and mails:
        db.close()
        return
    try:
        old = sqlite3.connect(OLD_DB_PATH)
        old.row_factory = sqlite3.Row
        rows = old.execute(
            'SELECT id,email,name,provider,imap_host,imap_port,smtp_host,smtp_port,starttls,password FROM accounts'
        ).fetchall()
        old.close()
        if n and not mails:
            db.execute('DELETE FROM accounts')
        for r in rows:
            db.execute(
                "INSERT OR REPLACE INTO accounts(id,email,name,provider,imap_host,imap_port,smtp_host,smtp_port,starttls,password) VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(r))
        db.commit()
    except Exception:
        pass
    db.close()


def get_account(acc_id):
    if not acc_id:
        return None
    db = get_db()
    row = db.execute('SELECT * FROM accounts WHERE id=?', (acc_id,)).fetchone()
    db.close()
    if not row:
        return None
    acc = dict(row)
    if int(acc.get('active') if acc.get('active') is not None else 1) == 0:
        return None
    try:
        acc['password'] = pw_dec(acc['password']) if acc.get('password') else ''
    except Exception:
        acc['password'] = ''
    raw_oauth = acc.get('oauth_refresh') or ''
    if raw_oauth:
        try:
            acc['oauth_refresh'] = pw_dec(raw_oauth)
        except Exception:
            acc['oauth_refresh'] = raw_oauth
    return acc


def require_account():
    acc_id = request.args.get('acc', type=int)
    if acc_id is None and request.is_json:
        acc_id = (request.get_json(silent=True) or {}).get('acc')
    acc = get_account(acc_id)
    if not acc:
        abort(make_response(jsonify({'error': '账号不存在'}), 404))
    return acc


def is_demo_account(acc):
    if not acc:
        return False
    email = (acc.get('email') or '').strip().lower()
    return acc.get('provider') == DEMO_PROVIDER or email == DEMO_EMAIL


def public_account_row(row):
    d = dict(row)
    d['demo'] = is_demo_account(d)
    return d


DEMO_FOLDERS = [
    {'id': 'INBOX', 'label': '收件箱'},
    {'id': 'Sent', 'label': '已发送'},
    {'id': STARRED_ID, 'label': '星标'},
    {'id': 'Drafts', 'label': '草稿'},
    {'id': 'Spam', 'label': '垃圾邮件'},
    {'id': 'Trash', 'label': '已删除'},
]


def _demo_fmt(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def _last_weekday(now, hour, minute):
    d = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if d.weekday() >= 5:
        d -= timedelta(days=d.weekday() - 4)
        return d.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if d >= now:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _recent_midnight(now):
    d = now.replace(hour=0, minute=1, second=0, microsecond=0)
    if d > now:
        d -= timedelta(days=1)
    return d


def _html_letter(inner, width='560px'):
    return (
        '<div style="font-family:\'Noto Sans SC\',\'Microsoft YaHei\',sans-serif;'
        'font-size:15px;line-height:1.75;color:#1f2933;max-width:' + width + ';">'
        + inner + '</div>'
    )


def _demo_mail_specs(now):
    musk_at = _last_weekday(now, 15, 42)
    vc_at = _last_weekday(now, 10, 8)
    offer_at = _recent_midnight(now)
    stamp = int(now.timestamp())
    cat_at = (now - timedelta(days=2 + stamp % 3)).replace(
        hour=8 + stamp % 14, minute=(stamp // 11) % 60, second=0, microsecond=0)
    visa_at = musk_at.replace(hour=21, minute=6)
    if visa_at > now:
        visa_at = now - timedelta(hours=6)
    lottery_at = now - timedelta(days=6, hours=3)
    spam_at = now - timedelta(days=3, hours=2)
    welcome_at = now
    self_to = DEMO_NAME + ' <' + DEMO_EMAIL + '>'

    musk_body = (
        '你好，\n\n'
        '我是 Elon。SpaceX 正在组建第一批火星移民计划的地面预备队。\n'
        '我们看过你的材料，觉得你适合加入。\n\n'
        '出发窗口大约在下个十年。加入后你会参与栖息舱、生命维持和地面模拟。\n'
        '没有现成的岗位说明书。火星上也没有。\n\n'
        '如果你愿意，回复这封邮件。我们下周通一次话。\n\n'
        'Elon Musk\nSpaceX'
    )
    offer_body = (
        'Dear Applicant,\n\n'
        'We are pleased to inform you that you have been offered admission to '
        'The University of Hong Kong for the coming academic year.\n\n'
        'Programme: Master of Science\n'
        'Faculty: Faculty of Engineering\n'
        'Start date: September 2026\n\n'
        'This offer is conditional upon fulfilment of any outstanding requirements '
        'stated in your applicant portal. Please confirm your acceptance by 17 October 2026.\n\n'
        'We look forward to welcoming you to HKU.\n\n'
        'Yours sincerely,\n'
        'Admissions Office\n'
        'The University of Hong Kong\n'
        'Pokfulam, Hong Kong'
    )
    vc_body = (
        '嗨，\n\n'
        '我们内部过了一下你的项目，方向对，人也对。\n'
        '准备先投 500 万，占一点早期份额。\n\n'
        '下周二或周三上午有空的话，随便找家咖啡馆坐十分钟。\n'
        '不用准备 PPT，把你现在最卡的那件事说清楚就行。\n\n'
        '林启明\n星火资本'
    )
    cat_body = (
        'asdfjkl;  想你啊喵www   qwertyuiop\n'
        'miao miao  喵喵喵 asdghjk  LOVE\n'
        'hhhhh   你怎么还不回我  fghjkl\n'
        'bbbbbbbbb  噗   nnnnn  I miss u\n'
        'xcvbnm  饭！！   ttttt  喵？'
    )
    welcome_body = (
        '你好，欢迎来到 PuMail。\n\n'
        '这是一个本地演示账号，用来让你先熟悉收信、写信和侧边栏。\n'
        '添加自己的邮箱后，这个演示账号会自动退出。\n\n'
        'Less is more.\n'
        'PuMail 团队'
    )
    visa_body = (
        '写给自己：\n\n'
        '9 月 28 日上午 9:00，美国驻华使馆面签。\n'
        '提前一晚把护照、DS-160 确认页、面签信、照片和材料袋放好。\n'
        '8 点前到场，手机关静音。\n\n'
        '别睡过头。'
    )
    lottery_body = (
        '【国家幸运彩票】尊敬的用户：\n\n'
        '您的邮箱在本次特别抽奖中被抽中，奖金 1000 万元已到账。\n'
        '请于 24 小时内点击链接提交银行卡信息，逾期作废。\n\n'
        '（这封信被你扔进了已删除。很有眼光。）'
    )
    spam_body = (
        '亲爱的旅客：\n\n'
        '恭喜您被阳光假期选为本月幸运用户，免费马尔代夫双人往返机票等您来领。\n'
        '只需预付 99 元服务费，名额有限，今天截止。\n\n'
        '阳光假期旅游'
    )
    return [
        {
            'uid': 'demo-welcome',
            'folder': 'INBOX',
            'subject': '欢迎来到 PuMail',
            'sender': 'PuMail 官方',
            'from_addr': 'hello@pumail.com',
            'to': self_to,
            'ts': welcome_at.timestamp(),
            'unread': True,
            'starred': False,
            'body': welcome_body,
            'html': _html_letter(
                '<p>你好，欢迎来到 <b>PuMail</b>。</p>'
                '<p>这是一个本地演示账号，用来让你先熟悉收信、写信和侧边栏。<br>'
                '添加自己的邮箱后，这个演示账号会自动退出。</p>'
                '<p style="color:#6b7280;margin-top:24px;">Less is more.<br>PuMail 团队</p>'
            ),
        },
        {
            'uid': 'demo-musk',
            'folder': 'INBOX',
            'subject': '入职邀请 · 第一批火星移民计划',
            'sender': '马斯克',
            'from_addr': 'elon@spacex.com',
            'to': self_to,
            'ts': musk_at.timestamp(),
            'unread': True,
            'starred': False,
            'body': musk_body,
            'html': _html_letter(
                '<p>你好，</p>'
                '<p>我是 Elon。SpaceX 正在组建<strong>第一批火星移民计划</strong>的地面预备队。'
                '我们看过你的材料，觉得你适合加入。</p>'
                '<p>出发窗口大约在下个十年。加入后你会参与栖息舱、生命维持和地面模拟。<br>'
                '没有现成的岗位说明书。火星上也没有。</p>'
                '<p>如果你愿意，回复这封邮件。我们下周通一次话。</p>'
                '<p style="color:#6b7280;margin-top:24px;">Elon Musk<br>SpaceX</p>'
            ),
        },
        {
            'uid': 'demo-hku',
            'folder': 'INBOX',
            'subject': 'Offer of Admission — The University of Hong Kong',
            'sender': 'HongKong University',
            'from_addr': 'admissions@hku.hk',
            'to': self_to,
            'ts': offer_at.timestamp(),
            'unread': False,
            'starred': False,
            'body': offer_body,
            'html': _html_letter(
                '<p>Dear Applicant,</p>'
                '<p>We are pleased to inform you that you have been offered admission to '
                '<b>The University of Hong Kong</b> for the coming academic year.</p>'
                '<p>Programme: Master of Science<br>'
                'Faculty: Faculty of Engineering<br>'
                'Start date: September 2026</p>'
                '<p>This offer is conditional upon fulfilment of any outstanding requirements '
                'stated in your applicant portal. Please confirm your acceptance by '
                '<b>17 October 2026</b>.</p>'
                '<p>We look forward to welcoming you to HKU.</p>'
                '<p style="color:#6b7280;margin-top:24px;">Yours sincerely,<br>'
                'Admissions Office<br>The University of Hong Kong<br>Pokfulam, Hong Kong</p>'
            ),
        },
        {
            'uid': 'demo-vc',
            'folder': 'INBOX',
            'subject': '投 500 万，下周聊一下？',
            'sender': '林启明 · 星火资本',
            'from_addr': 'qiming@sparkvc.com',
            'to': self_to,
            'ts': vc_at.timestamp(),
            'unread': True,
            'starred': False,
            'body': vc_body,
            'html': _html_letter(
                '<p>嗨，</p>'
                '<p>我们内部过了一下你的项目，方向对，人也对。<br>'
                '准备先投 <b>500 万</b>，占一点早期份额。</p>'
                '<p>下周二或周三上午有空的话，随便找家咖啡馆坐十分钟。<br>'
                '不用准备 PPT，把你现在最卡的那件事说清楚就行。</p>'
                '<p style="color:#6b7280;margin-top:24px;">林启明<br>星火资本</p>'
            ),
        },
        {
            'uid': 'demo-cat',
            'folder': 'INBOX',
            'subject': '小猫想你',
            'sender': '噗噗',
            'from_addr': 'pupu@catmail.local',
            'to': self_to,
            'ts': cat_at.timestamp(),
            'unread': False,
            'starred': True,
            'body': cat_body,
            'html': '',
        },
        {
            'uid': 'demo-visa',
            'folder': 'Sent',
            'subject': '提醒：9.28 上午 9 点美国面签',
            'sender': DEMO_NAME,
            'from_addr': DEMO_EMAIL,
            'to': self_to,
            'ts': visa_at.timestamp(),
            'unread': False,
            'starred': False,
            'mine': True,
            'body': visa_body,
            'html': _html_letter(
                '<p>写给自己：</p>'
                '<p><b>9 月 28 日上午 9:00</b>，美国驻华使馆面签。<br>'
                '提前一晚把护照、DS-160 确认页、面签信、照片和材料袋放好。<br>'
                '8 点前到场，手机关静音。</p>'
                '<p>别睡过头。</p>'
            ),
        },
        {
            'uid': 'demo-lottery',
            'folder': 'Trash',
            'subject': '恭喜！您中了 1000 万',
            'sender': '国家幸运彩票',
            'from_addr': 'prize@lucky-lotto.biz',
            'to': self_to,
            'ts': lottery_at.timestamp(),
            'unread': False,
            'starred': False,
            'local_deleted': True,
            'body': lottery_body,
            'html': _html_letter(
                '<p>【国家幸运彩票】尊敬的用户：</p>'
                '<p>您的邮箱在本次特别抽奖中被抽中，奖金 <b>1000 万元</b>已到账。<br>'
                '请于 24 小时内点击链接提交银行卡信息，逾期作废。</p>'
                '<p style="color:#9ca3af;">（这封信被你扔进了已删除。很有眼光。）</p>'
            ),
        },
        {
            'uid': 'demo-spam',
            'folder': 'Spam',
            'subject': '恭喜您被抽中去马尔代夫',
            'sender': '阳光假期旅游',
            'from_addr': 'win@sunnyholiday.promo',
            'to': self_to,
            'ts': spam_at.timestamp(),
            'unread': True,
            'starred': False,
            'body': spam_body,
            'html': _html_letter(
                '<p>亲爱的旅客：</p>'
                '<p>恭喜您被阳光假期选为本月幸运用户，<b>免费马尔代夫双人往返机票</b>等您来领。<br>'
                '只需预付 99 元服务费，名额有限，今天截止。</p>'
                '<p style="color:#6b7280;margin-top:24px;">阳光假期旅游</p>'
            ),
        },
    ]


def _insert_demo_mail(acc, spec):
    ts = float(spec.get('ts') or time.time())
    date = _demo_fmt(datetime.fromtimestamp(ts))
    folder = spec['folder']
    upsert_headers(acc, folder, [{
        'uid': spec['uid'],
        'subject': spec.get('subject') or '(无主题)',
        'from': spec.get('sender') or '',
        'from_addr': spec.get('from_addr') or '',
        'date': date,
        'to': spec.get('to') or '',
        'unread': bool(spec.get('unread')),
        'starred': bool(spec.get('starred')),
        'ts': ts,
    }])
    save_body(acc['id'], folder, spec['uid'], {
        'body': spec.get('body') or '',
        'html': spec.get('html') or '',
        'attachments': [],
        'to': spec.get('to') or '',
        'cc': '',
        'message_id': '<' + spec['uid'] + '@pumail.com>',
        'in_reply_to': '',
    })
    fields = {}
    if spec.get('mine'):
        fields['mine'] = 1
    if spec.get('local_deleted'):
        fields['local_deleted'] = 1
        fields['unread'] = 0
    if fields:
        mark_local(acc['id'], folder, spec['uid'], **fields)


def _wipe_demo_accounts():
    db = get_db()
    rows = db.execute(
        'SELECT id FROM accounts WHERE provider=? OR lower(email)=?',
        (DEMO_PROVIDER, DEMO_EMAIL)
    ).fetchall()
    ids = [r['id'] for r in rows]
    for aid in ids:
        wipe_account_local(db, aid)
    db.commit()
    db.close()
    for aid in ids:
        try:
            cache_bust(aid)
        except Exception:
            pass
    return ids


def dismiss_demo_accounts():
    setting_set(DEMO_DISMISSED_KEY, '1')
    return _wipe_demo_accounts()


def _seed_demo_payload(acc):
    db_save_folders(acc['id'], list(DEMO_FOLDERS))
    db = get_db()
    n = db.execute('SELECT COUNT(*) FROM mails WHERE acc_id=?', (acc['id'],)).fetchone()[0]
    db.close()
    if n:
        for fol in ('INBOX', 'Sent', 'Drafts', 'Spam', 'Trash'):
            set_caught_up(acc['id'], fol, True)
        return
    now = datetime.now()
    for spec in _demo_mail_specs(now):
        _insert_demo_mail(acc, spec)
    for fol in ('INBOX', 'Sent', 'Drafts', 'Spam', 'Trash'):
        set_caught_up(acc['id'], fol, True)


def ensure_demo_account():
    if setting_get(DEMO_DISMISSED_KEY) == '1':
        _wipe_demo_accounts()
        return None
    db = get_db()
    real = db.execute(
        "SELECT COUNT(*) FROM accounts WHERE COALESCE(active,1)=1 AND provider!=? AND lower(email)!=?",
        (DEMO_PROVIDER, DEMO_EMAIL)
    ).fetchone()[0]
    leftover = db.execute(
        "SELECT COUNT(*) FROM accounts WHERE COALESCE(active,1)=1 AND (provider=? OR lower(email)=?)",
        (DEMO_PROVIDER, DEMO_EMAIL)
    ).fetchone()[0]
    if real:
        db.close()
        if leftover:
            dismiss_demo_accounts()
        return None
    row = db.execute(
        "SELECT id FROM accounts WHERE COALESCE(active,1)=1 AND (provider=? OR lower(email)=?) ORDER BY id LIMIT 1",
        (DEMO_PROVIDER, DEMO_EMAIL)
    ).fetchone()
    if row:
        aid = row['id']
        db.close()
        acc = get_account(aid)
        if acc:
            _seed_demo_payload(acc)
        return aid
    nxt = db.execute('SELECT COALESCE(MAX(sort_order),0)+1 FROM accounts').fetchone()[0]
    cur = db.execute(
        "INSERT INTO accounts(email,name,provider,imap_host,imap_port,smtp_host,smtp_port,starttls,password,oauth_refresh,sort_order,active)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
        (DEMO_EMAIL, DEMO_NAME, DEMO_PROVIDER, '', 0, '', 0, 0, '', '', nxt)
    )
    aid = cur.lastrowid
    db.commit()
    db.close()
    acc = get_account(aid)
    if acc:
        _seed_demo_payload(acc)
    return aid


def list_public_accounts():
    ensure_demo_account()
    db = get_db()
    rows = db.execute(
        'SELECT id,email,name,provider,sort_order FROM accounts WHERE COALESCE(active,1)=1 ORDER BY sort_order, id'
    ).fetchall()
    db.close()
    return [public_account_row(r) for r in rows]


def save_demo_sent(acc, d):
    sent_id = db_folder_label(acc['id'], '已发送') or 'Sent'
    uid = 'demo-sent-' + str(int(time.time() * 1000))
    now = time.time()
    upsert_headers(acc, sent_id, [{
        'uid': uid,
        'subject': d.get('subject') or '(无主题)',
        'from': acc.get('name') or acc.get('email') or DEMO_NAME,
        'from_addr': acc.get('email') or DEMO_EMAIL,
        'date': _demo_fmt(datetime.now()),
        'to': d.get('to') or '',
        'unread': False,
        'starred': False,
        'ts': now,
    }])
    save_body(acc['id'], sent_id, uid, {
        'body': d.get('body') or '',
        'html': d.get('html') or '',
        'attachments': [],
        'to': d.get('to') or '',
        'cc': d.get('cc') or '',
        'message_id': make_msgid(domain='pumail.com'),
        'in_reply_to': d.get('in_reply_to') or '',
    })
    mark_local(acc['id'], sent_id, uid, mine=1, unread=0)
    cache_bust(acc['id'])
    return uid


# ---------- 邮件解析工具 ----------
def dec(s):
    if not s:
        return ''
    try:
        parts = decode_header(s)
    except Exception:
        if isinstance(s, bytes):
            return s.decode('utf-8', errors='replace')
        return str(s)
    out = []
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or 'utf-8', errors='replace'))
            except (LookupError, UnicodeError):
                out.append(txt.decode('utf-8', errors='replace'))
        else:
            out.append(txt or '')
    return ''.join(out)


def decode_mutf7(name):
    """IMAP 文件夹名是 modified UTF-7，转回中文"""
    def repl(m):
        b = m.group(1).replace(',', '/')
        pad = (-len(b)) % 4
        try:
            return base64.b64decode(b + '=' * pad).decode('utf-16-be')
        except Exception:
            return m.group(0)
    return re.sub(r'&([^&-]*)-', repl, name)


def encode_mutf7(name):
    name = name or ''
    if name.isascii():
        return name
    out, buf = [], []

    def flush():
        if not buf:
            return
        raw = ''.join(buf).encode('utf-16-be')
        b64 = base64.b64encode(raw).decode('ascii').rstrip('=')
        out.append('&' + b64.replace('/', ',') + '-')
        buf.clear()

    for ch in name:
        o = ord(ch)
        if ch == '&':
            flush()
            out.append('&-')
        elif 0x20 <= o <= 0x7e:
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return ''.join(out)


def imap_log(acc, *parts):
    try:
        aid = acc.get('id') if isinstance(acc, dict) else acc
        host = (acc.get('imap_host') if isinstance(acc, dict) else '') or ''
        line = ' '.join(str(p) for p in parts)
        msg = f"{time.strftime('%H:%M:%S')} acc={aid} {host} {line}\n"
        with open(os.path.join(DATA_DIR, 'imap-debug.log'), 'a', encoding='utf-8') as f:
            f.write(msg)
    except Exception:
        pass


def strip_html(h):
    h = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', h)
    h = re.sub(r'(?i)<br\s*/?>', '\n', h)
    h = re.sub(r'(?i)</(p|div|tr|li|h\d|blockquote)>', '\n', h)
    h = re.sub(r'(?s)<.*?>', '', h)
    h = html_mod.unescape(h)
    return re.sub(r'\n{3,}', '\n\n', h).strip()


def _strip_css_prop(h, prop):
    return re.sub(rf'(?i){prop}\s*:\s*[^;]+;?\s*', '', h or '')


def _clean_style_attr(raw):
    style = html_mod.unescape(raw or '')
    kept = []
    for part in style.split(';'):
        part = part.strip()
        if ':' not in part:
            continue
        prop, _, val = part.partition(':')
        prop, val = prop.strip(), val.strip()
        if not prop or not val:
            continue
        if re.search(r'(?i)expression|javascript:|behavior|-moz-binding', val):
            continue
        kept.append(f'{prop}: {val}')
    return '; '.join(kept)


def _sanitize_style_block(css):
    css = re.sub(r'(?is)@import\b[^;]*;?', '', css or '')
    css = re.sub(r'(?i)expression\s*\([^)]*\)', 'none', css)
    css = re.sub(r'(?i)javascript:', '', css)
    css = re.sub(r'(?i)behavior\s*:[^;]+;?', '', css)
    css = re.sub(r'(?i)-moz-binding\s*:[^;]+;?', '', css)
    css = re.sub(r'(?i)position\s*:\s*(fixed|sticky)', 'position: relative', css)
    return css


def inline_cid_images(msg, html):
    if not html or 'cid:' not in html.lower():
        return html
    cids = {}
    for part in msg.walk():
        cid = (part.get('Content-ID') or '').strip().strip('<>')
        payload = part.get_payload(decode=True)
        if not cid or not payload:
            continue
        ct = part.get_content_type() or 'application/octet-stream'
        url = 'data:' + ct + ';base64,' + base64.b64encode(payload).decode('ascii')
        cids[cid.lower()] = url
        fn = dec(part.get_filename() or '')
        if fn:
            cids[fn.lower()] = url

    def sub_attr(m):
        cid = html_mod.unescape(m.group(3)).strip().strip('<>').lower()
        url = cids.get(cid)
        if not url:
            return m.group(0)
        return m.group(1) + '=' + m.group(2) + url + m.group(2)

    html = re.sub(r'(?i)(src|href|background)\s*=\s*(["\'])\s*cid:([^"\']+)\2', sub_attr, html)

    def sub_url(m):
        cid = m.group(1).strip().strip('<>').lower()
        url = cids.get(cid)
        return ('url(' + url + ')') if url else m.group(0)

    return re.sub(r'(?i)url\(\s*[\'"]?cid:([^\'")]+)[\'"]?\s*\)', sub_url, html)


def html_needs_refetch(html):
    if not html:
        return False
    if re.search(r'(?i)\bcid:', html):
        return True
    if re.search(r'(?i)<img\b(?![^>]*\bsrc\s*=\s*["\'][^"\']+["\'])', html):
        return True
    if re.search(r'(?is)<style[\s>]', html):
        return False
    if re.search(r'(?i)class=["\'][^"\']*(?:button|btn|cta|pad)', html):
        return True
    if re.search(r'(?i)color\s*:\s*#fff(?:fff)?', html) and re.search(r'(?i)\sclass=', html):
        return True
    return False


def repair_broken_style_quotes(h):
    """旧版清洗把 font-family: \"Helvetica Neue\" 截断后，style 属性会提前结束。"""
    def repl(m):
        inside = re.sub(r'(?:^|;)\s*[^;:]*$', '', m.group(1)).rstrip().rstrip(';')
        after = re.sub(r'^[^;]*;', '', m.group(2)).lstrip()
        joined = '; '.join(x for x in (inside, after) if x)
        return 'style="' + joined
    return re.sub(r'style="([^"]*)"\s*,\s*([^"]*)"', repl, h or '', flags=re.I)


def sanitize_html(h):
    raw = h or ''
    styles = [_sanitize_style_block(s) for s in re.findall(r'(?is)<style[^>]*>(.*?)</style>', raw)]
    m = re.search(r'(?is)<body[^>]*>(.*)</body>', raw)
    h = m.group(1) if m else raw
    h = re.sub(r'(?is)<style[^>]*>.*?</style>', '', h)
    h = re.sub(r'(?is)<script.*?>.*?</script>', '', h)
    h = re.sub(r'(?is)<iframe.*?>.*?</iframe>', '', h)
    h = re.sub(r'(?is)<object.*?>.*?</object>', '', h)
    h = re.sub(r'(?i)\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', h)
    h = re.sub(r'(?i)javascript:', '', h)
    h = re.sub(r'(?i)\starget\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', h)
    h = repair_broken_style_quotes(h)
    h = re.sub(
        r'style="([^"]*)"',
        lambda m: 'style="' + html_mod.escape(_clean_style_attr(m.group(1)), quote=True) + '"',
        h,
    )
    h = re.sub(r'(?i)<a\b', '<a target="_blank" rel="noopener noreferrer"', h)
    prefix = ''.join('<style>' + s + '</style>' for s in styles if s and s.strip())
    return (prefix + h).strip()


def get_bodies(msg):
    plain = html_part = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ct = part.get_content_type()
        disp = str(part.get('Content-Disposition') or '')
        if 'attachment' in disp.lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or 'utf-8'
        try:
            text = payload.decode(charset, errors='replace')
        except Exception:
            text = payload.decode('utf-8', errors='replace')
        if ct == 'text/plain' and plain is None:
            plain = text
        elif ct == 'text/html' and html_part is None:
            html_part = text
    if html_part:
        html_part = inline_cid_images(msg, html_part)
    body = (plain or '').strip() or (strip_html(html_part) if html_part else '') or '(无正文)'
    html_out = sanitize_html(html_part) if html_part else ''
    return body, html_out


def get_attachments(msg):
    atts = []
    for i, part in enumerate(msg.walk()):
        fn = part.get_filename()
        if fn:
            payload = part.get_payload(decode=True) or b''
            atts.append({'idx': i, 'name': dec(fn), 'size': len(payload)})
    return atts


# ---------- IMAP / SMTP ----------
IMAP_CLIENT_ID = '("name" "PuMail" "version" "1.0.0" "vendor" "PuMail" "support-email" "pumail@localhost")'


def needs_imap_id(host):
    h = (host or '').lower()
    return any(h.endswith(s) for s in ('.163.com', '.126.com', '.yeah.net'))


def _http_form(url, data, timeout=25):
    body = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        try:
            return json.loads(raw)
        except Exception:
            return {'error': 'http_error', 'error_description': raw or str(e)}


def outlook_oauth_url(kind):
    return f'https://login.microsoftonline.com/{OUTLOOK_OAUTH_TENANT}/oauth2/v2.0/{kind}'


def outlook_xoauth2(user, token):
    return f'user={user}\x01auth=Bearer {token}\x01\x01'


def outlook_start_device(email):
    d = _http_form(outlook_oauth_url('devicecode'), {
        'client_id': OUTLOOK_OAUTH_CLIENT,
        'scope': OUTLOOK_OAUTH_SCOPE,
        'login_hint': email or '',
    })
    if d.get('error') or not d.get('device_code'):
        raise RuntimeError(d.get('error_description') or d.get('error') or '无法开始 Microsoft 登录')
    return d


def outlook_poll_device(device_code):
    d = _http_form(outlook_oauth_url('token'), {
        'client_id': OUTLOOK_OAUTH_CLIENT,
        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        'device_code': device_code,
    })
    return d


def outlook_refresh_access(refresh_token):
    d = _http_form(outlook_oauth_url('token'), {
        'client_id': OUTLOOK_OAUTH_CLIENT,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'scope': OUTLOOK_OAUTH_SCOPE,
    })
    if not d.get('access_token'):
        raise RuntimeError(d.get('error_description') or d.get('error') or 'Microsoft 登录已过期，请重新添加账号')
    return d


def outlook_access_token(acc):
    if acc.get('oauth_access'):
        return acc['oauth_access']
    aid = acc.get('id')
    now = time.time()
    with _outlook_lock:
        cached = _outlook_token_cache.get(aid)
        if cached and cached.get('access') and cached.get('exp', 0) - 60 > now:
            return cached['access']
    refresh = acc.get('oauth_refresh') or ''
    if not refresh:
        raise RuntimeError('Outlook 需要用 Microsoft 登录，授权码已无法使用')
    d = outlook_refresh_access(refresh)
    access = d['access_token']
    new_refresh = d.get('refresh_token') or refresh
    exp = now + int(d.get('expires_in') or 3600)
    if new_refresh != refresh and aid:
        db = get_db()
        db.execute('UPDATE accounts SET oauth_refresh=? WHERE id=?', (pw_enc(new_refresh), aid))
        db.commit()
        db.close()
        acc['oauth_refresh'] = new_refresh
    if aid:
        with _outlook_lock:
            _outlook_token_cache[aid] = {'access': access, 'exp': exp}
    return access


def upsert_mail_account(email_, name, pkey, imap_host, imap_port, smtp_host, smtp_port, starttls, password='', oauth_refresh=''):
    db = get_db()
    active = db.execute(
        'SELECT id FROM accounts WHERE email=? AND COALESCE(active,1)=1', (email_,)
    ).fetchone()
    if active:
        db.close()
        raise RuntimeError('该邮箱已添加')
    nxt = db.execute('SELECT COALESCE(MAX(sort_order),0)+1 FROM accounts').fetchone()[0]
    parked = db.execute(
        'SELECT id FROM accounts WHERE email=? AND COALESCE(active,1)=0 ORDER BY id DESC LIMIT 1',
        (email_,)
    ).fetchone()
    enc_pw = pw_enc(password) if password else ''
    enc_oauth = pw_enc(oauth_refresh) if oauth_refresh else ''
    if parked:
        db.execute(
            "UPDATE accounts SET name=?,provider=?,imap_host=?,imap_port=?,smtp_host=?,"
            "smtp_port=?,starttls=?,password=?,oauth_refresh=?,sort_order=?,active=1 WHERE id=?",
            (name, pkey, imap_host, imap_port, smtp_host, smtp_port, starttls, enc_pw, enc_oauth, nxt, parked['id'])
        )
        new_id = parked['id']
    else:
        cur = db.execute(
            "INSERT INTO accounts(email,name,provider,imap_host,imap_port,smtp_host,smtp_port,starttls,password,oauth_refresh,sort_order,active)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (email_, name, pkey, imap_host, imap_port, smtp_host, smtp_port, starttls, enc_pw, enc_oauth, nxt)
        )
        new_id = cur.lastrowid
    db.commit()
    db.close()
    return new_id


def imap_auth(conn, acc):
    email_ = (acc.get('email') or '').strip()
    if acc.get('oauth_refresh'):
        token = outlook_access_token(acc)
        payload = outlook_xoauth2(email_, token).encode('utf-8')
        conn.authenticate('XOAUTH2', lambda _r: payload)
        return
    password = acc.get('password') or ''
    try:
        conn.login(email_, password)
        return
    except Exception as first:
        try:
            raw = b'\0' + email_.encode('utf-8') + b'\0' + password.encode('utf-8')
            conn.authenticate('PLAIN', lambda _r: raw)
            return
        except Exception:
            host = (acc.get('imap_host') or '').lower()
            if 'outlook' in host or 'office365' in host or acc.get('provider') == 'outlook':
                raise RuntimeError(
                    'Outlook / Hotmail 已停用授权码登录，请在添加账号时点「用 Microsoft 登录」'
                ) from first
            raise first


def imap_send_id(conn):
    try:
        imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')
        typ, data = conn._simple_command('ID', IMAP_CLIENT_ID)
        try:
            conn._untagged_response(typ, data, 'ID')
        except Exception:
            pass
        return typ == 'OK'
    except Exception:
        return False


def imap_open(acc, timeout=30):
    host = acc.get('imap_host') or ''
    port = int(acc.get('imap_port') or 993)
    imap_log(acc, 'CONNECT', host, port)
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    except Exception as e:
        imap_log(acc, 'CONNECT fail', type(e).__name__, e)
        raise
    try:
        conn.sock.settimeout(30)
    except Exception:
        pass
    imap_log(acc, 'CONNECT ok')
    if needs_imap_id(host):
        ok = imap_send_id(conn)
        imap_log(acc, 'ID before-login', 'ok' if ok else 'fail')
    try:
        imap_auth(conn, acc)
    except Exception as e:
        imap_log(acc, 'LOGIN fail', type(e).__name__, e)
        raise
    imap_log(acc, 'LOGIN ok')
    if needs_imap_id(host):
        ok = imap_send_id(conn)
        imap_log(acc, 'ID after-login', 'ok' if ok else 'fail')
    return conn


def imap_exists_count(conn):
    n = getattr(conn, '_pumail_exists', 0) or 0
    if n:
        return n
    try:
        typ, data = conn.response('EXISTS')
        if data:
            for item in data:
                if item is None:
                    continue
                s = item.decode('ascii', 'ignore') if isinstance(item, (bytes, bytearray)) else str(item)
                if s.strip().isdigit():
                    return int(s.strip())
    except Exception:
        pass
    return 0


def imap_uids_from_fetch(fetched):
    uids = []
    if not fetched:
        return uids
    for part in fetched:
        meta = part[0] if isinstance(part, tuple) else part
        if isinstance(meta, bytes):
            m = re.search(br'UID\s+(\d+)', meta)
            if m:
                uids.append(m.group(1))
    return uids


def imap_all_uids(conn):
    """163 等邮箱在未发 ID 或仅支持 SEARCH 时，UID SEARCH ALL 会空。"""
    typ, data = conn.uid('search', None, 'ALL')
    if typ == 'OK' and data and data[0]:
        uids = data[0].split()
        if uids:
            imap_log(None, 'SEARCH uid-all', len(uids))
            return uids
    imap_log(None, 'SEARCH uid-all empty', typ)
    try:
        typ, data = conn.search(None, 'ALL')
        seqs = data[0].split() if typ == 'OK' and data and data[0] else []
        if seqs:
            typ, fetched = conn.fetch(b','.join(seqs), '(UID)')
            uids = imap_uids_from_fetch(fetched) if typ == 'OK' else []
            if uids or seqs:
                imap_log(None, 'SEARCH seq-all', len(uids or seqs))
                return uids or seqs
    except Exception as e:
        imap_log(None, 'SEARCH seq-all err', type(e).__name__, e)
    n = imap_exists_count(conn)
    if n <= 0:
        imap_log(None, 'SEARCH exists=0')
        return []
    try:
        typ, fetched = conn.fetch(b'1:%d' % n, '(UID)')
        uids = imap_uids_from_fetch(fetched) if typ == 'OK' else []
        if uids:
            imap_log(None, 'SEARCH exists-fallback', 'exists', n, 'uids', len(uids))
            return uids
        imap_log(None, 'SEARCH exists-fallback empty', 'exists', n, 'typ', typ)
        return [str(i).encode() for i in range(1, n + 1)]
    except Exception as e:
        imap_log(None, 'SEARCH exists-fallback err', type(e).__name__, e)
        return []


def smtp_send(acc, to_addrs, msg):
    host = acc['smtp_host']
    port = int(acc['smtp_port'] or 465)
    use_starttls = bool(int(acc.get('starttls') or 0)) or port == 587
    attempts = []
    if use_starttls:
        attempts.append(('starttls', host, port if port != 465 else 587))
        attempts.append(('ssl', host, 465))
    else:
        attempts.append(('ssl', host, port))
        if host.endswith('.qq.com') or host.endswith('.163.com') or host.endswith('.126.com') or host.endswith('.gmail.com'):
            attempts.append(('starttls', host, 587))

    errors = []
    for kind, h, p in attempts:
        smtp = None
        try:
            if kind == 'ssl':
                smtp = smtplib.SMTP_SSL(h, p, timeout=40)
            else:
                smtp = smtplib.SMTP(h, p, timeout=40)
            smtp.ehlo()
            if kind == 'starttls':
                smtp.starttls()
                smtp.ehlo()
            if acc.get('oauth_refresh'):
                token = outlook_access_token(acc)
                payload = outlook_xoauth2(acc['email'], token)
                smtp.auth('XOAUTH2', lambda _c=None: payload)
            else:
                smtp.login(acc['email'], acc['password'])
            refused = smtp.send_message(msg, from_addr=acc['email'], to_addrs=to_addrs)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            return
        except Exception as e:
            errors.append(f'{kind} {h}:{p} → {e}')
            # 把每次失败的细节写进调试日志，方便排查"发不出去"
            imap_log(acc, 'SMTP fail', kind, h, p, type(e).__name__, e)
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass
    raise RuntimeError('；'.join(errors) if errors else 'SMTP 发送失败')


def append_to(conn, msg, folders, flags='\\Seen'):
    payload = msg.as_bytes() if hasattr(msg, 'as_bytes') else msg.as_string().encode('utf-8')
    names = []
    for folder in folders:
        cands = mailbox_wire_names(folder) or ([folder] if folder else [])
        for cand in cands:
            raw = str(cand).strip().strip('"')
            if raw and raw not in names:
                names.append(raw)
    flag_opts = []
    for fl in (flags, '\\Draft', '\\Seen', ''):
        if fl not in flag_opts:
            flag_opts.append(fl)
    stamp = imaplib.Time2Internaldate(time.time())
    for name in names:
        for fl in flag_opts:
            try:
                conn.append(name, fl, stamp, payload)
                return name
            except Exception:
                continue
    return ''


FOLDER_ALIASES = {
    '收件箱': ['INBOX'],
    '已发送': ['Sent Messages', 'Sent Items', 'Sent Mail', '[Gmail]/Sent Mail', 'Sent', '已发送', '&XfJT0ZAB-'],
    '草稿': ['Drafts', '[Gmail]/Drafts', '草稿', '&g0l6P3ux-'],
    '垃圾邮件': ['Junk', 'Spam', '[Gmail]/Spam', 'Bulk Mail', '垃圾邮件', '垃圾', '&V4NXPpCuTvY-'],
    '已删除': ['Trash', '[Gmail]/Trash', 'Deleted Messages', '已删除', '&XfJSIJZk-'],
}


def mailbox_wire_names(name):
    if not name:
        return []
    out = []
    if str(name).isascii():
        out.extend((f'"{name}"', name))
    enc = encode_mutf7(name)
    if enc and enc.isascii():
        for item in (f'"{enc}"', enc):
            if item not in out:
                out.append(item)
    return out


def select_folder(conn, folder, readonly=True, acc=None):
    if folder == STARRED_ID:
        folder = 'INBOX'
    folder = folder or 'INBOX'
    label = folder_label(folder)
    names = []
    for n in [folder, decode_mutf7(folder)] + FOLDER_ALIASES.get(label, []):
        if n and n not in names:
            names.append(n)
    cands, seen = [], set()
    for name in names:
        for cand in mailbox_wire_names(name):
            if cand not in seen:
                seen.add(cand)
                cands.append(cand)
    current = getattr(conn, '_pumail_mailbox', None)
    if current and current in cands:
        return True
    for cand in cands:
        try:
            typ, data = conn.select(cand, readonly=readonly)
            if typ == 'OK':
                exists = 0
                try:
                    raw = data[0] if data else b'0'
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode('ascii', 'ignore')
                    exists = int(str(raw).strip() or 0)
                except Exception:
                    exists = imap_exists_count(conn)
                try:
                    conn._pumail_exists = exists
                    conn._pumail_mailbox = cand
                except Exception:
                    pass
                imap_log(acc, 'SELECT ok', cand, 'exists', exists)
                return True
            imap_log(acc, 'SELECT no', cand, typ)
        except (UnicodeEncodeError, UnicodeError):
            imap_log(acc, 'SELECT encode-skip', cand)
            continue
        except Exception as e:
            imap_log(acc, 'SELECT err', cand, type(e).__name__, e)
            continue
    imap_log(acc, 'SELECT fail', folder, 'label', label, 'tried', len(cands))
    return False


# ---------- 文件夹 ----------
FOLDER_RULES = [
    ('INBOX', '收件箱'),
    ('Sent Messages', '已发送'),
    ('Sent Items', '已发送'),
    ('Sent Mail', '已发送'),
    ('[Gmail]/Sent Mail', '已发送'),
    ('Sent', '已发送'),
    ('已发送', '已发送'),
    ('&XfJT0ZAB-', '已发送'),
    ('Drafts', '草稿'),
    ('[Gmail]/Drafts', '草稿'),
    ('草稿', '草稿'),
    ('&g0l6P3ux-', '草稿'),
    ('Junk', '垃圾邮件'),
    ('Spam', '垃圾邮件'),
    ('[Gmail]/Spam', '垃圾邮件'),
    ('Bulk Mail', '垃圾邮件'),
    ('垃圾', '垃圾邮件'),
    ('&V4NXPpCuTvY-', '垃圾邮件'),
    ('Trash', '已删除'),
    ('[Gmail]/Trash', '已删除'),
    ('Deleted Messages', '已删除'),
    ('已删除', '已删除'),
    ('&XfJSIJZk-', '已删除'),
]


def folder_label(name):
    decoded = decode_mutf7(name)
    if decoded.upper() == 'INBOX' or name.upper() == 'INBOX':
        return '收件箱'
    for key, label in FOLDER_RULES:
        if key.lower() in decoded.lower() or key.lower() in name.lower():
            return label
    return decoded


def _unquote_imap(token):
    token = (token or '').strip()
    if token.upper() == 'NIL':
        return ''
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return token


def parse_list_line(item):
    """解析 IMAP LIST 行：(flags) delimiter mailbox"""
    if isinstance(item, tuple):
        item = item[-1]
    if isinstance(item, bytes):
        item = item.decode('utf-8', errors='replace')
    item = item.strip()
    if not item.startswith('('):
        return None
    depth = 0
    end = None
    for i, ch in enumerate(item):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None
    flags = item[1:end]
    rest = item[end + 1:].strip()
    m = re.match(r'^(NIL|"(?:[^"\\]|\\.)*"|[^\s]+)\s+(.+)$', rest, re.I | re.S)
    if not m:
        return None
    raw_name = _unquote_imap(m.group(2).strip())
    if not raw_name:
        return None
    return flags, raw_name


def list_folders(conn):
    folders = []
    typ, data = conn.list()
    if typ != 'OK' or not data:
        return [{'id': 'INBOX', 'label': '收件箱'}]
    seen = set()
    for item in data:
        parsed = parse_list_line(item)
        if not parsed:
            continue
        flags, raw = parsed
        if 'Noselect' in flags or 'NoSelect' in flags:
            continue
        name = decode_mutf7(raw)
        if name.lower() == 'inbox' or raw.lower() == 'inbox':
            raw = 'INBOX'
            name = 'INBOX'
        if raw in seen:
            continue
        seen.add(raw)
        fl = flags.lower()
        if name.upper() == 'INBOX' or '\\inbox' in fl:
            label = '收件箱'
        elif '\\sent' in fl:
            label = '已发送'
        elif '\\draft' in fl:
            label = '草稿'
        elif '\\junk' in fl or '\\spam' in fl:
            label = '垃圾邮件'
        elif '\\trash' in fl or '\\bin' in fl:
            label = '已删除'
        else:
            label = folder_label(raw)
        folders.append({'id': raw, 'label': label})
    if not folders:
        return [{'id': 'INBOX', 'label': '收件箱'}]
    imap_log(None, 'LIST', ','.join(f"{f['label']}={f['id']}" for f in folders))
    keep = ['收件箱', '已发送', '星标', '草稿', '垃圾邮件', '已删除']
    picked = {}
    for f in folders:
        if f['label'] in keep and f['label'] not in picked:
            picked[f['label']] = f
    picked['星标'] = {'id': STARRED_ID, 'label': '星标'}
    if '收件箱' not in picked:
        picked['收件箱'] = {'id': 'INBOX', 'label': '收件箱'}
    return [picked[k] for k in keep if k in picked]


_REPLY_TOKEN = r'(re|fwd|fw|回复|答复|答覆|回覆|转发)(\s*\[\d+\])?\s*[:：]\s*'


def display_subject(s):
    s = re.sub(r'\s+', ' ', (s or '').replace('\r', ' ').replace('\n', ' ')).strip()
    while True:
        n = re.sub(r'^' + _REPLY_TOKEN, '', s, flags=re.I).strip()
        if n == s:
            break
        s = n
    return s or '(无主题)'


def thread_core(s):
    s = display_subject(s).lower()
    while True:
        n = re.sub(_REPLY_TOKEN, '', s, flags=re.I)
        n = re.sub(r'\s+', ' ', n).strip()
        if n == s:
            break
        s = n
    compact = re.sub(r'\s+', '', s)
    while True:
        longest = 0
        for n in range(2, len(compact) // 2 + 1):
            chunk = compact[:n]
            if chunk == compact[n:2 * n] and re.search(r'[\u4e00-\u9fffA-Za-z]', chunk):
                longest = n
        if not longest:
            break
        compact = compact[longest:]
    return compact or '(无主题)'


def norm_subject(s):
    return thread_core(s)


_OTP_SUBJ = re.compile(
    r'一次性\s*[代验認认]?[码碼]|'
    r'验证码|驗證碼|校验码|校驗碼|动态码|動態碼|'
    r'安全代码|安全代碼|'
    r'verification\s*code|security\s*code|'
    r'one[-\s]?time\s+(?:code|password|passcode)|'
    r'\botp\b|'
    r'(?:your|google|microsoft|apple)\s+(?:verification\s+|security\s+)?code',
    re.I,
)


def is_otp_mail(it):
    return bool(_OTP_SUBJ.search(it.get('subject') or ''))


def thread_group_key(it, folder=''):
    if is_otp_mail(it):
        uid = str(it.get('uid') or '')
        fold = it.get('folder') or folder or ''
        acc = it.get('acc') or it.get('acc_id') or ''
        return f'otp|{acc}|{fold}|{uid}'
    return norm_subject(it.get('subject'))


def parse_any_date(raw):
    raw = (raw or '').strip()
    if not raw:
        return 0.0
    try:
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        pass
    m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}):(\d{2}):(\d{2})', raw)
    if m:
        mon = {n: i + 1 for i, n in enumerate(_IMAP_MON)}.get(m.group(2))
        if mon:
            try:
                t = (int(m.group(3)), mon, int(m.group(1)),
                     int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, -1)
                return time.mktime(time.struct_time(t))
            except Exception:
                return 0.0
    return 0.0


def mail_timestamp(m):
    if not isinstance(m, dict):
        return 0.0
    if m.get('ts'):
        try:
            ts = float(m['ts'])
            if ts > 0:
                return ts
        except Exception:
            pass
    return parse_any_date(m.get('date') or m.get('date_raw') or '')


def format_addr_list(raw):
    parts = []
    for nm, addr in getaddresses([raw or '']):
        nm = dec(nm)
        if nm and addr:
            parts.append(f'{nm} <{addr}>')
        elif addr:
            parts.append(addr)
        elif nm:
            parts.append(nm)
    return ', '.join(parts)


def group_threads(items, folder=''):
    groups = []
    index = {}
    for it in items:
        key = thread_group_key(it, folder)
        if key not in index:
            index[key] = len(groups)
            groups.append([])
        groups[index[key]].append(it)
    threads = []
    for msgs in groups:
        msgs = sorted(msgs, key=lambda m: mail_timestamp(m) or 0)
        latest = dict(msgs[-1])
        latest['subject'] = display_subject(msgs[-1].get('subject') or latest.get('subject'))
        latest['thread_key'] = thread_group_key(latest, folder)
        latest['thread_count'] = len(msgs)
        latest['thread_uids'] = [m['uid'] for m in msgs]
        latest['unread'] = any(m.get('unread') for m in msgs)
        latest['starred'] = any(m.get('starred') for m in msgs)
        latest['ts'] = mail_timestamp(latest)
        if not latest.get('date') and latest['ts']:
            latest['date'] = time.strftime('%Y/%m/%d %H:%M', time.localtime(latest['ts']))
        latest['thread_parts'] = [{'folder': m.get('folder') or folder, 'uid': m['uid']} for m in msgs]
        threads.append(latest)
    return threads


def parse_fetch_items(data):
    items = []
    if not data:
        return items
    for part in data:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        meta = part[0] if isinstance(part[0], bytes) else b''
        raw = part[1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        uid_m = re.search(br'UID\s+(\d+)', meta)
        if not uid_m:
            continue
        flags = meta
        try:
            m = message_from_bytes(raw)
        except Exception:
            continue
        nm, addr = parseaddr(dec(m.get('From')))
        internal = ''
        im = re.search(br'INTERNALDATE\s+"([^"]+)"', meta)
        if im:
            internal = im.group(1).decode('ascii', errors='replace')
        date = dec(m.get('Date')) or internal or ''
        items.append({
            'uid': uid_m.group(1).decode(),
            'subject': dec(m.get('Subject')) or '(无主题)',
            'from': nm or addr,
            'from_addr': addr or '',
            'date': date,
            'to': format_addr_list(m.get('To')),
            'unread': b'\\Seen' not in flags,
            'starred': b'\\Flagged' in flags,
            'ts': parse_any_date(date),
            'in_reply_to': (dec(m.get('In-Reply-To')) or '').strip(),
        })
    return items


def fetch_header_chunk(conn, uids):
    if not uids:
        return []
    raw_uids = [u if isinstance(u, (bytes, bytearray)) else str(u).encode() for u in uids]
    specs = (
        '(FLAGS UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE IN-REPLY-TO REFERENCES)])',
        '(FLAGS UID INTERNALDATE RFC822.HEADER)',
        '(FLAGS UID INTERNALDATE BODY.PEEK[HEADER])',
    )
    items = []
    for spec in specs:
        try:
            typ, d = conn.uid('fetch', b','.join(raw_uids), spec)
        except Exception as e:
            imap_log(None, 'FETCH err', spec[:24], type(e).__name__, e)
            continue
        if typ != 'OK':
            imap_log(None, 'FETCH no', spec[:24], typ)
            continue
        items = parse_fetch_items(d)
        if items:
            imap_log(None, 'FETCH ok', spec[:24], len(items), '/', len(raw_uids))
            break
    if not items:
        got = []
        for u in raw_uids[:HEADER_CHUNK]:
            try:
                typ, d = conn.uid('fetch', u, specs[1])
                if typ == 'OK':
                    got.extend(parse_fetch_items(d))
            except Exception:
                continue
        items = got
        imap_log(None, 'FETCH one-by-one', len(items), '/', len(raw_uids))
    order = {u.decode() if isinstance(u, bytes) else str(u): i for i, u in enumerate(raw_uids)}
    items.sort(key=lambda x: order.get(x['uid'], 999))
    return items


def backfill_missing_dates(acc, folder, limit=40):
    if not folder or folder == STARRED_ID:
        return
    db = get_db()
    rows = db.execute(
        """SELECT uid FROM mails WHERE acc_id=? AND folder=?
           AND (date_raw='' OR date_raw IS NULL OR ts=0) LIMIT ?""",
        (acc['id'], folder, limit)).fetchall()
    db.close()
    if not rows:
        return
    uids = [str(r['uid']).encode() for r in rows]

    def work(conn):
        if not select_folder(conn, folder, readonly=True):
            return
        upsert_headers(acc, folder, fetch_header_chunk(conn, uids))

    with_imap(acc, work)


def imap_search(conn, q, flagged=False):
    if flagged and not q:
        return conn.uid('search', None, 'FLAGGED')
    if not q:
        return conn.uid('search', None, 'ALL')
    quoted = '"' + q.replace('\\', '\\\\').replace('"', '\\"') + '"'
    attempts = [
        ('UTF-8', f'OR OR SUBJECT {quoted} FROM {quoted} TEXT {quoted}'),
        (None, 'OR', 'SUBJECT', q, 'FROM', q),
        (None, 'TEXT', q),
        (None, 'ALL'),
    ]
    for args in attempts:
        try:
            if args[0] == 'UTF-8':
                typ, data = conn.uid('search', 'UTF-8', args[1])
            else:
                typ, data = conn.uid('search', *args)
            if typ == 'OK':
                return typ, data
        except Exception:
            continue
    return conn.uid('search', None, 'ALL')


# ---------- 静态页面 ----------
@app.after_request
def after_request(resp):
    if request.path == '/' or request.path in ('/index.html',):
        resp.set_cookie('pupu_ok', APP_SECRET, httponly=True, samesite='Lax')
    return resp


@app.before_request
def before_request():
    if request.path.startswith('/api/'):
        if request.cookies.get('pupu_ok') != APP_SECRET:
            return jsonify({'error': '未授权，请从首页打开 PuMail'}), 401
    return None


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/<path:filename>')
def static_files(filename):
    if filename.startswith('api/'):
        abort(404)
    # guide.html 需要注入 markdown 内容（避免 file:// 下 fetch 被阻止）
    if filename == 'guide.html':
        filepath = os.path.join(BASE_DIR, 'guide.html')
        md_path = os.path.join(BASE_DIR, '邮箱绑定指南.md')
        if os.path.exists(filepath) and os.path.exists(md_path):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                with open(md_path, 'r', encoding='utf-8') as f:
                    md = f.read()
                injection = '<script>window.__GUIDE_MD__=' + json.dumps(md) + ';</script>'
                html_content = html_content.replace('</head>', injection + '</head>')
                return html_content
            except Exception:
                pass
    return send_from_directory(BASE_DIR, filename)


# ---------- API：账号 ----------
@app.route('/api/storage', methods=['GET'])
def api_storage():
    return jsonify(storage_info())


@app.route('/api/storage/open', methods=['POST'])
def api_storage_open():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        if os.name == 'nt':
            os.startfile(DATA_DIR)
        else:
            subprocess.Popen(['xdg-open', DATA_DIR])
    except Exception as exc:
        return jsonify({'error': '无法打开文件夹：' + str(exc)}), 500
    return jsonify(storage_info(ok=True))


@app.route('/api/storage/pick', methods=['POST'])
def api_storage_pick():
    body = request.get_json(silent=True) or {}
    path = str(body.get('path') or '').strip()
    if not path:
        path = _pick_folder(DATA_DIR)
        if not path:
            return jsonify({'cancelled': True, **storage_info()})
    try:
        with _storage_lock:
            info = migrate_data_dir(path)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': '迁移失败，数据仍在原位置。请重启后再试：' + str(exc)}), 500
    return jsonify(info)


def boot_folder_unread(acc, folders):
    sent_unread = bool(cache_get(f"{acc['id']}|sent_unread"))
    db = get_db()
    unread_rows = db.execute(
        """SELECT folder, COUNT(*) FROM mails
           WHERE acc_id=? AND local_deleted=0 AND unread=1
           GROUP BY folder""",
        (acc['id'],)).fetchall()
    unread_map = {r[0]: r[1] for r in unread_rows}
    starred_unread = db.execute(
        """SELECT COUNT(*) FROM mails
           WHERE acc_id=? AND starred=1 AND unread=1 AND local_deleted=0""",
        (acc['id'],)).fetchone()[0]
    if not sent_unread:
        sent_id = db_folder_label(acc['id'], '已发送')
        if sent_id:
            n = db.execute(
                """SELECT COUNT(*) FROM mails WHERE acc_id=? AND folder='INBOX' AND unread=1
                   AND thread_key IN (SELECT thread_key FROM mails WHERE acc_id=? AND folder=? AND mine=1)""",
                (acc['id'], acc['id'], sent_id)).fetchone()[0]
            sent_unread = n > 0
        cache_set(f"{acc['id']}|sent_unread", sent_unread)
    db.close()
    out = []
    for f in folders:
        item = dict(f)
        label = item.get('label')
        fid = item.get('id')
        count = unread_map.get(fid) or 0
        if label == '已发送':
            item['unread'] = bool(sent_unread or count)
            item['unread_count'] = count
        elif label == '星标':
            item['unread'] = bool(starred_unread)
            item['unread_count'] = int(starred_unread or 0)
        elif label == '收件箱':
            count = folder_unread_count(acc['id'], 'INBOX')
            item['unread'] = bool(count)
            item['unread_count'] = count
        elif label == '已删除':
            item['unread'] = False
            item['unread_count'] = 0
        else:
            item['unread'] = bool(count)
            item['unread_count'] = count
        out.append(item)
    return out


def boot_mail_pack(acc, folder):
    threads = threads_from_db(acc, folder)
    items = threads[:BOOT_MAIL_LIMIT]
    more = len(threads) > len(items)
    real = 'INBOX' if folder == STARRED_ID else resolve_mail_folder(acc['id'], folder)
    if folder != STARRED_ID and not folder_caught_up(acc['id'], real):
        more = True
    return {
        'items': items,
        'total': len(threads),
        'page': 1,
        'has_more': more,
    }


def boot_seed_account(acc):
    """Local-only seed: folders, unread, latest 20. No IMAP wait."""
    errors = []
    folders = sort_folders(db_load_folders(acc['id']) or [{'id': 'INBOX', 'label': '收件箱'}])
    mails = {}
    want = ['INBOX']
    for label in ('草稿', '已发送'):
        fid = db_folder_label(acc['id'], label)
        if fid:
            want.append(fid)
    for fol in want:
        try:
            mails[fol] = boot_mail_pack(acc, fol)
        except Exception as e:
            errors.append((acc.get('email') or '') + ' 读取失败：' + str(e))
    return {
        'id': acc['id'],
        'folders': boot_folder_unread(acc, folders),
        'mails': mails,
        'errors': errors,
    }


_boot_sync_lock = threading.Lock()
_boot_sync_busy = set()
# 启动同步按账号一个一个来（以前是 4 个账号一起冲，日志里的 imap busy 就来自这里）
_boot_sync_pool = ThreadPoolExecutor(max_workers=1)


def boot_sync_account(acc):
    if is_demo_account(acc):
        return
    aid = acc.get('id')
    with _boot_sync_lock:
        if aid in _boot_sync_busy:
            return
        _boot_sync_busy.add(aid)
    try:
        try:
            ensure_folders(acc)
        except Exception:
            pass
        targets = ['INBOX']
        for label in ('草稿', '已发送'):
            fid = db_folder_label(acc['id'], label)
            if fid and fid not in targets:
                targets.append(fid)
        for fol in targets:
            # 收件箱优先，草稿/已发送排最后；都算后台任务，给用户正在等待的操作让位
            prio = sync_queue.PRIORITY_OTHER if fol == 'INBOX' else sync_queue.PRIORITY_BACKGROUND
            try:
                sync_folder_headers(acc, fol, limit=BOOT_MAIL_LIMIT, priority=prio)
            except Exception:
                pass
    finally:
        with _boot_sync_lock:
            _boot_sync_busy.discard(aid)


@app.route('/api/boot')
def api_boot():
    accounts = list_public_accounts()
    if not accounts:
        return jsonify({'accounts': [], 'seeds': {}, 'gaia': None, 'errors': []})
    accs = iter_accounts()
    seeds = {}
    errors = []
    if accs:
        for acc in accs:
            try:
                packed = boot_seed_account(acc)
            except Exception as e:
                errors.append(str(e))
                continue
            seeds[str(packed['id'])] = {
                'folders': packed.get('folders') or [],
                'mails': packed.get('mails') or {},
            }
            errors.extend(packed.get('errors') or [])
        for acc in accs:
            _boot_sync_pool.submit(boot_sync_account, acc)
    first = setting_get(GAIA_FIRST_KEY) != '1'
    gaia = None
    try:
        folders = []
        for kind, label in (('INBOX', '收件箱'), ('SENT', '已发送'), ('DRAFTS', '草稿')):
            # 未读数使用与邮件列表相同的时间窗口，确保数字与实际显示一致
            n = gaia_folder_unread(kind, recent_only=not first)
            folders.append({'id': kind, 'label': label, 'unread': n > 0, 'unread_count': n})
        threads = gaia_threads('INBOX', '', recent_only=not first)
        gaia = {
            'folders': folders,
            'mails': {
                'INBOX': {
                    'items': threads[:BOOT_MAIL_LIMIT],
                    'total': len(threads),
                    'page': 1,
                    'has_more': len(threads) > BOOT_MAIL_LIMIT,
                }
            },
        }
    except Exception as e:
        errors.append(str(e))
    summary = unread_summary(recent_only=not first)
    return jsonify({
        'accounts': accounts,
        'seeds': seeds,
        'gaia': gaia,
        'errors': errors,
        'unread_per_account': summary['per_account'],
        'unread_summary': summary,
    })


def peek_new_uids(conn, last_uid):
    """UID SEARCH only — no header fetch, never SEARCH ALL."""
    if last_uid > 0:
        typ, data = conn.uid('search', None, 'UID', f'{last_uid + 1}:*')
        if typ == 'OK' and data and data[0]:
            return [u for u in data[0].split() if uid_int(u) > last_uid]
        return []
    n = imap_exists_count(conn)
    if n <= 0:
        return []
    recent = imap_uids_since(conn)
    if recent is not None:
        return recent[:FIRST_BATCH]
    return []


def refresh_account(acc, folder='INBOX'):
    """Lightweight: search for newer UIDs; fetch headers only if new mail exists."""
    errors = []
    found = {'new': False, 'count': 0}
    t0 = time.time()
    if is_demo_account(acc):
        packed = boot_seed_account(acc)
        packed['errors'] = []
        packed['new_mail'] = False
        packed['new_count'] = 0
        return packed
    if folder == STARRED_ID:
        packed = boot_seed_account(acc)
        packed['errors'] = []
        packed['new_mail'] = False
        packed['new_count'] = 0
        return packed
    real = 'INBOX'
    if folder and folder != 'INBOX':
        try:
            real = resolve_mail_folder(acc['id'], folder) or 'INBOX'
        except Exception:
            real = folder

    def work(conn):
        if not select_folder(conn, real, readonly=True, acc=acc):
            return
        last = get_last_uid(acc['id'], real)
        fresh = peek_new_uids(conn, last)
        found['count'] = len(fresh)
        found['new'] = bool(fresh)
        if not fresh:
            imap_log(acc, 'REFRESH none', real, 'last', last, 'ms', int((time.time() - t0) * 1000))
            return
        parsed_n = 0
        for i in range(0, len(fresh), HEADER_CHUNK):
            parsed = fetch_header_chunk(conn, fresh[i:i + HEADER_CHUNK])
            parsed_n += len(parsed)
            upsert_headers(acc, real, parsed)
        top = max((uid_int(u) for u in fresh), default=last)
        set_folder_state(acc['id'], real, max(top, last))
        imap_log(acc, 'REFRESH new', real, 'count', found['count'], 'parsed', parsed_n, 'ms', int((time.time() - t0) * 1000))

    try:
        with_imap(acc, work, ping=False, retry=False, timeout=8)
    except Exception as e:
        imap_log(acc, 'REFRESH fail', type(e).__name__, e, 'ms', int((time.time() - t0) * 1000))
        errors.append((acc.get('email') or '') + ' 刷新失败：' + str(e))
    if found['new']:
        packed = boot_seed_account(acc)
    else:
        packed = {'id': acc['id'], 'folders': [], 'mails': {}}
    packed['errors'] = (packed.get('errors') or []) + errors
    packed['new_mail'] = found['new']
    packed['new_count'] = found['count']
    return packed


@app.route('/api/refresh')
def api_refresh():
    accounts = list_public_accounts()
    if not accounts:
        return jsonify({'accounts': [], 'seeds': {}, 'gaia': None, 'errors': [], 'new_mail': False, 'new_count': 0, 'unread_per_account': {}})
    accs = iter_accounts()
    want_id = request.args.get('acc', type=int)
    folder = request.args.get('folder') or 'INBOX'
    seeds = {}
    errors = []
    new_mail = False
    new_count = 0
    primary = [a for a in accs if not want_id or a['id'] == want_id]
    if primary:
        workers = min(4, len(primary))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(refresh_account, acc, folder if want_id else 'INBOX') for acc in primary]
            try:
                for fut in as_completed(futs, timeout=6):
                    try:
                        packed = fut.result()
                    except Exception as e:
                        errors.append(str(e))
                        continue
                    if packed.get('new_mail'):
                        new_mail = True
                        new_count += int(packed.get('new_count') or 0)
                        seeds[str(packed['id'])] = {
                            'folders': packed.get('folders') or [],
                            'mails': packed.get('mails') or {},
                        }
                    errors.extend(packed.get('errors') or [])
            except TimeoutError:
                pass
    first = setting_get(GAIA_FIRST_KEY) != '1'
    gaia = None
    if new_mail:
        try:
            folders = []
            for kind, label in (('INBOX', '收件箱'), ('SENT', '已发送'), ('DRAFTS', '草稿')):
                # 未读数使用与邮件列表相同的时间窗口，确保数字与实际显示一致
                n = gaia_folder_unread(kind, recent_only=not first)
                folders.append({'id': kind, 'label': label, 'unread': n > 0, 'unread_count': n})
            threads = gaia_threads('INBOX', '', recent_only=not first)
            gaia = {
                'folders': folders,
                'mails': {
                    'INBOX': {
                        'items': threads[:BOOT_MAIL_LIMIT],
                        'total': len(threads),
                        'page': 1,
                        'has_more': len(threads) > BOOT_MAIL_LIMIT,
                    }
                },
            }
        except Exception as e:
            errors.append(str(e))
    summary = unread_summary(recent_only=not first)
    return jsonify({
        'accounts': accounts if new_mail else [],
        'seeds': seeds,
        'gaia': gaia,
        'errors': errors,
        'new_mail': new_mail,
        'new_count': new_count,
        'unread_per_account': summary['per_account'],
        'unread_summary': summary,
    })


@app.route('/api/gaia/folders')
def api_gaia_folders():
    first = setting_get(GAIA_FIRST_KEY) != '1'
    recent = not first
    folders = []
    for kind, label in (('INBOX', '收件箱'), ('SENT', '已发送'), ('DRAFTS', '草稿')):
        # 未读数使用与邮件列表相同的时间窗口，确保数字与实际显示一致
        n = gaia_folder_unread(kind, recent_only=not first)
        folders.append({'id': kind, 'label': label, 'unread': n > 0, 'unread_count': n})
    return jsonify({'folders': folders, 'first': first,
                    'unread_summary': unread_summary(recent_only=recent)})


@app.route('/api/gaia/unread-per-account')
def api_gaia_unread_per_account():
    """返回每个账号的未读邮件数，用于在侧边栏账号图标上显示红点。"""
    first = setting_get(GAIA_FIRST_KEY) != '1'
    return jsonify({'unread': gaia_unread_per_account(recent_only=not first)})


@app.route('/api/gaia/mails')
def api_gaia_mails():
    kind = (request.args.get('folder') or 'INBOX').upper()
    if kind not in ('INBOX', 'SENT', 'DRAFTS'):
        kind = 'INBOX'
    page_no = max(1, request.args.get('page', 1, type=int))
    q = (request.args.get('q') or '').strip()
    head = request.args.get('head') == '1'
    first = setting_get(GAIA_FIRST_KEY) != '1'
    accs = iter_accounts()
    if accs:
        workers = min(4, len(accs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda a: sync_gaia_account(a, kind, first), accs))
        if first:
            setting_set(GAIA_FIRST_KEY, '1')
    threads = gaia_threads(kind, q, recent_only=not first)
    start = 0 if head else (page_no - 1) * PER_PAGE
    take = FIRST_BATCH if head else PER_PAGE
    items = threads[start:start + take]
    unread_count = gaia_folder_unread(kind, recent_only=not first)
    return jsonify({
        'items': items,
        'total': len(threads),
        'page': page_no,
        'per_page': PER_PAGE,
        'folder': kind,
        'has_more': start + len(items) < len(threads),
        'first': first,
        'syncing': False,
        'errors': pop_sync_errors(),
        'unread_count': unread_count,
        'unread_summary': unread_summary(recent_only=not first),
    })


@app.route('/api/providers')
def api_providers():
    return jsonify(PROVIDERS)


@app.route('/api/accounts', methods=['GET'])
def api_accounts():
    return jsonify(list_public_accounts())


@app.route('/api/accounts', methods=['POST'])
def api_add_account():
    d = request.get_json(force=True)
    email_ = (d.get('email') or '').strip()
    name = (d.get('name') or '').strip()
    pkey = d.get('provider') or 'custom'
    p = PROVIDERS.get(pkey, PROVIDERS['custom'])
    if pkey == 'custom':
        imap_host = (d.get('imap_host') or '').strip()
        smtp_host = (d.get('smtp_host') or '').strip()
        imap_port = int(d.get('imap_port') or 993)
        smtp_port = int(d.get('smtp_port') or 465)
        starttls = 1 if d.get('starttls') else 0
        if not imap_host or not smtp_host:
            return jsonify({'error': '自定义服务商需要填写 IMAP / SMTP 主机'}), 400
    else:
        imap_host, smtp_host = p['imap'], p['smtp']
        imap_port, smtp_port = p['imap_port'], p['smtp_port']
        starttls = 1 if p['starttls'] else 0
    password = d.get('password') or ''
    if not email_:
        return jsonify({'error': '邮箱地址不能为空'}), 400
    if pkey == 'outlook' and not password:
        return jsonify({'error': 'Outlook / Hotmail 已停用授权码，请点「用 Microsoft 登录」'}), 400
    if not password:
        return jsonify({'error': '邮箱地址和授权码不能为空'}), 400
    db = get_db()
    n = db.execute('SELECT COUNT(*) FROM accounts WHERE COALESCE(active,1)=1').fetchone()[0]
    if n >= 6:
        db.close()
        return jsonify({'error': '最多绑定 6 个邮箱账号'}), 400
    db.close()
    try:
        probe = {'imap_host': imap_host, 'imap_port': imap_port, 'email': email_, 'password': password, 'provider': pkey}
        conn = imap_open(probe)
        conn.logout()
    except Exception as e:
        msg = str(e)
        if pkey == 'outlook' or 'Outlook' in msg or 'mechanism is not supported' in msg.lower():
            return jsonify({'error': 'Outlook / Hotmail 已停用授权码登录，请点「用 Microsoft 登录」'}), 400
        return jsonify({'error': f'无法登录 IMAP 服务器：{e}。请检查地址和授权码。'}), 400
    try:
        new_id = upsert_mail_account(
            email_, name, pkey, imap_host, imap_port, smtp_host, smtp_port, starttls, password=password
        )
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    vault_upsert_account(email_, pkey, password)
    dismiss_demo_accounts()
    sync_idle_threads()
    return jsonify({'id': new_id, 'email': email_, 'name': name, 'provider': pkey})


@app.route('/api/accounts/outlook-oauth/start', methods=['POST'])
def api_outlook_oauth_start():
    d = request.get_json(force=True) or {}
    email_ = (d.get('email') or '').strip()
    name = (d.get('name') or '').strip()
    if not email_ or '@' not in email_:
        return jsonify({'error': '请先填写 Outlook / Hotmail 邮箱地址'}), 400
    db = get_db()
    n = db.execute('SELECT COUNT(*) FROM accounts WHERE COALESCE(active,1)=1').fetchone()[0]
    exist = db.execute(
        'SELECT id FROM accounts WHERE email=? AND COALESCE(active,1)=1', (email_,)
    ).fetchone()
    db.close()
    if exist:
        return jsonify({'error': '该邮箱已添加'}), 400
    if n >= 6:
        return jsonify({'error': '最多绑定 6 个邮箱账号'}), 400
    try:
        info = outlook_start_device(email_)
    except Exception as e:
        return jsonify({'error': str(e) or '无法开始 Microsoft 登录'}), 400
    pending_id = secrets.token_urlsafe(16)
    now = time.time()
    with _outlook_lock:
        stale = [k for k, v in _outlook_pending.items() if now - v.get('created', 0) > 900]
        for k in stale:
            _outlook_pending.pop(k, None)
        _outlook_pending[pending_id] = {
            'email': email_,
            'name': name,
            'device_code': info['device_code'],
            'created': now,
            'expires': now + int(info.get('expires_in') or 900),
        }
    return jsonify({
        'pending_id': pending_id,
        'user_code': info.get('user_code') or '',
        'verification_uri': info.get('verification_uri') or 'https://microsoft.com/devicelogin',
        'verification_uri_complete': info.get('verification_uri_complete') or '',
        'interval': int(info.get('interval') or 5),
        'expires_in': int(info.get('expires_in') or 900),
    })


@app.route('/api/accounts/outlook-oauth/poll', methods=['POST'])
def api_outlook_oauth_poll():
    d = request.get_json(force=True) or {}
    pending_id = (d.get('pending_id') or '').strip()
    with _outlook_lock:
        pending = dict(_outlook_pending.get(pending_id) or {})
    if not pending:
        return jsonify({'error': '登录已过期，请重新点「用 Microsoft 登录」'}), 400
    if time.time() > pending.get('expires', 0):
        with _outlook_lock:
            _outlook_pending.pop(pending_id, None)
        return jsonify({'error': '登录已超时，请重新点「用 Microsoft 登录」'}), 400
    info = outlook_poll_device(pending['device_code'])
    err = (info.get('error') or '').lower()
    if err in ('authorization_pending', 'slow_down'):
        return jsonify({'status': 'pending', 'interval': 5 if err == 'slow_down' else 0})
    if not info.get('access_token'):
        with _outlook_lock:
            _outlook_pending.pop(pending_id, None)
        return jsonify({'error': info.get('error_description') or info.get('error') or 'Microsoft 登录失败'}), 400
    refresh = info.get('refresh_token') or ''
    access = info.get('access_token')
    if not refresh:
        return jsonify({'error': 'Microsoft 未返回长期授权，请重试'}), 400
    p = PROVIDERS['outlook']
    email_ = pending['email']
    name = pending.get('name') or ''
    probe = {
        'email': email_,
        'provider': 'outlook',
        'imap_host': p['imap'],
        'imap_port': p['imap_port'],
        'oauth_refresh': refresh,
        'oauth_access': access,
        'password': '',
    }
    try:
        conn = imap_open(probe)
        conn.logout()
    except Exception as e:
        return jsonify({'error': f'已登录 Microsoft，但 IMAP 仍失败：{e}。请确认已开启 IMAP。'}), 400
    try:
        new_id = upsert_mail_account(
            email_, name, 'outlook', p['imap'], p['imap_port'], p['smtp'], p['smtp_port'],
            1 if p['starttls'] else 0, password='', oauth_refresh=refresh
        )
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    with _outlook_lock:
        _outlook_pending.pop(pending_id, None)
        _outlook_token_cache[new_id] = {
            'access': access,
            'exp': time.time() + int(info.get('expires_in') or 3600),
        }
    vault_upsert_account(email_, 'outlook', '', oauth=True)
    dismiss_demo_accounts()
    sync_idle_threads()
    return jsonify({'status': 'ok', 'id': new_id, 'email': email_, 'name': name, 'provider': 'outlook'})


@app.route('/api/accounts/<int:aid>', methods=['PATCH'])
def api_rename_account(aid):
    d = request.get_json(force=True) or {}
    name = (d.get('name') or '').strip()
    db = get_db()
    row = db.execute('SELECT id FROM accounts WHERE id=?', (aid,)).fetchone()
    if not row:
        db.close()
        return jsonify({'error': '账号不存在'}), 404
    db.execute('UPDATE accounts SET name=? WHERE id=?', (name[:40], aid))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'id': aid, 'name': name})


def wipe_account_local(db, aid):
    db.execute('DELETE FROM mails WHERE acc_id=?', (aid,))
    db.execute('DELETE FROM folder_cache WHERE acc_id=?', (aid,))
    db.execute('DELETE FROM folder_state WHERE acc_id=?', (aid,))
    db.execute('DELETE FROM tombstones WHERE acc_id=?', (aid,))
    db.execute('DELETE FROM sync_jobs WHERE acc_id=?', (aid,))
    db.execute('DELETE FROM accounts WHERE id=?', (aid,))


def imap_drop(acc_id):
    with _imap_guard:
        conn = _imap_pool.pop(acc_id, None)
    if not conn:
        return
    def bye(c=conn):
        try:
            c.logout()
        except Exception:
            try:
                c.shutdown()
            except Exception:
                pass
    threading.Thread(target=bye, daemon=True).start()


@app.route('/api/accounts/<int:aid>', methods=['DELETE'])
def api_del_account(aid):
    purge = request.args.get('purge') == '1'
    body = request.get_json(silent=True) or {}
    if body.get('purge'):
        purge = True
    db = get_db()
    try:
        row = db.execute('SELECT id,email,provider FROM accounts WHERE id=?', (aid,)).fetchone()
        if not row:
            return jsonify({'error': '账号不存在'}), 404
        email_ = row['email'] if row else ''
        if is_demo_account(dict(row)):
            setting_set(DEMO_DISMISSED_KEY, '1')
            purge = True
        if purge:
            wipe_account_local(db, aid)
        else:
            db.execute('UPDATE accounts SET active=0 WHERE id=?', (aid,))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({'error': '退出失败：' + str(e)}), 500
    finally:
        db.close()
    cache_bust(aid)
    try:
        imap_drop(aid)
    except Exception:
        pass
    _stop_idle_for_account(aid)
    try:
        vault_drop_account(email_)
    except Exception:
        pass
    return jsonify({'ok': True, 'purged': purge})


def cache_get(key):
    hit = _mem_cache.get(key)
    if not hit:
        return None
    if time.time() - hit[0] > CACHE_TTL:
        _mem_cache.pop(key, None)
        return None
    return hit[1]


def cache_set(key, value):
    _mem_cache[key] = (time.time(), value)


def cache_bust(acc_id):
    prefix = f'{acc_id}|'
    for key in list(_mem_cache):
        if key.startswith(prefix):
            _mem_cache.pop(key, None)


def folder_id_by_label(folders, label):
    for f in folders:
        if f.get('label') == label:
            return f.get('id')
    return None


def ensure_pack(acc, folder, q, flagged, fresh, conn_ref):
    cache_key = f"{acc['id']}|mails|{folder}|{int(flagged)}|{q}"
    pack = None if fresh else cache_get(cache_key)
    if pack is None:
        if conn_ref[0] is None:
            conn_ref[0] = imap_open(acc)
        if not select_folder(conn_ref[0], folder, readonly=True):
            pack = {'uids': [], 'items': [], 'scanned': 0}
            cache_set(cache_key, pack)
            return pack, cache_key
        typ, data = imap_search(conn_ref[0], q, flagged=flagged)
        uids = data[0].split() if data and data[0] else []
        uids.reverse()
        pack = {'uids': uids, 'items': [], 'scanned': 0}
    return pack, cache_key


def fill_pack(acc, pack, cache_key, folder, need_items, conn_ref):
    while pack['scanned'] < len(pack['uids']) and len(pack['items']) < need_items:
        take = min(HEADER_CHUNK, need_items - len(pack['items']))
        chunk = pack['uids'][pack['scanned']:pack['scanned'] + take]
        if not chunk:
            break
        if conn_ref[0] is None:
            conn_ref[0] = imap_open(acc)
        if not select_folder(conn_ref[0], folder, readonly=True):
            break
        pack['items'].extend(fetch_header_chunk(conn_ref[0], chunk))
        pack['scanned'] += len(chunk)
    cache_set(cache_key, pack)


def load_pack(acc, folder, q, flagged, fresh, need):
    conn_ref = [None]
    try:
        pack, key = ensure_pack(acc, folder, q, flagged, fresh, conn_ref)
        fill_pack(acc, pack, key, folder, need, conn_ref)
        return pack, key
    finally:
        if conn_ref[0] is not None:
            try:
                conn_ref[0].logout()
            except Exception:
                pass


def pack_can_deep(pack, cap):
    return pack['scanned'] < min(cap, len(pack['uids']))


def mark_mine(items, email, folder):
    em = (email or '').lower()
    for it in items:
        addr = (it.get('from_addr') or '').lower()
        it['folder'] = folder
        it['mine'] = bool(addr and addr == em)
        it['ts'] = mail_timestamp(it)


def is_reply_subject(s):
    return bool(re.match(
        r'^(re|fwd|fw|回复|答复|答覆|回覆|转发)(\s*\[\d+\])?\s*[:：]',
        (s or '').strip(), re.I))


def is_reply_msg(m):
    if (m.get('in_reply_to') or '').strip():
        return True
    return is_reply_subject(m.get('subject'))


def thread_originated_me(msgs):
    msgs = sorted(msgs, key=lambda m: m.get('ts') or 0)
    if not msgs:
        return False
    oldest = msgs[0]
    if any(not m.get('mine') for m in msgs):
        return bool(oldest.get('mine'))
    if any(is_reply_msg(m) for m in msgs):
        return False
    return bool(oldest.get('mine'))


def pair_threads(inbox_items, sent_items, mine_email, sent_id):
    mark_mine(inbox_items, mine_email, 'INBOX')
    mark_mine(sent_items, mine_email, sent_id)
    groups = {}
    for it in inbox_items + sent_items:
        groups.setdefault(thread_group_key(it), []).append(it)
    inbox_out, sent_out = [], []
    sent_unread = False
    for key, msgs in groups.items():
        msgs = sorted(msgs, key=lambda m: m.get('ts') or 0)
        newest, oldest = msgs[-1], msgs[0]
        originated_me = thread_originated_me(msgs)
        seen, parts = set(), []
        for m in msgs:
            token = f"{m.get('folder')}|{m['uid']}"
            if token in seen:
                continue
            seen.add(token)
            parts.append({'folder': m.get('folder') or 'INBOX', 'uid': m['uid']})
        unread = any(m.get('unread') and m.get('folder') == 'INBOX' for m in msgs)
        row = dict(newest)
        row['subject'] = display_subject(oldest.get('subject') or newest.get('subject'))
        row['thread_key'] = key
        row['thread_count'] = len(parts)
        row['thread_uids'] = [p['uid'] for p in parts]
        row['thread_parts'] = parts
        row['unread'] = unread
        row['starred'] = any(m.get('starred') for m in msgs)
        row['date'] = newest.get('date')
        row['ts'] = newest.get('ts') or mail_timestamp(newest)
        row['originated_me'] = originated_me
        if originated_me:
            if unread:
                sent_unread = True
            sent_out.append(row)
        else:
            inbox_out.append(row)
    inbox_out.sort(key=lambda t: t.get('ts') or 0, reverse=True)
    sent_out.sort(key=lambda t: t.get('ts') or 0, reverse=True)
    return inbox_out, sent_out, sent_unread


# ---------- 本地库 / IMAP 复用 ----------
_imap_pool = {}
_imap_locks = {}
_imap_guard = threading.Lock()
_sync_errors = []
_syncing = set()


def acc_lock(acc_id):
    with _imap_guard:
        lock = _imap_locks.get(acc_id)
        if lock is None:
            lock = threading.RLock()
            _imap_locks[acc_id] = lock
        return lock


def imap_get(acc, ping=True, timeout=30):
    aid = acc['id']
    conn = _imap_pool.get(aid)
    if conn is not None:
        if not ping:
            return conn
        try:
            typ, _ = conn.noop()
            if typ == 'OK':
                return conn
        except Exception:
            try:
                conn.logout()
            except Exception:
                pass
            _imap_pool.pop(aid, None)
    conn = imap_open(acc, timeout=timeout)
    _imap_pool[aid] = conn
    return conn


def _with_imap_locked(acc, fn, ping=True, retry=True, timeout=30):
    """真正执行 IMAP 操作。调用方保证同一账号串行（见 with_imap）。"""
    lock = acc_lock(acc['id'])
    # 队列已经保证同一账号串行，这里的锁只是兜底；
    # 以前这里只等 8 秒就抛 "imap busy"，会让界面弹出"同步失败"。
    got = lock.acquire(timeout=300)
    if not got:
        raise TimeoutError('账号连接被长时间占用，请稍后再试')
    try:
        try:
            return fn(imap_get(acc, ping=ping, timeout=timeout))
        except Exception:
            if not retry:
                raise
            try:
                old = _imap_pool.pop(acc['id'], None)
                if old:
                    old.logout()
            except Exception:
                pass
            return fn(imap_get(acc, ping=True, timeout=timeout))
    finally:
        lock.release()


def with_imap(acc, fn, ping=True, retry=True, timeout=30, priority=None):
    """所有账号相关的 IMAP 操作都排进该账号的队列，串行执行。

    · 同一账号同一时刻只有一个操作在用连接，不再互相抢锁；
    · 默认优先级是"用户正在等待"（点开文件夹、手动刷新）；
    · 后台任务传 sync_queue.PRIORITY_BACKGROUND，会主动给用户操作让位。
    """
    if priority is None:
        priority = sync_queue.PRIORITY_USER
    return sync_queue.run(
        acc['id'],
        lambda: _with_imap_locked(acc, fn, ping=ping, retry=retry, timeout=timeout),
        priority=priority,
    )


def db_save_folders(acc_id, folders):
    db = get_db()
    db.execute('DELETE FROM folder_cache WHERE acc_id=?', (acc_id,))
    for f in folders:
        db.execute('INSERT INTO folder_cache(acc_id, folder_id, label) VALUES(?,?,?)',
                   (acc_id, f['id'], f.get('label') or ''))
    db.commit()
    db.close()


def db_load_folders(acc_id):
    db = get_db()
    rows = db.execute('SELECT folder_id, label FROM folder_cache WHERE acc_id=?', (acc_id,)).fetchall()
    db.close()
    return [{'id': r['folder_id'], 'label': r['label']} for r in rows]


def db_folder_label(acc_id, label):
    for f in db_load_folders(acc_id):
        if f.get('label') == label:
            return f.get('id')
    return None


def pending_delete_keys(acc_id):
    db = get_db()
    rows = db.execute(
        "SELECT folder, uid FROM sync_jobs WHERE acc_id=? AND kind IN ('delete','purge')",
        (acc_id,)).fetchall()
    stones = db.execute('SELECT folder, uid FROM tombstones WHERE acc_id=?', (acc_id,)).fetchall()
    db.close()
    keys = {(r['folder'], str(r['uid'])) for r in rows}
    keys.update((r['folder'], str(r['uid'])) for r in stones)
    return keys


def add_tombstone(acc_id, folder, uid):
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO tombstones(acc_id,folder,uid,created) VALUES(?,?,?,?)',
        (acc_id, folder, str(uid), time.time()))
    db.commit()
    db.close()


def clear_tombstones_uid(acc_id, uid):
    db = get_db()
    db.execute('DELETE FROM tombstones WHERE acc_id=? AND uid=?', (acc_id, str(uid)))
    db.commit()
    db.close()


def clear_pending(acc_id, folder, uid):
    db = get_db()
    db.execute('UPDATE mails SET pending_sync=? WHERE acc_id=? AND folder=? AND uid=?',
               ('', acc_id, folder, str(uid)))
    db.commit()
    db.close()


def upsert_headers(acc, folder, items):
    email = (acc.get('email') or '').lower()
    skip = pending_delete_keys(acc['id'])
    db = get_db()
    for it in items:
        uid = str(it['uid'])
        if (folder, uid) in skip:
            continue
        addr = (it.get('from_addr') or '').lower()
        ts = mail_timestamp(it)
        key = thread_group_key(it, folder)
        db.execute(
            """INSERT INTO mails(acc_id,folder,uid,subject,sender,from_addr,recipients,date_raw,ts,unread,starred,mine,thread_key,in_reply_to)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(acc_id,folder,uid) DO UPDATE SET
                 subject=excluded.subject, sender=excluded.sender, from_addr=excluded.from_addr,
                 recipients=excluded.recipients, date_raw=excluded.date_raw, ts=excluded.ts,
                 unread=CASE WHEN mails.pending_sync!='' THEN mails.unread ELSE excluded.unread END,
                 starred=CASE WHEN mails.pending_sync!='' THEN mails.starred ELSE excluded.starred END,
                 mine=excluded.mine, thread_key=excluded.thread_key,
                 in_reply_to=CASE WHEN excluded.in_reply_to!='' THEN excluded.in_reply_to ELSE mails.in_reply_to END""",
            (acc['id'], folder, uid, it.get('subject') or '', it.get('from') or '',
             it.get('from_addr') or '', it.get('to') or '', it.get('date') or '', ts,
             1 if it.get('unread') else 0, 1 if it.get('starred') else 0,
             1 if addr and addr == email else 0, key, (it.get('in_reply_to') or '').strip()))
    db.commit()
    db.close()


def save_body(acc_id, folder, uid, data):
    db = get_db()
    db.execute(
        """UPDATE mails SET body=?, html=?, attachments=?, recipients=?, cc=?, message_id=?, in_reply_to=?, has_body=1
           WHERE acc_id=? AND folder=? AND uid=?""",
        (data.get('body') or '', data.get('html') or '',
         json.dumps(data.get('attachments') or [], ensure_ascii=False),
         data.get('to') or '', data.get('cc') or '', data.get('message_id') or '',
         data.get('in_reply_to') or '',
         acc_id, folder, str(uid)))
    db.commit()
    db.close()


def load_mail_row(acc_id, folder, uid):
    db = get_db()
    row = db.execute('SELECT * FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                     (acc_id, folder, str(uid))).fetchone()
    db.close()
    return dict(row) if row else None


def load_folder_mails(acc_id, folder, include_deleted=False):
    db = get_db()
    if folder == STARRED_ID:
        rows = db.execute(
            'SELECT * FROM mails WHERE acc_id=? AND starred=1 AND local_deleted=0',
            (acc_id,)).fetchall()
    else:
        q = 'SELECT * FROM mails WHERE acc_id=? AND folder=?'
        args = [acc_id, folder]
        if not include_deleted:
            q += ' AND local_deleted=0'
        rows = db.execute(q, args).fetchall()
    db.close()
    return [dict(r) for r in rows]


def rows_to_items(rows):
    items = []
    for r in rows:
        items.append({
            'uid': r['uid'],
            'subject': r['subject'],
            'from': r['sender'],
            'from_addr': r['from_addr'],
            'date': r['date_raw'] or (time.strftime('%Y/%m/%d %H:%M', time.localtime(r['ts'])) if r.get('ts') else ''),
            'unread': bool(r['unread']),
            'starred': bool(r['starred']),
            'folder': r['folder'],
            'acc': r.get('acc_id') or '',
            'acc_id': r.get('acc_id') or '',
            'mine': bool(r['mine']),
            'ts': r['ts'] or 0,
            'in_reply_to': (r.get('in_reply_to') or '') if hasattr(r, 'get') else '',
        })
    return items


def slim_thread(t):
    return {
        'uid': t.get('uid'),
        'subject': t.get('subject'),
        'from': t.get('from'),
        'from_addr': t.get('from_addr'),
        'date': t.get('date') or (time.strftime('%Y/%m/%d %H:%M', time.localtime(t['ts'])) if t.get('ts') else ''),
        'ts': t.get('ts') or 0,
        'unread': bool(t.get('unread')),
        'starred': bool(t.get('starred')),
        'thread_key': t.get('thread_key'),
        'thread_count': t.get('thread_count') or 1,
        'thread_uids': t.get('thread_uids') or [t.get('uid')],
        'thread_parts': t.get('thread_parts') or [],
        'originated_me': bool(t.get('originated_me')),
    }


def resolve_mail_folder(acc_id, folder):
    folder = folder or 'INBOX'
    if folder == STARRED_ID or folder_has_mail(acc_id, folder):
        return folder
    label = folder_label(folder)
    cached = db_folder_label(acc_id, label)
    if cached and (cached == folder or folder_has_mail(acc_id, cached)):
        return cached
    for name in FOLDER_ALIASES.get(label, []):
        if folder_has_mail(acc_id, name):
            return name
    return cached or folder


def threads_from_db(acc, folder, q=''):
    folder = resolve_mail_folder(acc['id'], folder)
    sent_id = db_folder_label(acc['id'], '已发送')
    if sent_id:
        sent_id = resolve_mail_folder(acc['id'], sent_id)
    trash_id = db_folder_label(acc['id'], '已删除')
    if folder == STARRED_ID:
        items = rows_to_items(load_folder_mails(acc['id'], STARRED_ID))
        threads = group_threads(items)
    elif trash_id and folder == trash_id:
        items = rows_to_items(load_folder_mails(acc['id'], trash_id, include_deleted=True))
        threads = group_threads(items, trash_id)
        for t in threads:
            t['unread'] = False
    elif (not q) and folder in ('INBOX', sent_id) and sent_id:
        inbox_items = rows_to_items(load_folder_mails(acc['id'], 'INBOX'))
        sent_items = rows_to_items(load_folder_mails(acc['id'], sent_id))
        inbox_out, sent_out, sent_unread = pair_threads(inbox_items, sent_items, acc.get('email'), sent_id)
        cache_set(f"{acc['id']}|sent_unread", sent_unread)
        threads = inbox_out if folder == 'INBOX' else sent_out
    else:
        items = rows_to_items(load_folder_mails(acc['id'], folder))
        threads = group_threads(items, folder)
    if q:
        ql = q.lower()
        threads = [t for t in threads if ql in (t.get('subject') or '').lower()
                   or ql in (t.get('from') or '').lower()]
    return [slim_thread(t) for t in threads]


def folder_has_mail(acc_id, folder):
    db = get_db()
    if folder == STARRED_ID:
        n = db.execute('SELECT COUNT(*) FROM mails WHERE acc_id=? AND starred=1 AND local_deleted=0',
                       (acc_id,)).fetchone()[0]
    else:
        n = db.execute('SELECT COUNT(*) FROM mails WHERE acc_id=? AND folder=? AND local_deleted=0',
                       (acc_id, folder)).fetchone()[0]
    db.close()
    return n > 0


def set_folder_state(acc_id, folder, last_uid, oldest_uid=None):
    db = get_db()
    row = db.execute('SELECT last_uid, oldest_uid, caught_up FROM folder_state WHERE acc_id=? AND folder=?',
                     (acc_id, folder)).fetchone()
    prev_old = int(row['oldest_uid']) if row and row['oldest_uid'] else 0
    caught = int(row['caught_up']) if row else 0
    low = int(oldest_uid) if oldest_uid else prev_old
    db.execute(
        """INSERT INTO folder_state(acc_id,folder,last_uid,last_sync,oldest_uid,caught_up)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(acc_id,folder) DO UPDATE SET
             last_uid=excluded.last_uid, last_sync=excluded.last_sync,
             oldest_uid=excluded.oldest_uid, caught_up=folder_state.caught_up""",
        (acc_id, folder, int(last_uid or 0), time.time(), low, caught))
    db.commit()
    db.close()


def get_last_uid(acc_id, folder):
    db = get_db()
    row = db.execute('SELECT last_uid FROM folder_state WHERE acc_id=? AND folder=?',
                     (acc_id, folder)).fetchone()
    db.close()
    return int(row['last_uid']) if row else 0


def uid_int(u):
    s = u.decode() if isinstance(u, (bytes, bytearray)) else str(u or '')
    digits = ''.join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else 0


def imap_day(ts):
    d = time.gmtime(float(ts) if ts else time.time())
    return f"{d.tm_mday:02d}-{_IMAP_MON[d.tm_mon - 1]}-{d.tm_year}"


def imap_uids_since(conn, days=RECENT_DAYS):
    since = imap_day(time.time() - days * 86400)
    try:
        typ, data = conn.uid('search', None, 'SINCE', since)
        if typ == 'OK' and data and data[0]:
            return list(reversed(data[0].split()))
        if typ == 'OK':
            return []
    except Exception as e:
        imap_log(None, 'SEARCH SINCE err', e)
    return None


def folder_oldest_ts(acc_id, folder):
    db = get_db()
    row = db.execute(
        'SELECT MIN(ts) FROM mails WHERE acc_id=? AND folder=? AND ts>0',
        (acc_id, folder)).fetchone()
    db.close()
    return float(row[0]) if row and row[0] else 0


def known_uids(acc_id, folder):
    db = get_db()
    rows = db.execute('SELECT uid FROM mails WHERE acc_id=? AND folder=?', (acc_id, folder)).fetchall()
    db.close()
    return {str(r['uid']) for r in rows}


def get_oldest_uid(acc_id, folder):
    db = get_db()
    row = db.execute('SELECT oldest_uid FROM folder_state WHERE acc_id=? AND folder=?',
                     (acc_id, folder)).fetchone()
    val = int(row['oldest_uid']) if row and row['oldest_uid'] else 0
    if not val:
        r = db.execute(
            "SELECT MIN(CAST(uid AS INTEGER)) FROM mails WHERE acc_id=? AND folder=?",
            (acc_id, folder)).fetchone()
        val = int(r[0] or 0) if r else 0
    db.close()
    return val


def folder_caught_up(acc_id, folder):
    db = get_db()
    row = db.execute('SELECT caught_up FROM folder_state WHERE acc_id=? AND folder=?',
                     (acc_id, folder)).fetchone()
    db.close()
    return bool(row and row['caught_up'])


def set_caught_up(acc_id, folder, flag):
    db = get_db()
    db.execute(
        """INSERT INTO folder_state(acc_id,folder,last_uid,last_sync,oldest_uid,caught_up)
           VALUES(?,?,0,?,?,?)
           ON CONFLICT(acc_id,folder) DO UPDATE SET caught_up=excluded.caught_up""",
        (acc_id, folder, time.time(), 0, 1 if flag else 0))
    db.commit()
    db.close()


def enqueue_job(acc_id, kind, folder, uid, extra=''):
    acc = get_account(acc_id)
    if is_demo_account(acc):
        return
    db = get_db()
    db.execute('DELETE FROM sync_jobs WHERE acc_id=? AND kind=? AND folder=? AND uid=?',
               (acc_id, kind, folder or '', str(uid or '')))
    db.execute('INSERT INTO sync_jobs(acc_id,kind,folder,uid,extra,created) VALUES(?,?,?,?,?,?)',
               (acc_id, kind, folder or '', str(uid or ''), extra or '', time.time()))
    db.commit()
    db.close()


def push_sync_error(msg):
    with _imap_guard:
        _sync_errors.append(str(msg))
        if len(_sync_errors) > 20:
            del _sync_errors[:-20]


def pop_sync_errors():
    with _imap_guard:
        out = _sync_errors[:]
        _sync_errors.clear()
        return out


def sync_folder_headers(acc, folder, limit=None, priority=None):
    if not folder or folder == STARRED_ID or is_demo_account(acc):
        return

    def work(conn):
        if not select_folder(conn, folder, readonly=True, acc=acc):
            imap_log(acc, 'SYNC abort: select failed', folder)
            return
        last = get_last_uid(acc['id'], folder)
        if last:
            to_fetch = peek_new_uids(conn, last)
            imap_log(acc, 'SYNC peek', folder, 'last', last, 'fresh', len(to_fetch),
                     'exists', getattr(conn, '_pumail_exists', '?'))
        else:
            recent = imap_uids_since(conn)
            if recent is not None:
                to_fetch = recent[:limit] if limit else recent[:FIRST_BATCH]
                imap_log(acc, 'SYNC since', RECENT_DAYS, 'days', len(to_fetch))
            else:
                to_fetch = list(reversed(imap_all_uids(conn)))[:limit or FIRST_BATCH]
                imap_log(acc, 'SYNC fallback-all', folder, 'take', len(to_fetch))
        parsed_n = 0
        for i in range(0, len(to_fetch), HEADER_CHUNK):
            chunk = to_fetch[i:i + HEADER_CHUNK]
            parsed = fetch_header_chunk(conn, chunk)
            parsed_n += len(parsed)
            upsert_headers(acc, folder, parsed)
        imap_log(acc, 'SYNC upsert', folder, 'fetch', len(to_fetch), 'parsed', parsed_n)
        if to_fetch:
            top = max(last, max((uid_int(u) for u in to_fetch), default=last))
            fetched_low = min((uid_int(u) for u in to_fetch), default=top)
            old = get_oldest_uid(acc['id'], folder)
            set_folder_state(acc['id'], folder, top, oldest_uid=old or fetched_low)

    with_imap(acc, work, priority=priority)
    # 记下最后一次同步时间，供"新鲜度"判断使用
    mark_folder_synced(acc['id'], folder)


def fetch_older_headers(acc, folder, limit=PER_PAGE):
    if not folder or folder == STARRED_ID:
        return []
    out = []

    def work(conn):
        nonlocal out
        if not select_folder(conn, folder, readonly=True):
            set_caught_up(acc['id'], folder, True)
            return
        uids = list(reversed(imap_all_uids(conn)))
        oldest = get_oldest_uid(acc['id'], folder)
        have = known_uids(acc['id'], folder)
        older = [u for u in uids if (not oldest or uid_int(u) < oldest) and str(uid_int(u)) not in have]
        if not older:
            before_ts = folder_oldest_ts(acc['id'], folder) or time.time()
            typ2, data2 = conn.uid('search', None, 'BEFORE', imap_day(before_ts))
            found = data2[0].split() if typ2 == 'OK' and data2 and data2[0] else []
            older = [u for u in reversed(found) if str(uid_int(u)) not in have]
        if not older:
            before_ts = (folder_oldest_ts(acc['id'], folder) or time.time()) - 86400
            typ2, data2 = conn.uid('search', None, 'BEFORE', imap_day(before_ts))
            found = data2[0].split() if typ2 == 'OK' and data2 and data2[0] else []
            older = [u for u in reversed(found) if str(uid_int(u)) not in have]
        chunk = older[:limit]
        if not chunk:
            set_caught_up(acc['id'], folder, True)
            return
        items = []
        for i in range(0, len(chunk), HEADER_CHUNK):
            items.extend(fetch_header_chunk(conn, chunk[i:i + HEADER_CHUNK]))
        for it in items:
            it['folder'] = folder
            it['ts'] = mail_timestamp(it)
        if items:
            upsert_headers(acc, folder, items)
        low = min(uid_int(u) for u in chunk)
        set_folder_state(acc['id'], folder, get_last_uid(acc['id'], folder) or uid_int(uids[0]), oldest_uid=low)
        if len(chunk) < limit:
            set_caught_up(acc['id'], folder, True)
        out = items

    with_imap(acc, work)
    return out


GAIA_DAYS = 10
GAIA_FIRST_KEY = 'gaia_first_done'


def iter_accounts():
    db = get_db()
    rows = db.execute(
        'SELECT id FROM accounts WHERE COALESCE(active,1)=1 ORDER BY sort_order, id'
    ).fetchall()
    ids = [r['id'] for r in rows]
    db.close()
    out = []
    for aid in ids:
        acc = get_account(aid)
        if acc:
            out.append(acc)
    return out


def gaia_kind_folder(acc, kind):
    if kind == 'INBOX':
        return 'INBOX'
    label = '已发送' if kind == 'SENT' else '草稿' if kind == 'DRAFTS' else ''
    if not label:
        return None
    fid = db_folder_label(acc['id'], label)
    return fid


def sync_folder_since(acc, folder, days=GAIA_DAYS):
    if not folder or folder == STARRED_ID:
        return

    def work(conn):
        if not select_folder(conn, folder, readonly=True, acc=acc):
            return
        uids = imap_uids_since(conn, days)
        if uids is None:
            uids = list(reversed(imap_all_uids(conn)))[:FIRST_BATCH]
        last = get_last_uid(acc['id'], folder)
        if last:
            newer = [u for u in uids if uid_int(u) > last]
            uids = newer or uids[:20]
        for i in range(0, len(uids), HEADER_CHUNK):
            parsed = fetch_header_chunk(conn, uids[i:i + HEADER_CHUNK])
            upsert_headers(acc, folder, parsed)
        if uids:
            top = max(uid_int(u) for u in uids)
            old = get_oldest_uid(acc['id'], folder)
            set_folder_state(acc['id'], folder, max(top, last or 0), oldest_uid=old)

    with_imap(acc, work)


def sync_gaia_account(acc, kind, first):
    if is_demo_account(acc):
        return
    try:
        ensure_folders(acc)
    except Exception as exc:
        push_sync_error((acc.get('email') or '') + ' 文件夹失败：' + str(exc))
        return
    folder = gaia_kind_folder(acc, kind)
    if not folder:
        return
    try:
        if first:
            sync_folder_headers(acc, folder, limit=None)
            for _ in range(24):
                if folder_caught_up(acc['id'], folder):
                    break
                extra = fetch_older_headers(acc, folder, PER_PAGE)
                if not extra:
                    break
        else:
            sync_folder_since(acc, folder, GAIA_DAYS)
    except Exception as exc:
        push_sync_error((acc.get('email') or '') + ' 扫描失败：' + str(exc))


def gaia_threads(kind, q='', recent_only=False):
    cutoff = time.time() - GAIA_DAYS * 86400 if recent_only else 0
    out = []
    for acc in iter_accounts():
        if is_demo_account(acc):
            continue
        folder = gaia_kind_folder(acc, kind)
        if not folder:
            continue
        for t in threads_from_db(acc, folder, q):
            ts = t.get('ts') or 0
            if cutoff and ts and ts < cutoff:
                continue
            item = dict(t)
            item['acc'] = acc['id']
            item['acc_email'] = acc.get('email') or ''
            item['folder'] = t.get('folder') or folder
            out.append(item)
    out.sort(key=lambda t: (0 if t.get('unread') else 1, -(t.get('ts') or 0)))
    return out


def gaia_folder_unread(kind, recent_only=False):
    """统计总览中实际显示的未读会话数，而不是会话内邮件总数。"""
    if kind == 'INBOX':
        return sum(gaia_unread_per_account(recent_only).values())
    return unread_thread_count(gaia_threads(kind, recent_only=recent_only), recent_only)


def sort_threads_unread_first(threads):
    return sorted(threads, key=lambda t: (0 if t.get('unread') else 1, -(t.get('ts') or 0)))


def unread_thread_count(threads, recent_only=False, limit=None):
    cutoff = time.time() - GAIA_DAYS * 86400 if recent_only else 0
    visible = sort_threads_unread_first(threads)
    if limit is not None:
        visible = visible[:limit]
    return sum(1 for thread in visible
               if thread.get('unread') and (not cutoff or not thread.get('ts') or thread['ts'] >= cutoff))


def folder_unread_count(acc_id, folder):
    """返回当前列表第一页中实际显示的未读会话数。"""
    acc = get_account(acc_id)
    return unread_thread_count(threads_from_db(acc, folder), limit=PER_PAGE) if acc else 0


def gaia_unread_per_account(recent_only=False):
    """返回每个账号收件箱中实际显示的未读会话数。"""
    result = {}
    for acc in iter_accounts():
        if is_demo_account(acc):
            continue
        folder = gaia_kind_folder(acc, 'INBOX')
        if not folder:
            continue
        result[acc['id']] = unread_thread_count(threads_from_db(acc, folder), recent_only, PER_PAGE)
    return result


def unread_summary(recent_only=False):
    """供所有未读徽标共用的收件箱未读会话汇总。"""
    per_account = gaia_unread_per_account(recent_only)
    return {'per_account': per_account, 'inbox': sum(per_account.values())}


def fetch_one_body(acc, folder, uid):
    if is_demo_account(acc):
        row = load_mail_row(acc['id'], folder, uid)
        return row_to_message(row, acc) if row else None

    def work(conn):
        if not select_folder(conn, folder, readonly=True):
            return None
        typ, d = conn.uid('fetch', str(uid).encode(), '(RFC822 FLAGS)')
        if not d or not d[0] or not isinstance(d[0], tuple):
            return None
        raw = d[0][1]
        flags = d[0][0] if isinstance(d[0][0], bytes) else b''
        msg = message_from_bytes(raw)
        nm, addr = parseaddr(dec(msg.get('From')))
        body, html_out = get_bodies(msg)
        data = {
            'uid': str(uid),
            'folder': folder,
            'subject': dec(msg.get('Subject')) or '(无主题)',
            'from': (nm + ' <' + addr + '>') if nm else addr,
            'from_addr': addr,
            'to': format_addr_list(msg.get('To')),
            'cc': format_addr_list(msg.get('Cc')),
            'date': dec(msg.get('Date')) or '',
            'body': body,
            'html': html_out,
            'attachments': get_attachments(msg),
            'starred': b'\\Flagged' in flags,
            'unread': b'\\Seen' not in flags,
            'message_id': (msg.get('Message-ID') or '').strip(),
            'in_reply_to': (dec(msg.get('In-Reply-To')) or '').strip(),
            'mine': (addr or '').lower() == (acc.get('email') or '').lower(),
        }
        if (folder, str(uid)) not in pending_delete_keys(acc['id']):
            upsert_headers(acc, folder, [data])
            save_body(acc['id'], folder, uid, data)
        return data

    return with_imap(acc, work)


def row_to_message(row, acc):
    atts = []
    try:
        atts = json.loads(row.get('attachments') or '[]')
    except Exception:
        atts = []
    return {
        'uid': row['uid'],
        'folder': row['folder'],
        'subject': row['subject'],
        'from': row['sender'] or row.get('from_addr') or '',
        'from_addr': row.get('from_addr') or '',
        'to': row.get('recipients') or '',
        'cc': row.get('cc') or '',
        'date': row.get('date_raw') or '',
        'body': row.get('body') or '',
        'html': sanitize_html(row.get('html') or '') if row.get('html') else '',
        'attachments': atts,
        'starred': bool(row.get('starred')),
        'unread': bool(row.get('unread')),
        'message_id': row.get('message_id') or '',
        'mine': bool(row.get('mine')),
        'has_body': bool(row.get('has_body')),
    }


def mark_local(acc_id, folder, uid, **fields):
    if not fields:
        return
    sets = ', '.join(f'{k}=?' for k in fields)
    vals = list(fields.values()) + [acc_id, folder, str(uid)]
    db = get_db()
    db.execute(f'UPDATE mails SET {sets} WHERE acc_id=? AND folder=? AND uid=?', vals)
    db.commit()
    db.close()


def drain_jobs(acc):
    db = get_db()
    jobs = [dict(r) for r in db.execute(
        'SELECT * FROM sync_jobs WHERE acc_id=? ORDER BY id LIMIT 30', (acc['id'],)).fetchall()]
    db.close()
    for job in jobs:
        try:
            def work(conn, job=job):
                kind, folder, uid = job['kind'], job['folder'] or 'INBOX', job['uid']
                if kind == 'star':
                    store_flag(conn, folder, uid, '\\Flagged', True)
                elif kind == 'unstar':
                    store_flag(conn, folder, uid, '\\Flagged', False)
                elif kind == 'seen':
                    store_flag(conn, folder, uid, '\\Seen', True)
                elif kind == 'purge':
                    if select_folder(conn, folder, readonly=False):
                        conn.uid('store', str(uid).encode(), '+FLAGS', '(\\Deleted)')
                        conn.expunge()
                elif kind == 'delete':
                    ok = move_mail(conn, folder, uid, ('Trash', 'Deleted Messages', '已删除', 'Deleted'))
                    if not ok:
                        if select_folder(conn, folder, readonly=False):
                            conn.uid('store', str(uid).encode(), '+FLAGS', '(\\Deleted)')
                            conn.expunge()
                elif kind == 'restore':
                    dest = (job.get('extra') or 'INBOX').strip() or 'INBOX'
                    move_mail(conn, folder, uid, (dest, 'INBOX', 'Inbox', '收件箱'))
                return True

            with_imap(acc, work)
            add_tombstone(acc['id'], job['folder'] or 'INBOX', job['uid']) if job['kind'] in ('delete', 'purge') else None
            clear_pending(acc['id'], job['folder'] or 'INBOX', job['uid'])
            db = get_db()
            db.execute('DELETE FROM sync_jobs WHERE id=?', (job['id'],))
            db.commit()
            db.close()
        except Exception as e:
            db = get_db()
            db.execute('UPDATE sync_jobs SET tries=tries+1, error=? WHERE id=?', (str(e), job['id']))
            db.commit()
            db.close()
            push_sync_error('同步失败：' + str(e))
            if job['kind'] in ('delete', 'purge'):
                add_tombstone(acc['id'], job['folder'] or 'INBOX', job['uid'])


def prefetch_unread_bodies(acc, folder, limit=8):
    db = get_db()
    rows = db.execute(
        """SELECT folder, uid FROM mails
           WHERE acc_id=? AND unread=1 AND has_body=0 AND local_deleted=0
           AND (?='' OR folder=?)
           ORDER BY ts DESC LIMIT ?""",
        (acc['id'], folder or '', folder or '', limit)).fetchall()
    db.close()
    for r in rows:
        try:
            fetch_one_body(acc, r['folder'], r['uid'])
        except Exception:
            break


def background_sync(acc, folder=None, force=True):
    if is_demo_account(acc):
        return
    key = f"{acc['id']}|{folder or '*'}"
    with _imap_guard:
        if key in _syncing:
            return
        _syncing.add(key)

    def run():
        try:
            folders = db_load_folders(acc['id'])
            sent_id = db_folder_label(acc['id'], '已发送')
            targets = []
            if folder and folder != STARRED_ID:
                targets.append(folder)
            else:
                targets.append('INBOX')
            drain_jobs(acc)
            seen = set()
            for fol in targets:
                if not fol or fol in seen:
                    continue
                seen.add(fol)
                # force=True 照旧每次都同步（发信/删除等操作后要立刻刷新）；
                # 打开文件夹时传 force=False，只有超过新鲜度阈值才真正联网。
                if force or folder_needs_sync(acc['id'], fol):
                    sync_folder_headers(acc, fol, limit=FIRST_BATCH)
                    backfill_missing_dates(acc, fol)
            prefetch_unread_bodies(acc, folder if folder and folder != STARRED_ID else 'INBOX', limit=12)
        except Exception as e:
            push_sync_error('同步失败：' + str(e))
        finally:
            _syncing.discard(key)

    threading.Thread(target=run, daemon=True).start()


def sort_folders(folders):
    rank = {'收件箱': 0, '已发送': 1, '星标': 2, '草稿': 3, '垃圾邮件': 4, '已删除': 5}
    return sorted(folders or [], key=lambda f: rank.get(f.get('label'), 99))


@app.route('/api/accounts/order', methods=['PATCH'])
def api_reorder_accounts():
    ids = (request.get_json(force=True) or {}).get('ids') or []
    ids = [int(i) for i in ids if str(i).isdigit() or isinstance(i, int)]
    if not ids:
        return jsonify({'error': '缺少账号顺序'}), 400
    db = get_db()
    for i, aid in enumerate(ids, start=1):
        db.execute('UPDATE accounts SET sort_order=? WHERE id=?', (i, aid))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'ids': ids})


def ensure_folders(acc, force=False):
    if is_demo_account(acc):
        folders = db_load_folders(acc['id'])
        if not folders:
            folders = list(DEMO_FOLDERS)
            db_save_folders(acc['id'], folders)
        return sort_folders(folders)
    folders = db_load_folders(acc['id'])
    if folders and not force:
        labels = {f.get('label') for f in folders}
        if needs_imap_id(acc.get('imap_host')) and '已发送' not in labels:
            force = True
        else:
            return sort_folders(folders)

    def work(conn):
        return list_folders(conn)

    folders = sort_folders(with_imap(acc, work))
    db_save_folders(acc['id'], folders)
    cache_set(f"{acc['id']}|folders", folders)
    return folders


# ---------- API：文件夹与邮件 ----------
@app.route('/api/folders')
def api_folders():
    acc = require_account()
    fresh = request.args.get('fresh') == '1'
    folders = ensure_folders(acc, force=fresh)
    sent_unread = bool(cache_get(f"{acc['id']}|sent_unread"))
    db = get_db()
    unread_rows = db.execute(
        """SELECT folder, COUNT(*) FROM mails
           WHERE acc_id=? AND local_deleted=0 AND unread=1
           GROUP BY folder""",
        (acc['id'],)).fetchall()
    unread_map = {r[0]: r[1] for r in unread_rows}
    starred_unread = db.execute(
        """SELECT COUNT(*) FROM mails
           WHERE acc_id=? AND starred=1 AND unread=1 AND local_deleted=0""",
        (acc['id'],)).fetchone()[0]
    if not sent_unread:
        sent_id = db_folder_label(acc['id'], '已发送')
        if sent_id:
            n = db.execute(
                """SELECT COUNT(*) FROM mails WHERE acc_id=? AND folder='INBOX' AND unread=1
                   AND thread_key IN (SELECT thread_key FROM mails WHERE acc_id=? AND folder=? AND mine=1)""",
                (acc['id'], acc['id'], sent_id)).fetchone()[0]
            sent_unread = n > 0
        cache_set(f"{acc['id']}|sent_unread", sent_unread)
    db.close()
    out = []
    for f in folders:
        item = dict(f)
        label = item.get('label')
        fid = item.get('id')
        if label == '已发送':
            item['unread'] = bool(sent_unread or unread_map.get(fid))
        elif label == '星标':
            item['unread'] = bool(starred_unread)
        elif label == '收件箱':
            count = folder_unread_count(acc['id'], 'INBOX')
            item['unread'] = bool(count)
            item['unread_count'] = count
        elif label == '已删除':
            item['unread'] = False
        else:
            item['unread'] = bool(unread_map.get(fid))
        out.append(item)
    background_sync(acc, 'INBOX')
    return jsonify(out)


@app.route('/api/mails')
def api_mails():
    acc = require_account()
    folder = request.args.get('folder') or 'INBOX'
    page_no = max(1, request.args.get('page', 1, type=int))
    q = (request.args.get('q') or '').strip()
    head = request.args.get('head') == '1'
    ensure_folders(acc)
    real_folder = 'INBOX' if folder == STARRED_ID else resolve_mail_folder(acc['id'], folder)
    sent_id = db_folder_label(acc['id'], '已发送')
    if sent_id:
        sent_id = resolve_mail_folder(acc['id'], sent_id)
    fresh = request.args.get('fresh') == '1'
    was_empty = not folder_has_mail(acc['id'], real_folder)
    # 收件箱每次都刷；其它文件夹一天刷一次就够了
    stale = folder_needs_sync(acc['id'], real_folder)
    if (was_empty or fresh or stale) and not is_demo_account(acc):
        imap_log(acc, 'MAILS sync', 'folder', folder, 'empty', was_empty, 'fresh', fresh)
        try:
            if folder != STARRED_ID:
                try:
                    sync_folder_headers(acc, real_folder, limit=FIRST_BATCH)
                except Exception as e:
                    imap_log(acc, 'sync folder err', real_folder, e)
                    push_sync_error('同步失败：' + str(e))
        except Exception as e:
            imap_log(acc, 'sync err', e)
            push_sync_error(str(e))
        if (was_empty and not folder_has_mail(acc['id'], folder)
                and needs_imap_id(acc.get('imap_host'))
                and folder in ('INBOX', sent_id)):
            push_sync_error('网易文件夹仍是空的。已写入 IMAP 调试日志，请确认已开 IMAP 并用授权码。')
    threads = threads_from_db(acc, folder, q)
    # 必须在分页前让未读会话排在前面；否则未读邮件可能落在第 2 页，
    # 前端即使排序也无法显示它。
    threads = sort_threads_unread_first(threads)
    imap_log(acc, 'MAILS out', folder, 'db-threads', len(threads), 'sent_id', sent_id or '-')
    start = 0 if head else (page_no - 1) * PER_PAGE
    take = FIRST_BATCH if head else PER_PAGE
    items = threads[start:start + take]
    local_more = start + len(items) < len(threads)
    if (not local_more and not head and not q
            and folder != STARRED_ID and page_no > 1 and not is_demo_account(acc)):
        try:
            older = fetch_older_headers(acc, real_folder, PER_PAGE)
            extra = [slim_thread(t) for t in group_threads(older, real_folder)]
            if extra:
                items = (items or []) + extra
        except Exception as e:
            return jsonify({'error': str(e)}), 400
    imap_more = (folder != STARRED_ID and not q and not is_demo_account(acc)
                 and not folder_caught_up(acc['id'], real_folder))
    sibling_folder = sibling_items = None
    background_sync(acc, real_folder if folder != STARRED_ID else 'INBOX', force=False)
    unread_count = folder_unread_count(acc['id'], real_folder)
    return jsonify({
        'items': items,
        'total': len(threads),
        'page': page_no,
        'per_page': PER_PAGE,
        'folder': real_folder,
        'has_more': local_more or imap_more,
        'sent_unread': bool(cache_get(f"{acc['id']}|sent_unread")),
        'needs_deep': False,
        'syncing': was_empty or stale or head,
        'sibling_folder': sibling_folder,
        'sibling_items': sibling_items,
        'errors': pop_sync_errors(),
        'unread_count': unread_count,
        'unread_summary': unread_summary(),
    })


@app.route('/api/mail')
def api_mail():
    acc = require_account()
    uid = request.args.get('uid')
    folder = request.args.get('folder') or 'INBOX'
    if folder == STARRED_ID:
        folder = 'INBOX'
    row = load_mail_row(acc['id'], folder, uid)
    if row and row.get('has_body') and not html_needs_refetch(row.get('html') or ''):
        mark_local(acc['id'], folder, uid, unread=0, pending_sync='seen')
        enqueue_job(acc['id'], 'seen', folder, uid)
        background_sync(acc, folder)
        return jsonify(row_to_message(row, acc))
    data = fetch_one_body(acc, folder, uid)
    if not data:
        abort(make_response(jsonify({'error': '邮件不存在'}), 404))
    mark_local(acc['id'], folder, uid, unread=0, pending_sync='seen')
    enqueue_job(acc['id'], 'seen', folder, uid)
    background_sync(acc, folder)
    return jsonify(data)


@app.route('/api/thread')
def api_thread():
    acc = require_account()
    folder = request.args.get('folder') or 'INBOX'
    if folder == STARRED_ID:
        folder = 'INBOX'
    pairs = []
    refs = (request.args.get('refs') or '').strip()
    if refs:
        for part in refs.split(','):
            if '|' not in part:
                continue
            fol, uid = part.split('|', 1)
            fol, uid = fol.strip() or folder, uid.strip()
            if fol and uid:
                pairs.append((fol, uid))
    else:
        for uid in [u.strip() for u in (request.args.get('uids') or '').split(',') if u.strip()]:
            pairs.append((folder, uid))
    if not pairs:
        return jsonify({'messages': []})
    messages = []
    for fol, uid in pairs:
        row = load_mail_row(acc['id'], fol, uid)
        if row and row.get('has_body') and not html_needs_refetch(row.get('html') or ''):
            messages.append(row_to_message(row, acc))
        else:
            data = fetch_one_body(acc, fol, uid)
            if data:
                messages.append(data)
        mark_local(acc['id'], fol, uid, unread=0, pending_sync='seen')
        enqueue_job(acc['id'], 'seen', fol, uid)
    messages.sort(key=mail_timestamp)
    background_sync(acc, folder)
    return jsonify({'messages': messages, 'folder': folder, 'errors': pop_sync_errors()})


@app.route('/api/sync-errors')
def api_sync_errors():
    acc_id = request.args.get('acc', type=int)
    if not get_account(acc_id):
        return jsonify({'errors': []})
    return jsonify({'errors': pop_sync_errors()})


@app.route('/api/attachment')
def api_attachment():
    acc = require_account()
    uid = request.args.get('uid')
    idx = request.args.get('idx', type=int)
    folder = request.args.get('folder') or 'INBOX'

    def work(conn):
        if not select_folder(conn, folder, readonly=True):
            return None
        typ, d = conn.uid('fetch', str(uid).encode(), '(RFC822)')
        if not d or not d[0]:
            return None
        return message_from_bytes(d[0][1])

    msg = with_imap(acc, work)
    if not msg:
        abort(404)
    parts = list(msg.walk())
    if idx is None or idx >= len(parts):
        abort(404)
    part = parts[idx]
    payload = part.get_payload(decode=True) or b''
    name = dec(part.get_filename()) or 'attachment'
    return send_file(io.BytesIO(payload), as_attachment=True, download_name=name)


# ---------- API：写信 / 草稿 / 操作 ----------
def add_attachments(msg, attachments):
    for att in attachments or []:
        raw = att.get('data') or ''
        try:
            payload = base64.b64decode(raw)
        except Exception:
            continue
        name = att.get('name') or 'attachment'
        main, sub = (att.get('type') or 'application/octet-stream').split('/', 1)
        part = MIMEBase(main, sub)
        part.set_payload(payload)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=name)
        msg.attach(part)


def build_msg(acc, to_addrs, subject, body, cc_addrs=None, bcc_addrs=None, attachments=None,
              in_reply_to=None, references=None, html=None):
    msg = MIMEMultipart('mixed')
    display = acc.get('name') or acc['email'].split('@')[0]
    msg['From'] = formataddr((str(Header(display, 'utf-8')), acc['email']))
    msg['To'] = ', '.join(to_addrs)
    if cc_addrs:
        msg['Cc'] = ', '.join(cc_addrs)
    msg['Subject'] = Header(subject or '(无主题)', 'utf-8')
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=acc['email'].split('@')[-1])
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = ((references or '') + ' ' + in_reply_to).strip()
    msg['MIME-Version'] = '1.0'
    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(body or '', 'plain', 'utf-8'))
    cleaned = (html or '').strip()
    if cleaned and cleaned not in ('<br>', '<div><br></div>', '<br/>'):
        style = "font-family: 'Times New Roman', Times, '微软雅黑', 'Microsoft YaHei', sans-serif;"
        alt.attach(MIMEText(f'<div style="{style}">{html}</div>', 'html', 'utf-8'))
    msg.attach(alt)
    add_attachments(msg, attachments)
    return msg


def parse_addrs(s):
    return [a.strip() for a in re.split(r'[,;，；]', s or '') if a.strip()]


@app.route('/api/send', methods=['POST'])
def api_send():
    d = request.get_json(force=True)
    acc = require_account()
    if is_demo_account(acc):
        to_addrs = parse_addrs(d.get('to'))
        if not to_addrs:
            return jsonify({'error': '请填写收件人'}), 400
        save_demo_sent(acc, d)
        return jsonify({'ok': True, 'simulated': True})
    to_addrs = parse_addrs(d.get('to'))
    cc_addrs = parse_addrs(d.get('cc'))
    bcc_addrs = parse_addrs(d.get('bcc'))
    if not to_addrs:
        return jsonify({'error': '请填写收件人'}), 400
    all_addrs = to_addrs + cc_addrs + bcc_addrs
    msg = build_msg(acc, to_addrs, d.get('subject', ''), d.get('body', ''),
                    cc_addrs, bcc_addrs, d.get('attachments'),
                    d.get('in_reply_to'), d.get('references'), d.get('html'))
    try:
        smtp_send(acc, all_addrs, msg)
        cache_bust(acc['id'])
    except Exception as e:
        imap_log(acc, 'SEND fail', type(e).__name__, e)
        return jsonify({'error': f'发送失败：{e}'}), 400
    sent_id = db_folder_label(acc['id'], '已发送')
    saved = False
    try:
        conn = imap_open(acc)
        aliases = []
        if sent_id:
            aliases.append(sent_id)
        aliases.extend(FOLDER_ALIASES.get('已发送', []))
        stored = append_to(conn, msg, aliases)
        dest = stored or sent_id
        if dest and select_folder(conn, dest, readonly=True, acc=acc):
            uids = list(imap_all_uids(conn))
            if uids:
                upsert_headers(acc, sent_id or dest, fetch_header_chunk(conn, [uids[-1]]))
                saved = True
        conn.logout()
    except Exception as e:
        imap_log(acc, 'send append', e)
    if not saved and sent_id:
        upsert_headers(acc, sent_id, [{
            'uid': 'local-' + str(int(time.time() * 1000)),
            'subject': d.get('subject') or '(无主题)',
            'from': acc.get('name') or acc.get('email') or '',
            'from_addr': acc.get('email') or '',
            'date': time.strftime('%Y/%m/%d %H:%M'),
            'to': d.get('to') or '',
            'unread': False,
            'starred': False,
            'ts': time.time(),
        }])
    return jsonify({'ok': True})


@app.route('/api/draft', methods=['POST'])
def api_draft():
    d = request.get_json(force=True)
    acc = require_account()
    if is_demo_account(acc):
        to_s = (d.get('to') or '').strip()
        subject = (d.get('subject') or '').strip()
        body = (d.get('body') or '').strip()
        html = (d.get('html') or '').strip()
        html_empty = html in ('', '<br>', '<br/>', '<div><br></div>', '<div></div>')
        if not to_s and not subject and not body and html_empty:
            return jsonify({'error': '请先填写收件人、主题或正文'}), 400
        folder = db_folder_label(acc['id'], '草稿') or 'Drafts'
        uid = 'demo-draft-' + str(int(time.time() * 1000))
        upsert_headers(acc, folder, [{
            'uid': uid,
            'subject': subject or '(无主题)',
            'from': acc.get('name') or acc.get('email') or '',
            'from_addr': acc.get('email') or '',
            'date': _demo_fmt(datetime.now()),
            'to': to_s,
            'unread': False,
            'starred': False,
            'ts': time.time(),
        }])
        save_body(acc['id'], folder, uid, {
            'body': d.get('body') or '',
            'html': d.get('html') or '',
            'attachments': [],
            'to': to_s,
            'cc': d.get('cc') or '',
            'message_id': '',
            'in_reply_to': '',
        })
        cache_bust(acc['id'])
        return jsonify({'ok': True, 'local': True, 'simulated': True})
    to_s = (d.get('to') or '').strip()
    subject = (d.get('subject') or '').strip()
    body = (d.get('body') or '').strip()
    html = (d.get('html') or '').strip()
    html_empty = html in ('', '<br>', '<br/>', '<div><br></div>', '<div></div>')
    if not to_s and not subject and not body and html_empty:
        return jsonify({'error': '请先填写收件人、主题或正文'}), 400
    msg = build_msg(acc, parse_addrs(d.get('to')), d.get('subject', ''), d.get('body', ''),
                    parse_addrs(d.get('cc')), parse_addrs(d.get('bcc')), d.get('attachments'),
                    html=d.get('html'))
    draft_id = db_folder_label(acc['id'], '草稿')
    saved = False
    dest = ''
    try:
        conn = imap_open(acc)
        aliases = []
        if draft_id:
            aliases.append(draft_id)
        aliases.extend(FOLDER_ALIASES.get('草稿', []))
        dest = append_to(conn, msg, aliases, flags='\\Draft')
        if dest and select_folder(conn, dest, readonly=True, acc=acc):
            uids = list(imap_all_uids(conn))
            if uids:
                upsert_headers(acc, draft_id or dest, fetch_header_chunk(conn, [uids[-1]]))
                saved = True
        conn.logout()
    except Exception as e:
        imap_log(acc, 'draft append', e)
    if not saved:
        folder = draft_id or 'Drafts'
        uid = 'local-draft-' + str(int(time.time() * 1000))
        upsert_headers(acc, folder, [{
            'uid': uid,
            'subject': subject or '(无主题)',
            'from': acc.get('name') or acc.get('email') or '',
            'from_addr': acc.get('email') or '',
            'date': time.strftime('%Y/%m/%d %H:%M'),
            'to': to_s,
            'unread': False,
            'starred': False,
            'ts': time.time(),
        }])
        save_body(acc['id'], folder, uid, {
            'body': d.get('body') or '',
            'html': d.get('html') or '',
            'attachments': [],
            'to': to_s,
            'cc': d.get('cc') or '',
            'message_id': '',
            'in_reply_to': '',
        })
        saved = True
    cache_bust(acc['id'])
    return jsonify({'ok': True, 'local': not dest})


def store_flag(conn, folder, uid, flag, on):
    if folder == STARRED_ID:
        folder = 'INBOX'
    if not select_folder(conn, folder, readonly=False):
        raise RuntimeError('无法打开文件夹')
    verb = '+FLAGS' if on else '-FLAGS'
    typ, _ = conn.uid('store', str(uid).encode(), verb, f'({flag})')
    if typ != 'OK':
        raise RuntimeError('无法更新星标')


def imap_mbox(name):
    name = name or ''
    if re.search(r'[\s"\\]', name) or not name.isascii():
        return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return name


def move_mail(conn, folder, uid, candidates):
    if folder == STARRED_ID:
        folder = 'INBOX'
    if not select_folder(conn, folder, readonly=False):
        return False
    target = None
    typ, data = conn.list()
    names = []
    if typ == 'OK' and data:
        for item in data:
            parsed = parse_list_line(item)
            if parsed:
                names.append(parsed[1])
    for cand in candidates:
        for name in names:
            decoded = decode_mutf7(name)
            if cand.lower() == name.lower() or cand.lower() == decoded.lower() or cand.lower() in decoded.lower():
                target = name
                break
        if target:
            break
    if target is None:
        return False
    if folder.lower() == target.lower() or decode_mutf7(folder).lower() == decode_mutf7(target).lower():
        return True
    boxed = imap_mbox(target)
    try:
        typ, _ = conn.uid('move', str(uid).encode(), boxed)
        if typ == 'OK':
            return True
    except Exception:
        pass
    try:
        typ, _ = conn.uid('copy', str(uid).encode(), boxed)
        if typ == 'OK':
            conn.uid('store', str(uid).encode(), '+FLAGS', '(\\Deleted)')
            conn.expunge()
            return True
    except Exception:
        pass
    return False


@app.route('/api/star', methods=['POST'])
def api_star():
    d = request.get_json(force=True)
    acc = require_account()
    folder = d.get('folder') or 'INBOX'
    if folder == STARRED_ID:
        folder = 'INBOX'
    on = bool(d.get('on'))
    mark_local(acc['id'], folder, d.get('uid'), starred=1 if on else 0,
               pending_sync='star' if on else 'unstar')
    enqueue_job(acc['id'], 'star' if on else 'unstar', folder, d.get('uid'))
    background_sync(acc, folder)
    return jsonify({'ok': True})


@app.route('/api/archive', methods=['POST'])
def api_archive():
    d = request.get_json(force=True)
    acc = require_account()
    if is_demo_account(acc):
        return jsonify({'ok': True, 'simulated': True})
    conn = imap_open(acc)
    try:
        ok = move_mail(conn, d.get('folder') or 'INBOX', d.get('uid'),
                       ('Archive', 'All Mail', '归档'))
        return jsonify({'ok': ok})
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@app.route('/api/delete', methods=['POST'])
def api_delete():
    d = request.get_json(force=True)
    acc = require_account()
    src = d.get('folder') or 'INBOX'
    if src == STARRED_ID:
        src = 'INBOX'
    uid = d.get('uid')
    if d.get('purge'):
        db = get_db()
        db.execute('DELETE FROM mails WHERE acc_id=? AND folder=? AND uid=?', (acc['id'], src, str(uid)))
        db.commit()
        db.close()
        add_tombstone(acc['id'], src, uid)
        enqueue_job(acc['id'], 'purge', src, uid)
        background_sync(acc, src)
        return jsonify({'ok': True, 'purged': True})
    trash_id = db_folder_label(acc['id'], '已删除') or 'Deleted Messages'
    uid = str(uid)
    try:
        db = get_db()
        row = db.execute('SELECT * FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                         (acc['id'], src, uid)).fetchone()
        if not row:
            row = db.execute('SELECT * FROM mails WHERE acc_id=? AND uid=?',
                             (acc['id'], uid)).fetchone()
            if row:
                src = row['folder']
        if row:
            if src == trash_id:
                db.execute('UPDATE mails SET local_deleted=1, unread=0, pending_sync=? WHERE acc_id=? AND folder=? AND uid=?',
                           ('purge', acc['id'], src, uid))
            else:
                clash = db.execute(
                    'SELECT 1 FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                    (acc['id'], trash_id, uid)).fetchone()
                if clash:
                    db.execute('DELETE FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                               (acc['id'], src, uid))
                else:
                    try:
                        db.execute(
                            'UPDATE mails SET folder=?, local_deleted=1, unread=0, pending_sync=? WHERE acc_id=? AND folder=? AND uid=?',
                            (trash_id, 'delete', acc['id'], src, uid))
                    except sqlite3.IntegrityError:
                        db.execute('DELETE FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                                   (acc['id'], src, uid))
        db.commit()
        db.close()
    except Exception:
        try:
            db.close()
        except Exception:
            pass
    add_tombstone(acc['id'], src, uid)
    enqueue_job(acc['id'], 'delete', src, uid)
    background_sync(acc, src)
    return jsonify({'ok': True})


@app.route('/api/restore', methods=['POST'])
def api_restore():
    d = request.get_json(force=True)
    acc = require_account()
    src = d.get('folder') or db_folder_label(acc['id'], '已删除') or 'INBOX'
    if src == STARRED_ID:
        src = 'INBOX'
    uid = str(d.get('uid') or '')
    dest = 'INBOX'
    db = get_db()
    row = db.execute('SELECT * FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                     (acc['id'], src, uid)).fetchone()
    if not row:
        row = db.execute('SELECT * FROM mails WHERE acc_id=? AND uid=?',
                         (acc['id'], uid)).fetchone()
        if row:
            src = row['folder']
    if row:
        clash = db.execute(
            'SELECT 1 FROM mails WHERE acc_id=? AND folder=? AND uid=?',
            (acc['id'], dest, uid)).fetchone()
        if clash and src != dest:
            db.execute('DELETE FROM mails WHERE acc_id=? AND folder=? AND uid=?',
                       (acc['id'], src, uid))
            db.execute(
                'UPDATE mails SET local_deleted=0, unread=0, pending_sync=? WHERE acc_id=? AND folder=? AND uid=?',
                ('', acc['id'], dest, uid))
        else:
            db.execute(
                """UPDATE mails SET folder=?, local_deleted=0, unread=0, pending_sync=?
                   WHERE acc_id=? AND folder=? AND uid=?""",
                (dest, 'restore', acc['id'], src, uid))
    db.commit()
    db.close()
    clear_tombstones_uid(acc['id'], uid)
    enqueue_job(acc['id'], 'restore', src, uid, dest)
    background_sync(acc, dest)
    return jsonify({'ok': True})


TRANSLATE_ENGINES = {
    'deepseek': {
        'label': 'DeepSeek',
        'base': 'https://api.deepseek.com/v1/chat/completions',
        'model': 'deepseek-chat',
    },
    'openai': {
        'label': 'OpenAI',
        'base': 'https://api.openai.com/v1/chat/completions',
        'model': 'gpt-4o-mini',
    },
    'custom': {'label': '自定义', 'base': '', 'model': ''},
}


def setting_get(key, default=''):
    db = get_db()
    row = db.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    db.close()
    return row['value'] if row and row['value'] is not None else default


def setting_set(key, value):
    db = get_db()
    db.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, value))
    db.commit()
    db.close()


# ---------- 文件夹"新鲜度"（决定打开文件夹时要不要联网同步） ----------
# 收件箱最重要：打开就刷；其它文件夹（已发送/草稿/垃圾箱…）一般不变，一天刷一次就够。
INBOX_SYNC_TTL = 0
FOLDER_SYNC_TTL = 24 * 3600


def folder_synced_at(acc_id, folder):
    """该文件夹最后一次同步成功的时间戳；从未同步过返回 0。"""
    try:
        raw = setting_get('folder_synced:%s:%s' % (acc_id, folder or ''))
        return float(raw) if raw else 0.0
    except Exception:
        return 0.0


def mark_folder_synced(acc_id, folder):
    if not folder:
        return
    try:
        setting_set('folder_synced:%s:%s' % (acc_id, folder), str(time.time()))
    except Exception:
        pass


def folder_needs_sync(acc_id, folder):
    """超过新鲜度阈值就需要联网同步。收件箱阈值是 0，即每次都刷。"""
    ttl = INBOX_SYNC_TTL if (folder or '').upper() == 'INBOX' else FOLDER_SYNC_TTL
    return (time.time() - folder_synced_at(acc_id, folder)) > ttl


# ---------- 开机自启动 ----------
_AUTOSTART_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_AUTOSTART_NAME = 'PuMail'


def _autostart_exe_path():
    """Return the path that should be registered for auto-start."""
    if getattr(sys, 'frozen', False):
        return sys.executable
    # Dev mode: use the Python interpreter + server.py
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def _read_registry_autostart():
    """Check if PuMail is registered in the current user's Run key."""
    if os.name != 'nt':
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _AUTOSTART_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _set_registry_autostart(enable):
    """Enable or disable PuMail auto-start via the Run registry key."""
    if os.name != 'nt':
        return
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                         winreg.KEY_SET_VALUE | winreg.KEY_READ)
    try:
        if enable:
            winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_exe_path())
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


@app.route('/api/general', methods=['GET'])
def api_general_get():
    return jsonify({
        'notice_sound': setting_get('notice_sound', '0') == '1',
    })


@app.route('/api/general', methods=['POST'])
def api_general_set():
    d = request.get_json(force=True) or {}
    result = {}
    if 'notice_sound' in d:
        enable = bool(d.get('notice_sound'))
        setting_set('notice_sound', '1' if enable else '0')
        result['notice_sound'] = enable
    return jsonify({'ok': True, **result})


def translate_settings():
    engine = setting_get('translate_engine', 'deepseek')
    key = setting_get('translate_key', '')
    try:
        key = pw_dec(key) if key else ''
    except Exception:
        key = ''
    return {
        'engine': engine if engine in TRANSLATE_ENGINES else 'deepseek',
        'api_key': key,
        'api_base': setting_get('translate_base', ''),
        'model': setting_get('translate_model', ''),
        'target': setting_get('translate_target', 'zh-CN'),
        'auto': setting_get('translate_auto', '0') == '1',
        'has_key': bool(key),
    }


def translate_chat(cfg, text, target=None, keep_format=False):
    engine = cfg.get('engine') or 'deepseek'
    meta = TRANSLATE_ENGINES.get(engine) or TRANSLATE_ENGINES['deepseek']
    key = (cfg.get('api_key') or '').strip()
    if not key:
        raise RuntimeError('请先填写 API Key')
    if engine == 'custom':
        base = (cfg.get('api_base') or '').strip()
        if not base:
            raise RuntimeError('请填写自定义 API 地址')
        if '/chat/completions' not in base:
            base = base.rstrip('/') + '/v1/chat/completions'
    else:
        base = (cfg.get('api_base') or '').strip() or meta['base']
    model = (cfg.get('model') or meta.get('model') or 'gpt-4o-mini').strip()
    lang = target or cfg.get('target') or 'zh-CN'
    names = {'zh-CN': '简体中文', 'zh-TW': '繁体中文', 'en': 'English', 'ja': '日本語', 'ko': '한국어'}
    dest = names.get(lang, lang)
    if keep_format:
        system = (
            f'你是翻译引擎。把用户给出的文本翻译成{dest}。'
            '必须原样保留每一段开头的标记 [[PM:数字]]，标记单独成行，数量和顺序不能变。'
            '不要把标记写进译文句子里，也不要额外发明 ###数字### 或其它标记。'
            '保留原有换行。只返回译文，不要解释。'
        )
    else:
        system = (
            f'你是翻译引擎。把用户给出的文本翻译成{dest}。'
            '只返回译文，不要解释，不要添加 ###数字### 或 [[PM:数字]] 这类标记。'
        )
    body = {
        'model': model,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': text},
        ],
    }
    req = urllib.request.Request(
        base,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': 'Bearer ' + key,
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:240]
        raise RuntimeError(f'API {e.code}：{detail or e.reason}')
    except Exception as e:
        raise RuntimeError(str(e))
    try:
        return (data['choices'][0]['message']['content'] or '').strip()
    except Exception:
        raise RuntimeError('API 返回格式无法解析')


@app.route('/api/translate/settings', methods=['GET', 'POST'])
def api_translate_settings():
    if request.method == 'GET':
        cfg = translate_settings()
        cfg['api_key'] = ''
        cfg['engines'] = {k: {'label': v['label'], 'base': v.get('base', ''), 'model': v.get('model', '')}
                          for k, v in TRANSLATE_ENGINES.items()}
        return jsonify(cfg)
    d = request.get_json(force=True) or {}
    engine = d.get('engine') or 'deepseek'
    if engine not in TRANSLATE_ENGINES:
        engine = 'deepseek'
    setting_set('translate_engine', engine)
    if 'api_base' in d:
        setting_set('translate_base', (d.get('api_base') or '').strip())
    if 'model' in d:
        setting_set('translate_model', (d.get('model') or '').strip())
    if 'target' in d:
        setting_set('translate_target', (d.get('target') or 'zh-CN').strip())
    if 'auto' in d:
        setting_set('translate_auto', '1' if d.get('auto') else '0')
    key = d.get('api_key')
    if key is not None and str(key).strip():
        setting_set('translate_key', pw_enc(str(key).strip()))
        vault_upsert_api_key(engine, str(key).strip())
    cfg = translate_settings()
    cfg['api_key'] = ''
    return jsonify({'ok': True, **{k: cfg[k] for k in ('engine', 'api_base', 'model', 'target', 'auto', 'has_key')}})


@app.route('/api/translate/test', methods=['POST'])
def api_translate_test():
    d = request.get_json(silent=True) or {}
    cfg = translate_settings()
    if (d.get('api_key') or '').strip():
        cfg['api_key'] = d.get('api_key').strip()
    if d.get('engine'):
        cfg['engine'] = d['engine']
    if d.get('api_base') is not None:
        cfg['api_base'] = d.get('api_base') or ''
    if d.get('model') is not None:
        cfg['model'] = d.get('model') or ''
    if d.get('target'):
        cfg['target'] = d['target']
    try:
        out = translate_chat(cfg, 'Hello, PuMail.', cfg.get('target') or 'zh-CN')
        return jsonify({'ok': True, 'sample': out})
    except Exception as e:
        msg = str(e)
        m = re.search(r'API\s+(\d+)', msg)
        return jsonify({'error': msg, 'code': int(m.group(1)) if m else None}), 400


@app.route('/api/translate', methods=['POST'])
def api_translate():
    d = request.get_json(force=True) or {}
    text = (d.get('text') or '').strip()
    if not text:
        return jsonify({'error': '没有可翻译的文本'}), 400
    cfg = translate_settings()
    if (d.get('api_key') or '').strip():
        cfg['api_key'] = d.get('api_key').strip()
    if d.get('engine'):
        cfg['engine'] = d['engine']
    if d.get('api_base') is not None:
        cfg['api_base'] = d.get('api_base') or ''
    if d.get('model') is not None:
        cfg['model'] = d.get('model') or ''
    try:
        out = translate_chat(cfg, text, d.get('target') or cfg.get('target'), bool(d.get('keep_format')))
        return jsonify({'ok': True, 'text': out, 'target': d.get('target') or cfg.get('target')})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/vault/status', methods=['GET'])
def api_vault_status():
    return jsonify({'configured': vault_configured(), 'unlocked': vault_unlocked()})


@app.route('/api/vault/setup', methods=['POST'])
def api_vault_setup():
    d = request.get_json(force=True) or {}
    password = d.get('password') or ''
    confirm = d.get('confirm')
    if confirm is not None and confirm != password:
        return jsonify({'error': '两次输入的主密码不一致'}), 400
    try:
        payload = vault_setup(password)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, **vault_public(payload)})


@app.route('/api/vault/unlock', methods=['POST'])
def api_vault_unlock():
    d = request.get_json(force=True) or {}
    try:
        payload = vault_unlock(d.get('password') or '')
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, **vault_public(payload)})


@app.route('/api/vault/lock', methods=['POST'])
def api_vault_lock():
    vault_lock()
    return jsonify({'ok': True})


@app.route('/api/vault/reset', methods=['POST'])
def api_vault_reset():
    vault_reset()
    return jsonify({'ok': True, 'configured': False})


def _quiet_sync_error_logs():
    try:
        import werkzeug.serving as _ws
    except Exception:
        return
    orig = _ws.WSGIRequestHandler.log_request

    def log_request(self, code='-', size='-'):
        if '/api/sync-errors' in (getattr(self, 'path', '') or ''):
            return
        return orig(self, code, size)

    _ws.WSGIRequestHandler.log_request = log_request
    logging.getLogger('werkzeug').setLevel(logging.INFO)


# ---------- IMAP IDLE 后台推送 + 通知队列 ----------
# 支持 IDLE 的服务商：QQ / Gmail / Outlook
# 兜底轮询的服务商：163 / 126 / Apple
_IDLE_PROVIDERS = {'qq', 'gmail', 'outlook', 'custom'}
_POLL_INTERVAL = {
    '163': 180,   # 3 分钟
    '126': 180,
    'apple': 300,  # 5 分钟
}
_IDLE_NOOP_SEC = 29 * 60  # 29 分钟
_POLL_DEFAULT = 180

_idle_threads = {}       # acc_id -> threading.Thread
_idle_stop = {}          # acc_id -> threading.Event
_poll_threads = {}       # acc_id -> threading.Thread (polling fallback)
_poll_stop = {}          # acc_id -> threading.Event
_pending_notifications = []
_notify_lock = threading.Lock()
_idle_manager_stop = threading.Event()
_idle_manager_thread = None


def _push_notification(acc_id, email, count):
    with _notify_lock:
        _pending_notifications.append({
            'acc_id': acc_id,
            'email': email or '',
            'count': count,
            'ts': time.time(),
        })
        if len(_pending_notifications) > 50:
            del _pending_notifications[:-50]


def _idle_thread_loop(acc):
    """IMAP IDLE 长连接线程：监控 INBOX，新邮件到达时推送通知。
    策略：进入 IDLE -> 等待服务器推送或超时 -> DONE 退出 -> 查新 UID -> NOOP 保活 -> 重新 IDLE
    """
    aid = acc['id']
    email = acc.get('email') or ''
    stop_ev = _idle_stop.get(aid)
    conn = None
    while stop_ev and not stop_ev.is_set():
        try:
            conn = imap_open(acc, timeout=60)
            if not select_folder(conn, 'INBOX', readonly=True, acc=acc):
                imap_log(acc, 'IDLE select fail', 'INBOX')
                break
            last_uid = get_last_uid(aid, 'INBOX')
            imap_log(acc, 'IDLE start', 'INBOX', 'last_uid', last_uid)

            while stop_ev and not stop_ev.is_set():
                # 进入 IDLE 模式
                try:
                    conn.idle()
                except Exception:
                    break

                # 等待服务器推送，最长 _IDLE_NOOP_SEC 秒
                deadline = time.time() + _IDLE_NOOP_SEC
                while time.time() < deadline:
                    if stop_ev.is_set():
                        break
                    remaining = max(0.5, deadline - time.time())
                    try:
                        conn.poll(remaining)
                    except Exception:
                        break

                # 退出 IDLE
                try:
                    conn.done()
                except Exception:
                    pass

                if stop_ev.is_set():
                    break

                # 退出 IDLE 后查新 UID（比解析 IDLE 响应更可靠）
                try:
                    fresh = peek_new_uids(conn, last_uid)
                    if fresh:
                        parsed = fetch_header_chunk(conn, fresh)
                        if parsed:
                            upsert_headers(acc, 'INBOX', parsed)
                            top = max((uid_int(u) for u in fresh), default=last_uid)
                            set_folder_state(aid, 'INBOX', max(top, last_uid))
                            last_uid = max(top, last_uid)
                            _push_notification(aid, email, len(fresh))
                            imap_log(acc, 'IDLE new', 'INBOX', 'count', len(fresh))
                except Exception as e:
                    imap_log(acc, 'IDLE fetch err', type(e).__name__, e)

                # NOOP 保活
                try:
                    conn.noop()
                except Exception:
                    break

        except Exception as e:
            imap_log(acc, 'IDLE error', type(e).__name__, e)
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass
                conn = None

        if stop_ev and not stop_ev.is_set():
            stop_ev.wait(10)
    imap_log(acc, 'IDLE stopped', 'INBOX')


def _poll_thread_loop(acc):
    """轮询线程：对不支持 IDLE 的服务商定时检查新邮件。"""
    aid = acc['id']
    email = acc.get('email') or ''
    provider = (acc.get('provider') or '').lower()
    interval = _POLL_INTERVAL.get(provider, _POLL_DEFAULT)
    stop_ev = _poll_stop.get(aid)

    while stop_ev and not stop_ev.is_set():
        try:
            def work(conn):
                nonlocal last_uid
                if not select_folder(conn, 'INBOX', readonly=True, acc=acc):
                    return
                fresh = peek_new_uids(conn, last_uid)
                if fresh:
                    parsed = fetch_header_chunk(conn, fresh)
                    if parsed:
                        upsert_headers(acc, 'INBOX', parsed)
                        top = max((uid_int(u) for u in fresh), default=last_uid)
                        set_folder_state(aid, 'INBOX', max(top, last_uid))
                        last_uid = max(top, last_uid)
                        _push_notification(aid, email, len(fresh))
                        imap_log(acc, 'POLL new', 'INBOX', 'count', len(fresh))

            last_uid = get_last_uid(aid, 'INBOX')
            with_imap(acc, work, ping=True, retry=True, timeout=15)
        except Exception as e:
            imap_log(acc, 'POLL error', type(e).__name__, e)

        if stop_ev:
            stop_ev.wait(interval)
    imap_log(acc, 'POLL stopped', 'INBOX')


def _start_idle_for_account(acc):
    """为单个账号启动 IDLE 或轮询线程。"""
    aid = acc['id']
    provider = (acc.get('provider') or '').lower()
    if is_demo_account(acc):
        return

    # 先停止已有的线程
    _stop_idle_for_account(aid)

    if provider in _IDLE_PROVIDERS:
        ev = threading.Event()
        _idle_stop[aid] = ev
        t = threading.Thread(target=_idle_thread_loop, args=(acc,), daemon=True, name=f'idle-{aid}')
        _idle_threads[aid] = t
        t.start()
        imap_log(acc, 'IDLE thread started', 'INBOX')
    else:
        ev = threading.Event()
        _poll_stop[aid] = ev
        t = threading.Thread(target=_poll_thread_loop, args=(acc,), daemon=True, name=f'poll-{aid}')
        _poll_threads[aid] = t
        t.start()
        imap_log(acc, 'POLL thread started', 'INBOX', 'interval', _POLL_INTERVAL.get(provider, _POLL_DEFAULT))


def _stop_idle_for_account(aid):
    """停止单个账号的 IDLE/轮询线程。"""
    ev = _idle_stop.pop(aid, None)
    if ev:
        ev.set()
    t = _idle_threads.pop(aid, None)
    if t:
        t.join(timeout=5)

    ev = _poll_stop.pop(aid, None)
    if ev:
        ev.set()
    t = _poll_threads.pop(aid, None)
    if t:
        t.join(timeout=5)


def sync_idle_threads():
    """根据当前账号列表同步 IDLE/轮询线程。"""
    try:
        accounts = iter_accounts()
    except Exception:
        return
    active_ids = {acc['id'] for acc in accounts if not is_demo_account(acc)}

    # 停止已删除账号的线程
    for aid in list(_idle_threads.keys()) + list(_poll_threads.keys()):
        if aid not in active_ids:
            _stop_idle_for_account(aid)

    # 为新账号启动线程
    for acc in accounts:
        if acc['id'] not in _idle_threads and acc['id'] not in _poll_threads:
            if not is_demo_account(acc):
                _start_idle_for_account(acc)


def stop_all_idle_threads():
    """停止所有 IDLE/轮询线程（应用退出时调用）。"""
    _idle_manager_stop.set()
    for aid in list(_idle_threads.keys()) + list(_poll_threads.keys()):
        _stop_idle_for_account(aid)


@app.route('/api/notifications')
def api_notifications():
    """前端轮询待处理通知。返回后清除。"""
    with _notify_lock:
        out = _pending_notifications[:]
        _pending_notifications.clear()
    return jsonify({'notifications': out})


_quiet_sync_error_logs()


if __name__ == '__main__':
    init_db()
    print('PuMail 启动成功 -> http://127.0.0.1:5000')
    if os.environ.get('PUMAIL_NO_BROWSER') != '1':
        webbrowser.open('http://127.0.0.1:5000')

    # 启动 IDLE/轮询线程
    import atexit
    atexit.register(stop_all_idle_threads)
    sync_idle_threads()

    app.run(host='127.0.0.1', port=5000, debug=False)
