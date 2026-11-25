/* 附件下载回归测试：点击附件必须回到本应用后端取字节，不能交给系统浏览器。
   不访问真实邮箱，也不真正写盘。 */
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

function fakeElement(tagName) {
  const classes = new Set();
  const handlers = {};
  const el = {
    tagName: String(tagName || 'div').toUpperCase(),
    children: [],
    style: {},
    dataset: {},
    attrs: {},
    textContent: '',
    attBox: null,
    clicked: 0,
    removed: 0,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(name, value) { el.attrs[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(el.attrs, name) ? el.attrs[name] : null;
    },
    appendChild(child) { el.children.push(child); return child; },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    click() { el.clicked += 1; },
    remove() { el.removed += 1; },
    scrollIntoView() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest(sel) {
      if (sel === '.atts') return el.attBox;
      if (sel === 'a[target="_blank"]') return el.attrs.target === '_blank' ? el : null;
      if (sel === 'a[href], area[href]') return el.attrs.href ? el : null;
      return null;
    },
  };
  el.handlers = handlers;
  Object.defineProperty(el, 'href', {
    get() { return el.attrs.href || ''; },
    set(value) { el.attrs.href = String(value); },
  });
  return el;
}

function fixture() {
  const elements = new Map();
  const saved = [];
  const timers = [];
  const notices = [];
  const document = {
    handlers: {},
    addEventListener(type, fn) { (document.handlers[type] = document.handlers[type] || []).push(fn); },
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, fakeElement('div'));
      return elements.get(id);
    },
    querySelectorAll: () => [],
    createElement: (tag) => fakeElement(tag),
    body: fakeElement('body'),
  };
  const context = vm.createContext({
    document,
    window: {},
    console,
    URLSearchParams,
    URL: {
      createObjectURL: (blob) => {
        saved.push(blob);
        return 'blob:pumail-attachment';
      },
      revokeObjectURL: () => {},
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
  });
  const src = fs.readFileSync(process.env.PUMAIL_APP_JS || 'app.js', 'utf8').replace(
    /applyTheme\(currentTheme\(\)\);\s*bindEvents\(\);\s*startApp\(\);\s*$/, '');
  vm.runInContext(src, context);
  context.toast = (...args) => notices.push(args);
  context.testState = vm.runInContext('state', context);
  return { c: context, document, elements, saved, timers, notices };
}

function readingPane(f) {
  const pane = fakeElement('section');
  f.elements.set('readingPane', pane);
  f.c.bindMailLinkGuard();
  return pane;
}

function attachmentLink(overrides = {}) {
  const box = fakeElement('div');
  const link = fakeElement('a');
  link.attBox = box;
  link.attrs.href = overrides.href || '/api/attachment?acc=1&folder=INBOX&uid=2239&part=2&name=通知书.pdf';
  link.dataset.attName = overrides.name || '通知书.pdf';
  link.textContent = '📎 通知书.pdf (206.2 KB)';
  return link;
}

function clickEvent(link, pane) {
  const event = {
    target: link,
    prevented: 0,
    stopped: 0,
    preventDefault() { event.prevented += 1; },
    stopPropagation() { event.stopped += 1; },
    composedPath: () => [link, link.attBox, pane, {}],
  };
  return event;
}

test('点击附件走应用内下载，不交给系统浏览器（修复 401 的根因）', async () => {
  const f = fixture();
  const pane = readingPane(f);
  const opened = [];
  f.c.window.pumailAPI = { openExternal: (url) => opened.push(url) };
  const calls = [];
  f.c.fetch = (url, opts) => {
    calls.push([url, opts]);
    return new Promise(() => {});   // 保持"下载中"，只验证路由
  };
  const link = attachmentLink();
  const event = clickEvent(link, pane);
  pane.handlers.click.forEach((fn) => fn(event));

  assert.equal(event.prevented, 1);
  assert.equal(event.stopped, 1);
  assert.deepEqual(opened, []);          // 旧代码在这里把接口地址交给了系统浏览器
  assert.equal(calls.length, 1);
  assert.match(calls[0][0], /^\/api\/attachment\?/);
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.match(link.textContent, /正在下载/);
});

test('正文里的普通链接仍然交给系统浏览器打开', () => {
  const f = fixture();
  const pane = readingPane(f);
  const opened = [];
  f.c.window.pumailAPI = { openExternal: (url) => opened.push(url) };
  const link = fakeElement('a');
  link.attrs.href = 'https://example.com/a';
  const event = clickEvent(link, pane);
  pane.handlers.click.forEach((fn) => fn(event));
  assert.deepEqual(opened, ['https://example.com/a']);
});

