/* PuMail 前端逻辑：通过后端 API 接入真实多账号邮箱 */
const $ = (id) => document.getElementById(id);
const STARRED_ID = '__STARRED__';
const GAIA_ID = '__GAIA__';
const ACC_LIMIT = 6;
const RAIL_LIMIT = 3;
const NAME_UNIT_MAX = 20;

const state = {
  accounts: [],
  acc: null,
  folder: 'INBOX',
  folders: [],
  page: 0,
  q: '',
  current: null,
  loading: false,
  hasMore: true,
  selected: new Set(),
  goneKeys: new Set(),
  ctxTarget: null,
  seenUnread: new Set(),
  listGen: 0,
  translateAuto: false,
  translateTarget: 'zh-CN',
  trMode: 'orig',
  trGen: 0,
  trEn: new Map(),
  composeAcc: null,
  noticeSound: false,
  unreadPerAccount: {},
};

const mailCache = new Map();
const folderCache = new Map();
const starOverrides = new Map();

/* ---------- 基础请求 ---------- */
async function api(path, opts = {}) {
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { 'Content-Type': 'application/json' };
  }
  let r;
  try {
    r = await fetch(path, { credentials: 'same-origin', ...opts });
  } catch (err) {
    throw new Error('本地服务未连接，请重试');
  }
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || ('请求失败 ' + r.status));
  return d;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/\r/g, '').replace(/\n/g, ' ');
}

function toast(msg, sub) {
  let t = document.querySelector('.toast');
  if (!t) {
    t = document.createElement('div');
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.replaceChildren();
  const line = document.createElement('span');
  line.textContent = msg;
  t.appendChild(line);
  if (sub) {
    const hint = document.createElement('small');
    hint.className = 'toast-sub';
    hint.textContent = sub;
    t.appendChild(hint);
  }
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 2600);
}

function showSyncErrors(errors) {
  const now = Date.now();
  (errors || []).forEach((msg) => {
    const text = String(msg || '').trim();
    if (!text) return;
    if (state._syncToastAt && state._syncToastAt[text] && now - state._syncToastAt[text] < 30000) return;
    if (!state._syncToastAt) state._syncToastAt = {};
    state._syncToastAt[text] = now;
    toast(text);
  });
}

function askConfirm(message) {
  return new Promise((resolve) => {
    $('confirmText').textContent = message;
    $('confirmOverlay').hidden = false;
    const done = (ok) => {
      $('confirmOverlay').hidden = true;
      $('confirmYes').onclick = null;
      $('confirmNo').onclick = null;
      resolve(ok);
    };
    $('confirmYes').onclick = () => done(true);
    $('confirmNo').onclick = () => done(false);
  });
}

function askLeaveAccount(acc) {
  return new Promise((resolve) => {
    const name = (acc && (acc.name || acc.email)) || '这个账号';
    $('leaveTitle').textContent = `退出「${name}」？`;
    $('leaveOverlay').hidden = false;
    const done = (choice) => {
      $('leaveOverlay').hidden = true;
      $('leaveKeep').onclick = null;
      $('leavePurge').onclick = null;
      $('leaveCancel').onclick = null;
      resolve(choice);
    };
    $('leaveKeep').onclick = () => done('keep');
    $('leavePurge').onclick = () => done('purge');
    $('leaveCancel').onclick = () => done(null);
  });
}

let vaultState = { configured: false, unlocked: false };
let vaultSetupWait = null;
let vaultUnlockWait = null;

function maskSecret(s) {
  const n = Math.max(6, String(s || '').length);
  return '•'.repeat(Math.min(n, 18));
}

function setVaultError(id, msg) {
  const el = $(id);
  if (!el) return;
  if (msg) {
    el.textContent = msg;
    el.hidden = false;
  } else {
    el.textContent = '';
    el.hidden = true;
  }
}

async function refreshVaultStatus() {
  try {
    vaultState = await api('/api/vault/status');
  } catch (err) {
    vaultState = { configured: false, unlocked: false };
  }
  return vaultState;
}

function closeVaultSetup(result) {
  const el = $('vaultSetupOverlay');
  if (el) el.hidden = true;
  setPasswordVisible($('vaultSetupInput'), $('vaultSetupToggle'), false, '隐藏主密码', '显示主密码');
  if ($('vaultSetupInput')) $('vaultSetupInput').value = '';
  if ($('vaultSetupConfirm')) $('vaultSetupConfirm').value = '';
  setVaultError('vaultSetupError', '');
  const wait = vaultSetupWait;
  vaultSetupWait = null;
  if (wait) wait(result || null);
}

function askSetupMasterPassword() {
  return new Promise((resolve) => {
    if (vaultSetupWait) vaultSetupWait(null);
    vaultSetupWait = resolve;
    setVaultError('vaultSetupError', '');
    $('vaultSetupOverlay').hidden = false;
    if ($('vaultSetupInput')) $('vaultSetupInput').focus();
  });
}

async function submitVaultSetup(e) {
  e.preventDefault();
  const pw = ($('vaultSetupInput').value || '');
  const confirm = ($('vaultSetupConfirm').value || '');
  if (pw.length < 6) {
    setVaultError('vaultSetupError', '主密码至少 6 位');
    return;
  }
  if (pw !== confirm) {
    setVaultError('vaultSetupError', '两次输入的主密码不一致');
    return;
  }
  try {
    const d = await api('/api/vault/setup', { method: 'POST', body: { password: pw, confirm } });
    vaultState = { configured: true, unlocked: true };
    closeVaultSetup(d);
  } catch (err) {
    setVaultError('vaultSetupError', err.message);
  }
}

function closeVaultUnlock(result) {
  const el = $('vaultUnlockOverlay');
  if (el) el.hidden = true;
  setPasswordVisible($('vaultUnlockInput'), $('vaultUnlockToggle'), false, '隐藏主密码', '显示主密码');
  if ($('vaultUnlockInput')) $('vaultUnlockInput').value = '';
  setVaultError('vaultUnlockError', '');
  const wait = vaultUnlockWait;
  vaultUnlockWait = null;
  if (wait) wait(result || null);
}

function askUnlockMasterPassword() {
  return new Promise((resolve) => {
    if (vaultUnlockWait) vaultUnlockWait(null);
    vaultUnlockWait = resolve;
    setVaultError('vaultUnlockError', '');
    if ($('vaultUnlockInput')) $('vaultUnlockInput').value = '';
    $('vaultUnlockOverlay').hidden = false;
    if ($('vaultUnlockInput')) $('vaultUnlockInput').focus();
  });
}

async function submitVaultUnlock(e) {
  e.preventDefault();
  const pw = ($('vaultUnlockInput').value || '');
  if (!pw) {
    setVaultError('vaultUnlockError', '请输入主密码');
    return;
  }
  try {
    const d = await api('/api/vault/unlock', { method: 'POST', body: { password: pw } });
    vaultState = { configured: true, unlocked: true };
    closeVaultUnlock(d);
  } catch (err) {
    setVaultError('vaultUnlockError', err.message);
  }
}

function closeVaultList() {
  const el = $('vaultListOverlay');
  if (el) el.hidden = true;
}

function renderVaultList(data) {
  const box = $('vaultListBody');
  if (!box) return;
  const accounts = data.accounts || [];
  const keys = data.api_keys || [];
  if (!accounts.length && !keys.length) {
    box.innerHTML = '<p class="vault-empty">还没有保存的密码信息。添加邮箱账号或保存 API 密钥后会显示在这里。</p>';
    return;
  }
  let html = '';
  if (accounts.length) {
    html += '<section class="vault-section"><h3>邮箱账号</h3>' + accounts.map((a) => `
      <article class="vault-card">
        <div class="vault-card-title">${esc(a.email)}</div>
        <div class="vault-row"><dt>授权码类型</dt><dd>${esc(a.auth_type || '授权码')}</dd></div>
        <div class="vault-row"><dt>授权码</dt><dd class="vault-secret">
          <code data-secret="${esc(a.secret || '')}" data-masked="1">${esc(a.secret ? maskSecret(a.secret) : '（未保存）')}</code>
          ${a.secret ? '<button type="button" class="vault-reveal" title="显示">显示</button>' : ''}
        </dd></div>
      </article>`).join('') + '</section>';
  }
  if (keys.length) {
    html += '<section class="vault-section"><h3>API 密钥</h3>' + keys.map((k) => `
      <article class="vault-card">
        <div class="vault-card-title">${esc(k.name || 'API 密钥')}</div>
        <div class="vault-row"><dt>类型</dt><dd>${esc(k.kind === 'translate' ? '翻译引擎' : (k.kind || '密钥'))}</dd></div>
        <div class="vault-row"><dt>密钥</dt><dd class="vault-secret">
          <code data-secret="${esc(k.secret || '')}" data-masked="1">${esc(k.secret ? maskSecret(k.secret) : '（未保存）')}</code>
          ${k.secret ? '<button type="button" class="vault-reveal" title="显示">显示</button>' : ''}
        </dd></div>
      </article>`).join('') + '</section>';
  }
  box.innerHTML = html;
}

function toggleVaultReveal(btn) {
  const code = btn.parentElement && btn.parentElement.querySelector('code');
  if (!code) return;
  const secret = code.getAttribute('data-secret') || '';
  const show = code.dataset.masked === '1';
  code.textContent = show ? secret : maskSecret(secret);
  code.dataset.masked = show ? '0' : '1';
  btn.textContent = show ? '隐藏' : '显示';
  btn.title = show ? '隐藏' : '显示';
}

async function ensureVaultReadyForSave() {
  await refreshVaultStatus();
  if (!vaultState.configured) {
    const d = await askSetupMasterPassword();
    return !!(d && d.ok);
  }
  if (!vaultState.unlocked) {
    const d = await askUnlockMasterPassword();
    return !!(d && d.ok);
  }
  return true;
}

async function openVaultManager() {
  await refreshVaultStatus();
  const data = vaultState.configured
    ? await askUnlockMasterPassword()
    : await askSetupMasterPassword();
  if (!data || !data.ok) return;
  renderVaultList(data);
  $('vaultListOverlay').hidden = false;
}

async function forgotMasterPassword() {
  const ok = await askConfirm('这将删除本机保存的全部密码信息文件，包括已加密的授权码、应用密码和 API 密钥。清除后需要重新设置主密码，并在之后添加邮箱时重新保存授权码。此操作不可恢复。');
  if (!ok) return;
  try {
    await api('/api/vault/reset', { method: 'POST' });
    vaultState = { configured: false, unlocked: false };
    closeVaultUnlock(null);
    closeVaultList();
    toast('已清除全部密码数据，请重新设置主密码');
  } catch (err) {
    toast('清除失败：' + err.message);
  }
}

function formatDate(s) {
  if (s == null || s === '') return '';
  if (typeof s === 'number' || (typeof s === 'string' && /^\d+(\.\d+)?$/.test(s) && Number(s) > 1000000000)) {
    const d0 = new Date(Number(s) < 1e12 ? Number(s) * 1000 : Number(s));
    if (!Number.isNaN(d0.getTime())) s = d0.toISOString();
  }
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) {
    return String(s).replace(/\s+\([^)]+\)\s*$/, '').replace(/ GMT.*$/, '');
  }
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const md = `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${hm}`;
  if (d.getFullYear() === now.getFullYear()) return md;
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

function avatarLetter(name) {
  const s = String(name || '?').replace(/<.*?>/g, '').trim();
  return (s[0] || '?').toUpperCase();
}

function isHanChar(ch) {
  return /[\u3400-\u9FFF\uF900-\uFAFF]/.test(ch);
}

function nameUnits(s) {
  let n = 0;
  for (const ch of String(s || '')) n += isHanChar(ch) ? 2.5 : 1;
  return n;
}

function clipAccName(s) {
  let out = '';
  let n = 0;
  for (const ch of String(s || '')) {
    const u = isHanChar(ch) ? 2.5 : 1;
    if (n + u > NAME_UNIT_MAX) break;
    out += ch;
    n += u;
  }
  return out;
}

function nameInitial(name, email) {
  const s = String((name || '').trim() || email || '?');
  return s[0] || '?';
}

function displaySubject(s) {
  let t = String(s || '').replace(/\s+/g, ' ').trim();
  const re = /^(re|fwd|fw|回复|答复|答覆|回覆|转发)(\s*\[\d+\])?\s*[:：]\s*/i;
  while (true) {
    const n = t.replace(re, '').trim();
    if (n === t) break;
    t = n;
  }
  return t || '(无主题)';
}

function dateValue(s) {
  if (s && typeof s === 'object') {
    const ts = Number(s.ts);
    if (ts > 1000000000) return ts < 1e12 ? ts * 1000 : ts;
    s = s.date;
  }
  if (s == null || s === '') return 0;
  if (typeof s === 'number' || (typeof s === 'string' && /^\d+(\.\d+)?$/.test(s) && Number(s) > 1000000000)) {
    const n = Number(s);
    return n < 1e12 ? n * 1000 : n;
  }
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime();
}

const STAR_SVG = '<svg class="star-icon" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M12 3.8c.28 0 .53.16.64.42l1.72 4.22 4.55.37c.63.05.88.84.4 1.25l-3.47 2.92 1.06 4.44c.15.61-.53 1.1-1.08.77L12 15.95l-3.82 2.24c-.55.33-1.23-.16-1.08-.77l1.06-4.44-3.47-2.92c-.48-.41-.23-1.2.4-1.25l4.55-.37L11.36 4.22A.7.7 0 0 1 12 3.8z"/></svg>';

const BATCH_STAR_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';

function setStarBtn(on) {
  if (state.folder === STARRED_ID) {
    $('starBtn').textContent = '已星标';
    return;
  }
  $('starBtn').textContent = on ? '已星标' : '星标';
}

function applyStarredBatchChrome() {
  const btn = $('batchStar');
  if (!btn) return;
  const starred = state.folder === STARRED_ID;
  if (!btn.querySelector('svg')) btn.innerHTML = BATCH_STAR_SVG;
  btn.title = starred ? '取消星标' : '星标';
  btn.setAttribute('aria-label', starred ? '取消星标' : '批量星标');
}

function waitMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showSkeletons(n) {
  const html = Array.from({ length: n }, () => `
    <div class="mail-row is-skel">
      <label class="mail-check"><input type="checkbox" disabled /></label>
      <span class="mail-avatar-wrap"><span class="mail-avatar"> </span></span>
      <div class="mail-main">
        <div class="mail-line"><span class="skel from"></span><span class="mail-date"> </span></div>
        <span class="skel subj"></span>
      </div>
      <span class="mail-star"></span>
    </div>`).join('');
  $('mailList').innerHTML = html;
  updateListLoading();
}

async function animateRowsOut(rows, kind) {
  const list = rows.filter(Boolean);
  if (!list.length) return;
  const cls = kind === 'purge' ? 'anim-purge' : 'anim-delete';
  const ms = kind === 'purge' ? 500 : 420;
  list.forEach((row) => {
    row.style.height = row.offsetHeight + 'px';
    row.style.transition = `height ${ms}ms cubic-bezier(.22,.61,.36,1), padding ${ms}ms cubic-bezier(.22,.61,.36,1)`;
    row.classList.add(cls);
  });
  requestAnimationFrame(() => {
    list.forEach((row) => {
      row.style.height = '0';
      row.style.paddingTop = '0';
      row.style.paddingBottom = '0';
    });
  });
  await waitMs(ms);
  list.forEach((row) => row.remove());
}

function dropKeysFromCache(keys) {
  keys.forEach((k) => state.goneKeys.add(k));
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  if (!cached) return;
  cached.items = cached.items.filter((m) => !keys.has(threadKeyOf(m)));
}

function keepCurrentCache(fn) {
  const key = mailCacheKey(state.folder, state.q);
  const snap = mailCache.get(key);
  fn();
  if (snap) mailCache.set(key, snap);
}

