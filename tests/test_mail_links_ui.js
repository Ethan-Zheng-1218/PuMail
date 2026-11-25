/* 正文链接回归测试：纯文本 http 链接要变成可点击链接，点击交给系统默认浏览器。
   不访问网络，也不启动 Electron。 */
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

function textNode(value) {
  return { nodeType: 3, nodeValue: String(value), parentNode: null, parentElement: null };
}

function element(tag) {
  const classes = new Set();
  const handlers = {};
  const el = {
    nodeType: 1,
    tagName: String(tag || 'div').toUpperCase(),
    childNodes: [],
    attrs: {},
    dataset: {},
    style: {},
    className: '',
    title: '',
    textContent: '',
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
    appendChild(child) {
      child.parentNode = el;
      child.parentElement = el;
      el.childNodes.push(child);
      return child;
    },
    replaceChild(next, old) {
      const idx = el.childNodes.indexOf(old);
      const nodes = next.nodeType === 11 ? next.childNodes.slice() : [next];
      nodes.forEach((n) => { n.parentNode = el; n.parentElement = el; });
      if (idx >= 0) el.childNodes.splice(idx, 1, ...nodes);
      else el.childNodes.push(...nodes);
      return old;
    },
    replaceChildren(next) {
      const nodes = next && next.nodeType === 11 ? next.childNodes.slice() : [];
      el.childNodes = [];
      nodes.forEach((n) => { n.parentNode = el; n.parentElement = el; el.childNodes.push(n); });
    },
    closest(selector) {
      if (selector === '.atts') return null;
      const tags = String(selector).split(',').map((s) => s.trim().toUpperCase());
      let node = el;
      while (node && node.nodeType === 1) {
        if (tags.includes(node.tagName)) return node;
        node = node.parentElement;
      }
      return null;
    },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  el.handlers = handlers;
  Object.defineProperty(el, 'href', {
    get() { return el.attrs.href || ''; },
    set(value) { el.attrs.href = String(value); },
  });
  return el;
}

function fragment() {
  return {
    nodeType: 11,
    childNodes: [],
    appendChild(child) {
      child.parentNode = this;
      this.childNodes.push(child);
      return child;
    },
  };
}

function fixture() {
  const elements = new Map();
  const document = {
    handlers: {},
    addEventListener(type, fn) { (document.handlers[type] = document.handlers[type] || []).push(fn); },
    createTextNode: (value) => textNode(value),
    createDocumentFragment: () => fragment(),
    createElement: (tag) => element(tag),
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, element('div'));
      return elements.get(id);
    },
    querySelectorAll: () => [],
    body: element('body'),
  };
  const context = vm.createContext({
    document,
    window: {},
    console,
    URLSearchParams,
    setTimeout: () => 0,
    clearTimeout: () => {},
  });
  const src = fs.readFileSync(process.env.PUMAIL_APP_JS || 'frontend/app.js', 'utf8').replace(
    /applyTheme\(currentTheme\(\)\);\s*bindEvents\(\);\s*startApp\(\);\s*$/, '');
  vm.runInContext(src, context);
  context.toast = () => {};
  return { c: context, document, elements };
}

function anchorsIn(node, out = []) {
  (node.childNodes || []).forEach((child) => {
    if (child.nodeType === 1 && child.tagName === 'A') out.push(child);
    if (child.childNodes) anchorsIn(child, out);
  });
  return out;
}

test('纯文本正文里的 http 链接变成可点击链接', () => {
  const f = fixture();
  const body = element('div');
  f.c.fillPlainBody(body, '请复制下面链接到浏览器中打开。\nhttp://oa.sumao.com/hr/ratify/mobile/entry/invite_qr_code/index.html\n谢谢。');
  const links = anchorsIn(body);
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'http://oa.sumao.com/hr/ratify/mobile/entry/invite_qr_code/index.html');
  assert.equal(links[0].textContent, 'http://oa.sumao.com/hr/ratify/mobile/entry/invite_qr_code/index.html');
  assert.equal(links[0].attrs.target, '_blank');
  assert.equal(links[0].attrs.rel, 'noopener noreferrer');
  assert.equal(links[0].className, 'mail-link');
  const text = body.childNodes.filter((n) => n.nodeType === 3).map((n) => n.nodeValue).join('');
  assert.match(text, /请复制下面链接到浏览器中打开/);
  assert.match(text, /谢谢。/);
});

