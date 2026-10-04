const { app, BrowserWindow, dialog, Tray, Menu, nativeImage, shell, ipcMain } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const PORT = 5000;
const HOME = `http://127.0.0.1:${PORT}`;
const AUTOSTART_KEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Run';
const AUTOSTART_NAME = 'PuMail';

let server = null;
let window = null;
let tray = null;
let quitting = false;

// ---------- 开机自启动（注册表） ----------
function readAutostart() {
  if (process.platform !== 'win32') return false;
  try {
    const Registry = require('winreg');
    const key = new Registry({ hive: Registry.HKCU, key: AUTOSTART_KEY });
    return new Promise((resolve) => {
      key.get(AUTOSTART_NAME, (err) => resolve(!err));
    });
  } catch {
    // winreg not available, use child_process fallback
    return new Promise((resolve) => {
      try {
        const { execSync } = require('child_process');
        const out = execSync(
          `reg query "HKCU\\${AUTOSTART_KEY}" /v "${AUTOSTART_NAME}"`,
          { windowsHide: true }
        );
        resolve(out && out.length > 0);
      } catch {
        resolve(false);
      }
    });
  }
}

function setAutostart(enable) {
  if (process.platform !== 'win32') return Promise.resolve();
  const exePath = process.execPath;
  return new Promise((resolve, reject) => {
    try {
      const { execSync } = require('child_process');
      if (enable) {
        execSync(
          `reg add "HKCU\\${AUTOSTART_KEY}" /v "${AUTOSTART_NAME}" /t REG_SZ /d "${exePath}" /f`,
          { windowsHide: true }
        );
      } else {
        execSync(
          `reg delete "HKCU\\${AUTOSTART_KEY}" /v "${AUTOSTART_NAME}" /f`,
          { windowsHide: true }
        );
      }
      resolve();
    } catch (e) {
      reject(e);
    }
  });
}

ipcMain.handle('get-auto-start', async () => {
  return await readAutostart();
});

ipcMain.handle('set-auto-start', async (_e, enable) => {
  await setAutostart(enable);
  return { ok: true };
});

ipcMain.handle('open-external', async (_e, url) => {
  if (typeof url === 'string' && url) {
    await shell.openExternal(url);
  }
  return { ok: true };
});

ipcMain.handle('open-local-page', async (_e, name) => {
  if (typeof name !== 'string' || !name) return { ok: false };
  const base = app.isPackaged
    ? path.join(process.resourcesPath, 'pumail-server')
    : path.join(path.resolve(__dirname, '..'), 'dist', 'PuMail', '_internal');
  const filePath = path.join(base, name);
  if (!fs.existsSync(filePath)) return { ok: false, error: 'File not found: ' + filePath };

  // 对 guide.html，将 markdown 内容内嵌注入以避免 file:// 下 fetch 被阻止
  if (name === 'guide.html') {
    try {
      const mdPath = path.join(base, '邮箱绑定指南.md');
      let html = fs.readFileSync(filePath, 'utf8');
      // 将相对路径的 CSS 引用改为绝对 file:// 路径，以便临时文件也能加载样式
      const cssAbsUrl = 'file://' + path.join(base, 'styles.css').replace(/\\/g, '/');
      html = html.replace(/href="styles\.css[^"]*"/, 'href="' + cssAbsUrl + '"');
      // 将返回用户手册链接改为绝对路径
      const helpAbsUrl = 'file://' + path.join(base, 'help.html').replace(/\\/g, '/');
      html = html.replace('href="help.html"', 'href="' + helpAbsUrl + '"');
      if (fs.existsSync(mdPath)) {
        let md = fs.readFileSync(mdPath, 'utf8');
        // 将 markdown 中的相对图片路径转为绝对 file:// 路径
        const imgBase = 'file://' + (base.replace(/\\/g, '/'));
        md = md.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, src) => {
          if (src.startsWith('http://') || src.startsWith('https://') || src.startsWith('file://')) return match;
          return '![' + alt + '](' + imgBase + '/' + src + ')';
        });
        const injection = '<script>window.__GUIDE_MD__=' + JSON.stringify(md) + ';</script>';
        html = html.replace('</head>', injection + '</head>');
      }
      const tmpPath = path.join(require('os').tmpdir(), 'pumail-guide.html');
      fs.writeFileSync(tmpPath, html, 'utf8');
      const fileUrl = 'file://' + tmpPath.replace(/\\/g, '/');
      await shell.openExternal(fileUrl);
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  }

  const fileUrl = 'file://' + filePath.replace(/\\/g, '/');
  await shell.openExternal(fileUrl);
  return { ok: true };
});

// ---------- 任务栏闪烁通知 ----------
let _flashTimer = null;
ipcMain.handle('flash-taskbar', async () => {
  if (!window) return { ok: false };
  // 如果窗口已经在前台，不需要闪烁
  if (window.isFocused()) return { ok: true };
  // 闪烁任务栏图标
  window.flashFrame(true);
  // 同时让右下角托盘图标闪烁（有声音之外再加一个视觉提醒）
  startTrayBlink();
  // 15 秒后自动停止任务栏闪烁
  if (_flashTimer) clearTimeout(_flashTimer);
  _flashTimer = setTimeout(() => {
    if (window) window.flashFrame(false);
  }, 15000);
  return { ok: true };
});

ipcMain.handle('clear-taskbar-flash', async () => {
  if (_flashTimer) {
    clearTimeout(_flashTimer);
    _flashTimer = null;
  }
  if (window) window.flashFrame(false);
  return { ok: true };
});

