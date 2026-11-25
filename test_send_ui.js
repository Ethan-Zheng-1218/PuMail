/* 直接执行 app.js 的真实处理函数；不访问真实邮箱。 */
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

function fixture() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '发送', disabled: false, hidden: false,
      classList: { add() {}, remove() {}, toggle() {} }, dataset: {},
      setAttribute() {}, removeAttribute() {}, scrollIntoView() {}, addEventListener() {},
      replaceChildren() {}, appendChild() {},
      focus() { context.document.activeElement = this; },
      querySelectorAll() { return []; },
      querySelector() { return null; },
    });
    return elements.get(id);
  }
  const notices = [];
  const context = vm.createContext({
    document: { getElementById: element, querySelectorAll: () => [], querySelector: () => null,
      createElement: () => ({ dataset: {}, appendChild() {} }) },
    window: {}, console, setTimeout, clearTimeout, URLSearchParams,
  });
  const src = fs.readFileSync('app.js', 'utf8').replace(
    /applyTheme\(currentTheme\(\)\);\s*bindEvents\(\);\s*startApp\(\);\s*$/, '');
  vm.runInContext(src + '\nglobalThis.testState = state;', context);
  context.realApi = context.api;
  context.toast = (...args) => notices.push(args);
  context.commitAddrTyping = () => {};
  context.addrFieldValue = id => element(id).value;
  context.resetReplyBox = () => { element('replyBody').value = ''; };
  context.stashThreadBodies = () => {};
  context.bumpCurrentRowTime = () => {};
  context.refreshAfterSend = () => {};
  context.appendThreadMessage = () => ({ scrollIntoView() {} });
  const current = { acc: 1, subject: 'Question', message_id: '<first@example.com>', threadMessages: [] };
  context.testState.acc = 1;
  context.testState.accounts = [{ id: 1, email: 'me@example.com' }, { id: 2, email: 'other@example.com' }];
  context.testState.current = current;
  element('replyToInput').value = 'you@example.com';
  element('replyBody').value = 'Reply body';
  return { c: context, element, notices, current };
}

function selectionFixture(selected = []) {
  const f = fixture();
  selected.forEach(key => f.c.testState.selected.add(key));
  const box = { checked: selected.includes('fifth') };
  const row = {
    dataset: { key: 'fifth', uid: '5', uids: '5', refs: 'INBOX|5' },
    classList: { toggle(_name, checked) { row.checked = checked; } },
    querySelector() { return box; },
  };
  const opened = [];
  f.c.openThread = (...args) => opened.push(args);
  f.c.updateBatchBar = () => {};
  f.c.bindRow(row);
  return { ...f, row, box, opened, click: () => row.onclick({ target: { closest: () => null } }) };
}

test('多选时点击第五封邮件行，同时勾选并打开正文，保留前四封', () => {
  const f = selectionFixture(['first', 'second', 'third', 'fourth']);
  f.click();
  assert.equal(f.c.testState.selected.size, 5);
  assert.equal(f.c.testState.selected.has('fifth'), true);
  assert.equal(f.box.checked, true);
  assert.equal(f.row.checked, true);
  assert.equal(f.opened.length, 1);
  assert.equal(f.opened[0][0], '5');
});

test('没有多选时点击邮件行仍只阅读', () => {
  const f = selectionFixture(); f.click();
  assert.equal(f.c.testState.selected.size, 0);
  assert.equal(f.box.checked, false);
  assert.equal(f.opened.length, 1);
});

test('重复点击已选邮件只阅读，不取消选择', () => {
  const f = selectionFixture(['first', 'fifth']); f.click(); f.click();
  assert.equal(f.c.testState.selected.size, 2);
  assert.equal(f.box.checked, true);
  assert.equal(f.opened.length, 2);
});

test('直接点击勾选框仍可取消选择，且不额外打开邮件', () => {
  const f = selectionFixture(['fifth']);
  f.row.onclick({ target: { closest: () => ({}) } });
  f.box.checked = false; f.box.onchange();
  assert.equal(f.c.testState.selected.size, 0);
  assert.equal(f.opened.length, 0);
  f.click();
  assert.equal(f.c.testState.selected.size, 0);
});

test('回复发送立即反馈，重复点击只提交一次，完成后恢复按钮', async () => {
  const f = fixture(); let finish; let calls = 0;
  f.c.api = () => { calls += 1; return new Promise(resolve => { finish = resolve; }); };
  const first = f.c.sendInlineReply();
  assert.equal(f.element('sendReplyBtn').disabled, true);
  assert.match(f.element('sendReplyBtn').textContent, /发送中/);
  await f.c.sendInlineReply();
  assert.equal(calls, 1);
  finish({ ok: true }); await first;
  assert.equal(f.element('sendReplyBtn').disabled, false);
  assert.equal(f.element('replyBody').value, '');
  assert.ok(f.notices.some(n => n[0] === '已发送'));
});