test('点击正文里的链接交给系统默认浏览器，不在应用内跳转', () => {
  const f = fixture();
  const pane = element('section');
  f.elements.set('readingPane', pane);
  f.c.bindMailLinkGuard();
  const opened = [];
  f.c.window.pumailAPI = { openExternal: (url) => opened.push(url) };

  const link = element('a');
  link.href = 'https://example.com/path?q=1';
  link.attrs.target = '_blank';
  const event = {
    target: link,
    prevented: 0,
    stopped: 0,
    preventDefault() { event.prevented += 1; },
    stopPropagation() { event.stopped += 1; },
    composedPath: () => [link, pane],
  };
  pane.handlers.click.forEach((fn) => fn(event));

  assert.deepEqual(opened, ['https://example.com/path?q=1']);
  assert.equal(event.prevented, 1);
  assert.equal(event.stopped, 1);
});

test('HTML 正文里的裸链接也会变成链接，已有 <a> 和 script 不动', () => {
  const f = fixture();
  const root = element('div');
  const row = element('div');
  row.appendChild(textNode('入职登记链接：http://oa.sumao.com/hr/entry?a=1&b=2，请尽快填写'));
  const existing = element('a');
  existing.href = 'https://example.com/already';
  existing.appendChild(textNode('已有链接 https://inside.example.com'));
  const script = element('script');
  script.appendChild(textNode('var u = "http://should-not-link.example.com";'));
  root.appendChild(row);
  root.appendChild(existing);
  root.appendChild(script);

  const count = f.c.linkifyMailHtml(root);
  assert.equal(count, 1);
  const links = anchorsIn(root);
  assert.equal(links.length, 2);   // 新生成 1 个 + 原有的 1 个
  assert.equal(links[0].href, 'http://oa.sumao.com/hr/entry?a=1&b=2');
  assert.equal(links[1].href, 'https://example.com/already');
  assert.equal(script.childNodes[0].nodeValue.includes('http://should-not-link.example.com'), true);
});

test('网址末尾的中文标点和不成对右括号不算进链接', () => {
  const f = fixture();
  assert.equal(f.c.trimUrlEnd('http://a.com/x)。'), 'http://a.com/x');
  assert.equal(f.c.trimUrlEnd('https://a.com/path?x=1，'), 'https://a.com/path?x=1');
  assert.equal(f.c.trimUrlEnd('http://a.com/a(b)'), 'http://a.com/a(b)');
  assert.equal(f.c.trimUrlEnd('http://a.com/a(b))'), 'http://a.com/a(b)');
  assert.equal(f.c.trimUrlEnd('http://a.com/end.'), 'http://a.com/end');
});

test('正文没有网址时保持纯文本，不产生空链接', () => {
  const f = fixture();
  const body = element('div');
  f.c.fillPlainBody(body, '这是一封普通邮件，没有链接。');
  assert.equal(anchorsIn(body).length, 0);
  assert.equal(body.childNodes.map((n) => n.nodeValue).join(''), '这是一封普通邮件，没有链接。');
});

test('恢复原文时重新生成链接（翻译来回切换后仍可点击）', () => {
  const f = fixture();
  const body = element('div');
  body.dataset.isHtml = '0';
  body.dataset.origText = '打开 http://oa.sumao.com/x 登记';
  body.classList.add = () => {};
  body.dataset.stashed = '1';
  f.c.restoreBody(body);
  const links = anchorsIn(body);
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'http://oa.sumao.com/x');
});