function avatarColor(name) {
  let h = 0;
  for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 42%, 38%)`;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result || '');
      resolve(s.includes(',') ? s.split(',')[1] : s);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function collectAttachments() {
  const files = Array.from($('attachInput').files || []);
  return Promise.all(files.map(async (f) => ({
    name: f.name,
    type: f.type || 'application/octet-stream',
    data: await fileToBase64(f),
  })));
}

function mailFolder() {
  return state.folder === STARRED_ID ? 'INBOX' : state.folder;
}

function normKey(s) {
  return String(s || '').replace(/\s+/g, ' ').trim();
}

function threadKeyOf(item) {
  const key = normKey(item && item.thread_key) || String((item && item.uid) || '');
  if (!key) return '';
  // 总览会把多个邮箱的邮件放在同一个列表里，不同邮箱的同一主题只是"同名"，
  // 不能当成同一封信：带上账号，避免列表里互相覆盖（未读被已读顶掉）。
  const acc = item && item.acc != null && item.acc !== '' ? String(item.acc) : '';
  return acc ? `${acc}|${key}` : key;
}

const OTP_SUBJ = /一次性\s*[代验認认]?[码碼]|验证码|驗證碼|校验码|校驗碼|动态码|動態碼|安全代码|安全代碼|verification\s*code|security\s*code|one[-\s]?time\s+(?:code|password|passcode)|\botp\b|(?:your|google|microsoft|apple)\s+(?:verification\s+|security\s+)?code/i;

function isOtpMail(m) {
  return OTP_SUBJ.test((m && m.subject) || '');
}

function expandOtpThreads(items) {
  const out = [];
  (items || []).forEach((m) => {
    if (!isOtpMail(m)) {
      out.push(m);
      return;
    }
    const parts = (m.thread_parts && m.thread_parts.length)
      ? m.thread_parts
      : (m.thread_uids && m.thread_uids.length ? m.thread_uids : [m.uid]).map((u) => ({
        folder: m.folder, uid: u, acc: m.acc,
      }));
    parts.forEach((p) => {
      const uid = p.uid;
      const folder = p.folder || m.folder || '';
      const acc = p.acc || m.acc || '';
      out.push({
        ...m,
        uid,
        folder,
        acc,
        thread_key: `otp|${acc}|${folder}|${uid}`,
        thread_count: 1,
        thread_uids: [uid],
        thread_parts: [{ folder, uid, acc }],
      });
    });
  });
  const map = new Map();
  out.forEach((m) => map.set(threadKeyOf(m), m));
  return [...map.values()];
}

function pinStore() {
  return JSON.parse(localStorage.getItem('pupu_pins_' + state.acc) || '{}');
}

function pinTime(key) {
  return Number(pinStore()[key] || 0);
}

function setPinned(key, on) {
  const store = pinStore();
  if (on) store[key] = Date.now();
  else delete store[key];
  localStorage.setItem('pupu_pins_' + state.acc, JSON.stringify(store));
}

function isPinned(key) {
  return !!pinTime(key);
}

/* ---------- 账号管理 ---------- */
async function loadTranslatePrefs() {
  try {
    const d = await api('/api/translate/settings');
    state.translateAuto = !!d.auto;
    state.translateTarget = d.target || 'zh-CN';
    if ($('trAuto')) $('trAuto').checked = !!d.auto;
    if ($('trTarget')) $('trTarget').value = state.translateTarget;
  } catch (err) {
    state.translateAuto = false;
  }
}

async function loadGeneralPrefs() {
  try {
    const d = await api('/api/general');
    state.noticeSound = !!d.notice_sound;
  } catch (err) {
    state.noticeSound = false;
  }
}

async function loadAccounts() {
  try {
    state.accounts = (await api('/api/accounts')).sort((a, b) => (a.sort_order || a.id) - (b.sort_order || b.id));
  } catch (err) {
    toast('加载账号失败：' + err.message);
    state.accounts = [];
  }
  renderAccountBar();
  if (!state.accounts.length) {
    openAccountForm();
    return;
  }
  const saved = localStorage.getItem('pupu_acc');
  if (saved === GAIA_ID) {
    await switchAccount(GAIA_ID);
    return;
  }
  const found = state.accounts.find((a) => a.id === Number(saved)) || state.accounts[0];
  await switchAccount(found.id);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function seedMailCache(accId, folder, pack) {
  if (!pack) return;
  mailCache.set(`${accId}|${folder}|`, {
    items: pack.items || [],
    total: pack.total || 0,
    page: 1,
    hasMore: !!pack.has_more,
    deep: true,
    filling: false,
  });
}

function applyUnreadSummary(summary) {
  if (!summary) return;
  if (summary.per_account) state.unreadPerAccount = summary.per_account;
  const gaiaFolders = folderCache.get(GAIA_ID);
  const inbox = gaiaFolders && gaiaFolders.find((f) => f.id === 'INBOX');
  if (inbox && summary.inbox != null) {
    inbox.unread_count = Math.max(0, Number(summary.inbox) || 0);
    inbox.unread = inbox.unread_count > 0;
  }
}

function applyBootSeed(boot) {
  if (!boot) return;
  state.accounts = (boot.accounts || []).slice().sort((a, b) => (a.sort_order || a.id) - (b.sort_order || b.id));
  applyUnreadSummary(boot.unread_summary);
  if (!boot.unread_summary && boot.unread_per_account) state.unreadPerAccount = boot.unread_per_account || {};
  const seeds = boot.seeds || {};
  Object.keys(seeds).forEach((aid) => {
    const id = Number(aid);
    if (!id) return;
    const seed = seeds[aid] || {};
    if (seed.folders) folderCache.set(id, seed.folders);
    Object.entries(seed.mails || {}).forEach(([folder, pack]) => seedMailCache(id, folder, pack));
  });
  if (boot.gaia) {
    folderCache.set(GAIA_ID, boot.gaia.folders || []);
    seedMailCache(GAIA_ID, 'INBOX', (boot.gaia.mails || {}).INBOX);
    applyUnreadSummary(boot.unread_summary);
  }
  renderAccountBar();
  if (!state.accounts.length) return;
  const saved = localStorage.getItem('pupu_acc');
  const next = saved === GAIA_ID
    ? GAIA_ID
    : ((state.accounts.find((a) => a.id === Number(saved)) || state.accounts[0]).id);
  state.acc = next;
  state.folder = 'INBOX';
  localStorage.setItem('pupu_acc', String(state.acc));
  renderAccountBar();
  renderFolderNav(folderCache.get(state.acc) || []);
  const cached = mailCache.get(mailCacheKey('INBOX', ''));
  if (cached) paintList(cached);
  else showFolderMails();
  fetchFolderMails({ background: true, merge: true });
  showSyncErrors(boot.errors);
}

function playNoticeSound() {
  if (!state.noticeSound) return;
  try {
    const audio = new Audio(location.origin + '/notice14.mp3');
    audio.volume = 0.6;
    audio.addEventListener('error', () => {
      toast('提示音文件加载失败，请检查 notice14.mp3 是否存在');
    });
    audio.play().catch((err) => {
      toast('提示音播放失败：' + (err.message || '未知错误'));
    });
  } catch (e) {
    toast('提示音播放失败：' + (e.message || '未知错误'));
  }
}

// ---------- 后台通知轮询（IDLE/轮询线程推送的新邮件） ----------
let _notifyTimer = null;
function startNotificationPolling() {
  if (_notifyTimer) return;
  _notifyTimer = setInterval(async () => {
    try {
      const d = await api('/api/notifications');
      const notes = d.notifications || [];
      for (const n of notes) {
        // SSE 连着的时候由事件负责响铃，这里只负责清空队列，避免响两次
        if (!_eventOk) handleNewMailNotice(n);
      }
    } catch (err) {
      // 静默忽略，下次轮询再试
    }
  }, 3000);
}

// ---------- 收信事件（SSE） ----------
// 后端每写完一封邮件、或改了已读状态，就推一条事件过来；
// 界面收到后自己把列表和徽标补上 —— 这样"响了却看不到邮件"不会再发生。
let _eventSource = null;
let _eventLastId = 0;
let _eventReconnectTimer = null;
let _eventOk = false;
let _badgeRefreshTimer = null;

function handleNewMailNotice(n) {
  playNoticeSound();
  const label = n.email ? `${n.email} 收到 ${n.count} 封新邮件` : `收到 ${n.count} 封新邮件`;
  toast(label);
  // 触发 Windows 任务栏图标闪烁
  if (window.pumailAPI && window.pumailAPI.flashTaskbar) {
    window.pumailAPI.flashTaskbar();
  }
}

function scheduleBadgeRefresh() {
  if (_badgeRefreshTimer) return;
  _badgeRefreshTimer = setTimeout(async () => {
    _badgeRefreshTimer = null;
    try {
      const pack = await api('/api/unread');
      applyUnreadSummary((isGaia() ? pack.recent : pack.all) || pack.all);
    } catch (err) {
      // 下次事件再补
    }
    renderAccountBar();
  }, 250);
}

function refreshListFromEvent(accId, folder) {
  if (!state.acc) return;
  const gaia = isGaia();
  const visible = gaia
    ? (!folder || state.folder === folder)
    : (Number(state.acc) === Number(accId) && (!folder || state.folder === folder));
  if (!visible) return;
  // 本地库已经写好，这里只是重新读一次本地数据，增量合并进当前列表，不整页重绘
  fetchFolderMails({ background: true, merge: true });
}

function handleServerEvent(ev) {
  if (!ev) return;
  if (ev.type === 'newmail') {
    handleNewMailNotice(ev);
    return;
  }
  if (ev.type === 'notice') {
    if (ev.text) toast(ev.text);
    scheduleBadgeRefresh();
    return;
  }
  if (ev.type === 'mail' || ev.type === 'flags') {
    scheduleBadgeRefresh();
    refreshListFromEvent(ev.acc, ev.folder);
    return;
  }
  if (ev.type === 'scanned') {
    // 总览的后台扫描落地了：徽标 + 当前总览列表跟上
    scheduleBadgeRefresh();
    if (isGaia() && (!ev.folder || state.folder === ev.folder)) {
      fetchFolderMails({ background: true, merge: true });
    }
  }
}

function closeEventStream() {
  if (_eventSource) {
    try { _eventSource.close(); } catch (err) { /* ignore */ }
    _eventSource = null;
  }
  _eventOk = false;
}

function scheduleEventReconnect() {
  if (_eventReconnectTimer) return;
  _eventReconnectTimer = setTimeout(() => {
    _eventReconnectTimer = null;
    connectEventStream();
  }, 4000);
}

function connectEventStream() {
  if (!window.EventSource) return;
  closeEventStream();
  try {
    _eventSource = new EventSource('/api/events?since=' + (_eventLastId || 0));
  } catch (err) {
    scheduleEventReconnect();
    return;
  }
  _eventSource.onopen = () => { _eventOk = true; };
  _eventSource.onerror = () => {
    // EventSource 自己也会重连，但我们自己接管更可控（用最后处理过的事件 id）
    _eventOk = false;
    closeEventStream();
    scheduleEventReconnect();
  };
  _eventSource.onmessage = (raw) => {
    let data = null;
    try {
      data = JSON.parse(raw.data);
    } catch (err) {
      return;
    }
    if (!data || typeof data.id !== 'number') return;
    if (data.id <= _eventLastId) return;   // 重连补发的重复事件
    _eventLastId = data.id;
    handleServerEvent(data);
  };
}

function applyRefreshResult(boot) {
  if (!boot) return;
  if (boot.accounts) {
    state.accounts = (boot.accounts || []).slice().sort((a, b) => (a.sort_order || a.id) - (b.sort_order || b.id));
  }
  applyUnreadSummary(boot.unread_summary);
  if (!boot.unread_summary && boot.unread_per_account) state.unreadPerAccount = boot.unread_per_account || {};
  const seeds = boot.seeds || {};
  Object.keys(seeds).forEach((aid) => {
    const id = Number(aid);
    if (!id) return;
    const seed = seeds[aid] || {};
    if (seed.folders) folderCache.set(id, seed.folders);
    Object.entries(seed.mails || {}).forEach(([folder, pack]) => seedMailCache(id, folder, pack));
  });
  if (boot.gaia) {
    folderCache.set(GAIA_ID, boot.gaia.folders || []);
    seedMailCache(GAIA_ID, 'INBOX', (boot.gaia.mails || {}).INBOX);
    applyUnreadSummary(boot.unread_summary);
  }
  renderAccountBar();
  renderFolderNav(folderCache.get(state.acc) || state.folders || []);
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  if (cached) paintList(cached);
  else showFolderMails();
  fetchFolderMails({ background: true, merge: true });
  showSyncErrors(boot.errors);
}

async function refreshAllMail() {
  const btn = $('refreshBtn');
  if (!btn || btn.classList.contains('is-busy')) return;
  btn.classList.add('is-busy');
  btn.disabled = true;
  try {
    const params = new URLSearchParams();
    if (!isGaia() && state.acc) params.set('acc', state.acc);
    params.set('folder', state.folder || 'INBOX');
    const boot = await api('/api/refresh?' + params);
    if (!boot || !boot.new_mail) return;
    playNoticeSound();
    applyRefreshResult(boot);
  } catch (err) {
    toast('刷新失败：' + (err.message || err));
    try { await refreshMails(); } catch (e) { /* already toasted */ }
  } finally {
    btn.classList.remove('is-busy');
    btn.disabled = false;
  }
}

function hideBootSplash() {
  const el = $('bootSplash');
  document.documentElement.classList.remove('booting');
  document.body.classList.remove('booting');
  if (!el || el.hidden) return Promise.resolve();
  el.classList.add('is-done');
  return new Promise((resolve) => {
    setTimeout(() => {
      el.hidden = true;
      resolve();
    }, 420);
  });
}

async function startApp() {
  loadTranslatePrefs();
  loadGeneralPrefs();
  startNotificationPolling();
  connectEventStream();
  let boot = null;
  const bootPromise = api('/api/boot').then((d) => {
    boot = d;
    return d;
  }).catch(() => null);

  await sleep(1000);
  if (boot) applyBootSeed(boot);
  else {
    try { await loadAccounts(); } catch (e) { /* enter UI anyway */ }
    bootPromise.then((d) => { if (d) applyBootSeed(d); });
  }
  await hideBootSplash();
  if (!state.accounts.length) openAccountForm();
}

function isGaia() {
  return state.acc === GAIA_ID;
}

function isDemoAccount(a) {
  return !!(a && (a.demo || a.provider === 'demo' || String(a.email || '').toLowerCase() === 'shipupuo@pumail.com'));
}

function accountById(id) {
  return (state.accounts || []).find((a) => a.id === Number(id)) || null;
}

function composeAccount() {
  return accountById(state.composeAcc || (!isGaia() && state.acc) || 0);
}

function actionAcc() {
  if (state.composeAcc) return state.composeAcc;
  if (isGaia()) {
    const cur = state.current && state.current.acc;
    if (cur) return cur;
    return (state.accounts[0] && state.accounts[0].id) || null;
  }
  return state.acc;
}

async function switchAccount(id) {
  const next = String(id) === GAIA_ID ? GAIA_ID : Number(id);
  if (state.acc === next) {
    renderAccountBar();
    return;
  }
  if (next !== GAIA_ID && !next) return;
  state.listGen += 1;
  clearTimeout(state._syncTimer);
  state.acc = next;
  state.goneKeys = new Set();
  state.seenUnread = new Set();
  clearSearch();
  localStorage.setItem('pupu_acc', String(state.acc));
  closeMessage();
  clearSelection();
  renderAccountBar();
  state.folder = 'INBOX';
  await loadFolders();
  showFolderMails();
}

function renderAccountBar() {
  const gaiaBtn = $('gaiaBtn');
  if (gaiaBtn) gaiaBtn.classList.toggle('active', isGaia());
  const rail = $('accRail');
  if (rail) {
    const shown = state.accounts.slice(0, RAIL_LIMIT);
    rail.innerHTML = shown.length
      ? shown.map((a) => {
          const unread = state.unreadPerAccount[a.id] || 0;
          const unreadBadge = unread > 0
            ? `<span class="acc-unread-badge" title="${unread} 封未读">${unread > 99 ? '99+' : unread}</span>`
            : '';
          return `
          <button type="button" class="acc-rail-item ${a.id === state.acc ? 'active' : ''}" data-id="${a.id}">
            <span class="avatar">${esc(nameInitial(a.name, a.email))}</span>
            <span class="acc-rail-meta">
              <strong>${esc(a.name || a.email.split('@')[0])}</strong>
              <small>${esc(a.email)}</small>
            </span>
            ${unreadBadge}
          </button>`;
        }).join('')
      : '<p class="acc-rail-empty">未绑定邮箱</p>';
  }
  renderSettingsAccounts();
  paintThemePicks();
  updateAddAccountBtn();
}

function updateAddAccountBtn() {
  const btn = $('addAccBtn');
  if (!btn) return;
  const full = state.accounts.length >= ACC_LIMIT;
  btn.hidden = full;
  btn.disabled = full;
}

function renderSettingsAccounts() {
  const box = $('settingsAccList');
  if (!box) return;
  const editing = document.activeElement && document.activeElement.classList.contains('acc-name-live')
    ? document.activeElement.dataset.id : '';
  box.innerHTML = state.accounts.length
    ? state.accounts.map((a) => `
        <div class="settings-acc-row" data-id="${a.id}">
          ${isDemoAccount(a) ? '<span class="acc-drag is-disabled" aria-hidden="true"></span>' : '<span class="acc-drag" title="拖动排序" aria-hidden="true"><svg viewBox="0 0 14 10" width="14" height="10" fill="none" aria-hidden="true"><path d="M1 1h12M1 5h12M1 9h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></span>'}
          <div class="avatar">${esc(nameInitial(a.name, a.email))}</div>
          <div class="settings-acc-meta">
            <input class="acc-name-live" data-id="${a.id}" value="${esc(a.name || '')}" maxlength="20" readonly spellcheck="false" title="${isDemoAccount(a) ? '演示账号' : '双击修改名称'}" placeholder="显示名称" ${isDemoAccount(a) ? 'data-demo="1"' : ''} />
            <p>${esc(a.email)}</p>
          </div>
          <button type="button" class="ghost settings-leave" data-id="${a.id}">退出账号</button>
        </div>`).join('')
    : '<p class="settings-note muted">还没有添加邮箱账号。</p>';
  if (editing) {
    const live = box.querySelector(`.acc-name-live[data-id="${CSS.escape(String(editing))}"]`);
    if (live) {
      live.readOnly = false;
      live.classList.add('is-editing');
    }
  }
  bindAccountDrag();
}

function bindAccountDrag() {
  const box = $('settingsAccList');
  if (!box) return;
  box.querySelectorAll('.settings-acc-row').forEach((row) => {
    row.removeAttribute('draggable');
    const handle = row.querySelector('.acc-drag');
    if (!handle || handle.classList.contains('is-disabled')) return;
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      startAccountSort(row, e);
    });
  });
}

function startAccountSort(row, e) {
  const box = $('settingsAccList');
  if (!box) return;
  const rows = [...box.querySelectorAll('.settings-acc-row')];
  if (rows.length < 2) return;
  const from = rows.indexOf(row);
  if (from < 0) return;
  const stride = row.offsetHeight + (parseFloat(getComputedStyle(box).rowGap || getComputedStyle(box).gap) || 10);
  const startY = e.clientY;
  let to = from;
  row.classList.add('is-sorting');
  row.style.zIndex = '4';
  row.style.transition = 'none';
  if (e.currentTarget && e.currentTarget.setPointerCapture) {
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
  }
  const applySlots = (target) => {
    rows.forEach((r, i) => {
      if (r === row) return;
      let shift = 0;
      if (from < target && i > from && i <= target) shift = -stride;
      else if (from > target && i >= target && i < from) shift = stride;
      r.style.transition = 'transform .22s cubic-bezier(.22, 1, .36, 1)';
      r.style.transform = 'translate3d(0,' + shift + 'px,0)';
    });
  };
  const onMove = (ev) => {
    ev.preventDefault();
    const raw = ev.clientY - startY;
    const min = -from * stride;
    const max = (rows.length - 1 - from) * stride;
    const dy = Math.max(min, Math.min(max, raw));
    row.style.transform = 'translate3d(0,' + dy + 'px,0)';
    const next = Math.max(0, Math.min(rows.length - 1, from + Math.round(dy / stride)));
    if (next !== to) {
      to = next;
      applySlots(to);
    }
    const br = box.getBoundingClientRect();
    if (ev.clientY < br.top + 36) box.scrollTop -= 10;
    else if (ev.clientY > br.bottom - 36) box.scrollTop += 10;
  };
  const onUp = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
    rows.forEach((r) => {
      r.style.transition = '';
      r.style.transform = '';
      r.style.zIndex = '';
    });
    row.classList.remove('is-sorting');
    if (to !== from) {
      const ref = to < from ? rows[to] : rows[to].nextSibling;
      box.insertBefore(row, ref);
    }
    const ids = [...box.querySelectorAll('.settings-acc-row')].map((el) => Number(el.dataset.id));
    persistAccountOrder(ids);
  };
  window.addEventListener('pointermove', onMove, { passive: false });
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);
}

function clearSearch() {
  state.q = '';
  if ($('searchInput')) $('searchInput').value = '';
}

function currentAccEmail() {
  const id = actionAcc();
  const a = (state.accounts || []).find((x) => x.id === id);
  return ((a && a.email) || '').trim().toLowerCase();
}

function activeSearchQuery() {
  const q = (state.q || '').trim();
  const mail = currentAccEmail();
  if (mail && q.toLowerCase() === mail) return '';
  return q;
}

function refreshAfterSend() {
  const sent = (state.folders || []).find((f) => f.label === '已发送');
  if (sent) invalidateMailCache(sent.id);
  fetchFolderMails({ background: true, merge: true });
}

async function persistAccountOrder(ids) {
  state.accounts = ids.map((id) => state.accounts.find((a) => a.id === id)).filter(Boolean);
  renderAccountBar();
  try {
    await api('/api/accounts/order', { method: 'PATCH', body: { ids } });
  } catch (err) {
    toast('排序保存失败：' + err.message);
  }
}

function openSettings(pane) {
  showSettingsPane(pane || 'account');
  renderAccountBar();
  $('settingsOverlay').hidden = false;
  if ((pane || 'account') === 'translate') loadTranslateSettings();
  if ((pane || 'account') === 'privacy') loadStorageSettings();
}

function closeSettings() {
  const live = document.querySelector('.acc-name-live.is-editing');
  if (live) live.blur();
  $('settingsOverlay').hidden = true;
}

function currentTheme() {
  return localStorage.getItem('pupu_theme') === 'light' ? 'light' : 'dark';
}

function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.classList.toggle('theme-light', next === 'light');
  localStorage.setItem('pupu_theme', next);
  paintThemePicks();
  syncMailHtmlTheme();
}

function paintThemePicks() {
  const cur = currentTheme();
  document.querySelectorAll('.theme-pick').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.theme === cur);
  });
}

function showSettingsPane(id) {
  document.querySelectorAll('.settings-pane').forEach((el) => {
    el.hidden = el.id !== 'pane-' + id;
  });
  document.querySelectorAll('.settings-nav-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.pane === id);
  });
  if (id === 'general') loadGeneralSettings();
  if (id === 'translate') loadTranslateSettings();
  if (id === 'privacy') loadStorageSettings();
}

function applyStorageInfo(d) {
  if ($('installPathText') && d.install_dir) $('installPathText').textContent = d.install_dir;
  if ($('dataPathText') && d.data_dir) $('dataPathText').textContent = d.data_dir;
}

async function loadStorageSettings() {
  if ($('storageStatus')) $('storageStatus').textContent = '';
  try {
    applyStorageInfo(await api('/api/storage'));
  } catch (err) {
    if ($('storageStatus')) $('storageStatus').textContent = '无法读取存储位置：' + err.message;
  }
}

async function loadGeneralSettings() {
  try {
    const d = await api('/api/general');
    const soundToggle = $('noticeSoundToggle');
    if (soundToggle) soundToggle.checked = !!d.notice_sound;
    const modeSel = $('syncModeSelect');
    if (modeSel && d.sync_mode) modeSel.value = d.sync_mode;
    const pollSel = $('pollIntervalSelect');
    if (pollSel && d.poll_interval) {
      const want = String(d.poll_interval);
      if ([...pollSel.options].some((o) => o.value === want)) pollSel.value = want;
    }
    paintSyncModeNote();
  } catch (err) {
    // ignore
  }
  loadSyncStatus();
  // 开机自启动由 Electron 管理，通过 IPC 读取
  try {
    const toggle = $('autoStartToggle');
    if (toggle && window.pumailAPI) {
      toggle.checked = !!(await window.pumailAPI.getAutoStart());
    }
  } catch (err) {
    // ignore
  }
}

$('autoStartToggle').addEventListener('change', async (e) => {
  if (!window.pumailAPI) {
    e.target.checked = !e.target.checked;
    toast('当前环境不支持开机自启动设置');
    return;
  }
  try {
    await window.pumailAPI.setAutoStart(e.target.checked);
    toast(e.target.checked ? '已开启开机自动启动' : '已关闭开机自动启动');
  } catch (err) {
    e.target.checked = !e.target.checked;
    toast('设置失败：' + (err.message || '未知错误'));
  }
});

$('noticeSoundToggle').addEventListener('change', async (e) => {
  try {
    await api('/api/general', {
      method: 'POST',
      body: { notice_sound: e.target.checked }
    });
    toast(e.target.checked ? '已开启提示音' : '已关闭提示音');
  } catch (err) {
    e.target.checked = !e.target.checked;
    toast('设置失败：' + err.message);
  }
});

/* ---------- 收信模式 / 收信状态 ---------- */
const SYNC_MODE_TEXT = {
  realtime: '所有邮箱都用 IMAP 待机：服务器一有新邮件立刻推给电脑，最及时。',
  eco: 'QQ / Gmail / Outlook 保持实时；163 / 126 / iCloud 改成定时检查（间隔更长），省一点电。',
  manual: '不在后台收信：只有点右上角刷新、或打开文件夹时才会联网取信。',
};

function paintSyncModeNote() {
  const el = $('syncModeNote');
  const sel = $('syncModeSelect');
  if (el && sel) el.textContent = SYNC_MODE_TEXT[sel.value] || '';
}

async function loadSyncStatus() {
  const box = $('syncStatusList');
  if (!box) return;
  try {
    const d = await api('/api/sync-status');
    const accounts = d.accounts || [];
    if (!accounts.length) {
      box.innerHTML = '<p class="settings-note muted">还没有绑定邮箱。</p>';
      return;
    }
    box.innerHTML = accounts.map((a) => {
      const modeText = a.mode === 'idle' ? '实时待机'
        : a.mode === 'poll' ? '定时检查'
          : a.mode === 'manual' ? '手动' : '未启动';
      const cls = a.mode === 'idle' ? 'is-idle' : a.mode === 'poll' ? 'is-poll' : 'is-manual';
      const ago = a.last_sync_ago == null
        ? '还没同步过'
        : (a.last_sync_ago < 90 ? `${a.last_sync_ago} 秒前同步` : `${Math.round(a.last_sync_ago / 60)} 分钟前同步`);
      const wait = a.mode === 'poll' && a.poll_interval
        ? `每 ${a.poll_interval} 秒检查一次`
        : (a.mode === 'idle' ? (a.thread_alive ? '等待服务器推送' : '连接重建中') : (a.last_event || ''));
      const err = a.last_error ? `<small class="sync-status-err">最近错误：${esc(a.last_error)}</small>` : '';
      return `
        <div class="sync-status-row">
          <div class="sync-status-acc">
            <strong>${esc(a.email)}</strong>
            <small>${esc(ago)} · ${esc(wait)}</small>
            ${err}
          </div>
          <span class="sync-status-mode ${cls}">${modeText}</span>
        </div>`;
    }).join('');
  } catch (err) {
    box.innerHTML = `<p class="settings-note muted">读取失败：${esc(err.message || '')}</p>`;
  }
}

if ($('syncModeSelect')) {
  $('syncModeSelect').addEventListener('change', async (e) => {
    const mode = e.target.value;
    try {
      await api('/api/general', { method: 'POST', body: { sync_mode: mode } });
      paintSyncModeNote();
      toast(mode === 'manual' ? '已切换为手动收信'
        : mode === 'eco' ? '已切换为省电模式' : '已切换为实时推送');
      setTimeout(loadSyncStatus, 900);
    } catch (err) {
      toast('设置失败：' + err.message);
    }
  });
}

if ($('pollIntervalSelect')) {
  $('pollIntervalSelect').addEventListener('change', async (e) => {
    const seconds = Number(e.target.value) || 120;
    try {
      await api('/api/general', { method: 'POST', body: { poll_interval: seconds } });
      toast(`163 / 126 / iCloud 改为每 ${seconds >= 60 ? Math.round(seconds / 60) + ' 分钟' : seconds + ' 秒'}检查一次`);
      setTimeout(loadSyncStatus, 900);
    } catch (err) {
      toast('设置失败：' + err.message);
    }
  });
}

async function openStorageLocation() {
  const btn = $('storageOpenBtn');
  if (btn) btn.disabled = true;
  try {
    await api('/api/storage/open', { method: 'POST' });
  } catch (err) {
    toast('打开失败：' + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function changeStorageLocation() {
  const btn = $('storageChangeBtn');
  const status = $('storageStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = '请在弹出的窗口中选择文件夹…';
  try {
    const d = await api('/api/storage/pick', { method: 'POST' });
    if (d.cancelled) {
      if (status) status.textContent = '';
      return;
    }
    applyStorageInfo(d);
    if (status) status.textContent = d.migrated ? '本地数据已迁移到新位置。' : '存储位置已更新。';
    toast(d.migrated ? '本地数据已迁移到新位置' : '存储位置已更新');
  } catch (err) {
    if (status) status.textContent = '更改失败，数据仍在原位置。可重启后再试。';
    toast('更改失败：' + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function currentTranslateEngine() {
  const on = document.querySelector('#trEngine .engine-pick.active');
  return (on && on.dataset.engine) || 'deepseek';
}

function setTranslateEngine(engine) {
  document.querySelectorAll('#trEngine .engine-pick').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.engine === engine);
  });
  if ($('trCustomBox')) $('trCustomBox').hidden = engine !== 'custom';
}

function translatePayload(extra = {}) {
  const engine = currentTranslateEngine();
  const body = {
    engine,
    api_key: ($('trKey').value || '').trim(),
    api_base: ($('trBase').value || '').trim(),
    model: ($('trModel').value || '').trim(),
    target: ($('trTarget') && $('trTarget').value) || state.translateTarget || 'zh-CN',
    auto: $('trAuto').checked,
    ...extra,
  };
  return body;
}

function looksChinese(text) {
  const s = String(text || '');
  const han = (s.match(/[\u4e00-\u9fff]/g) || []).length;
  const letters = (s.match(/[A-Za-z\u00C0-\u024F]/g) || []).length;
  return han > 0 && han >= letters;
}

function setPasswordVisible(input, btn, show, hideLabel, showLabel) {
  if (!input) return;
  input.type = show ? 'text' : 'password';
  if (!btn) return;
  btn.classList.toggle('is-on', show);
  btn.setAttribute('aria-pressed', show ? 'true' : 'false');
  btn.setAttribute('aria-label', show ? hideLabel : showLabel);
  btn.title = show ? hideLabel : showLabel;
}

function toggleTranslateKey() {
  const input = $('trKey');
  const btn = $('trKeyToggle');
  if (!input) return;
  const show = input.classList.contains('is-masked');
  input.classList.toggle('is-masked', !show);
  if (!btn) return;
  btn.classList.toggle('is-on', show);
  btn.setAttribute('aria-pressed', show ? 'true' : 'false');
  btn.setAttribute('aria-label', show ? '隐藏密钥' : '显示密钥');
  btn.title = show ? '隐藏密钥' : '显示密钥';
}

function toggleAccountPassword() {
  const input = $('accPasswordInput');
  setPasswordVisible(input, $('accPwToggle'), input && input.type === 'password', '隐藏授权码', '显示授权码');
}

function resetAccountPasswordField() {
  setPasswordVisible($('accPasswordInput'), $('accPwToggle'), false, '隐藏授权码', '显示授权码');
  const hint = $('accCapsHint');
  if (hint) hint.hidden = true;
}

function updateAccCapsHint(e) {
  const hint = $('accCapsHint');
  const input = $('accPasswordInput');
  if (!hint || !input) return;
  const focused = document.activeElement === input;
  const on = !!(focused && e && e.getModifierState && e.getModifierState('CapsLock'));
  hint.hidden = !on;
}

async function loadTranslateSettings() {
  if (!$('trKey')) return;
  try {
    const d = await api('/api/translate/settings');
    setTranslateEngine(d.engine || 'deepseek');
    $('trBase').value = d.api_base || '';
    $('trModel').value = d.model || '';
    state.translateTarget = d.target || 'zh-CN';
    $('trTarget').value = state.translateTarget;
    state.translateAuto = !!d.auto;
    $('trAuto').checked = !!d.auto;
    $('trKey').placeholder = '请于相应网站获取';
  } catch (err) {
    toast('读取设置失败：' + err.message);
  }
}

async function saveTranslateSettings() {
  try {
    if (($('trKey').value || '').trim()) {
      await refreshVaultStatus();
      if (vaultState.configured && !vaultState.unlocked) {
        const unlocked = await askUnlockMasterPassword();
        if (!unlocked || !unlocked.ok) {
          toast('已取消。未输入主密码时，密钥不会写入密码管理。');
        }
      }
    }
    await api('/api/translate/settings', { method: 'POST', body: translatePayload() });
    state.translateAuto = !!$('trAuto').checked;
    state.translateTarget = ($('trTarget') && $('trTarget').value) || state.translateTarget;
    toast('翻译设置已保存');
    $('trKey').placeholder = '请于相应网站获取';
  } catch (err) {
    toast('保存失败：' + err.message);
  }
}

function setTrTestHint(ok, text) {
  const el = $('trTestHint');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('is-ok', !!ok);
  el.classList.toggle('is-err', !ok && !!text);
}

async function testTranslateSettings() {
  const hint = $('trTestHint');
  const btn = $('trTest');
  setTrTestHint(false, '测试中…');
  if (hint) hint.classList.remove('is-err');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/translate/test', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(translatePayload()),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) {
      const code = d.code || ((String(d.error || '').match(/API\s+(\d+)/) || [])[1]);
      setTrTestHint(false, code ? ('连接失败 ' + code) : ('连接失败：' + (d.error || r.status)));
      return;
    }
    setTrTestHint(true, '连接成功。');
  } catch (err) {
    setTrTestHint(false, '连接失败：' + (err.message || '未知错误'));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function commitAccName(input) {
  const id = Number(input && input.dataset.id);
  const a = state.accounts.find((x) => x.id === id);
  if (!a || isDemoAccount(a)) return;
  const name = clipAccName(input.value.trim());
  input.value = name;
  if (name === (a.name || '')) {
    renderAccountBar();
    return;
  }
  try {
    await api('/api/accounts/' + id, { method: 'PATCH', body: { name } });
    a.name = name;
    renderAccountBar();
  } catch (err) {
    toast('改名失败：' + err.message);
    input.value = a.name || '';
  }
}

const PROVIDER_TIPS = {
  qq: 'QQ 邮箱请在网页版「设置 → 账户」开启 IMAP，并使用授权码，不是 QQ 密码。',
  163: '163 邮箱请在网页版开启 IMAP/SMTP，使用授权码登录。收件箱是 INBOX，已发送通常是「已发送」。',
  126: '126 邮箱请在网页版开启 IMAP，并使用授权码，不是登录密码。',
  gmail: 'Gmail 已关闭普通密码登录。请先开启两步验证，再生成 16 位应用专用密码。服务器：imap.gmail.com:993 / smtp.gmail.com:465。',
  outlook: 'Outlook / Hotmail 个人邮箱已停用授权码。请点「用 Microsoft 登录」，在打开的网页输入代码完成授权。',
  icloud: 'iCloud 请在 appleid.apple.com 生成 App 专用密码。',
  custom: '请填写 IMAP / SMTP 主机与端口。网易、谷歌等请优先选对应服务商。',
};

function guessProvider(email) {
  const d = (email.split('@')[1] || '').toLowerCase();
  if (d === 'qq.com' || d === 'vip.qq.com' || d.endsWith('.qq.com')) return 'qq';
  if (d === '163.com' || d.endsWith('.163.com')) return '163';
  if (d === '126.com' || d.endsWith('.126.com')) return '126';
  if (d === 'gmail.com' || d === 'googlemail.com') return 'gmail';
  if (/^(outlook|hotmail|live|msn)\./.test(d) || d.endsWith('office365.com')) return 'outlook';
  if (d === 'icloud.com' || d === 'me.com' || d === 'mac.com') return 'icloud';
  return '';
}

const EMAIL_SUFFIXES = ['@qq.com', '@163.com', '@126.com', '@gmail.com', '@outlook.com', '@icloud.com'];

function isCompleteEmail(v) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(v || '').trim());
}

function hideEmailSuggest() {
  const list = $('accEmailSuggest');
  if (list) list.hidden = true;
}

function hideEmailHint() {
  const hint = $('accEmailHint');
  if (hint) hint.hidden = true;
}

function showEmailHint(msg) {
  const hint = $('accEmailHint');
  const text = $('accEmailHintText');
  if (text) text.textContent = msg || '请输入包含 @ 的有效邮箱地址';
  if (hint) hint.hidden = false;
}

function updateEmailSuggest() {
  const input = $('accEmailInput');
  const list = $('accEmailSuggest');
  if (!input || !list) return;
  const raw = input.value.replace(/\s/g, '');
  hideEmailHint();
  if (!raw || raw.includes('@')) {
    list.hidden = true;
    return;
  }
  list.replaceChildren();
  EMAIL_SUFFIXES.forEach((s) => {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.full = raw + s;
    btn.textContent = s;
    li.appendChild(btn);
    list.appendChild(li);
  });
  list.hidden = false;
}

function pickEmailSuggest(full) {
  const input = $('accEmailInput');
  if (!input || !full) return;
  input.value = full;
  hideEmailSuggest();
  hideEmailHint();
  const guessed = guessProvider(full);
  if (guessed) $('accProviderInput').value = guessed;
  toggleCustomServer();
  input.focus();
}

function applyAccountEmailInput() {
  const guessed = guessProvider($('accEmailInput').value.trim());
  if (guessed) $('accProviderInput').value = guessed;
  toggleCustomServer();
  updateEmailSuggest();
}

function updateAccountFormHint() {
  const tip = $('accFormTip');
  if (!tip) return;
  const p = $('accProviderInput').value;
  tip.textContent = PROVIDER_TIPS[p] || PROVIDER_TIPS.custom;
}

let outlookPollTimer = 0;
let outlookPendingId = '';

function resetOutlookOauthUi() {
  outlookPendingId = '';
  if (outlookPollTimer) {
    clearTimeout(outlookPollTimer);
    outlookPollTimer = 0;
  }
  if ($('outlookCodeWrap')) $('outlookCodeWrap').hidden = true;
  if ($('outlookUserCode')) $('outlookUserCode').textContent = '';
  const btn = $('outlookLoginBtn');
  if (btn) {
    btn.disabled = false;
    btn.textContent = '用 Microsoft 登录';
  }
}

function syncAccountAuthUi() {
  const outlook = $('accProviderInput').value === 'outlook';
  const pwLabel = $('accPasswordLabel');
  const pw = $('accPasswordInput');
  if (pwLabel) pwLabel.hidden = outlook;
  if (pw) {
    pw.required = !outlook;
    if (outlook) pw.value = '';
  }
  if ($('outlookOauthBox')) $('outlookOauthBox').hidden = !outlook;
  if ($('outlookLoginBtn')) $('outlookLoginBtn').hidden = !outlook;
  if ($('accSubmitBtn')) $('accSubmitBtn').hidden = outlook;
  if (!outlook) resetOutlookOauthUi();
  updateAccountFormHint();
}

function openAccountForm() {
  resetAccountPasswordField();
  resetOutlookOauthUi();
  hideEmailSuggest();
  hideEmailHint();
  $('accountOverlay').hidden = false;
  toggleCustomServer();
}

function closeAccountForm() {
  resetOutlookOauthUi();
  hideEmailSuggest();
  hideEmailHint();
  $('accountOverlay').hidden = true;
}

function toggleCustomServer() {
  $('customServerBox').hidden = $('accProviderInput').value !== 'custom';
  syncAccountAuthUi();
}

async function startOutlookOauth() {
  const email = $('accEmailInput').value.trim();
  if (!isCompleteEmail(email)) {
    hideEmailSuggest();
    showEmailHint(email ? '请输入包含 @ 的有效邮箱地址' : '请先填写邮箱地址');
    $('accEmailInput').focus();
    return;
  }
  if (!(await ensureVaultReadyForSave())) return;
  resetOutlookOauthUi();
  const btn = $('outlookLoginBtn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '正在连接 Microsoft…';
  }
  try {
    const d = await api('/api/accounts/outlook-oauth/start', {
      method: 'POST',
      body: { email, name: $('accNameInput').value.trim() },
    });
    outlookPendingId = d.pending_id;
    if ($('outlookUserCode')) $('outlookUserCode').textContent = d.user_code || '';
    if ($('outlookCodeWrap')) $('outlookCodeWrap').hidden = !d.user_code;
    const url = d.verification_uri_complete || d.verification_uri;
    if (url && window.pumailAPI && window.pumailAPI.openExternal) window.pumailAPI.openExternal(url);
    else if (url) window.open(url, '_blank', 'noopener');
    if (btn) btn.textContent = '等待 Microsoft 确认…';
    pollOutlookOauth(Number(d.interval) || 5);
  } catch (err) {
    resetOutlookOauthUi();
    toast('登录失败：' + err.message);
  }
}

function pollOutlookOauth(wait) {
  outlookPollTimer = setTimeout(async () => {
    if (!outlookPendingId) return;
    try {
      const d = await api('/api/accounts/outlook-oauth/poll', {
        method: 'POST',
        body: { pending_id: outlookPendingId },
      });
      if (d.status === 'pending') {
        pollOutlookOauth(d.interval ? 5 : 3);
        return;
      }
      resetOutlookOauthUi();
      closeAccountForm();
      $('accountForm').reset();
      resetAccountPasswordField();
      syncAccountAuthUi();
      toast('账号添加成功');
      await loadAccounts();
    } catch (err) {
      resetOutlookOauthUi();
      toast('登录失败：' + err.message);
    }
  }, Math.max(2, Number(wait) || 5) * 1000);
}

async function submitAccountForm(e) {
  e.preventDefault();
  const email = $('accEmailInput').value.trim();
  if (!isCompleteEmail(email)) {
    hideEmailSuggest();
    showEmailHint(email ? '请输入包含 @ 的有效邮箱地址' : '请先填写邮箱地址');
    $('accEmailInput').focus();
    return;
  }
  if (!(await ensureVaultReadyForSave())) return;
  const d = {
    email,
    name: $('accNameInput').value.trim(),
    provider: $('accProviderInput').value,
    password: $('accPasswordInput').value,
    imap_host: $('accImapHost').value.trim(),
    imap_port: $('accImapPort').value.trim(),
    smtp_host: $('accSmtpHost').value.trim(),
    smtp_port: $('accSmtpPort').value.trim(),
    starttls: $('accStarttls').checked,
  };
  try {
    await api('/api/accounts', { method: 'POST', body: d });
    closeAccountForm();
    $('accountForm').reset();
    resetAccountPasswordField();
    toggleCustomServer();
    toast('账号添加成功');
    await loadAccounts();
  } catch (err) {
    toast('添加失败：' + err.message);
  }
}

function wipeLocalAccountCaches(accId, purge) {
  const prefix = `${accId}|`;
  for (const key of [...mailCache.keys()]) {
    if (key.startsWith(prefix)) mailCache.delete(key);
  }
  folderCache.delete(accId);
  for (const key of [...starOverrides.keys()]) {
    if (key.startsWith(prefix)) starOverrides.delete(key);
  }
  if (purge) localStorage.removeItem('pupu_pins_' + accId);
}

async function removeAccount(id) {
  const accId = Number(id);
  if (!accId) return;
  const acc = (state.accounts || []).find((a) => a.id === accId);
  if (isDemoAccount(acc)) {
    try {
      await api('/api/accounts/' + accId + '?purge=1', { method: 'DELETE' });
      wipeLocalAccountCaches(accId, true);
      if (state.acc === accId) {
        localStorage.removeItem('pupu_acc');
        state.acc = null;
        closeMessage();
      }
      toast('演示账号已退出');
      await loadAccounts();
    } catch (err) {
      toast('退出失败：' + err.message);
    }
    return;
  }
  const choice = await askLeaveAccount(acc);
  if (!choice) return;
  try {
    await api('/api/accounts/' + accId + (choice === 'purge' ? '?purge=1' : ''), { method: 'DELETE' });
    wipeLocalAccountCaches(accId, choice === 'purge');
    if (state.acc === accId) {
      localStorage.removeItem('pupu_acc');
      state.acc = null;
      closeMessage();
    }
    toast(choice === 'purge' ? '账号已退出，本地数据已清除' : '账号已退出，本地数据已保留');
    await loadAccounts();
  } catch (err) {
    toast('退出失败：' + err.message);
  }
}

function mailCacheKey(folder, q, accId) {
  return `${accId == null ? state.acc : accId}|${folder}|${q || ''}`;
}

function starOverrideId(key, accId) {
  return `${accId == null ? state.acc : accId}|${normKey(key)}`;
}

function cacheItemSnapshot(row, key) {
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  const uid = row && row.dataset ? row.dataset.uid : '';
  const found = cached && cached.items.find((m) => threadKeyOf(m) === key || (uid && String(m.uid) === String(uid)));
  if (found) return { ...found };
  if (!row) return null;
  return {
    uid,
    thread_key: key,
    thread_uids: (row.dataset.uids || uid || '').split(',').filter(Boolean),
    thread_parts: (row.dataset.refs || '').split(',').filter(Boolean).map((r) => {
      const i = r.indexOf('|');
      return i === -1 ? { folder: mailFolder(), uid: r } : { folder: r.slice(0, i) || mailFolder(), uid: r.slice(i + 1) };
    }),
    subject: (row.querySelector('.mail-subject') || {}).textContent || '',
    from: ((row.querySelector('.mail-from') || {}).textContent || '').replace(/\s·.*$/, ''),
    date: (row.querySelector('.mail-date') || {}).textContent || '',
    unread: row.classList.contains('unread'),
    starred: true,
    folder: mailFolder(),
  };
}

function applyStarLocal(key, on, snap) {
  const k = normKey(key);
  if (!k || !state.acc) return;
  starOverrides.set(starOverrideId(k), !!on);
  const prefix = `${state.acc}|`;
  const uid = snap && snap.uid;
  let sawStarred = false;
  for (const [ck, cached] of mailCache) {
    if (!ck.startsWith(prefix) || !cached || !cached.items) continue;
    const folder = ck.slice(prefix.length).split('|')[0];
    cached.items.forEach((m) => {
      if (threadKeyOf(m) === k || (uid && String(m.uid) === String(uid))) m.starred = !!on;
    });
    if (folder !== STARRED_ID) continue;
    sawStarred = true;
    if (on) {
      const have = cached.items.some((m) => threadKeyOf(m) === k || (uid && String(m.uid) === String(uid)));
      if (!have && snap) cached.items.unshift({ ...snap, starred: true, thread_key: snap.thread_key || k });
    } else {
      cached.items = cached.items.filter((m) => threadKeyOf(m) !== k && (!uid || String(m.uid) !== String(uid)));
    }
  }
  if (on && snap && !sawStarred) {
    mailCache.set(mailCacheKey(STARRED_ID, ''), {
      items: [{ ...snap, starred: true, thread_key: snap.thread_key || k }],
      total: 1,
      page: 1,
      hasMore: true,
      deep: false,
    });
  }
}

function applyIncomingStars(items, folder) {
  return (items || []).reduce((out, m) => {
    const id = starOverrideId(threadKeyOf(m));
    if (starOverrides.has(id)) {
      const want = starOverrides.get(id);
      if (!!m.starred === want) starOverrides.delete(id);
      else m.starred = want;
      if (folder === STARRED_ID && !want) return out;
    }
    out.push(m);
    return out;
  }, []);
}

function invalidateMailCache(folder) {
  const prefix = `${state.acc}|`;
  for (const key of [...mailCache.keys()]) {
    if (!key.startsWith(prefix)) continue;
    if (!folder || key.startsWith(`${state.acc}|${folder}|`)) mailCache.delete(key);
  }
}

function currentFolderLabel() {
  const cur = (state.folders || []).find((f) => f.id === state.folder);
  return cur ? cur.label : '';
}

function isTrashFolder() {
  return currentFolderLabel() === '已删除';
}

function isDraftOrSpamFolder() {
  const label = currentFolderLabel();
  return label === '草稿' || label === '垃圾邮件';
}

function isBulkCleanFolder() {
  return isTrashFolder() || isDraftOrSpamFolder();
}

function applyFolderChrome() {
  const trash = isTrashFolder();
  const bulk = isBulkCleanFolder();
  $('listPane').classList.toggle('is-starred', state.folder === STARRED_ID);
  $('listPane').classList.toggle('is-trash', trash);
  $('listPane').classList.toggle('is-bulk', bulk);
  applyStarredBatchChrome();
  $('searchWrap').hidden = bulk;
  if ($('batchStar')) $('batchStar').hidden = bulk;
  if ($('batchPin')) $('batchPin').hidden = bulk;
  if ($('batchDel')) $('batchDel').hidden = bulk;
  if ($('purgeBtn')) $('purgeBtn').hidden = !bulk;
  if ($('restoreBtn')) $('restoreBtn').hidden = !bulk;
  $('starBtn').hidden = trash;
  $('pinBtn').hidden = trash;
  $('replyBtn').hidden = trash;
  $('deleteBtn').textContent = trash ? '粉碎' : '删除';
  $('deleteBtn').classList.remove('danger');
  if (state.folder === STARRED_ID && state.current) {
    state.current.starred = true;
    setStarBtn(true);
  }
  $('replyThread').hidden = trash;
  if ($('selectAllBox') && !bulk) $('selectAllBox').checked = false;
  updateBatchBar();
}

function paintList(cached) {
  state.page = cached.page;
  state.hasMore = cached.hasMore;
  applyFolderChrome();
  $('mailList').innerHTML = '';
  if (!cached.items.length) {
    $('mailList').innerHTML = '<p class="empty-list">没有邮件</p>';
  } else {
    renderMailRows(sortMailList(cached.items));
  }
  updateListLoading();
  refreshFolderUnreadFromList();
}

function rowKey(row) {
  return normKey(row && row.dataset ? row.dataset.key : '') || String((row && row.dataset && row.dataset.uid) || '');
}

function findMailRow(list, m) {
  const key = threadKeyOf(m);
  const uid = String(m.uid || '');
  return [...list.querySelectorAll('.mail-row')].find((row) => rowKey(row) === key || (uid && row.dataset.uid === uid));
}

function syncList(cached) {
  state.page = cached.page;
  state.hasMore = cached.hasMore;
  applyFolderChrome();
  const empty = $('mailList').querySelector('.empty-list');
  if (empty) empty.remove();
  const items = sortMailList(cached.items);
  const keys = new Set(items.map(threadKeyOf));
  $('mailList').querySelectorAll('.mail-row:not(.is-skel)').forEach((row) => {
    if (!keys.has(rowKey(row))) row.remove();
  });
  $('mailList').querySelectorAll('.mail-row.is-skel').forEach((row) => row.remove());
  const have = new Set([...$('mailList').querySelectorAll('.mail-row')].map(rowKey));
  const add = items.filter((m) => !have.has(threadKeyOf(m)));
  if (add.length) renderMailRows(add, { enter: true });
  const list = $('mailList');
  items.forEach((m) => {
    const row = findMailRow(list, m);
    if (row) list.appendChild(row);
  });
  if (!items.length && !list.querySelector('.mail-row')) {
    list.innerHTML = '<p class="empty-list">没有邮件</p>';
  }
  updateBatchBar();
  updateListLoading();
  refreshFolderUnreadFromList();
}

/* ---------- 文件夹 ---------- */
const FOLDER_ORDER = ['收件箱', '已发送', '星标', '草稿', '垃圾邮件', '已删除'];

function sortFolders(folders) {
  const rank = Object.fromEntries(FOLDER_ORDER.map((label, i) => [label, i]));
  return (folders || []).slice().sort((a, b) => (rank[a.label] ?? 99) - (rank[b.label] ?? 99));
}

function renderFolderNav(folders) {
  folders = sortFolders(folders);
  state.folders = folders;
  if (!folders.length) {
    $('folders').innerHTML = '<button type="button" class="folder active" data-id="INBOX">收件箱</button>';
    $('folderTitle').textContent = '收件箱';
    state.folder = 'INBOX';
    return;
  }
  if (!folders.some((f) => f.id === state.folder)) {
    state.folder = folders[0].id;
  }
  $('folders').innerHTML = folders
    .map((f) => `<button type="button" class="folder ${f.id === state.folder ? 'active' : ''} ${folderShowsUnread(f) ? 'has-unread' : ''}" data-id="${esc(f.id)}"><span class="folder-label">${esc(f.label)}<span class="folder-dot">${folderUnreadLabel(f)}</span></span></button>`)
    .join('');
  $('folders').querySelectorAll('.folder').forEach((btn) => {
    btn.onclick = () => switchFolder(btn.dataset.id);
  });
  const cur = folders.find((f) => f.id === state.folder);
  $('folderTitle').textContent = cur ? cur.label : '收件箱';
  refreshFolderUnreadFromList();
}

function folderUnreadCount(f) {
  if (!f || f.label === '已删除') return 0;
  if (f.unread_count != null && Number.isFinite(Number(f.unread_count))) return Math.max(0, Number(f.unread_count));
  return f.unread ? 1 : 0;
}

function folderUnreadLabel(f) {
  if (!f || f.unread_count == null) return '';
  const n = folderUnreadCount(f);
  if (n > 99) return '99+';
  return n > 0 ? String(n) : '';
}

function folderShowsUnread(f) {
  return folderUnreadCount(f) > 0;
}

function paintFolderUnreadBtn(id, f) {
  const btn = [...document.querySelectorAll('.folder')].find((b) => b.dataset.id === id);
  if (!btn) return;
  btn.classList.toggle('has-unread', folderShowsUnread(f));
  const dot = btn.querySelector('.folder-dot');
  if (dot) dot.textContent = folderUnreadLabel(f);
}

function setFolderUnread(id, on, count) {
  const f = (state.folders || []).find((x) => x.id === id);
  if (f) {
    if (count != null) f.unread_count = Math.max(0, Number(count) || 0);
    else if (!on) f.unread_count = 0;
    f.unread = count != null ? count > 0 : !!on;
  }
  if (on) state.seenUnread.delete(id);
  else state.seenUnread.add(id);
  paintFolderUnreadBtn(id, f || { id, unread: !!on, unread_count: count });
}

function dismissFolderUnread(id) {
  refreshFolderUnreadFromList();
}

function refreshFolderUnreadFromList() {
  if (isTrashFolder()) {
    setFolderUnread(state.folder, false, 0);
    return;
  }
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  if (!cached) return;
  // 优先使用服务端返回的未读总数，避免分页加载时缓存不完整导致数字偏小
  if (cached.unreadCount != null) {
    setFolderUnread(state.folder, cached.unreadCount > 0, cached.unreadCount);
    return;
  }
  const n = cached.items.reduce((sum, m) => sum + (m.unread ? 1 : 0), 0);
  if (state.q && n === 0) return;
  setFolderUnread(state.folder, n > 0, n);
}

function switchFolder(id) {
  if (id === state.folder) {
    refreshFolderUnreadFromList();
    return;
  }
  state.folder = id;
  state.goneKeys = new Set();
  closeMessage();
  clearSelection();
  const cur = (state.folders || []).find((f) => f.id === id);
  $('folderTitle').textContent = cur ? cur.label : id;
  $('folders').querySelectorAll('.folder').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.id === id);
  });
  if (isBulkCleanFolder()) clearSearch();
  applyFolderChrome();
  showFolderMails();
}

async function loadFolders() {
  try {
    if (isGaia()) {
      const cached = folderCache.get(GAIA_ID);
      if (cached) renderFolderNav(cached);
      const d = await api('/api/gaia/folders');
      const folders = d.folders || [];
      folderCache.set(GAIA_ID, folders);
      applyUnreadSummary(d.unread_summary);
      renderFolderNav(folders);
      renderAccountBar();
      return;
    }
    const cached = folderCache.get(state.acc);
    if (cached) renderFolderNav(cached);
    const folders = await api('/api/folders?acc=' + state.acc);
    folderCache.set(state.acc, folders);
    renderFolderNav(folders);
  } catch (err) {
    if (!folderCache.get(state.acc)) toast('加载文件夹失败：' + err.message);
  }
}

/* ---------- 邮件列表 ---------- */
function showFolderMails(opts = {}) {
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  if (!opts.fresh && cached && (cached.items.length || cached.deep)) {
    cached.items = expandOtpThreads(cached.items);
    paintList(cached);
    updateListLoading();
    setListSyncing(false);
    fetchFolderMails({ background: true, merge: true });
    return;
  }
  state.page = 0;
  state.hasMore = true;
  showSkeletons(5);
  updateBatchBar();
  updateListLoading();
  // 本地没有这个文件夹的数据，需要联网同步：中间栏先给出提示，别让用户干等
  setListSyncing(true, '正在同步…');
  fetchFolderMails({ head: true, fresh: !!opts.fresh });
}

async function refreshMails() {
  invalidateMailCache(state.folder);
  state.page = 0;
  state.hasMore = true;
  state.selected.clear();
  updateBatchBar();
  await fetchFolderMails({ fresh: true });
}

async function fetchFolderMails(opts = {}) {
  if (!state.acc) return;
  const acc = state.acc;
  const folder = state.folder;
  const q = activeSearchQuery();
  const gen = opts.background ? state.listGen : ++state.listGen;
  const page = opts.append ? state.page + 1 : 1;
  if (!opts.append && !opts.background) state.loading = true;
  try {
    const params = new URLSearchParams({ folder, page });
    if (!isGaia()) params.set('acc', acc);
    if (q) params.set('q', q);
    if (opts.fresh) params.set('fresh', '1');
    if (opts.deep) params.set('deep', '1');
    if (opts.head) params.set('head', '1');
    if (isGaia() && (opts.head || opts.fresh) && !opts.background) {
      if ($('listLoading')) {
        $('listLoading').hidden = false;
        $('listLoading').textContent = '正在扫描全部邮箱，首次可能较久…';
      }
    }
    const d = await api((isGaia() ? '/api/gaia/mails?' : '/api/mails?') + params);
    // 后端判断"这个文件夹该不该提示等待"（收件箱有内容可看时不提示）
    if (state.acc === acc && state.folder === folder) {
      const show = d.show_syncing != null ? !!d.show_syncing : !!d.syncing;
      setListSyncing(show, show ? '正在同步…' : '');
    }
    applyUnreadSummary(d.unread_summary);
    renderAccountBar();
    if (d.sent_unread != null) applySentUnread(d.sent_unread);
    if (d.sibling_folder && d.sibling_items && d.sibling_items.length
        && !mailCache.has(mailCacheKey(d.sibling_folder, '', acc))) {
      mailCache.set(mailCacheKey(d.sibling_folder, '', acc), {
        items: d.sibling_items,
        total: d.sibling_items.length,
        page: 1,
        hasMore: true,
        deep: false,
      });
    }
    const prev = mailCache.get(mailCacheKey(folder, q, acc));
    const more = d.has_more != null ? !!d.has_more : (d.page * d.per_page < d.total);
    if (opts.background && prev && prev.page > 1 && !opts.fresh && !opts.merge && !opts.deep) {
      prev.hasMore = more || prev.hasMore;
      return;
    }
    let items;
    const incoming = applyIncomingStars(
      (d.items || []).filter((m) => !state.goneKeys.has(threadKeyOf(m))),
      folder
    );
    if (opts.append && prev) items = prev.items.concat(incoming);
    else if ((opts.merge || opts.deep || opts.background) && prev && prev.items.length) {
      const map = new Map(prev.items.map((m) => [threadKeyOf(m), m]));
      incoming.forEach((m) => map.set(threadKeyOf(m), m));
      items = [...map.values()].filter((m) => !state.goneKeys.has(threadKeyOf(m)));
    } else {
      items = incoming;
    }
    items = expandOtpThreads(items);
    const cached = {
      items,
      total: d.total,
      page: opts.append && prev ? d.page : (prev && prev.page > 1 ? prev.page : d.page),
      hasMore: more || !!(opts.merge && prev && prev.hasMore),
      deep: opts.deep || !d.needs_deep,
      filling: !!opts.head,
      unreadCount: d.unread_count != null ? Number(d.unread_count) : undefined,
    };
    mailCache.set(mailCacheKey(folder, q, acc), cached);
    showSyncErrors(d.errors);
    const round = opts._syncRound || 0;
    if (!opts.append && round < 3 && d.syncing && !(d.errors && d.errors.length)) {
      clearTimeout(state._syncTimer);
      state._syncTimer = setTimeout(() => {
        if (state.acc === acc && state.folder === folder) {
          fetchFolderMails({ background: true, merge: true, _syncRound: round + 1 });
        }
      }, 1600);
    } else {
      clearTimeout(state._syncTimer);
      // 重试到上限（或不需要再试）之后就不要一直挂着"正在同步"了
      if (round >= 3) setListSyncing(false);
    }
    if (state.acc === acc && state.folder === folder && state.q === q && gen === state.listGen) {
      if (opts.append) {
        syncList(cached);
      } else if (opts.merge || opts.deep || (opts.background && prev && prev.items.length)) {
        syncList(cached);
      } else {
        paintList(cached);
      }
      updateListLoading();
    }
  } catch (err) {
    if (!opts.background && gen === state.listGen) {
      setListSyncing(false);
      toast('加载失败：' + err.message);
    }
  } finally {
    if (gen === state.listGen) state.loading = false;
    updateListLoading();
  }
}

function updateListLoading() {
  const el = $('listLoading');
  if (!el) return;
  el.hidden = !state.loadingMore;
  el.textContent = '正在加载更早的邮件…';
}

/* 中间那栏的"正在同步…"提示：打开文件夹、数据不新鲜时显示 */
function setListSyncing(on, text) {
  const el = $('listSyncing');
  if (!el) return;
  if (text) el.textContent = text;
  el.hidden = !on;
}

async function loadMoreMails() {
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  if (state.loading || state.loadingMore || (cached && !cached.hasMore) || !state.acc) return;
  if (cached) {
    state.page = cached.page;
    state.hasMore = cached.hasMore;
  }
  if (!state.hasMore) return;
  state.loadingMore = true;
  updateListLoading();
  try {
    await fetchFolderMails({ append: true });
  } finally {
    state.loadingMore = false;
    updateListLoading();
  }
}

function applySentUnread(flag) {
  const sent = (state.folders || []).find((f) => f.label === '已发送');
  if (!sent) return;
  setFolderUnread(sent.id, !!flag, flag ? Math.max(1, folderUnreadCount(sent)) : 0);
}

function sortMailList(items) {
  return items.slice().sort((a, b) => {
    const pa = pinTime(threadKeyOf(a));
    const pb = pinTime(threadKeyOf(b));
    if (pa && pb) return pb - pa;
    if (pa) return -1;
    if (pb) return 1;
    return dateValue(b) - dateValue(a);
  });
}

function renderMailRows(items, opts = {}) {
  const html = items.map((m) => {
    const key = threadKeyOf(m);
    const pinned = isPinned(key);
    const uids = (m.thread_uids || [m.uid]).join(',');
    const refs = (m.thread_parts || []).map((p) => `${p.folder}|${p.uid}`).join(',');
    const count = m.thread_count > 1 ? ` · ${m.thread_count}` : '';
    const unread = !isTrashFolder() && m.unread;
    const showStar = state.folder !== STARRED_ID && !isBulkCleanFolder() && m.starred;
    return `
    <div class="mail-row ${opts.enter ? 'anim-enter' : ''} ${unread ? 'unread' : ''} ${pinned ? 'pinned' : ''} ${state.current && state.current.uid === String(m.uid) ? 'active' : ''}"
         data-uid="${esc(m.uid)}" data-key="${esc(key)}" data-uids="${esc(uids)}" data-refs="${esc(refs)}" data-starred="${m.starred ? '1' : '0'}" data-acc="${esc(m.acc || actionAcc() || '')}" data-folder="${esc(m.folder || mailFolder())}">
      <label class="mail-check"><input type="checkbox" ${state.selected.has(key) ? 'checked' : ''} /></label>
      <span class="mail-avatar-wrap">
        <span class="mail-avatar" style="background:${avatarColor(m.from)}">${esc(avatarLetter(m.from))}</span>
        <span class="unread-dot" aria-hidden="true"></span>
      </span>
      <div class="mail-main">
        <div class="mail-line">
          <span class="mail-from">${esc(m.from)}${count}</span>
          <span class="mail-date">${esc(formatDate(m.date || m.ts))}</span>
        </div>
        <span class="mail-subject">${esc(m.subject)}</span>
      </div>
      <span class="mail-star">${showStar ? STAR_SVG : ''}</span>
    </div>`;
  }).join('');
  $('mailList').insertAdjacentHTML('beforeend', html);
  $('mailList').querySelectorAll('.mail-row').forEach(bindRow);
  updateBatchBar();
}

function bindRow(row) {
  const key = row.dataset.key;
  const box = row.querySelector('input[type="checkbox"]');
  row.onclick = (e) => {
    if (e.target.closest('.mail-check')) return;
    openThread(row.dataset.uid, row.dataset.uids, key, row.dataset.refs, row);
  };
  row.oncontextmenu = (e) => {
    e.preventDefault();
    if (isTrashFolder()) return;
    showCtx(e.clientX, e.clientY, row);
  };
  box.onchange = () => {
    if (box.checked) state.selected.add(key);
    else state.selected.delete(key);
    row.classList.toggle('checked', box.checked);
    updateBatchBar();
  };
}

function gaiaFolderKind(folder) {
  if (!folder || folder === 'INBOX') return 'INBOX';
  if (folder === 'SENT' || folder === 'DRAFTS') return folder;
  const packs = [];
  folderCache.forEach((folders) => packs.push(folders || []));
  packs.push(state.folders || []);
  for (const folders of packs) {
    const f = (folders || []).find((x) => x.id === folder);
    if (!f) continue;
    if (f.label === '已发送') return 'SENT';
    if (f.label === '草稿') return 'DRAFTS';
    if (f.label === '收件箱') return 'INBOX';
  }
  return 'INBOX';
}

function applyFolderCacheUnreadDelta(accId, folderId, delta) {
  if (!delta) return;
  const folders = folderCache.get(accId);
  if (!folders) return;
  const f = folders.find((x) => x.id === folderId)
    || (folderId === 'INBOX' && folders.find((x) => x.label === '收件箱'))
    || (folderId === 'SENT' && folders.find((x) => x.label === '已发送'))
    || (folderId === 'DRAFTS' && folders.find((x) => x.label === '草稿'));
  if (!f) return;
  const next = Math.max(0, folderUnreadCount(f) + delta);
  f.unread_count = next;
  f.unread = next > 0;
}

function mailMatchesReadTarget(m, accId, uid, key) {
  const mAcc = Number(m && m.acc || 0);
  if (mAcc && mAcc !== Number(accId)) return false;
  if (key && threadKeyOf(m) === key) return true;
  return !!(uid && String(m.uid) === String(uid));
}

function markMailReadInCaches(accId, uid, key, folder) {
  const aid = Number(accId);
  if (!aid) return;
  let flippedAcc = 0;
  let flippedGaia = 0;
  for (const [ck, cached] of mailCache) {
    if (!cached || !cached.items) continue;
    const cacheAcc = String(ck).split('|')[0];
    const cacheIsGaia = cacheAcc === GAIA_ID;
    const cacheIsAcc = Number(cacheAcc) === aid;
    if (!cacheIsGaia && !cacheIsAcc) continue;
    let flipped = 0;
    cached.items.forEach((m) => {
      if (!mailMatchesReadTarget(m, aid, uid, key)) return;
      if (!m.unread) return;
      m.unread = false;
      flipped += 1;
      if (cacheIsGaia) flippedGaia += 1;
      if (cacheIsAcc) flippedAcc += 1;
    });
    if (flipped > 0 && cached.unreadCount != null) {
      cached.unreadCount = Math.max(0, cached.unreadCount - flipped);
    }
  }
  const realFolder = folder || 'INBOX';
  if (isGaia()) {
    applyFolderCacheUnreadDelta(aid, realFolder, -(flippedAcc || 1));
  } else {
    applyFolderCacheUnreadDelta(GAIA_ID, gaiaFolderKind(realFolder), -(flippedGaia || 1));
  }
  // 同步更新账号红点数字
  if (state.unreadPerAccount[aid] != null) {
    state.unreadPerAccount[aid] = Math.max(0, (state.unreadPerAccount[aid] || 0) - (flippedAcc || 1));
  }
}

function markRowRead(uid) {
  const row = $('mailList').querySelector(`.mail-row[data-uid="${CSS.escape(String(uid))}"]`);
  const key = row ? row.dataset.key : '';
  const accId = Number((row && row.dataset.acc) || actionAcc());
  const folder = (row && row.dataset.folder) || mailFolder();
  if (row) row.classList.remove('unread');
  markMailReadInCaches(accId, uid, key, folder);
  renderAccountBar();
  refreshFolderUnreadFromList();
}

function setRowActive(uid) {
  $('mailList').querySelectorAll('.mail-row').forEach((row) => {
    row.classList.toggle('active', row.dataset.uid === String(uid));
  });
}

function updateBatchBar() {
  const n = state.selected.size;
  const bulk = isBulkCleanFolder();
  const bar = $('batchBar');
  if (bar) {
    const hasVisible = [...bar.querySelectorAll('button')].some((b) => !b.hidden);
    bar.hidden = n === 0 || !hasVisible;
  }
  if ($('trashTools')) $('trashTools').hidden = true;
  document.querySelector('.list-pane').classList.toggle('selecting', n > 0);
  const boxes = [...$('mailList').querySelectorAll('.mail-row input[type="checkbox"]')];
  if ($('selectAllBox')) $('selectAllBox').checked = boxes.length > 0 && boxes.every((b) => b.checked);
}

function clearSelection() {
  state.selected.clear();
  $('mailList').querySelectorAll('.mail-row').forEach((row) => {
    row.classList.remove('checked');
    const box = row.querySelector('input[type="checkbox"]');
    if (box) box.checked = false;
  });
  updateBatchBar();
}

function selectedRows() {
  return [...$('mailList').querySelectorAll('.mail-row')].filter((row) => state.selected.has(row.dataset.key));
}

function restoreParts(parts) {
  (parts || []).forEach((p) => {
    if (!p || !p.uid) return;
    silentApi('/api/restore', { method: 'POST', body: { acc: p.acc || actionAcc(), folder: p.folder || mailFolder(), uid: p.uid } });
  });
}

async function restoreSelected() {
  const rows = selectedRows();
  if (!rows.length) {
    toast('请先选择要恢复的邮件');
    return;
  }
  try {
    const keys = new Set(rows.map((row) => row.dataset.key));
    const anim = animateRowsOut(rows, 'delete');
    for (const row of rows) restoreParts(rowParts(row));
    toast('已恢复到收件箱');
    await anim;
    dropKeysFromCache(keys);
    invalidateMailCache('INBOX');
    if (state.current && keys.has(state.current.thread_key)) closeMessage();
    clearSelection();
  } catch (err) {
    toast('恢复失败：' + err.message);
  }
}

async function purgeSelected() {
  const rows = selectedRows();
  if (!rows.length) {
    toast('请先选择要粉碎的邮件');
    return;
  }
  if (!await askConfirm(`粉碎选中的 ${rows.length} 封邮件？此操作不可恢复。`)) return;
  try {
    const keys = new Set(rows.map((row) => row.dataset.key));
    const anim = animateRowsOut(rows, 'purge');
    for (const row of rows) purgeParts(rowParts(row));
    toast('已粉碎');
    await anim;
    dropKeysFromCache(keys);
    if (state.current && keys.has(state.current.thread_key)) closeMessage();
    clearSelection();
  } catch (err) {
    toast('粉碎失败：' + err.message);
  }
}

/* ---------- 读信 / 会话 ---------- */
function openThread(uid, uidsCsv, key, refsCsv, row) {
  const cachedHint = (mailCache.get(mailCacheKey(state.folder, state.q)) || { items: [] }).items
    .find((m) => threadKeyOf(m) === key || String(m.uid) === String(uid));
  const folder = (cachedHint && cachedHint.folder) || (row && row.dataset.folder) || mailFolder();
  const accId = Number((cachedHint && cachedHint.acc) || (row && row.dataset.acc) || actionAcc());
  const uids = (uidsCsv || uid).split(',').filter(Boolean);
  const refs = (refsCsv || '').split(',').filter(Boolean);
  const subject = row && row.querySelector('.mail-subject') ? row.querySelector('.mail-subject').textContent : '';
  const from = row && row.querySelector('.mail-from') ? row.querySelector('.mail-from').textContent : '';
  const dateTxt = row && row.querySelector('.mail-date') ? row.querySelector('.mail-date').textContent : '';
  const openGen = String(uid) + '|' + key + '|' + Date.now();
  state.current = {
    uid: String(uid),
    acc: accId,
    folder,
    from_addr: from,
    subject: displaySubject(subject),
    thread_key: key,
    thread_uids: uids,
    thread_parts: refs.map((r) => {
      const i = r.indexOf('|');
      const part = i === -1 ? { folder, uid: r } : { folder: r.slice(0, i) || folder, uid: r.slice(i + 1) };
      part.acc = accId;
      return part;
    }),
    starred: state.folder === STARRED_ID || !!(row && row.dataset.starred === '1'),
    openGen,
  };
  $('emptyRead').hidden = true;
  $('messageView').hidden = false;
  $('messageView').classList.add('is-loading');
  $('msgSubject').textContent = state.current.subject;
  const cachedItem = (mailCache.get(mailCacheKey(state.folder, state.q)) || { items: [] }).items
    .find((m) => threadKeyOf(m) === key || String(m.uid) === String(uid));
  renderParties({
    from: (cachedItem && cachedItem.from) || from,
    from_addr: (cachedItem && cachedItem.from_addr) || '',
    to: cachedItem && cachedItem.to,
  });
  refreshReplyPeople();
  $('msgDate').textContent = dateTxt;
  state.trMode = defaultTrMode();
  setTrLamp(state.trMode);
  $('threadView').innerHTML = '<p class="thread-loading">正在加载正文…</p>';
  if (state.folder === STARRED_ID) state.current.starred = true;
  setStarBtn(state.current.starred);
  $('pinBtn').textContent = isPinned(key) ? '取消置顶' : '置顶';
  applyFolderChrome();
  resetReplyBox();
  $('replySentList').innerHTML = '';
  markRowRead(uid);
  setRowActive(uid);
  (async () => {
    try {
      let d;
      try {
        const params = new URLSearchParams({ acc: accId, folder, uids: uids.join(',') });
        if (refs.length) params.set('refs', refs.join(','));
        d = await api('/api/thread?' + params);
        if (!(d.messages || []).length) throw new Error('empty');
      } catch (err) {
        const one = await api('/api/mail?' + new URLSearchParams({ acc: accId, folder, uid: String(uid) }));
        d = { messages: [one] };
      }
      if (!state.current || state.current.openGen !== openGen) return;
      const messages = d.messages || [];
      if (!messages.length) throw new Error('邮件不存在');
      const latest = messages[messages.length - 1];
      const first = messages[0];
      state.current.threadMessages = messages;
      refreshReplyPeople(messages);
      state.current.from_addr = first.from_addr || latest.from_addr || state.current.from_addr;
      state.current.subject = displaySubject(first.subject || latest.subject || state.current.subject);
      state.current.body = latest.body;
      state.current.message_id = latest.message_id;
      state.current.starred = messages.some((m) => m.starred) || state.folder === STARRED_ID || state.current.starred;
      state.current.origSubject = state.current.subject;
      $('msgSubject').textContent = state.current.subject;
      renderParties(first);
      $('msgDate').textContent = formatDate(latest.date) || dateTxt;
      renderThread(messages);
      setStarBtn(state.current.starred);
      if (!$('replyComposer').hidden) {
        const cur = addrFieldValue('replyToInput').trim();
        if (!cur || isOwnAddress(cur) || !parseMailbox(cur).email) {
          setAddrField('replyToInput', replyDefaultTo());
        }
        renderReplyPeople();
      }
      $('messageView').classList.remove('is-loading');
      showSyncErrors(d.errors);
    } catch (err) {
      if (state.current && state.current.openGen === openGen) {
        $('messageView').classList.remove('is-loading');
        $('threadView').innerHTML = '<p class="thread-loading">正文暂时无法加载</p>';
        toast('打开失败：' + err.message);
      }
    }
  })();
}

function parseMailbox(raw) {
  const s = String(raw || '').replace(/\s·\s+\d+\s*$/, '').trim();
  const m = s.match(/^(.*?)\s*<\s*([^>]+)\s*>$/);
  if (m) return { name: m[1].replace(/^["'\s]+|["'\s]+$/g, ''), email: m[2].trim() };
  if (s.includes('@')) return { name: '', email: s };
  return { name: s, email: '' };
}

function mailboxAlias(email) {
  const e = String(email || '').trim().toLowerCase();
  if (!e) return '';
  const a = (state.accounts || []).find((x) => (x.email || '').toLowerCase() === e);
  return a ? String(a.name || '').trim() : '';
}

function formatPartyLine(name, email, fallback) {
  const fromName = parseMailbox(name || '');
  const parsed = parseMailbox(fallback || '');
  const addr = String(email || fromName.email || parsed.email || '').trim();
  const shown = mailboxAlias(addr) || fromName.name || parsed.name || addr;
  if (shown && addr && shown.toLowerCase() !== addr.toLowerCase()) return `${shown} <${addr}>`;
  return shown || addr || '';
}

function formatAddrList(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  return s.split(',').map((part) => formatPartyLine('', '', part.trim())).filter(Boolean).join(', ');
}

function renderParties(m, nameOverrides) {
  if (state.current && !nameOverrides) {
    state.current.headerParties = {
      from: m.from, from_addr: m.from_addr, to: m.to, cc: m.cc,
    };
  }
  const src = (m && (m.from || m.from_addr || m.to || m.cc)) ? m : (state.current && state.current.headerParties) || {};
  const fromName = nameOverrides && nameOverrides.from;
  const from = formatPartyLine(fromName != null ? fromName : src.from, src.from_addr, src.from);
  const to = formatAddrList(src.to);
  const cc = formatAddrList(src.cc);
  const lines = [];
  if (from) lines.push(`<div><b>From</b> ${esc(from)}</div>`);
  if (to) lines.push(`<div><b>To</b> ${esc(to)}</div>`);
  if (cc) lines.push(`<div><b>CC</b> ${esc(cc)}</div>`);
  $('msgParties').innerHTML = lines.join('');
}

function senderDisplayName(m) {
  const parsed = parseMailbox((m && m.from) || '');
  const email = (m && m.from_addr) || parsed.email || '';
  return mailboxAlias(email) || parsed.name || '';
}

function restoreHeader() {
  if (!state.current) return;
  if (state.current.origSubject != null) $('msgSubject').textContent = state.current.origSubject;
  if (state.current.headerParties) renderParties(state.current.headerParties, {});
}

async function translateHeader(dest) {
  if (!state.current) return false;
  const subj = state.current.origSubject || $('msgSubject').textContent || '';
  const name = senderDisplayName(state.current.headerParties || {});
  const jobs = [];
  if (subj && looksForeign(subj, dest)) jobs.push({ kind: 'subj', text: subj });
  if (name && looksForeign(name, dest)) jobs.push({ kind: 'from', text: name });
  if (!jobs.length) {
    restoreHeader();
    return false;
  }
  const texts = await translatePieces(jobs.map((j) => j.text), dest);
  let fromName = name;
  jobs.forEach((j, i) => {
    if (j.kind === 'subj') $('msgSubject').textContent = texts[i] || j.text;
    if (j.kind === 'from') fromName = texts[i] || j.text;
  });
  renderParties(state.current.headerParties || {}, { from: fromName });
  return true;
}

function defaultTrMode() {
  return state.translateAuto ? 'zh' : 'orig';
}

function setTrLamp(mode) {
  const el = $('trLamp');
  if (!el) return;
  const on = mode === 'zh';
  el.classList.toggle('is-on', on);
  el.setAttribute('aria-pressed', on ? 'true' : 'false');
  el.title = on ? '原文' : '翻译';
  el.setAttribute('aria-label', on ? '原文' : '翻译');
}

function currentMailTarget() {
  return ($('trTarget') && $('trTarget').value) || state.translateTarget || 'zh-CN';
}

function alreadyInTarget(text, target) {
  const s = String(text || '');
  if (target === 'zh-CN' || target === 'zh-TW') return looksChinese(s);
  if (target === 'ja') return /[\u3040-\u30ff]/.test(s);
  if (target === 'ko') return /[\uac00-\ud7af]/.test(s);
  return /[A-Za-z]/.test(s) && !looksChinese(s) && !/[\u3040-\u30ff\uac00-\ud7af]/.test(s);
}

function looksForeign(text, target) {
  const s = String(text || '').trim();
  if (!s) return false;
  if (/^[\w.+-]+@[\w.-]+$/.test(s) || /^https?:\/\//i.test(s)) return false;
  return !alreadyInTarget(s, target || currentMailTarget());
}

function mailRoot(el) {
  if (el && el.shadowRoot) return el.shadowRoot.querySelector('.mail-root') || el.shadowRoot;
  return el;
}

function stashThreadBodies() {
  document.querySelectorAll('#threadView .thread-msg-body').forEach((el) => {
    if (el.dataset.stashed) return;
    el.dataset.stashed = '1';
    el.dataset.isHtml = el.classList.contains('is-html') ? '1' : '0';
    const root = mailRoot(el);
    el.dataset.origHtml = el.dataset.isHtml === '1' ? (root.innerHTML || '') : '';
    el.dataset.origText = (root.innerText || root.textContent || '').trim();
  });
}

function trCacheKey(text) {
  const s = String(text || '').trim();
  return s.slice(0, 240) + '|' + s.length;
}

function restoreBody(el) {
  const isHtml = el.dataset.isHtml === '1';
  el.classList.toggle('is-html', isHtml);
  if (isHtml) mountMailHtml(el, el.dataset.origHtml || '');
  else el.textContent = el.dataset.origText || '';
}

function bodyTextNodes(el) {
  const walker = document.createTreeWalker(mailRoot(el), NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      const parent = node.parentElement;
      if (parent && parent.closest('script, style')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  return nodes;
}

function cleanTranslation(text) {
  return String(text || '')
    .replace(/(?:\[\s*\[\s*PM\s*:\s*\d+\s*\]\s*\]|#{2,}\s*\d+\s*#{2,})/gi, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/[ \t]*\n[ \t]*/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function parseMarkedTranslation(raw, count) {
  if (!raw || !count) return null;
  if (count === 1) {
    const text = cleanTranslation(raw);
    return text ? [text] : null;
  }
  const usePm = /\[\s*\[\s*PM\s*:\s*\d+\s*\]\s*\]/i.test(raw);
  const re = usePm
    ? /\[\s*\[\s*PM\s*:\s*(\d+)\s*\]\s*\]\s*([\s\S]*?)(?=\n?\[\s*\[\s*PM\s*:\s*\d+\s*\]\s*\]|$)/gi
    : /#{2,}\s*(\d+)\s*#{2,}\s*([\s\S]*?)(?=\n?#{2,}\s*\d+\s*#{2,}|$)/g;
  const found = [];
  let m;
  while ((m = re.exec(raw))) {
    const i = Number(m[1]);
    const text = cleanTranslation(m[2]);
    if (Number.isFinite(i) && text) found.push({ i, text });
  }
  const fill = (offset) => {
    const out = new Array(count);
    let hit = 0;
    found.forEach(({ i, text }) => {
      const idx = i - offset;
      if (idx >= 0 && idx < count && out[idx] == null) {
        out[idx] = text;
        hit += 1;
      }
    });
    return hit === count ? out : null;
  };
  return fill(0) || fill(1);
}

function cacheTr(dest, src, text) {
  const out = cleanTranslation(text);
  if (!out) return '';
  state.trEn.set(dest + '|' + src.length + '|' + src.slice(0, 180), out);
  return out;
}

async function translateOne(text, target) {
  const src = String(text || '').trim();
  if (!src) return text;
  const dest = target || currentMailTarget();
  const key = dest + '|' + src.length + '|' + src.slice(0, 180);
  if (state.trEn.has(key)) return cleanTranslation(state.trEn.get(key)) || text;
  const d = await api('/api/translate', {
    method: 'POST',
    body: { text: src.slice(0, 4000), target: dest, keep_format: false },
  });
  const out = cacheTr(dest, src, (d && d.text) || '');
  if (!out) throw new Error('翻译结果为空');
  return out;
}

async function translatePieces(pieces, target) {
  const dest = target || currentMailTarget();
  if (!pieces.length) return [];
  if (pieces.length === 1) return [await translateOne(pieces[0], dest)];
  const packed = pieces.map((p, i) => `[[PM:${i}]]\n${p}`).join('\n');
  try {
    const d = await api('/api/translate', {
      method: 'POST',
      body: { text: packed.slice(0, 7000), target: dest, keep_format: true },
    });
    const parsed = parseMarkedTranslation(d && d.text, pieces.length);
    if (parsed) {
      parsed.forEach((text, i) => cacheTr(dest, pieces[i], text));
      return parsed;
    }
  } catch (err) {
    if (pieces.length === 1) throw err;
  }
  const mid = Math.ceil(pieces.length / 2);
  const left = await translatePieces(pieces.slice(0, mid), dest);
  const right = await translatePieces(pieces.slice(mid), dest);
  return left.concat(right);
}

async function applyThreadTranslate(mode) {
  const next = mode === 'zh' ? 'zh' : 'orig';
  state.trMode = next;
  setTrLamp(next);
  stashThreadBodies();
  const bodies = [...document.querySelectorAll('#threadView .thread-msg-body')];
  bodies.forEach(restoreBody);
  restoreHeader();
  hardenMailLinks($('threadView'));
  if (next === 'orig') return;
  const dest = currentMailTarget();
  const jobs = [];
  bodies.forEach((el) => {
    const nodes = bodyTextNodes(el);
    const pieces = nodes.map((node) => {
      const raw = node.nodeValue || '';
      const m = raw.match(/^(\s*)([\s\S]*?)(\s*)$/);
      return { node, lead: m[1], core: m[2], tail: m[3] };
    }).filter((p) => p.core && looksForeign(p.core, dest));
    if (pieces.length) jobs.push({ el, pieces });
  });
  const lamp = $('trLamp');
  const gen = ++state.trGen;
  if (lamp) lamp.classList.add('is-busy');
  try {
    const headerDone = translateHeader(dest);
    const bodyDone = Promise.all(jobs.map(async (job) => {
      const translated = await translatePieces(job.pieces.map((p) => p.core), dest);
      if (gen !== state.trGen) return;
      job.pieces.forEach((p, i) => {
        const text = cleanTranslation(translated[i] || '');
        p.node.nodeValue = p.lead + (text || p.core) + p.tail;
      });
    }));
    const hadHeader = await headerDone;
    await bodyDone;
    if (gen !== state.trGen) return;
    if (!jobs.length && !hadHeader) {
      toast('这封邮件没有需要翻译的外文');
      state.trMode = 'orig';
      setTrLamp('orig');
    }
  } catch (err) {
    if (gen !== state.trGen) return;
    toast('翻译失败：' + err.message);
    state.trMode = 'orig';
    setTrLamp('orig');
    bodies.forEach(restoreBody);
    restoreHeader();
  } finally {
    if (gen === state.trGen && lamp) lamp.classList.remove('is-busy');
  }
}

function bindTrSwitch() {
  const el = $('trLamp');
  if (!el || el.dataset.bound) return;
  el.dataset.bound = '1';
  el.addEventListener('click', () => {
    applyThreadTranslate(el.classList.contains('is-on') ? 'orig' : 'zh');
  });
}

function hardenMailLinks(root) {
  const box = root || $('readingPane');
  if (!box) return;
  const scope = box.querySelectorAll ? box : null;
  const links = scope ? box.querySelectorAll('a[href], area[href]') : [];
  links.forEach((a) => {
    const href = (a.getAttribute('href') || '').trim();
    if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript:')) return;
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  });
}

function bindMailLinkGuard() {
  const pane = $('readingPane');
  if (!pane || pane.dataset.linkGuard) return;
  pane.dataset.linkGuard = '1';
  pane.addEventListener('click', (e) => {
    const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
    const a = path.find((n) => n && (n.tagName === 'A' || n.tagName === 'AREA'))
      || (e.target.closest && e.target.closest('a[href], area[href]'));
    if (!a || !a.getAttribute) return;
    const href = (a.getAttribute('href') || '').trim();
    if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript:')) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    if (window.pumailAPI && window.pumailAPI.openExternal) {
      window.pumailAPI.openExternal(a.href);
    } else {
      window.open(a.href, '_blank', 'noopener,noreferrer');
    }
  }, true);
  pane.addEventListener('submit', (e) => {
    if (e.target.closest && e.target.closest('.thread-msg-body, .atts')) e.preventDefault();
  }, true);
}

function bindExternalLinks() {
  if (document.body.dataset.extLinkGuard) return;
  document.body.dataset.extLinkGuard = '1';
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[target="_blank"]');
    if (!a || !a.href) return;
    const href = (a.getAttribute('href') || '').trim();
    if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript:')) return;
    e.preventDefault();
    e.stopPropagation();
    if (window.pumailAPI && window.pumailAPI.openExternal) {
      window.pumailAPI.openExternal(a.href);
    } else {
      window.open(a.href, '_blank', 'noopener,noreferrer');
    }
  }, true);
}

const MAIL_SHADOW_CSS = `
:host {
  display: block;
  background: #fff;
  color: #222;
  border-radius: 10px;
  overflow-x: auto;
  max-width: 100%;
}
:host(.is-plain-html) {
  background: transparent;
  color: #f4f6f8;
  border-radius: 0;
}
:host(.is-plain-html.is-light) {
  color: #111827;
}
.mail-root {
  font: 15px/1.65 "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: inherit;
}
:host(.is-plain-html) .mail-root,
:host(.is-plain-html) .mail-root * {
  background: transparent !important;
  background-color: transparent !important;
  color: inherit !important;
}
:host(.is-plain-html) .mail-root a[href],
:host(.is-plain-html) .mail-root a[href] * { color: #34d399 !important; }
:host(.is-plain-html.is-light) .mail-root a[href],
:host(.is-plain-html.is-light) .mail-root a[href] * { color: #3d5a73 !important; }
:host(.is-plain-html) .mail-root hr {
  border: 0;
  border-top: 1px solid rgba(255,255,255,.45);
}
:host(.is-plain-html.is-light) .mail-root hr {
  border-top-color: rgba(17,24,39,.28);
}
.mail-root img { max-width: 100%; height: auto; }
.mail-root img[width="1"],
.mail-root img[height="1"] {
  max-width: 1px !important;
  width: 1px !important;
  height: 1px !important;
}
.mail-root a[href] { cursor: pointer; }
`;

function htmlLooksPlain(html) {
  const s = String(html || '');
  if (!s.trim()) return true;
  const imgs = (s.match(/<img\b[^>]*>/gi) || []).filter((t) => {
    return !(/width\s*=\s*["']?1\b/i.test(t) || /height\s*=\s*["']?1\b/i.test(t)
      || /width:\s*1px/i.test(t) || /height:\s*1px/i.test(t));
  });
  if (imgs.length >= 2) return false;
  if (/background-image\s*:/i.test(s)) return false;
  return true;
}

function syncMailHtmlTheme() {
  const light = currentTheme() === 'light';
  document.querySelectorAll('.thread-msg-body.is-html').forEach((el) => {
    el.classList.toggle('is-light', light);
  });
}

function mountMailHtml(host, html) {
  host.classList.add('thread-msg-body', 'is-html');
  host.classList.toggle('is-plain-html', htmlLooksPlain(html));
  host.classList.toggle('is-light', currentTheme() === 'light');
  const shadow = host.shadowRoot || host.attachShadow({ mode: 'open' });
  shadow.innerHTML = `<style>${MAIL_SHADOW_CSS}</style><div class="mail-root">${html || ''}</div>`;
  hardenMailLinks(shadow);
}

function currentAccEmail() {
  const id = Number((state.current && state.current.acc) || actionAcc() || state.acc || 0);
  const a = (state.accounts || []).find((x) => Number(x.id) === id);
  return String((a && a.email) || '').trim().toLowerCase();
}

function isOwnAddress(raw) {
  const email = String((parseMailbox(raw).email || raw || '')).trim().toLowerCase();
  if (!email || !email.includes('@')) return false;
  const cur = currentAccEmail();
  return !!(cur && email === cur);
}

function messageIsMine(m) {
  if (!m) return false;
  if (m.mine) return true;
  const email = String((parseMailbox(m.from_addr || m.from).email || '')).trim().toLowerCase();
  return !!(email && isOwnAddress(email));
}

function formatSentStamp(raw) {
  if (raw == null || raw === '') return '';
  const ms = dateValue(raw);
  if (!ms) return String(raw).replace(/\s+\([^)]+\)\s*$/, '').replace(/ GMT.*$/, '').trim();
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function buildThreadMeta(m) {
  const meta = document.createElement('div');
  meta.className = 'thread-msg-meta';
  const from = formatPartyLine(m.from, m.from_addr, m.from);
  const to = formatAddrList(m.to);
  const cc = formatAddrList(m.cc);
  const sent = formatSentStamp(m.date || m.ts);
  const subject = String(m.subject || (state.current && state.current.subject) || '').trim();
  const rows = [];
  if (subject) rows.push(['Subject:', subject]);
  if (from) rows.push(['From:', from]);
  if (to) rows.push(['To:', to]);
  if (cc) rows.push(['CC:', cc]);
  if (sent) rows.push(['Sent:', sent]);
  rows.forEach(([k, v]) => {
    const line = document.createElement('div');
    const key = document.createElement('b');
    key.textContent = k;
    line.appendChild(key);
    line.appendChild(document.createTextNode(' ' + v));
    meta.appendChild(line);
  });
  return meta;
}

function appendThreadMessage(m, box) {
  const host = box || $('threadView');
  const sec = document.createElement('section');
  sec.className = 'thread-msg' + (messageIsMine(m) ? ' mine' : '');
  const card = document.createElement('div');
  card.className = 'thread-msg-card';
  card.appendChild(buildThreadMeta(m));
  if (m.html) {
    const htmlHost = document.createElement('div');
    mountMailHtml(htmlHost, m.html);
    card.appendChild(htmlHost);
  } else {
    const body = document.createElement('div');
    body.className = 'thread-msg-body';
    body.textContent = m.body || '(无正文)';
    card.appendChild(body);
  }
  sec.appendChild(card);
  const atts = m.attachments || [];
  if (atts.length) {
    const attBox = document.createElement('div');
    attBox.className = 'atts';
    atts.forEach((a) => {
      const p = new URLSearchParams({
        acc: (state.current && state.current.acc) || actionAcc(),
        folder: m.folder || mailFolder(),
        uid: m.uid,
        idx: a.idx,
      });
      const link = document.createElement('a');
      link.href = '/api/attachment?' + p;
      link.textContent = `📎 ${a.name} (${(a.size / 1024).toFixed(1)} KB)`;
      attBox.appendChild(link);
    });
    sec.appendChild(attBox);
  }
  host.appendChild(sec);
  return sec;
}

function renderThread(messages) {
  const box = $('threadView');
  const ordered = messages.slice().sort((a, b) => dateValue(a) - dateValue(b));
  const chat = ordered.some((m) => messageIsMine(m));
  box.classList.toggle('is-chat', chat);
  box.innerHTML = '';
  ordered.forEach((m) => appendThreadMessage(m, box));
  stashThreadBodies();
  hardenMailLinks(box);
  applyThreadTranslate(state.trMode || defaultTrMode());
}

/* ---------- 写信 ---------- */
const COMPOSE_SCENES = [
  {
    id: 'mom',
    label: '报平安',
    to: '妈妈 <mom@familymail.com>',
    subject: '到了，别担心',
    body: '妈，高铁到了，一切顺利。今晚不回家吃饭，手机有电。',
  },
  {
    id: 'hotpot',
    label: '约饭',
    to: '朋友W <w.li@qq.com>',
    subject: '周末出来吃饭？',
    body: '那家自助潮牛火锅，周六去？我六点在店门口等你。人多的话提前说一声。',
  },
  {
    id: 'bills',
    label: '水电费',
    to: '室友小陈 <chen.room@outlook.com>',
    subject: '这个月水电费',
    body: '这个月水电一共 186，我先垫了。你转我就行，微信支付宝都行。',
  },
];

function startCompose(prefill) {
  if (!state.accounts.length) {
    toast('请先添加邮箱账号');
    openAccountForm();
    return;
  }
  if (isGaia()) {
    openSendAccPicker(prefill);
    return;
  }
  state.composeAcc = state.acc;
  openCompose(prefill);
}

function openSendAccPicker(prefill) {
  state._composePrefill = prefill || null;
  const box = $('sendAccList');
  if (!box) return;
  box.innerHTML = state.accounts.map((a) => `
    <button type="button" data-id="${a.id}">
      <span class="avatar">${esc(nameInitial(a.name, a.email))}</span>
      <span class="send-acc-meta">
        <strong>${esc(a.name || a.email.split('@')[0])}</strong>
        <small>${esc(a.email)}</small>
      </span>
    </button>`).join('');
  $('sendAccOverlay').hidden = false;
}

function closeSendAccPicker() {
  const el = $('sendAccOverlay');
  if (el) el.hidden = true;
  state._composePrefill = null;
}

function pickSendAccount(id) {
  const acc = Number(id);
  if (!acc || !state.accounts.some((a) => a.id === acc)) return;
  const prefill = state._composePrefill || null;
  state.composeAcc = acc;
  const el = $('sendAccOverlay');
  if (el) el.hidden = true;
  state._composePrefill = null;
  openCompose(prefill);
}

function renderComposeScenes(activeId) {
  const box = $('composeScenes');
  const picks = $('composeScenePicks');
  if (!box || !picks) return;
  const demo = isDemoAccount(composeAccount());
  box.hidden = !demo || !!state._composePrefill;
  if (box.hidden) return;
  picks.innerHTML = COMPOSE_SCENES.map((s) => (
    `<button type="button" class="compose-scene ${s.id === activeId ? 'active' : ''}" data-scene="${s.id}">${esc(s.label)}</button>`
  )).join('');
}

function applyComposeScene(id, force) {
  const scene = COMPOSE_SCENES.find((s) => s.id === id) || COMPOSE_SCENES[1];
  state.composeScene = scene.id;
  renderComposeScenes(scene.id);
  if (!force && state._composePrefill) return;
  setAddrField('toInput', scene.to);
  $('subjectInput').value = scene.subject;
  $('bodyInput').innerHTML = String(scene.body)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
}

function openCompose(prefill) {
  $('overlay').hidden = false;
  $('composeTitle').textContent = prefill ? prefill.title : '新邮件';
  state._composePrefill = prefill || null;
  setAddrField('toInput', prefill ? (prefill.to || '') : '');
  setAddrField('ccInput', prefill ? (prefill.cc || '') : '');
  setAddrField('bccInput', prefill ? (prefill.bcc || '') : '');
  $('subjectInput').value = prefill ? (prefill.subject || '') : '';
  $('bodyInput').innerHTML = prefill && prefill.body
    ? String(prefill.body).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>')
    : '';
  $('attachInput').value = '';
  savedComposeRange = null;
  syncComposeFmt();
  setCcOpen(!!((prefill && (prefill.cc || prefill.bcc)) || addrFieldValue('ccInput') || addrFieldValue('bccInput')));
  if (!prefill && isDemoAccount(composeAccount())) {
    applyComposeScene(state.composeScene || 'hotpot', true);
  } else {
    renderComposeScenes('');
  }
  setTimeout(() => $('toInput').focus(), 0);
}

function closeCompose() {
  $('overlay').hidden = true;
  setCcOpen(false);
  state.composeAcc = null;
}

function composeBodyPlain() {
  return ($('bodyInput').innerText || '').replace(/\u00a0/g, ' ');
}

function composeBodyHtml() {
  return $('bodyInput').innerHTML || '';
}

let savedComposeRange = null;

function selectionInEditor() {
  const ed = $('bodyInput');
  const sel = window.getSelection();
  if (!ed || !sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const node = range.commonAncestorContainer;
  if (node !== ed && !ed.contains(node)) return null;
  return range;
}

function syncComposeFmt() {
  const bold = $('fmtBold');
  const italic = $('fmtItalic');
  if (bold) bold.classList.toggle('active', !!document.queryCommandState('bold'));
  if (italic) italic.classList.toggle('active', !!document.queryCommandState('italic'));
}

function applyComposeFormat(cmd) {
  const ed = $('bodyInput');
  if (ed) ed.focus();
  const range = savedComposeRange || selectionInEditor();
  if (range) {
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  document.execCommand(cmd, false, null);
  const next = selectionInEditor();
  savedComposeRange = next ? next.cloneRange() : null;
  syncComposeFmt();
}

async function composePayload() {
  return {
    acc: actionAcc(),
    to: addrFieldValue('toInput'),
    cc: addrFieldValue('ccInput'),
    bcc: addrFieldValue('bccInput'),
    subject: $('subjectInput').value,
    body: composeBodyPlain(),
    html: composeBodyHtml(),
    attachments: await collectAttachments(),
  };
}

async function submitCompose(e) {
  e.preventDefault();
  if (!actionAcc()) {
    toast('请先添加邮箱账号');
    openAccountForm();
    return;
  }
  commitAddrTyping('toInput', true);
  commitAddrTyping('ccInput', true);
  commitAddrTyping('bccInput', true);
  if (!addrFieldValue('toInput').trim()) {
    toast('请填写收件人');
    return;
  }
  try {
    const sent = await api('/api/send', { method: 'POST', body: await composePayload() });
    closeCompose();
    toast('已发送', sent && sent.simulated ? '这是模拟邮件 🎭' : '');
    refreshAfterSend();
  } catch (err) {
    toast('发送失败：' + err.message);
  }
}

function composeHasDraftContent() {
  const to = addrFieldValue('toInput').trim();
  const subject = ($('subjectInput').value || '').trim();
  const body = composeBodyPlain().trim();
  return !!(to || subject || body);
}

function draftsFolderId() {
  const f = (state.folders || []).find((x) => x.label === '草稿');
  return f ? f.id : '';
}

async function saveDraft() {
  if (!actionAcc()) {
    toast('请先添加邮箱账号');
    return;
  }
  if (!composeHasDraftContent()) {
    toast('请先填写收件人、主题或正文');
    return;
  }
  try {
    await api('/api/draft', { method: 'POST', body: await composePayload() });
    closeCompose();
    toast('已存草稿');
    const draftId = draftsFolderId();
    if (draftId) invalidateMailCache(draftId);
    if (currentFolderLabel() === '草稿') showFolderMails({ fresh: true, merge: true });
  } catch (err) {
    toast('存草稿失败：' + err.message);
  }
}

/* ---------- 邮件操作 ---------- */
function silentApi(path, opts) {
  return api(path, opts).then((d) => {
    showSyncErrors(d.errors);
    return d;
  }).catch((err) => {
    toast('同步失败：' + err.message);
  });
}

function starParts(parts, on) {
  (parts || []).forEach((p) => {
    if (!p || !p.uid) return;
    silentApi('/api/star', { method: 'POST', body: { acc: p.acc || actionAcc(), folder: p.folder || mailFolder(), uid: p.uid, on } });
  });
}

function deleteParts(parts, purge) {
  (parts || []).forEach((p) => {
    if (!p || !p.uid) return;
    silentApi('/api/delete', {
      method: 'POST',
      body: { acc: p.acc || actionAcc(), folder: p.folder || mailFolder(), uid: p.uid, purge: !!purge },
    });
  });
}

function starUids(uids, on, folder) {
  starParts((uids || []).map((uid) => ({ folder: folder || mailFolder(), uid })), on);
}

function deleteUids(uids, folder) {
  deleteParts((uids || []).map((uid) => ({ folder: folder || mailFolder(), uid })), false);
}

function purgeParts(parts) {
  deleteParts(parts, true);
}

function rowParts(row) {
  const acc = Number(row && row.dataset && row.dataset.acc) || actionAcc();
  const refs = (row.dataset.refs || '').split(',').filter(Boolean);
  if (refs.length) {
    return refs.map((r) => {
      const i = r.indexOf('|');
      const part = i === -1 ? { folder: mailFolder(), uid: r } : { folder: r.slice(0, i) || mailFolder(), uid: r.slice(i + 1) };
      part.acc = acc;
      return part;
    });
  }
  return (row.dataset.uids || row.dataset.uid).split(',').map((uid) => ({ folder: mailFolder(), uid, acc }));
}

async function doAction(kind) {
  if (!state.current) return;
  const { uid, folder, thread_key, thread_uids } = state.current;
  const uids = thread_uids && thread_uids.length ? thread_uids : [uid];
  const parts = (state.current.thread_parts && state.current.thread_parts.length
    ? state.current.thread_parts
    : uids.map((u) => ({ folder: folder || mailFolder(), uid: u }))
  ).map((p) => ({ ...p, acc: p.acc || state.current.acc || actionAcc() }));
  try {
    if (kind === 'star') {
      const on = !state.current.starred;
      state.current.starred = on;
      setStarBtn(on);
      const row = $('mailList').querySelector(`.mail-row[data-uid="${CSS.escape(String(uid))}"]`);
      if (row) {
        row.dataset.starred = on ? '1' : '0';
        const star = row.querySelector('.mail-star');
        if (star) star.innerHTML = (on && state.folder !== STARRED_ID) ? STAR_SVG : '';
      }
      applyStarLocal(thread_key, on, (mailCache.get(mailCacheKey(state.folder, state.q)) || { items: [] }).items.find((m) => threadKeyOf(m) === thread_key || String(m.uid) === String(uid)));
      starParts(parts, on);
      if (state.folder === STARRED_ID && !on) {
        const gone = $('mailList').querySelector(`.mail-row[data-uid="${CSS.escape(String(uid))}"]`);
        await animateRowsOut(gone ? [gone] : [], 'delete');
        dropKeysFromCache(new Set([thread_key]));
        closeMessage();
      }
    } else if (kind === 'pin') {
      const next = !isPinned(thread_key);
      setPinned(thread_key, next);
      $('pinBtn').textContent = next ? '取消置顶' : '置顶';
      const cached = mailCache.get(mailCacheKey(state.folder, state.q));
      if (cached) paintList(cached);
    } else if (kind === 'delete') {
      const purge = isTrashFolder();
      if (purge) {
        if (!await askConfirm('粉碎后不可恢复，确定吗？')) return;
      }
      const row = $('mailList').querySelector(`.mail-row[data-uid="${CSS.escape(String(uid))}"]`);
      const keys = new Set([thread_key]);
      const anim = animateRowsOut(row ? [row] : [], purge ? 'purge' : 'delete');
      if (purge) purgeParts(parts);
      else deleteParts(parts, false);
      if (purge) toast('已粉碎');
      await anim;
      dropKeysFromCache(keys);
      closeMessage();
    }
  } catch (err) {
    toast('操作失败：' + err.message);
  }
}

function closeMessage() {
  state.trGen += 1;
  $('messageView').hidden = true;
  $('messageView').classList.remove('is-loading');
  $('emptyRead').hidden = false;
  $('threadView').innerHTML = '';
  $('threadView').classList.remove('is-chat');
  resetReplyBox();
  $('replySentList').innerHTML = '';
  state.current = null;
  $('mailList').querySelectorAll('.mail-row').forEach((row) => row.classList.remove('active'));
}

function setReplyCcOpen(open) {
  const box = $('replyCcBox');
  const btn = $('replyCcBtn');
  if (box) box.classList.toggle('open', !!open);
  if (btn) {
    btn.classList.toggle('active', !!open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
}

function ownMailSet() {
  return new Set((state.accounts || []).map((a) => String(a.email || '').trim().toLowerCase()).filter(Boolean));
}

function extractEmails(raw) {
  const s = String(raw || '').trim();
  if (!s) return [];
  const found = [];
  const seen = new Set();
  const add = (email, name) => {
    const e = String(email || '').trim();
    if (!e || !e.includes('@')) return;
    const key = e.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    found.push(name ? `${name} <${e}>` : e);
  };
  const used = new Array(s.length).fill(false);
  const angle = /(?:"([^"]+)"|([^,<;，；\n][^<;，；\n]*?))?\s*<\s*([^<>\s@]+@[^<>\s@]+\.[^<>\s@]+)\s*>/g;
  let m;
  while ((m = angle.exec(s))) {
    add(m[3], (m[1] || m[2] || '').trim());
    for (let i = m.index; i < m.index + m[0].length; i += 1) used[i] = true;
  }
  const leftover = s.split('').map((ch, i) => (used[i] ? ' ' : ch)).join('');
  const re = /[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}/g;
  while ((m = re.exec(leftover))) add(m[0]);
  return found;
}

function splitAddrParts(raw) {
  const extracted = extractEmails(raw);
  if (extracted.length) return extracted;
  return String(raw || '').split(/[,;，；]/).map((s) => s.trim()).filter(Boolean);
}

function addReplyPerson(map, raw) {
  const parsed = parseMailbox(raw);
  const email = String(parsed.email || (String(raw || '').includes('@') ? String(raw).trim() : '')).trim();
  if (!email || !email.includes('@') || isOwnAddress(email)) return;
  const key = email.toLowerCase();
  if (map.has(key)) return;
  map.set(key, { email, name: parsed.name || mailboxAlias(email) || '' });
}

function collectReplyPeople(messages) {
  const map = new Map();
  (messages || []).forEach((m) => {
    addReplyPerson(map, m.from_addr || '');
    addReplyPerson(map, m.from || '');
    splitAddrParts(m.to).forEach((p) => addReplyPerson(map, p));
    splitAddrParts(m.cc).forEach((p) => addReplyPerson(map, p));
  });
  return [...map.values()];
}

function refreshReplyPeople(messages) {
  if (!state.current) return;
  const fromMsgs = collectReplyPeople(messages);
  if (fromMsgs.length) {
    state.current.replyPeople = fromMsgs;
    return;
  }
  const hp = state.current.headerParties;
  state.current.replyPeople = collectReplyPeople(hp ? [hp] : [{
    from: state.current.from,
    from_addr: state.current.from_addr,
  }]);
}

function otherAddrsFrom(raw) {
  return splitAddrParts(raw).map((p) => {
    const e = (parseMailbox(p).email || p).trim();
    return e;
  }).filter((e) => e.includes('@') && !isOwnAddress(e));
}

function addrWrap(inputId) {
  return document.querySelector(`[data-addr-for="${inputId}"]`);
}

function addrTokensEl(inputId) {
  const wrap = addrWrap(inputId);
  return wrap ? wrap.querySelector('.addr-tokens') : null;
}

function getAddrTokens(inputId) {
  const el = addrTokensEl(inputId);
  if (!el) return splitAddrParts($(inputId) && $(inputId).value);
  return [...el.querySelectorAll('.addr-token')].map((n) => n.dataset.addr).filter(Boolean);
}

function uniqueAddrs(list) {
  const seen = new Set();
  const out = [];
  (list || []).forEach((raw) => {
    const email = String(raw || '').trim();
    if (!email) return;
    const key = (parseMailbox(email).email || email).toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    out.push(email);
  });
  return out;
}

function addrFieldCollapsed(inputId) {
  const wrap = addrWrap(inputId);
  return !!(wrap && wrap.classList.contains('is-collapsed'));
}

function renderAddrTokens(inputId, addrs) {
  const el = addrTokensEl(inputId);
  if (!el) {
    if ($(inputId)) $(inputId).value = (addrs || []).join(', ');
    return;
  }
  const list = uniqueAddrs(addrs);
  const collapsed = addrFieldCollapsed(inputId) && list.length > 3;
  el.replaceChildren();
  list.forEach((email, i) => {
    const span = document.createElement('span');
    span.className = 'addr-token';
    span.dataset.addr = email;
    span.title = email;
    if (collapsed && i >= 3) span.hidden = true;
    const text = document.createElement('span');
    text.className = 'addr-token-text';
    text.textContent = email;
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'addr-token-x';
    x.setAttribute('aria-label', '删除');
    x.textContent = '×';
    span.appendChild(text);
    span.appendChild(x);
    el.appendChild(span);
  });
  if (collapsed) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'addr-more';
    more.textContent = `+另外${list.length - 3}人`;
    el.appendChild(more);
  }
}

function addrFieldValue(inputId) {
  const extra = ($(inputId) && $(inputId).value.trim()) || '';
  return uniqueAddrs(getAddrTokens(inputId).concat(extra ? splitAddrParts(extra) : extra ? [extra] : [])).join(', ');
}

function setAddrField(inputId, raw) {
  renderAddrTokens(inputId, splitAddrParts(raw));
  if ($(inputId) && addrTokensEl(inputId)) $(inputId).value = '';
  collapseAddrField(inputId);
}

function expandAddrField(inputId) {
  const wrap = addrWrap(inputId);
  if (!wrap || !wrap.classList.contains('is-collapsed')) return;
  wrap.classList.remove('is-collapsed');
  renderAddrTokens(inputId, getAddrTokens(inputId));
}

function collapseAddrField(inputId) {
  const wrap = addrWrap(inputId);
  const input = $(inputId);
  if (!wrap) return;
  if (input && (document.activeElement === input || wrap.contains(document.activeElement))) return;
  if (wrap.matches(':hover')) return;
  if (getAddrTokens(inputId).length <= 3) {
    if (wrap.classList.contains('is-collapsed')) {
      wrap.classList.remove('is-collapsed');
      renderAddrTokens(inputId, getAddrTokens(inputId));
    }
    return;
  }
  wrap.classList.add('is-collapsed');
  renderAddrTokens(inputId, getAddrTokens(inputId));
}

function commitAddrTyping(inputId, force) {
  const input = $(inputId);
  if (!input || !addrTokensEl(inputId)) return;
  const raw = input.value;
  if (!raw.trim()) return;
  const trailing = /[,;，；]\s*$/.test(raw);
  if (!force && !trailing) return;
  const parts = splitAddrParts(raw);
  if (!parts.length) return;
  renderAddrTokens(inputId, getAddrTokens(inputId).concat(parts));
  input.value = '';
  if (inputId === 'replyToInput') renderReplyPeople();
}

function replyAddrList(id) {
  return splitAddrParts(addrFieldValue(id)).map((p) => {
    const parsed = parseMailbox(p);
    return (parsed.email || p).trim();
  }).filter(Boolean);
}

function setReplyAddrList(id, emails) {
  setAddrField(id, (emails || []).join(', '));
}

function toggleReplyTo(email) {
  const key = String(email || '').trim().toLowerCase();
  if (!key) return;
  const cur = replyAddrList('replyToInput');
  const next = cur.some((e) => e.toLowerCase() === key)
    ? cur.filter((e) => e.toLowerCase() !== key)
    : cur.concat([email]);
  setReplyAddrList('replyToInput', next);
  renderReplyPeople();
}

function replyDefaultTo() {
  const msgs = ((state.current && state.current.threadMessages) || []).slice()
    .sort((a, b) => dateValue(a) - dateValue(b));
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const m = msgs[i];
    if (messageIsMine(m)) {
      const to = otherAddrsFrom(m.to);
      if (to.length) return to.join(', ');
    } else {
      const from = (parseMailbox(m.from_addr || m.from).email || '').trim();
      if (from && !isOwnAddress(from)) return from;
    }
  }
  const hp = state.current && state.current.headerParties;
  if (hp) {
    const to = otherAddrsFrom(hp.to);
    if (to.length) return to.join(', ');
    const from = (parseMailbox(hp.from_addr || hp.from).email || '').trim();
    if (from && !isOwnAddress(from)) return from;
  }
  const people = (state.current && state.current.replyPeople) || [];
  return people[0] ? people[0].email : '';
}

function bindAddrEditor(inputId) {
  const input = $(inputId);
  if (!input || input.dataset.addrBound) return;
  input.dataset.addrBound = '1';
  const wrap = addrWrap(inputId);
  if (wrap) {
    wrap.addEventListener('click', (e) => {
      if (e.target.closest('.addr-token-x')) {
        const tok = e.target.closest('.addr-token');
        renderAddrTokens(inputId, getAddrTokens(inputId).filter((a) => a !== tok.dataset.addr));
        if (inputId === 'replyToInput') renderReplyPeople();
        expandAddrField(inputId);
        input.focus();
        return;
      }
      if (e.target.closest('.addr-more')) {
        expandAddrField(inputId);
        input.focus();
        return;
      }
      const tok = e.target.closest('.addr-token');
      if (tok) {
        const addr = tok.dataset.addr;
        renderAddrTokens(inputId, getAddrTokens(inputId).filter((a) => a !== addr));
        expandAddrField(inputId);
        input.value = addr;
        input.focus();
        if (inputId === 'replyToInput') renderReplyPeople();
        return;
      }
      expandAddrField(inputId);
      input.focus();
    });
    wrap.addEventListener('mouseenter', () => expandAddrField(inputId));
    wrap.addEventListener('mouseleave', () => collapseAddrField(inputId));
  }
  input.addEventListener('focus', () => expandAddrField(inputId));
  input.addEventListener('input', () => commitAddrTyping(inputId, false));
  input.addEventListener('paste', (e) => {
    const text = (e.clipboardData || window.clipboardData).getData('text') || '';
    const emails = extractEmails(text);
    if (!emails.length) return;
    e.preventDefault();
    const typing = input.value;
    const merged = uniqueAddrs(getAddrTokens(inputId).concat(typing ? splitAddrParts(typing) : []).concat(emails));
    renderAddrTokens(inputId, merged);
    input.value = '';
    expandAddrField(inputId);
    if (inputId === 'replyToInput') renderReplyPeople();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commitAddrTyping(inputId, true);
    } else if (e.key === 'Backspace' && !input.value) {
      const cur = getAddrTokens(inputId);
      if (cur.length) {
        renderAddrTokens(inputId, cur.slice(0, -1));
        if (inputId === 'replyToInput') renderReplyPeople();
      }
    }
  });
  input.addEventListener('blur', () => {
    commitAddrTyping(inputId, true);
    setTimeout(() => collapseAddrField(inputId), 120);
  });
}

function renderReplyPeople() {
  const box = $('replyPeople');
  if (!box) return;
  const people = (state.current && state.current.replyPeople) || [];
  box.replaceChildren();
  if (!people.length) {
    box.hidden = true;
    return;
  }
  const selected = new Set(replyAddrList('replyToInput').map((e) => e.toLowerCase()));
  people.forEach((p) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'reply-chip' + (selected.has(p.email.toLowerCase()) ? ' is-on' : '');
    btn.dataset.email = p.email;
    btn.title = p.email;
    btn.textContent = p.name && p.name.toLowerCase() !== p.email.toLowerCase()
      ? `${p.name} <${p.email}>`
      : p.email;
    box.appendChild(btn);
  });
  box.hidden = false;
  updateReplyToPlaceholder();
}

function updateReplyToPlaceholder() {
  const input = $('replyToInput');
  if (!input) return;
  const hasPick = $('replyPeople') && $('replyPeople').querySelector('.reply-chip.is-on');
  input.placeholder = hasPick ? '输入新邮箱' : '点上方选择，或输入新邮箱';
}

function resetReplyBox() {
  $('replyComposer').hidden = true;
  $('replyBody').value = '';
  setAddrField('replyToInput', '');
  setAddrField('replyCcInput', '');
  setAddrField('replyBccInput', '');
  ['replyToSuggest', 'replyCcSuggest', 'replyBccSuggest'].forEach((id) => {
    if ($(id)) $(id).hidden = true;
  });
  setReplyCcOpen(false);
  if ($('replyPeople')) {
    $('replyPeople').hidden = true;
    $('replyPeople').replaceChildren();
  }
}

function reply() {
  if (!state.current) return;
  refreshReplyPeople(state.current.threadMessages);
  setAddrField('replyToInput', replyDefaultTo());
  setAddrField('replyCcInput', '');
  setAddrField('replyBccInput', '');
  setReplyCcOpen(false);
  renderReplyPeople();
  updateReplyToPlaceholder();
  $('replyComposer').hidden = false;
  const to = $('replyToInput');
  if (to) to.focus();
  else $('replyBody').focus();
  $('replyComposer').scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function lastAddrToken(raw) {
  const parts = String(raw || '').split(/[,;，；]/);
  return parts[parts.length - 1].replace(/\s/g, '');
}

function replaceLastAddrToken(raw, full) {
  const parts = String(raw || '').split(/[,;，；]/);
  const prefix = parts.slice(0, -1).map((s) => s.trim()).filter(Boolean);
  return prefix.concat([full]).join(', ');
}

function bindReplyAddrSuggest(inputId, listId) {
  const input = $(inputId);
  const list = $(listId);
  if (!input || !list) return;
  const hide = () => { list.hidden = true; };
  const show = () => {
    const tok = lastAddrToken(input.value);
    if (!tok || tok.includes('@')) {
      hide();
      return;
    }
    list.replaceChildren();
    EMAIL_SUFFIXES.forEach((s) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = tok + s;
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        input.value = replaceLastAddrToken(input.value, tok + s);
        commitAddrTyping(inputId, true);
        hide();
        input.focus();
        if (inputId === 'replyToInput') renderReplyPeople();
      });
      li.appendChild(btn);
      list.appendChild(li);
    });
    list.hidden = false;
  };
  input.addEventListener('input', () => {
    show();
    if (inputId === 'replyToInput') renderReplyPeople();
  });
  input.addEventListener('blur', () => setTimeout(hide, 120));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hide();
  });
}

function hideReply() {
  resetReplyBox();
}

async function sendInlineReply() {
  if (!state.current) return;
  const text = $('replyBody').value.trim();
  commitAddrTyping('replyToInput', true);
  commitAddrTyping('replyCcInput', true);
  commitAddrTyping('replyBccInput', true);
  const to = addrFieldValue('replyToInput').trim();
  if (!to) {
    toast('请填写收件人');
    return;
  }
  if (!text) {
    toast('请先写点回复内容');
    return;
  }
  if (!actionAcc()) {
    toast('请先添加邮箱账号');
    return;
  }
  const subject = /^re\s*:/i.test(state.current.subject)
    ? state.current.subject
    : 'Re: ' + state.current.subject;
  try {
    const sentRes = await api('/api/send', {
      method: 'POST',
      body: {
        acc: state.current.acc || actionAcc(),
        to,
        cc: addrFieldValue('replyCcInput').trim(),
        bcc: addrFieldValue('replyBccInput').trim(),
        subject,
        body: text,
        in_reply_to: state.current.message_id || '',
      },
    });
    const box = $('threadView');
    box.classList.add('is-chat');
    const sent = {
      mine: true,
      subject,
      from: currentAccEmail(),
      from_addr: currentAccEmail(),
      to,
      cc: addrFieldValue('replyCcInput').trim(),
      date: new Date().toISOString(),
      body: text,
    };
    if (state.current) {
      state.current.threadMessages = (state.current.threadMessages || []).concat([sent]);
    }
    const sec = appendThreadMessage(sent, box);
    stashThreadBodies();
    if (state.trMode && state.trMode !== 'orig') applyThreadTranslate(state.trMode);
    resetReplyBox();
    toast('已发送', sentRes && sentRes.simulated ? '这是模拟邮件 🎭' : '');
    sec.scrollIntoView({ behavior: 'smooth', block: 'end' });
    bumpCurrentRowTime();
    refreshAfterSend();
  } catch (err) {
    toast('发送失败：' + err.message);
  }
}

function bumpCurrentRowTime() {
  if (!state.current) return;
  const now = new Date().toISOString();
  $('msgDate').textContent = formatDate(now);
  const cached = mailCache.get(mailCacheKey(state.folder, state.q));
  if (!cached) return;
  const item = cached.items.find((m) => threadKeyOf(m) === state.current.thread_key
    || String(m.uid) === String(state.current.uid));
  if (item) {
    const wasUnread = item.unread;
    item.date = now;
    item.unread = false;
    if (wasUnread && cached.unreadCount != null) {
      cached.unreadCount = Math.max(0, cached.unreadCount - 1);
    }
    // 同步更新账号红点数字
    if (wasUnread && state.current.acc && state.unreadPerAccount[state.current.acc] != null) {
      state.unreadPerAccount[state.current.acc] = Math.max(0, (state.unreadPerAccount[state.current.acc] || 0) - 1);
    }
    paintList(cached);
    setRowActive(state.current.uid);
    renderAccountBar();
  }
}

/* ---------- 右键 / 批量 ---------- */
function showCtx(x, y, row) {
  state.ctxTarget = row;
  const menu = $('ctxMenu');
  const starred = row.dataset.starred === '1' || state.folder === STARRED_ID;
  const starLabel = menu.querySelector('[data-act="star"] span');
  const pinLabel = menu.querySelector('[data-act="pin"] span');
  if (starLabel) starLabel.textContent = starred ? '取消星标' : '星标';
  if (pinLabel) pinLabel.textContent = isPinned(row.dataset.key) ? '取消置顶' : '置顶';
  menu.hidden = false;
  menu.style.pointerEvents = 'auto';
  const w = menu.offsetWidth;
  const h = menu.offsetHeight;
  const first = menu.querySelector('button');
  const insetX = first ? first.offsetLeft + 8 : 14;
  const insetY = first ? first.offsetTop : 6;
  let left = x - insetX - 6;
  let top = y + 8 - insetY;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  top = Math.max(8, Math.min(top, window.innerHeight - h - 8));
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
  state.ctxJustOpened = true;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => { state.ctxJustOpened = false; });
  });
}

function hideCtx() {
  $('ctxMenu').hidden = true;
  state.ctxTarget = null;
}

async function ctxActionMany(rows, act, lead) {
  const keys = new Set(rows.map((row) => row.dataset.key));
  try {
    if (act === 'star') {
      const starred = lead.dataset.starred === '1' || state.folder === STARRED_ID;
      const on = !starred;
      if (state.folder === STARRED_ID && !on) {
        const anim = animateRowsOut(rows, 'delete');
        for (const row of rows) {
          row.dataset.starred = '0';
          applyStarLocal(row.dataset.key, false, cacheItemSnapshot(row, row.dataset.key));
          starParts(rowParts(row), false);
        }
        toast('已取消星标');
        await anim;
        dropKeysFromCache(keys);
        if (state.current && keys.has(state.current.thread_key)) closeMessage();
        clearSelection();
        return;
      }
      for (const row of rows) {
        row.dataset.starred = on ? '1' : '0';
        const star = row.querySelector('.mail-star');
        if (star) star.innerHTML = (on && state.folder !== STARRED_ID) ? STAR_SVG : '';
        if (state.current && state.current.uid === row.dataset.uid) {
          state.current.starred = on;
          setStarBtn(on);
        }
        applyStarLocal(row.dataset.key, on, cacheItemSnapshot(row, row.dataset.key));
        starParts(rowParts(row), on);
      }
      toast(on ? '已星标' : '已取消星标');
      clearSelection();
    } else if (act === 'pin') {
      const on = !isPinned(lead.dataset.key);
      rows.forEach((row) => setPinned(row.dataset.key, on));
      toast(on ? '已置顶' : '已取消置顶');
      clearSelection();
      const cached = mailCache.get(mailCacheKey(state.folder, state.q));
      if (cached) paintList(cached);
    } else if (act === 'delete') {
      const anim = animateRowsOut(rows, 'delete');
      for (const row of rows) deleteParts(rowParts(row), false);
      toast('已删除');
      await anim;
      dropKeysFromCache(keys);
      if (state.current && keys.has(state.current.thread_key)) closeMessage();
      clearSelection();
    }
  } catch (err) {
    toast('操作失败：' + err.message);
  }
}

async function ctxAction(act) {
  const row = state.ctxTarget;
  const selected = selectedRows();
  const rows = (selected.length > 1 && row && selected.some((r) => r.dataset.key === row.dataset.key))
    ? selected
    : (row ? [row] : []);
  hideCtx();
  if (!rows.length) return;
  if (rows.length > 1) {
    await ctxActionMany(rows, act, row);
    return;
  }
  const uid = row.dataset.uid;
  const key = row.dataset.key;
  try {
    if (act === 'star') {
      const starred = row.dataset.starred === '1' || state.folder === STARRED_ID;
      const on = !starred;
      row.dataset.starred = on ? '1' : '0';
      const star = row.querySelector('.mail-star');
      if (star) star.innerHTML = (on && state.folder !== STARRED_ID) ? STAR_SVG : '';
      if (state.current && state.current.uid === uid) {
        state.current.starred = on;
        setStarBtn(on);
      }
      applyStarLocal(key, on, cacheItemSnapshot(row, key));
      starParts(rowParts(row), on);
      toast(on ? '已星标' : '已取消星标');
      if (state.folder === STARRED_ID && !on) {
        await animateRowsOut([row], 'delete');
        dropKeysFromCache(new Set([key]));
        if (state.current && state.current.uid === uid) closeMessage();
      }
    } else if (act === 'pin') {
      const on = !isPinned(key);
      setPinned(key, on);
      const cached = mailCache.get(mailCacheKey(state.folder, state.q));
      if (cached) paintList(cached);
      toast(on ? '已置顶' : '已取消置顶');
    } else if (act === 'delete') {
      const anim = animateRowsOut([row], 'delete');
      deleteParts(rowParts(row), false);
      await anim;
      dropKeysFromCache(new Set([key]));
      if (state.current && state.current.uid === uid) closeMessage();
    }
  } catch (err) {
    toast('操作失败：' + err.message);
  }
}

async function batchAction(act) {
  const rows = selectedRows();
  if (!rows.length) return;
  try {
    if (act === 'pin') {
      rows.forEach((row) => setPinned(row.dataset.key, true));
      toast('已置顶');
      clearSelection();
      const cached = mailCache.get(mailCacheKey(state.folder, state.q));
      if (cached) paintList(cached);
      return;
    }
    if (act === 'star') {
      if (state.folder === STARRED_ID) {
        const keys = new Set(rows.map((row) => row.dataset.key));
        const anim = animateRowsOut(rows, 'delete');
        for (const row of rows) {
          row.dataset.starred = '0';
          applyStarLocal(row.dataset.key, false, cacheItemSnapshot(row, row.dataset.key));
          starParts(rowParts(row), false);
        }
        toast('已取消星标');
        await anim;
        dropKeysFromCache(keys);
        if (state.current && keys.has(state.current.thread_key)) closeMessage();
        clearSelection();
        return;
      }
      for (const row of rows) {
        row.dataset.starred = '1';
        const star = row.querySelector('.mail-star');
        if (star) star.innerHTML = STAR_SVG;
        applyStarLocal(row.dataset.key, true, cacheItemSnapshot(row, row.dataset.key));
        starParts(rowParts(row), true);
      }
      toast('已星标');
      clearSelection();
      return;
    } else if (act === 'delete') {
      const keys = new Set(rows.map((row) => row.dataset.key));
      const anim = animateRowsOut(rows, 'delete');
      for (const row of rows) deleteParts(rowParts(row), false);
      await anim;
      dropKeysFromCache(keys);
      if (state.current && keys.has(state.current.thread_key)) closeMessage();
      clearSelection();
      return;
    }
  } catch (err) {
    toast('操作失败：' + err.message);
  }
}

/* ---------- 搜索 ---------- */
let searchTimer = null;
function onSearch(e) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const raw = e.target.value.trim();
    const mail = currentAccEmail();
    if (mail && raw.toLowerCase() === mail) {
      clearSearch();
      showFolderMails();
      return;
    }
    state.q = raw;
    if (!raw) {
      showFolderMails();
      return;
    }
    refreshMails();
  }, 400);
}

/* ---------- 抄送 / 密送 ---------- */
function setCcOpen(on) {
  $('ccBccBox').classList.toggle('open', !!on);
  $('toggleCcBtn').classList.toggle('active', !!on);
  $('toggleCcBtn').setAttribute('aria-expanded', on ? 'true' : 'false');
}

/* ---------- 分栏拖拽 ---------- */
function applyListWidth(px) {
  const app = document.querySelector('.app');
  const side = document.querySelector('.sidebar').offsetWidth;
  const minW = 240;
  const maxW = Math.max(minW, app.clientWidth - side - 6 - 280);
  const w = Math.min(maxW, Math.max(minW, px));
  $('listPane').style.width = w + 'px';
  return w;
}

function bindSplitter() {
  const saved = Number(localStorage.getItem('pupu_list_w'));
  if (saved) applyListWidth(saved);
  const bar = $('splitter');
  let startX = 0;
  let startW = 0;
  let dragging = false;
  bar.addEventListener('mousedown', (e) => {
    dragging = true;
    startX = e.clientX;
    startW = $('listPane').offsetWidth;
    bar.classList.add('dragging');
    document.querySelector('.app').classList.add('resizing');
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    applyListWidth(startW + (e.clientX - startX));
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove('dragging');
    document.querySelector('.app').classList.remove('resizing');
    localStorage.setItem('pupu_list_w', String($('listPane').offsetWidth));
  });
}

/* ---------- 绑定事件 ---------- */
function bindEvents() {
  if ($('refreshBtn')) $('refreshBtn').onclick = () => refreshAllMail();
  $('composeBtn').onclick = () => startCompose();
  $('closeCompose').onclick = closeCompose;
  if ($('composeScenePicks')) {
    $('composeScenePicks').addEventListener('click', (e) => {
      const btn = e.target.closest('.compose-scene');
      if (btn) applyComposeScene(btn.dataset.scene, true);
    });
  }
  if ($('sendAccCancel')) $('sendAccCancel').onclick = closeSendAccPicker;
  if ($('sendAccList')) {
    $('sendAccList').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-id]');
      if (btn) pickSendAccount(btn.dataset.id);
    });
  }
  $('composeForm').addEventListener('submit', submitCompose);
  $('saveDraftBtn').onclick = saveDraft;
  const toolbar = document.querySelector('.compose-toolbar');
  toolbar.addEventListener('mousedown', (e) => {
    if (!e.target.closest('button')) return;
    e.preventDefault();
    const range = selectionInEditor();
    savedComposeRange = range ? range.cloneRange() : null;
  });
  $('fmtBold').onclick = () => applyComposeFormat('bold');
  $('fmtItalic').onclick = () => applyComposeFormat('italic');
  $('bodyInput').addEventListener('mouseup', () => {
    const range = selectionInEditor();
    savedComposeRange = range ? range.cloneRange() : null;
    syncComposeFmt();
  });
  $('bodyInput').addEventListener('keyup', () => {
    const range = selectionInEditor();
    savedComposeRange = range ? range.cloneRange() : null;
    syncComposeFmt();
  });
  if ($('closeReplyBtn')) $('closeReplyBtn').onclick = hideReply;
  $('starBtn').onclick = () => doAction('star');
  $('pinBtn').onclick = () => doAction('pin');
  $('replyBtn').onclick = reply;
  $('deleteBtn').onclick = () => doAction('delete');
  $('cancelReplyBtn').onclick = hideReply;
  $('sendReplyBtn').onclick = sendInlineReply;
  if ($('replyCcBtn')) {
    $('replyCcBtn').onclick = () => setReplyCcOpen(!$('replyCcBox').classList.contains('open'));
  }
  if ($('replyPeople')) {
    $('replyPeople').addEventListener('click', (e) => {
      const chip = e.target.closest('.reply-chip');
      if (chip) toggleReplyTo(chip.dataset.email);
    });
  }
  bindAddrEditor('replyToInput');
  bindAddrEditor('replyCcInput');
  bindAddrEditor('replyBccInput');
  bindAddrEditor('toInput');
  bindAddrEditor('ccInput');
  bindAddrEditor('bccInput');
  bindReplyAddrSuggest('replyToInput', 'replyToSuggest');
  bindReplyAddrSuggest('replyCcInput', 'replyCcSuggest');
  bindReplyAddrSuggest('replyBccInput', 'replyBccSuggest');
  bindReplyAddrSuggest('toInput', 'toSuggest');
  bindReplyAddrSuggest('ccInput', 'ccSuggest');
  bindReplyAddrSuggest('bccInput', 'bccSuggest');
  const searchInput = $('searchInput');
  const unlockSearch = () => searchInput.removeAttribute('readonly');
  searchInput.addEventListener('pointerdown', unlockSearch);
  searchInput.addEventListener('focus', unlockSearch);
  searchInput.addEventListener('input', onSearch);
  $('settingsBtn').onclick = () => openSettings('account');
  $('closeSettings').onclick = closeSettings;
  $('settingsNav').addEventListener('click', (e) => {
    const btn = e.target.closest('.settings-nav-btn');
    if (btn) showSettingsPane(btn.dataset.pane);
  });
  $('addAccBtn').onclick = openAccountForm;
  if ($('vaultBtn')) $('vaultBtn').onclick = openVaultManager;
  if ($('vaultSetupForm')) $('vaultSetupForm').addEventListener('submit', submitVaultSetup);
  if ($('vaultUnlockForm')) $('vaultUnlockForm').addEventListener('submit', submitVaultUnlock);
  if ($('vaultSetupCancel')) $('vaultSetupCancel').onclick = () => closeVaultSetup(null);
  if ($('closeVaultSetup')) $('closeVaultSetup').onclick = () => closeVaultSetup(null);
  if ($('closeVaultUnlock')) $('closeVaultUnlock').onclick = () => closeVaultUnlock(null);
  if ($('closeVaultList')) $('closeVaultList').onclick = closeVaultList;
  if ($('vaultForgotBtn')) $('vaultForgotBtn').onclick = forgotMasterPassword;
  if ($('vaultSetupToggle')) {
    $('vaultSetupToggle').onclick = () => setPasswordVisible(
      $('vaultSetupInput'), $('vaultSetupToggle'),
      $('vaultSetupInput') && $('vaultSetupInput').type === 'password',
      '隐藏主密码', '显示主密码'
    );
  }
  if ($('vaultUnlockToggle')) {
    $('vaultUnlockToggle').onclick = () => setPasswordVisible(
      $('vaultUnlockInput'), $('vaultUnlockToggle'),
      $('vaultUnlockInput') && $('vaultUnlockInput').type === 'password',
      '隐藏主密码', '显示主密码'
    );
  }
  if ($('vaultListBody')) {
    $('vaultListBody').addEventListener('click', (e) => {
      const btn = e.target.closest('.vault-reveal');
      if (btn) toggleVaultReveal(btn);
    });
  }
  if ($('storageChangeBtn')) $('storageChangeBtn').onclick = changeStorageLocation;
  if ($('storageOpenBtn')) $('storageOpenBtn').onclick = openStorageLocation;
  if ($('gaiaBtn')) $('gaiaBtn').onclick = () => switchAccount(GAIA_ID);
  $('accRail').addEventListener('click', (e) => {
    const btn = e.target.closest('.acc-rail-item');
    if (!btn) return;
    const id = Number(btn.dataset.id);
    if (id && id !== state.acc) switchAccount(id);
  });
  $('settingsAccList').addEventListener('input', (e) => {
    const live = e.target.closest('.acc-name-live');
    if (!live) return;
    const clipped = clipAccName(live.value);
    if (clipped !== live.value) live.value = clipped;
  });
  $('settingsAccList').addEventListener('dblclick', (e) => {
    const live = e.target.closest('.acc-name-live');
    if (!live || live.dataset.demo) return;
    live.readOnly = false;
    live.classList.add('is-editing');
    live.focus();
    live.select();
  });
  $('settingsAccList').addEventListener('blur', (e) => {
    const live = e.target.closest('.acc-name-live');
    if (!live) return;
    live.readOnly = true;
    live.classList.remove('is-editing');
    commitAccName(live);
  }, true);
  $('settingsAccList').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !e.target.classList.contains('acc-name-live')) return;
    e.preventDefault();
    e.target.blur();
  });
  $('settingsAccList').addEventListener('click', (e) => {
    const leave = e.target.closest('.settings-leave');
    if (leave) removeAccount(leave.dataset.id);
  });
  $('themePicks').addEventListener('click', (e) => {
    const pick = e.target.closest('.theme-pick');
    if (pick) applyTheme(pick.dataset.theme);
  });
  $('trEngine').addEventListener('click', (e) => {
    const pick = e.target.closest('.engine-pick');
    if (pick) setTranslateEngine(pick.dataset.engine);
  });
  $('trSave').onclick = saveTranslateSettings;
  $('trTest').onclick = testTranslateSettings;
  $('trKeyToggle').onclick = toggleTranslateKey;
  if ($('accPwToggle')) $('accPwToggle').onclick = toggleAccountPassword;
  const accPw = $('accPasswordInput');
  if (accPw) {
    accPw.addEventListener('keydown', updateAccCapsHint);
    accPw.addEventListener('keyup', updateAccCapsHint);
    accPw.addEventListener('blur', () => {
      if ($('accCapsHint')) $('accCapsHint').hidden = true;
    });
  }
  $('closeAccountForm').onclick = closeAccountForm;
  if ($('guideBtn')) $('guideBtn').onclick = () => {
    const url = window.location.origin + '/guide.html';
    if (window.pumailAPI && window.pumailAPI.openExternal) {
      window.pumailAPI.openExternal(url);
    } else {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
  };
  if ($('outlookLoginBtn')) $('outlookLoginBtn').onclick = startOutlookOauth;
  $('accountForm').addEventListener('submit', submitAccountForm);
  $('accProviderInput').onchange = toggleCustomServer;
  $('accEmailInput').addEventListener('input', applyAccountEmailInput);
  const suggest = $('accEmailSuggest');
  if (suggest) {
    suggest.addEventListener('mousedown', (e) => e.preventDefault());
    suggest.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (btn) pickEmailSuggest(btn.dataset.full);
    });
  }
  document.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.acc-email-wrap')) return;
    hideEmailSuggest();
  });
  if ($('selectAllBox')) {
    $('selectAllBox').onchange = () => {
      const on = $('selectAllBox').checked;
      $('mailList').querySelectorAll('.mail-row').forEach((row) => {
        const box = row.querySelector('input[type="checkbox"]');
        if (!box) return;
        box.checked = on;
        row.classList.toggle('checked', on);
        if (on) state.selected.add(row.dataset.key);
        else state.selected.delete(row.dataset.key);
      });
      updateBatchBar();
    };
  }
  if ($('restoreBtn')) $('restoreBtn').onclick = () => restoreSelected();
  if ($('purgeBtn')) $('purgeBtn').onclick = () => purgeSelected();
  if ($('bulkDelBtn')) $('bulkDelBtn').onclick = () => batchAction('delete');
  if ($('batchStar')) $('batchStar').onclick = () => batchAction('star');
  if ($('batchPin')) $('batchPin').onclick = () => batchAction('pin');
  if ($('batchDel')) $('batchDel').onclick = () => batchAction('delete');
  $('ctxMenu').querySelectorAll('button').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      ctxAction(btn.dataset.act);
    });
  });
  document.addEventListener('pointerdown', (e) => {
    if ($('ctxMenu').hidden || state.ctxJustOpened) return;
    if (e.target.closest('#ctxMenu')) return;
    hideCtx();
  }, true);

  const plus = $('toggleCcBtn');
  plus.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    setCcOpen(!$('ccBccBox').classList.contains('open'));
  });
  $('mailList').addEventListener('scroll', () => {
    const el = $('mailList');
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) loadMoreMails();
  });

  // 写邮件窗口故意"点背景不关"：以前点一下旁边的地方，写了一半的信就没了。
  // 现在只有三个出口会关闭它：右上角 × 按钮、存草稿、发送。
  // （其它弹层——设置、添加账号等——仍然是点背景关闭，不受影响。）
  $('settingsOverlay').addEventListener('click', (e) => {
    if (e.target.id === 'settingsOverlay') closeSettings();
  });
  $('accountOverlay').addEventListener('click', (e) => {
    if (e.target.id === 'accountOverlay') closeAccountForm();
  });
  if ($('vaultSetupOverlay')) {
    $('vaultSetupOverlay').addEventListener('click', (e) => {
      if (e.target.id === 'vaultSetupOverlay') closeVaultSetup(null);
    });
  }
  if ($('vaultUnlockOverlay')) {
    $('vaultUnlockOverlay').addEventListener('click', (e) => {
      if (e.target.id === 'vaultUnlockOverlay') closeVaultUnlock(null);
    });
  }
  if ($('vaultListOverlay')) {
    $('vaultListOverlay').addEventListener('click', (e) => {
      if (e.target.id === 'vaultListOverlay') closeVaultList();
    });
  }
  $('confirmOverlay').addEventListener('click', (e) => {
    if (e.target.id === 'confirmOverlay') $('confirmNo').click();
  });
  $('leaveOverlay').addEventListener('click', (e) => {
    if (e.target.id === 'leaveOverlay') $('leaveCancel').click();
  });
  if ($('sendAccOverlay')) {
    $('sendAccOverlay').addEventListener('click', (e) => {
      if (e.target.id === 'sendAccOverlay') closeSendAccPicker();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if ($('sendAccOverlay') && !$('sendAccOverlay').hidden) closeSendAccPicker();
    else if (!$('leaveOverlay').hidden) $('leaveCancel').click();
    else if (!$('confirmOverlay').hidden) $('confirmNo').click();
    else if ($('vaultListOverlay') && !$('vaultListOverlay').hidden) closeVaultList();
    else if ($('vaultUnlockOverlay') && !$('vaultUnlockOverlay').hidden) closeVaultUnlock(null);
    else if ($('vaultSetupOverlay') && !$('vaultSetupOverlay').hidden) closeVaultSetup(null);
    else if (!$('ctxMenu').hidden) hideCtx();
    else if (!$('accountOverlay').hidden) {
      const list = $('accEmailSuggest');
      if (list && !list.hidden) hideEmailSuggest();
      else closeAccountForm();
    }
    else if (!$('settingsOverlay').hidden) closeSettings();
    // 写邮件窗口也不响应 Esc：避免手一抖把草稿丢了（同上，只用 × / 存草稿 / 发送）
    else if (!$('replyComposer').hidden) hideReply();
  });
  bindSplitter();
  bindTrSwitch();
  bindMailLinkGuard();
  bindExternalLinks();
}

applyTheme(currentTheme());
bindEvents();
startApp();