test('发送失败保留回复并恢复按钮，允许用户修正后重试', async () => {
  const f = fixture(); f.c.api = async () => { throw new Error('SMTP refused'); };
  await f.c.sendInlineReply();
  assert.equal(f.element('replyBody').value, 'Reply body');
  assert.equal(f.element('sendReplyBtn').disabled, false);
  assert.ok(f.notices.some(n => n[0].includes('SMTP refused')));
});

test('发送期间切换会话，旧回复不会写入新会话或清除新输入', async () => {
  const f = fixture(); let finish; let payload;
  f.c.api = (_path, opts) => { payload = opts.body; return new Promise(resolve => { finish = resolve; }); };
  const pending = f.c.sendInlineReply();
  await Promise.resolve();
  const next = { acc: 2, subject: 'Next', threadMessages: [] };
  f.c.testState.current = next;
  f.element('replyBody').value = 'New reply';
  finish({ ok: true }); await pending;
  assert.equal(payload.acc, 1);
  assert.equal(payload.in_reply_to, '<first@example.com>');
  assert.equal(next.threadMessages.length, 0);
  assert.equal(f.element('replyBody').value, 'New reply');
});

test('发送身份使用当前邮件账号，避免残留写信账号影响回复', async () => {
  const f = fixture(); f.c.testState.composeAcc = 2; let payload;
  f.c.api = async (_path, opts) => { payload = opts.body; return { ok: true }; };
  await f.c.sendInlineReply();
  assert.equal(payload.acc, 1);
  assert.equal(f.current.threadMessages[0].from_addr, 'me@example.com');
});

test('新邮件只保留声音和托盘提醒，不弹出浮动查看提示', () => {
  const f = fixture(); let sounds = 0, flashes = 0;
  f.c.scheduleBadgeRefresh = () => {};
  f.c.playNoticeSound = () => { sounds += 1; };
  f.c.window.pumailAPI = { flashTaskbar: () => { flashes += 1; } };
  f.c.handleNewMailNotice({ acc: 1, count: 2, uids: ['12'] });
  assert.equal(f.notices.length, 0);
  assert.equal(sounds, 1);
  assert.equal(flashes, 1);
});

test('点击回复直接聚焦正文，并保留自动填写的收件人及已有正文', () => {
  const f = fixture();
  f.current.threadMessages = [{ from_addr: 'you@example.com', to: 'me@example.com' }];
  f.c.reply();
  assert.equal(f.c.document.activeElement, f.element('replyBody'));
  assert.equal(f.element('replyComposer').hidden, false);
  assert.equal(f.element('replyToInput').value, 'you@example.com');
  assert.equal(f.element('replyBody').value, 'Reply body');
});

test('写邮件也能防止重复提交，失败后保留内容和恢复按钮', async () => {
  const f = fixture(); let fail, calls = 0;
  f.element('toInput').value = 'you@example.com';
  f.element('bodyInput').value = 'Keep this draft';
  f.c.composePayload = async () => ({ acc: 1, to: 'you@example.com', body: 'Keep this draft' });
  f.c.api = () => { calls += 1; return new Promise((_resolve, reject) => { fail = reject; }); };
  const event = { preventDefault() {} };
  const pending = f.c.submitCompose(event);
  await Promise.resolve();
  assert.equal(f.element('sendComposeBtn').disabled, true);
  await f.c.submitCompose(event);
  assert.equal(calls, 1);
  fail(new Error('SMTP unavailable')); await pending;
  assert.equal(f.element('sendComposeBtn').disabled, false);
  assert.equal(f.element('bodyInput').value, 'Keep this draft');
});

test('连接中断时明确提醒发送结果未确认，避免盲目重发', async () => {
  const f = fixture(); f.c.fetch = async () => { throw new Error('connection reset'); };
  await assert.rejects(f.c.realApi('/api/send', { method: 'POST', body: {} }), /发送结果未确认/);
});

test('发送接口返回无效响应时不能清空回复并宣称已发送', async () => {
  const f = fixture(); f.c.api = async () => ({});
  await f.c.sendInlineReply();
  assert.equal(f.element('replyBody').value, 'Reply body');
  assert.equal(f.notices.some(n => n[0] === '已发送'), false);
});