test('附件下载成功后按真实文件名交给浏览器保存', async () => {
  const f = fixture();
  const link = attachmentLink();
  const blob = { size: 211144, type: 'application/pdf' };
  f.c.fetch = async () => ({
    ok: true,
    status: 200,
    blob: async () => blob,
    headers: { get: () => "attachment; filename*=UTF-8''%E9%80%9A%E7%9F%A5%E4%B9%A6.pdf" },
  });
  await f.c.downloadAttachment(link);

  const anchor = f.document.body.children[0];
  assert.equal(f.saved.length, 1);
  assert.equal(anchor.href, 'blob:pumail-attachment');
  assert.equal(anchor.download, '通知书.pdf');
  assert.equal(anchor.clicked, 1);
  assert.equal(anchor.removed, 1);
  assert.equal(link.textContent, '📎 通知书.pdf (206.2 KB)');
  assert.equal(link.dataset.downloading, '');
  assert.ok(f.notices.some((n) => n[0] === '附件已开始下载' && n[1] === '通知书.pdf'));
});

test('后端拒绝时给出明确错误，不产生空文件，可再次点击重试', async () => {
  const f = fixture();
  const link = attachmentLink();
  f.c.fetch = async () => ({
    ok: false,
    status: 401,
    json: async () => ({ error: '未授权，请从首页打开 PuMail' }),
  });
  await f.c.downloadAttachment(link);
  assert.equal(f.saved.length, 0);
  assert.equal(f.document.body.children.length, 0);
  assert.equal(link.textContent, '📎 通知书.pdf (206.2 KB)');
  assert.equal(link.dataset.downloading, '');
  assert.ok(f.notices.some((n) => n[0].includes('附件下载失败') && n[0].includes('未授权')));
});

test('重复点击同一个附件不会并发下载两次', async () => {
  const f = fixture();
  const link = attachmentLink();
  let resolveFetch;
  let calls = 0;
  f.c.fetch = () => {
    calls += 1;
    return new Promise((resolve) => { resolveFetch = resolve; });
  };
  link.dataset.downloading = '1';
  await f.c.downloadAttachment(link);
  assert.equal(calls, 0);
  assert.ok(resolveFetch === undefined);
});

test('附件链接不会被当成外链加 target=_blank，普通链接仍然加', () => {
  const f = fixture();
  const att = attachmentLink();
  const ordinary = fakeElement('a');
  ordinary.attrs.href = 'https://example.com/report';
  const box = fakeElement('div');
  box.querySelectorAll = () => [att, ordinary];
  f.c.hardenMailLinks(box);
  assert.equal(att.attrs.target, undefined);
  assert.equal(ordinary.attrs.target, '_blank');
  assert.equal(ordinary.attrs.rel, 'noopener noreferrer');
});

test('全局外链守卫放过附件，交给附件下载逻辑处理', () => {
  const f = fixture();
  const opened = [];
  f.c.window.pumailAPI = { openExternal: (url) => opened.push(url) };
  f.c.bindExternalLinks();
  const att = attachmentLink();
  att.attrs.target = '_blank';   // 防御：即使残留 _blank 也不能交出去
  const event = {
    target: att,
    prevented: 0,
    preventDefault() { event.prevented += 1; },
    stopPropagation() {},
  };
  f.document.handlers.click.forEach((fn) => fn(event));
  assert.equal(event.prevented, 0);
  assert.deepEqual(opened, []);
});

test('文件名处理：路径分隔符、Windows 非法字符、前导点都被清理', () => {
  const f = fixture();
  assert.equal(f.c.attachmentFileName('../evil/name.pdf'), '_evil_name.pdf');
  assert.equal(f.c.attachmentFileName('a:b*c?.pdf'), 'a_b_c_.pdf');
  assert.equal(f.c.attachmentFileName('   '), 'attachment');
  assert.equal(f.c.attachmentFileName('通知书.pdf'), '通知书.pdf');
});

test('Content-Disposition 解析：优先 RFC 2231，其次普通引号形式', () => {
  const f = fixture();
  assert.equal(
    f.c.fileNameFromDisposition("attachment; filename=fallback.pdf; filename*=UTF-8''%E9%80%9A%E7%9F%A5%E4%B9%A6.pdf"),
    '通知书.pdf'
  );
  assert.equal(f.c.fileNameFromDisposition('attachment; filename="report 2026.pdf"'), 'report 2026.pdf');
  assert.equal(f.c.fileNameFromDisposition(''), '');
});