app.setAppUserModelId('app.pumail.desktop');

function serverDir() {
  if (app.isPackaged) return path.join(process.resourcesPath, 'pumail-server');
  return path.resolve(__dirname, '..', 'dist', 'PuMail');
}

function serverExe() {
  return path.join(serverDir(), 'PuMail.exe');
}

function iconFile() {
  const packedIco = path.join(process.resourcesPath, 'icon.ico');
  const localIco = path.join(__dirname, '..', 'cat.ico');
  const packedPng = path.join(process.resourcesPath, 'icon.png');
  const localPng = path.join(__dirname, 'build', 'icon.png');
  if (app.isPackaged && fs.existsSync(packedIco)) return packedIco;
  if (fs.existsSync(localIco)) return localIco;
  if (app.isPackaged && fs.existsSync(packedPng)) return packedPng;
  return localPng;
}

function appIcon() {
  const file = iconFile();
  if (!fs.existsSync(file)) return undefined;
  return nativeImage.createFromPath(file);
}

function pageIsUp() {
  return new Promise((resolve) => {
    const req = http.get(HOME, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(800, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function waitForPage() {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (await pageIsUp()) return resolve();
      if (Date.now() - started > 25000) return reject(new Error('PuMail 在 25 秒内没有打开'));
      setTimeout(tick, 300);
    };
    tick();
  });
}

function startBackend() {
  const exe = serverExe();
  if (!fs.existsSync(exe)) {
    dialog.showErrorBox('PuMail', '没有找到程序文件：\n' + exe);
    return;
  }
  const env = { ...process.env, PUMAIL_NO_BROWSER: '1' };
  server = spawn(exe, [], {
    cwd: serverDir(),
    env,
    windowsHide: true,
    stdio: 'ignore',
  });
  server.on('error', (err) => {
    dialog.showErrorBox('PuMail', '无法启动 PuMail：' + err.message);
  });
}

function stopBackend() {
  if (!server || server.killed) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(server.pid), '/f', '/t'], { windowsHide: true });
  } else {
    server.kill();
  }
  server = null;
}

function showWindow() {
  stopTrayBlink();
  if (!window) {
    createWindow();
    return;
  }
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function hideToTray() {
  if (window) window.hide();
}

function quitApp() {
  quitting = true;
  app.quit();
}

function createTray() {
  if (tray) return;
  tray = new Tray(trayNormalImage());
  tray.setToolTip('PuMail');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 PuMail', click: showWindow },
    { type: 'separator' },
    { label: '退出', click: quitApp },
  ]));
  tray.on('click', showWindow);
}

/* ---------- 托盘图标闪烁（有新邮件时） ----------
   像微信那样闪：一会儿是空白，一会儿显示原来的图标，来回切换，
   直到窗口被打开或者满 10 分钟。不需要任何额外的图标文件。 */
let _trayBlinkTimer = null;
let _trayBlinkOn = false;

function trayNormalImage() {
  const src = appIcon();
  return src ? src.resize({ width: 16, height: 16 }) : nativeImage.createEmpty();
}

function startTrayBlink(durationMs) {
  if (!tray) return;
  // 默认闪 10 分钟，或者直到用户打开窗口（showWindow 里会停）
  const total = durationMs || 10 * 60 * 1000;
  if (_trayBlinkTimer) clearInterval(_trayBlinkTimer);
  const startedAt = Date.now();
  _trayBlinkTimer = setInterval(() => {
    if (!tray || Date.now() - startedAt > total) {
      stopTrayBlink();
      return;
    }
    _trayBlinkOn = !_trayBlinkOn;
    try {
      // 空白 ↔ 原图标：微信那种闪法
      tray.setImage(_trayBlinkOn ? nativeImage.createEmpty() : trayNormalImage());
    } catch (err) {
      /* 图标切换失败不影响收信 */
    }
  }, 500);
}

function stopTrayBlink() {
  if (_trayBlinkTimer) {
    clearInterval(_trayBlinkTimer);
    _trayBlinkTimer = null;
  }
  _trayBlinkOn = false;
  if (tray) {
    try {
      tray.setImage(trayNormalImage());
    } catch (err) {
      /* 忽略 */
    }
  }
}

async function createWindow() {
  if (window) {
    showWindow();
    return;
  }
  if (!(await pageIsUp())) startBackend();
  try {
    await waitForPage();
  } catch (err) {
    dialog.showErrorBox('PuMail', err.message);
    quitApp();
    return;
  }
  window = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 1100,
    minHeight: 680,
    title: 'PuMail',
    icon: appIcon(),
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  window.loadURL(HOME);

  // 所有新窗口链接（target="_blank" 等）均在系统默认浏览器中打开
  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // 主窗口自身导航到外部链接时，也在系统默认浏览器中打开
  window.webContents.on('will-navigate', (e, url) => {
    const base = HOME;
    if (url && !url.startsWith(base)) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });

  window.on('close', (e) => {
    if (quitting) return;
    e.preventDefault();
    hideToTray();
  });
  window.on('closed', () => {
    window = null;
  });
}

// ---------- 单实例锁 ----------
const gotTheLock = app.requestSingleInstanceLock();

if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showWindow();
  });

  app.whenReady().then(() => {
    createTray();
    createWindow();
  });
}

app.on('before-quit', () => {
  quitting = true;
  stopBackend();
  if (tray) {
    tray.destroy();
    tray = null;
  }
});

app.on('window-all-closed', (e) => {
  if (!quitting) e.preventDefault();
});

app.on('activate', showWindow);
