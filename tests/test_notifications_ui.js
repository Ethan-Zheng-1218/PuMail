/* 执行真实前端、preload 和 Electron 通知处理；只替代系统窗口/计时器，不访问邮箱。 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

function fixture() {
  const handlers = new Map(), intervals = new Map(), timeouts = new Map();
  let timerId = 0;
  const timers = {
    setInterval(fn) { const id = ++timerId; intervals.set(id, fn); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn) { const id = ++timerId; timeouts.set(id, fn); return id; },
    clearTimeout(id) { timeouts.delete(id); },
  };
  const normal = { kind: 'normal', resize() { return this; } };
  let tray;
  class SystemTray {
    constructor(image) { this.image = image; tray = this; }
    setImage(image) { this.image = image; }
    setToolTip() {} setContextMenu() {} on() {}
  }
  const nativeWindow = { flashing: false, isFocused: () => false,
    flashFrame(on) { this.flashing = on; } };
  const electron = {
    app: { isPackaged: false, setAppUserModelId() {}, requestSingleInstanceLock: () => false,
      quit() {}, on() {} },
    Tray: SystemTray, Menu: { buildFromTemplate: x => x },
    nativeImage: { createFromPath: () => normal, createEmpty: () => ({ kind: 'empty' }) },
    ipcMain: { handle(name, fn) { handlers.set(name, fn); } },
  };
  const main = vm.createContext({ ...timers, console,
    process: { platform: process.platform, env: {}, pid: process.pid, resourcesPath: path.resolve('electron') },
    __dirname: path.resolve('electron'), nativeWindow,
    require: name => name === 'electron' ? electron : require(name) });
  vm.runInContext(fs.readFileSync('electron/main.js', 'utf8'), main);
  vm.runInContext('window = nativeWindow; createTray();', main);
  let bridge;
  const preload = vm.createContext({ require: name => {
    assert.equal(name, 'electron');
    return { contextBridge: { exposeInMainWorld(_name, api) { bridge = api; } },
      ipcRenderer: { invoke: (name, ...args) => handlers.get(name)({}, ...args) } };
  } });
  vm.runInContext(fs.readFileSync('electron/preload.js', 'utf8'), preload);
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { hidden: false, value: '', dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, replaceChildren() {}, scrollIntoView() {}, addEventListener() {},
      querySelector: () => null, querySelectorAll: () => [] });
    return elements.get(id);
  };
  const ui = vm.createContext({ ...timers, console, URLSearchParams,
    CSS: { escape: String }, localStorage: { getItem: () => null },
    window: { pumailAPI: bridge },
    document: { getElementById: element, querySelector: () => null, querySelectorAll: () => [] } });
  const source = fs.readFileSync('frontend/app.js', 'utf8').replace(
    /applyTheme\(currentTheme\(\)\);\s*bindEvents\(\);\s*startApp\(\);\s*$/, '');
  vm.runInContext(source + '\nglobalThis.testState = state;', ui);
  ui.renderAccountBar = () => {};
  ui.playNoticeSound = () => {};
  ui.testState.accounts = [{ id: 1, email: 'me@example.com' }, { id: 2, email: 'other@example.com' }];
  ui.testState.acc = 1;
  const start = async () => { await bridge.flashTaskbar(); };
  const flush = async () => {
    // Run scheduled badge work and the taskbar timeout; intervals run separately.
    const pending = [...timeouts.entries()];
    for (const [id, fn] of pending) { timeouts.delete(id); await fn(); }
  };
  const blink = () => { for (const fn of intervals.values()) fn(); };
  const pack = (allCount, recentCount = allCount) => ({
    all: { inbox: allCount, per_account: { 1: 0, 2: allCount } },
    recent: { inbox: recentCount, per_account: { 1: 0, 2: recentCount } },
  });
  return { ui, bridge, nativeWindow, intervals, tray: () => tray, normal, start, flush, blink, pack };
}

test('清除提醒同时停止托盘定时器，并把空白图标还原', async () => {
  const f = fixture(); await f.start(); f.blink();
  assert.equal(f.tray().image.kind, 'empty');
  await f.bridge.clearTaskbarFlash();
  assert.equal(f.nativeWindow.flashing, false);
  assert.equal(f.intervals.size, 0);
  assert.equal(f.tray().image, f.normal);
});

test('全部邮箱未读归零时，刷新汇总会停止托盘闪烁', async () => {
  const f = fixture(); await f.start(); f.blink();
  f.ui.api = async () => f.pack(0);
  f.ui.scheduleBadgeRefresh(); await f.flush();
  assert.equal(f.intervals.size, 0);
  assert.equal(f.tray().image, f.normal);
});

test('总览最近十天没有未读，但其他邮箱仍有旧未读时不清除提醒', async () => {
  const f = fixture(); await f.start();
  f.ui.testState.acc = '__GAIA__';
  f.ui.api = async () => f.pack(2, 0);
  f.ui.scheduleBadgeRefresh(); await f.flush();
  assert.equal(f.ui.testState.unreadPerAccount[2], 0);
  assert.equal(f.intervals.size, 1);
  f.blink(); assert.equal(f.tray().image.kind, 'empty');
});

test('未读请求失败或缺少完整汇总，保留闪烁而非误判为零', async () => {
  const f = fixture(); await f.start();
  for (const response of [null, {}, { recent: { inbox: 0 } }, { all: {} }]) {
    f.ui.api = async () => { if (response === null) throw new Error('offline'); return response; };
    f.ui.scheduleBadgeRefresh(); await f.flush();
    assert.equal(f.intervals.size, 1);
  }
});

test('读取最后一封邮件即使没有 SSE 事件也会刷新汇总、停止闪烁', async () => {
  const f = fixture(); await f.start(); f.blink();
  // Only replace mail presentation; openThread, read bookkeeping and notification code stay real.
  for (const name of ['renderParties', 'setTrLamp', 'setStarBtn', 'applyFolderChrome', 'renderThread']) {
    f.ui[name] = () => {};
  }
  let unreadRequests = 0;
  f.ui.api = async url => {
    if (url.startsWith('/api/thread?')) return { messages: [
      { uid: '42', from_addr: 'you@example.com', subject: 'Hello', body: 'Hello', unread: false },
    ] };
    assert.equal(url, '/api/unread'); unreadRequests += 1;
    return f.pack(0);
  };
  f.ui.openThread('42', '42', 'thread-42', '', null);
  await new Promise(resolve => setImmediate(resolve));
  await f.flush();
  assert.equal(unreadRequests, 1);
  assert.equal(f.intervals.size, 0);
  assert.equal(f.tray().image, f.normal);
});

test('迟到的新邮件通知会复核未读，归零后停止提醒，下次新邮件仍可再闪烁', async () => {
  const f = fixture();
  f.ui.api = async () => f.pack(0);
  f.ui.handleNewMailNotice({ acc: 1, count: 1 });
  assert.equal(f.intervals.size, 1);
  await f.flush();
  assert.equal(f.intervals.size, 0);
  f.ui.api = async () => f.pack(1);
  f.ui.handleNewMailNotice({ acc: 2, count: 1 }); await f.flush();
  assert.equal(f.intervals.size, 1);
});

test('旧的零未读响应晚于新邮件到达时，不得清除新邮件的闪烁', async () => {
  const f = fixture(); let finishOld;
  f.ui.api = () => new Promise(resolve => { finishOld = resolve; });
  f.ui.scheduleBadgeRefresh();
  const oldRefresh = f.flush();
  f.ui.api = async () => f.pack(1);
  f.ui.handleNewMailNotice({ acc: 2, count: 1 });
  assert.equal(f.intervals.size, 1);
  finishOld(f.pack(0)); await oldRefresh;
  assert.equal(f.intervals.size, 1);
  await f.flush();
  assert.equal(f.intervals.size, 1);
  f.blink(); assert.equal(f.tray().image.kind, 'empty');
});
